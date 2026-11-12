"""Decide which host may trade a slot.

YOPC is the primary. Box is the backup. A healthy primary leaves the backup
idle. The backup acquires the lease once, and only after the primary has
missed at least two slots and the lease is clearly free. A slow primary whose
lease has not clearly expired is not treated as dead. An unreadable lease, an
unreadable heartbeat, a clock-skew band around expiry, or a push that cannot
be confirmed fails closed: no order.

Local slot claim files are not a distributed lock. Completed slots are recorded
on the lease so the other host does not repeat them after handback.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_broker.failover.close_guard import market_close_coid, moc_close_coid
from paper_broker.failover.schedule import (
    BACKUP_HOST,
    GRACE_MIN,
    LEASE_TTL_S,
    MISSED_SLOTS,
    PRIMARY_HOST,
    SKEW_S,
    Slot,
    backup_window_ok,
    entry_permitted,
    heartbeat_covers_slot,
    is_successful,
    late_ok,
    lease_view,
    mark_outcome,
    missed_slots,
    phase_for,
)
from paper_broker.failover.transport import OwnershipPrerequisite, TransportError, WriteResult

PT = ZoneInfo("America/Los_Angeles")
PAPER_BASE = "https://paper-api.alpaca.markets"
PIN_LAST4 = "J7D0"


def _aware(dt: datetime, default: timezone | ZoneInfo = timezone.utc) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=default)
    return dt


def _utcnow(now: datetime) -> datetime:
    return _aware(now, PT).astimezone(timezone.utc)


def _parse_exp(doc: dict | None) -> datetime | None:
    if not doc or not doc.get("expires_utc"):
        return None
    return _aware(datetime.fromisoformat(str(doc["expires_utc"])))


def _url_host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return url or ""


@dataclass
class Heartbeat:
    host: str | None = None
    ts_utc: datetime | None = None
    kind: str | None = None
    slot: str | None = None
    rc: int | None = None
    ok: bool | None = None
    trading: bool | None = None
    date: str | None = None
    error: str | None = None

    def as_cover(self) -> dict:
        return {
            "host": self.host,
            "kind": self.kind,
            "slot": self.slot,
            "rc": self.rc,
            "ok": self.ok,
            "trading": self.trading,
            "date": self.date,
            "error": self.error,
        }

    @property
    def ts_pt(self) -> datetime | None:
        if self.ts_utc is None:
            return None
        return _aware(self.ts_utc).astimezone(PT)

    def is_primary_tick(self) -> bool:
        """True only for a real YOPC primary slot tick (scan/exit/eod/verify).

        A box dashboard or recovery-flatten kind=alive write must not count
        as primary liveness. Missing ts or host also does not count.
        """
        if self.error or self.ts_utc is None:
            return False
        if (self.host or "") != PRIMARY_HOST:
            return False
        return (self.kind or "") in ("scan", "exit", "eod", "verify")


@dataclass
class AccountContext:
    last4: str = PIN_LAST4
    base_url: str = PAPER_BASE
    clock_open: bool | None = True
    session_date: datetime | None = None
    live_flag: bool = False


@dataclass
class Decision:
    action: str
    reason: str
    role: str
    host: str
    missed: int = 0
    daemon_dead: bool = False
    slots: list = field(default_factory=list)
    fence_token: str | None = None
    fence_confirmed: bool = False
    publish_allowed: bool = False
    entry_permitted: bool = False
    orders_submitted: int = 0
    prerequisite: str | None = None
    dry_run: bool = False
    handback: bool = False


def guard_reason(account: AccountContext, slot: Slot, now: datetime) -> str | None:
    host = _url_host(account.base_url or "")
    if account.live_flag or (host == "api.alpaca.markets" and "paper-api" not in (account.base_url or "")):
        return "refuse_live"
    if "paper-api.alpaca.markets" not in (account.base_url or ""):
        return "refuse_base_url"
    if (account.last4 or "") != PIN_LAST4:
        return "refuse_account"
    if account.session_date is not None and _aware(account.session_date, PT).date() != slot.when.date():
        return "refuse_date"
    if _aware(now, PT).astimezone(PT).date() != slot.when.date():
        return "refuse_date"
    if account.clock_open is False and phase_for(slot) not in ("signal_only", "read_only"):
        return "refuse_session"
    return None


def order_ids_for(symbol: str, day, slot_hhmm: str) -> dict:
    """Deterministic ids. The same inputs produce the same ids on every host."""
    from datetime import date as date_cls
    d = day if isinstance(day, date_cls) else day.date()
    ymd = f"{d:%Y%m%d}"
    return {
        "entry": f"orbv4p-{ymd}-{slot_hhmm}-{symbol}",
        "moc": moc_close_coid(symbol, d),
        "market": market_close_coid(symbol, d),
        "recovery": f"orbv4p-xrec-20261109-{symbol}",
    }


class Runner:
    def __init__(
        self,
        *,
        role: str,
        host: str,
        instance: str,
        transport,
        state_dir: Path,
        now: datetime,
        slots: list[Slot],
        heartbeat: Heartbeat | None,
        account: AccountContext | None = None,
        skew_s: int = SKEW_S,
    ):
        if role == "primary" and host != PRIMARY_HOST:
            raise ValueError("primary host is yopc")
        if role == "backup" and host != BACKUP_HOST:
            raise ValueError("backup host is box")
        if role not in ("primary", "backup"):
            raise ValueError("role must be primary or backup")
        self.role = role
        self.host = host
        self.instance = instance
        self.transport = transport
        self.state_dir = Path(state_dir)
        self.now = _aware(now, PT).astimezone(PT)
        self.slots = list(slots)
        self.heartbeat = heartbeat
        self.account = account or AccountContext()
        self.skew_s = skew_s
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def plan(self, *, dry_run: bool = False) -> Decision:
        base = Decision(action="fail_closed", reason="", role=self.role, host=self.host, dry_run=dry_run)
        try:
            read = self.transport.read()
        except OwnershipPrerequisite as e:
            base.reason = "ownership_prerequisite"
            base.prerequisite = str(e)
            return base
        except TransportError as e:
            base.reason = "lease_unreadable"
            base.prerequisite = str(e)
            return base
        if self.role == "backup" and (self.heartbeat is None or self.heartbeat.error):
            base.reason = "heartbeat_unreadable"
            return base
        doc = read.doc
        view = self._view(doc)
        missed = self._missed()
        base.missed = len(missed)
        base.daemon_dead = bool(self.heartbeat and not self.heartbeat.error and len(missed) >= MISSED_SLOTS)
        if view == "ambiguous" and not self._same_holder(doc):
            # Clock skew around expiry: a non-holder must not take over.
            # The holder is treated as still holding (see _we_hold).
            base.reason = "lease_expiry_ambiguous"
            return base
        if view in ("held", "ambiguous") and not self._we_hold(doc):
            base.action = "idle"
            base.reason = "lease_held_elsewhere"
            return base
        if self.role == "primary":
            return self._plan_primary(base, read, doc, dry_run)
        return self._plan_backup(base, read, doc, missed, dry_run)

    def run(self, executor, *, dry_run: bool = False) -> Decision:
        decision = self.plan(dry_run=dry_run)
        if dry_run or decision.action != "run" or not decision.fence_confirmed:
            decision.orders_submitted = 0
            return decision
        os.environ["ORB_V4_FENCE_TOKEN"] = decision.fence_token or ""
        os.environ["ORB_V4_FENCE_REQUIRED"] = "1"
        total = 0
        ran: list[Slot] = []
        for slot in list(decision.slots):
            if is_successful(self.state_dir, slot) or slot.key in self._completed_keys():
                continue
            blocked = guard_reason(self.account, slot, self.now)
            if blocked:
                decision.action = "fail_closed"
                decision.reason = blocked
                decision.orders_submitted = total
                decision.slots = ran
                return decision
            if not self._token_still_ours(decision.fence_token):
                decision.action = "fail_closed"
                decision.reason = "fence_lost_before_submit"
                decision.fence_confirmed = False
                decision.publish_allowed = False
                decision.orders_submitted = total
                decision.slots = ran
                return decision
            result = executor(slot, decision.fence_token)
            rc = int(result.get("rc", 1))
            total += int(result.get("orders") or 0)
            mark_outcome(self.state_dir, slot, rc, detail=str(result.get("detail") or ""))
            if rc in (0, 3):
                self._note_completed(slot, decision.fence_token)
            ran.append(slot)
            if decision.publish_allowed:
                self._publish_slot(slot, rc, dry_run=dry_run)
        decision.orders_submitted = total
        decision.slots = ran
        return decision


    def _publish_slot(self, slot: Slot, rc: int, *, dry_run: bool) -> None:
        """Best-effort dashboard publish after a slot. Never raises or delays trading."""
        try:
            from paper_broker.failover.dashboard_publish import schedule_publish
            takeover = self._takeover_since() if self.role == "backup" else None
            schedule_publish(
                host=self.host,
                role=self.role,
                publish_allowed=True,
                slot=slot,
                rc=rc,
                takeover_since_utc=takeover,
                dry_run=dry_run,
            )
        except Exception:
            pass

    def _same_holder(self, doc: dict | None) -> bool:
        return bool(
            doc
            and doc.get("status") == "holding"
            and doc.get("owner") == self.host
            and doc.get("instance") == self.instance
        )

    def _we_hold(self, doc: dict | None) -> bool:
        # The 20s expiry skew is for takeover, not for the holder: an owner
        # still in that window may idle or renew instead of fail-closed
        # (2026-11-12 13:15 PT lease_expiry_ambiguous on the box).
        return bool(self._same_holder(doc) and self._view(doc) in ("held", "ambiguous"))

    def _stale(self) -> bool:
        hb = self.heartbeat
        if hb is None or not hb.is_primary_tick():
            return True
        age = (_utcnow(self.now) - _aware(hb.ts_utc)).total_seconds()
        return age > GRACE_MIN * 60

    def _plan_primary(self, base: Decision, read, doc, dry_run: bool) -> Decision:
        due = self._due_slots("primary")
        if not due and not self._we_hold(doc):
            base.action = "idle"
            base.reason = "nothing_due"
            return base
        if not due:
            base.action = "idle"
            base.reason = "lease_held_nothing_due"
            base.fence_token = (doc or {}).get("token")
            base.fence_confirmed = True
            base.publish_allowed = True
            return base
        blocked = next((guard_reason(self.account, s, self.now) for s in due), None)
        due = [s for s in due if guard_reason(self.account, s, self.now) is None]
        if not due:
            base.action = "fail_closed"
            base.reason = blocked or "refuse"
            return base
        return self._acquire(base, read, doc, due, "primary", dry_run)

    def _plan_backup(self, base, read, doc, missed, dry_run: bool) -> Decision:
        if self._we_hold(doc):
            if self._primary_returned(doc):
                return self._release(base, read, doc, dry_run)
            due = self._due_slots("backup")
            if not due:
                base.action = "idle"
                base.reason = "takeover_nothing_due"
                base.fence_token = doc.get("token")
                base.fence_confirmed = True
                base.publish_allowed = True
                return base
            return self._acquire(base, read, doc, due, "backup_renew", dry_run)
        if len(missed) < MISSED_SLOTS and not (self._stale() and self._critical_due()):
            base.action = "idle"
            base.reason = "primary_healthy"
            return base
        due = self._due_slots("backup")
        if not due:
            base.action = "idle"
            base.reason = "missed_but_outside_window"
            base.daemon_dead = len(missed) >= MISSED_SLOTS
            return base
        blocked = next((guard_reason(self.account, s, self.now) for s in due), None)
        due = [s for s in due if guard_reason(self.account, s, self.now) is None]
        if not due:
            base.action = "fail_closed"
            base.reason = blocked or "refuse"
            return base
        return self._acquire(base, read, doc, due, "takeover", dry_run)

    def _critical_due(self) -> bool:
        cover = self.heartbeat.as_cover() if self.heartbeat else None
        for slot in self.slots:
            if slot.kind not in ("exit", "eod", "verify"):
                continue
            if not backup_window_ok(slot, self.now):
                continue
            if is_successful(self.state_dir, slot) or slot.key in self._completed_keys():
                continue
            if heartbeat_covers_slot(cover, slot):
                continue
            return True
        return False

    def _due_slots(self, role: str) -> list[Slot]:
        done = self._completed_keys()
        cover = self.heartbeat.as_cover() if self.heartbeat else None
        out = []
        for slot in self.slots:
            if is_successful(self.state_dir, slot) or slot.key in done:
                continue
            if heartbeat_covers_slot(cover, slot):
                continue
            if role == "primary":
                if self.now < slot.fire_at or not late_ok(slot, self.now):
                    continue
            elif not backup_window_ok(slot, self.now):
                continue
            out.append(slot)
        return out

    def _completed_keys(self) -> set[str]:
        try:
            doc = self.transport.read().doc or {}
        except TransportError:
            return set()
        return set(doc.get("completed") or [])

    def _missed(self) -> list[Slot]:
        hb = self.heartbeat
        hb_pt = hb.ts_pt if hb is not None and hb.is_primary_tick() else None
        return missed_slots(self.slots, self.now, hb_pt, GRACE_MIN)

    def _view(self, doc: dict | None) -> str:
        if not doc or doc.get("status") != "holding":
            return "released" if doc else "missing"
        return lease_view(_parse_exp(doc), _utcnow(self.now), self.skew_s)

    def _primary_returned(self, doc: dict) -> bool:
        hb = self.heartbeat
        if hb is None or not hb.is_primary_tick():
            return False
        hb_ts = _aware(hb.ts_utc)
        since = self._takeover_since()
        if since is None:
            acquired_at = doc.get("acquired_utc")
            if not acquired_at:
                return False
            return hb_ts > _aware(datetime.fromisoformat(str(acquired_at)))
        return hb_ts > since

    def _takeover_path(self) -> Path:
        return self.state_dir / f"takeover_v4paper_{self.now:%Y%m%d}.json"

    def _takeover_since(self) -> datetime | None:
        path = self._takeover_path()
        if not path.is_file():
            return None
        try:
            return _aware(datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["since_utc"]))
        except Exception:
            return None

    def _problem_alert(self, kind: str, text: str, *, dry_run: bool = False) -> None:
        """Best-effort problem notice for a takeover or handback. Never raises."""
        try:
            from paper_broker.slack_alerts import maybe_alert
            maybe_alert("failover", text, key=kind, dry_run=dry_run)
        except Exception:
            pass

    def _write_takeover(self, missed: int) -> None:
        hb = _aware(self.heartbeat.ts_utc).isoformat() if self.heartbeat and self.heartbeat.ts_utc else None
        self._takeover_path().write_text(json.dumps({
            "since_utc": _utcnow(self.now).isoformat(),
            "host": self.host,
            "missed": missed,
            "last_primary_hb_utc": hb,
        }), encoding="utf-8")

    def _acquire(self, base: Decision, read, doc, due: list[Slot], why: str, dry_run: bool) -> Decision:
        slot = due[0]
        if dry_run:
            base.action = "would_run"
            base.reason = why
            base.slots = due
            base.entry_permitted = any(entry_permitted(s) for s in due)
            base.fence_confirmed = False
            base.publish_allowed = False
            return base
        now_utc = _utcnow(self.now)
        if self._we_hold(doc):
            epoch = int(doc["epoch"])
            token = str(doc["token"])
            acquired = doc.get("acquired_utc") or now_utc.isoformat()
        else:
            epoch = int(doc["epoch"]) + 1 if doc and doc.get("epoch") is not None else 1
            token = f"{epoch}:{self.host}:{self.instance}"
            acquired = now_utc.isoformat()
        ttl = max(LEASE_TTL_S.get(s.kind, 600) for s in due)
        proposed = {
            "schema": 1,
            "track": "v4paper",
            "account_pin_last4": PIN_LAST4,
            "primary_host": PRIMARY_HOST,
            "owner": self.host,
            "epoch": epoch,
            "token": token,
            "instance": self.instance,
            "status": "holding",
            "date": f"{slot.when:%Y-%m-%d}",
            "slot": slot.hhmm,
            "kind": slot.kind,
            "acquired_utc": acquired,
            "renewed_utc": now_utc.isoformat(),
            "expires_utc": (now_utc + timedelta(seconds=ttl)).isoformat(),
            "reason": why,
            "completed": list((doc or {}).get("completed") or []),
        }
        result = self._cas(read.sha or "", proposed)
        if self._confirm(result, token) is None:
            base.action = "fail_closed"
            base.reason = "ambiguous_timeout" if result.status == "ambiguous" else "cas_lost"
            base.fence_confirmed = False
            base.slots = []
            return base
        if why == "takeover" and not self._takeover_path().exists():
            self._write_takeover(base.missed)
            self._problem_alert(
                "takeover",
                f"ORB v4 PAPER 🚨 box backup takeover — {self.host} acquired the lease after the primary missed {base.missed} slot(s).",
                dry_run=dry_run,
            )
        base.action = "run"
        base.reason = why
        base.slots = due
        base.fence_token = token
        base.fence_confirmed = True
        base.publish_allowed = True
        base.entry_permitted = any(entry_permitted(s) for s in due)
        return base

    def _release(self, base: Decision, read, doc, dry_run: bool) -> Decision:
        base.handback = True
        base.publish_allowed = False
        if dry_run:
            base.action = "would_handback"
            base.reason = "primary_heartbeat_newer"
            return base
        now_utc = _utcnow(self.now)
        epoch = int(doc.get("epoch") or 0) + 1
        proposed = dict(doc)
        proposed.update({
            "epoch": epoch,
            "status": "released",
            "token": f"{epoch}:{self.host}:released",
            "owner": self.host,
            "released_utc": now_utc.isoformat(),
            "reason": "handback",
            "expires_utc": now_utc.isoformat(),
            "completed": list(doc.get("completed") or []),
        })
        result = self._cas(read.sha or "", proposed)
        if result.status != "ok":
            base.action = "fail_closed"
            base.reason = "handback_unconfirmed"
            base.fence_confirmed = False
            return base
        path = self._takeover_path()
        if path.exists():
            path.unlink()
        self._problem_alert(
            "handback",
            f"ORB v4 PAPER ℹ️ failover handback — {self.host} released the lease; primary heartbeat is newer.",
            dry_run=dry_run,
        )
        base.action = "handback"
        base.reason = "primary_heartbeat_newer"
        base.fence_confirmed = False
        base.fence_token = None
        base.slots = []
        return base

    def _note_completed(self, slot: Slot, token: str | None) -> None:
        if not token:
            return
        try:
            current = self.transport.read()
        except TransportError:
            return
        doc = current.doc or {}
        if doc.get("token") != token or doc.get("status") != "holding":
            return
        completed = list(doc.get("completed") or [])
        if slot.key not in completed:
            completed.append(slot.key)
        updated = dict(doc)
        updated["completed"] = completed
        updated["renewed_utc"] = _utcnow(self.now).isoformat()
        self._cas(current.sha or "", updated)

    def _cas(self, expected: str, doc: dict) -> WriteResult:
        try:
            return self.transport.cas_write(expected, doc)
        except TransportError as e:
            return WriteResult("ambiguous", None, str(e)[:300])

    def _confirm(self, result: WriteResult, token: str) -> dict | None:
        if result.status == "rejected":
            return None
        try:
            current = self.transport.read()
        except TransportError:
            return None
        doc = current.doc or {}
        if doc.get("token") == token and doc.get("status") == "holding":
            return doc
        return None

    def _token_still_ours(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            current = self.transport.read()
        except TransportError:
            return False
        doc = current.doc or {}
        if doc.get("token") != token or doc.get("status") != "holding":
            return False
        return lease_view(_parse_exp(doc), _utcnow(self.now), self.skew_s) == "held"
