"""PAPER slot grid, the preserved ParseExact fix, and success markers.

Scan grid: 06:00 through 12:45 PT every 15 minutes, fired 20 seconds later.
Signal-only before 07:30. Exit 12:46, eod/carry 12:55, verify 13:05.
Post-close 13:20 is read-only. A failed slot writes ``.fail`` and is not
successful. Only return codes 0 and 3 write ``.ok``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

# Preserved local fix from 2026-11-06. The broken format was 'yyyy-MM-dd HH:mm'
# against the concatenation date+HHmm ('2026-11-061045'), which threw and dropped
# every takeover slot.
# PowerShell: [DateTime]::ParseExact($day + $hhmm, 'yyyy-MM-ddHHmm', $null)
PARSE_EXACT_FORMAT = "yyyy-MM-ddHHmm"
PARSE_EXACT_STRPTIME = "%Y-%m-%d%H%M"
BROKEN_PARSE_FORMAT = "%Y-%m-%d %H:%M"

SLOT_DELAY_S = 20
SUCCESS_RCS = (0, 3)
PRIMARY_HOST = "yopc"
BACKUP_HOST = "box"
MISSED_SLOTS = 2
GRACE_MIN = 8
BACKUP_WINDOW_MIN = 10
SKEW_S = 20

LATE_OK_S = {"scan": 10 * 60, "exit": 3 * 60, "eod": 4 * 60, "verify": None, "postclose": 15 * 60}
LEASE_TTL_S = {"scan": 12 * 60, "exit": 16 * 60, "eod": 16 * 60, "verify": 10 * 60, "postclose": 10 * 60}


@dataclass(frozen=True)
class Slot:
    when: datetime
    kind: str

    @property
    def hhmm(self) -> str:
        return f"{self.when:%H%M}"

    @property
    def fire_at(self) -> datetime:
        delay = SLOT_DELAY_S if self.kind == "scan" else 0
        return self.when + timedelta(seconds=delay)

    @property
    def key(self) -> str:
        return f"{self.when:%Y-%m-%d} {self.kind} {self.hhmm}"


def parse_exact_slot(day: str, hhmm: str) -> datetime:
    """Parse date+HHmm with format yyyy-MM-ddHHmm. ValueError must not mark success."""
    if len(day) != 10 or len(hhmm) != 4 or not hhmm.isdigit():
        raise ValueError(f"slot stamp {day!r}+{hhmm!r} does not match {PARSE_EXACT_FORMAT}")
    return datetime.strptime(f"{day}{hhmm}", PARSE_EXACT_STRPTIME).replace(tzinfo=PT)


def broken_format_rejects(day: str, hhmm: str) -> bool:
    try:
        datetime.strptime(f"{day}{hhmm}", BROKEN_PARSE_FORMAT)
    except ValueError:
        return True
    return False


def regular_grid(day: date) -> list[Slot]:
    if day.weekday() >= 5:
        return []
    base = datetime(day.year, day.month, day.day, tzinfo=PT)
    out: list[Slot] = []
    t = base.replace(hour=6, minute=0)
    last = base.replace(hour=12, minute=45)
    while t <= last:
        out.append(Slot(t, "scan"))
        t += timedelta(minutes=15)
    out.append(Slot(base.replace(hour=12, minute=46), "exit"))
    out.append(Slot(base.replace(hour=12, minute=55), "eod"))
    out.append(Slot(base.replace(hour=13, minute=5), "verify"))
    return out


def postclose_slot(day: date) -> Slot:
    return Slot(datetime(day.year, day.month, day.day, 13, 20, tzinfo=PT), "postclose")


def phase_for(slot: Slot) -> str:
    if slot.kind == "scan" and (slot.when.hour, slot.when.minute) < (7, 30):
        return "signal_only"
    if slot.kind in ("postclose", "verify"):
        return "read_only"
    if slot.kind == "scan":
        return "arm"
    return slot.kind


def entry_permitted(slot: Slot) -> bool:
    return phase_for(slot) == "arm"


def late_ok(slot: Slot, now: datetime) -> bool:
    lag = (now - slot.when).total_seconds()
    if lag < 0:
        return False
    if slot.kind == "verify":
        return now.date() == slot.when.date()
    limit = LATE_OK_S.get(slot.kind)
    if limit is None:
        return False
    return lag <= limit


def backup_window_ok(slot: Slot, now: datetime) -> bool:
    lag = (now - slot.when).total_seconds()
    return 0 <= lag <= BACKUP_WINDOW_MIN * 60


def _marker(state: Path, slot: Slot, suffix: str) -> Path:
    return state / f"slot_{slot.kind}_{slot.when:%Y%m%d_%H%M}.{suffix}"


def mark_outcome(state: Path, slot: Slot, rc: int, *, detail: str = "") -> str:
    """Success is only rc 0 or 3. Failures never write ``.ok``."""
    state.mkdir(parents=True, exist_ok=True)
    ok = rc in SUCCESS_RCS
    name = "ok" if ok else "fail"
    path = _marker(state, slot, name)
    prev = 0
    if path.exists():
        try:
            prev = int(json.loads(path.read_text(encoding="utf-8")).get("attempts") or 0)
        except Exception:
            prev = 0
    path.write_text(json.dumps({
        "rc": rc,
        "ok": ok,
        "kind": slot.kind,
        "slot": slot.hhmm,
        "date": f"{slot.when:%Y-%m-%d}",
        "attempts": prev + 1,
        "detail": detail[:300],
    }), encoding="utf-8")
    return name


def is_successful(state: Path, slot: Slot) -> bool:
    path = _marker(state, slot, "ok")
    if not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return doc.get("ok") is True and doc.get("rc") in SUCCESS_RCS


def heartbeat_covers_slot(hb: dict | None, slot: Slot) -> bool:
    """A failed, non-trading, or different-slot heartbeat does not mark this slot successful.

    Only a YOPC primary tick can cover a slot. A box or kind=alive heartbeat never does.
    """
    if not hb or hb.get("error"):
        return False
    if hb.get("host") and str(hb.get("host")) != PRIMARY_HOST:
        return False
    if str(hb.get("kind") or "") not in ("scan", "exit", "eod", "verify"):
        return False
    if hb.get("trading") is False or hb.get("ok") is not True:
        return False
    if str(hb.get("kind") or "") != slot.kind:
        return False
    if str(hb.get("slot") or "") not in (slot.hhmm, f"{slot.when:%H:%M}"):
        return False
    hb_date = str(hb.get("date") or "")
    if hb_date and hb_date != f"{slot.when:%Y-%m-%d}":
        return False
    return True


def missed_slots(slots: list[Slot], now: datetime, hb_pt: datetime | None, grace_min: int = GRACE_MIN) -> list[Slot]:
    """Slots that are past grace and have no primary-liveness heartbeat after they fired.

    If there is no usable YOPC primary heartbeat (hb_pt is None), every slot
    past grace is missed. A box or kind=alive timestamp must not be passed in
    as hb_pt — that is what kept the backup idle on 2026-11-12.
    """
    cutoff = now - timedelta(minutes=grace_min)
    out = []
    for s in slots:
        if s.kind == "postclose" or s.when > cutoff:
            continue
        if hb_pt is not None and s.when <= hb_pt:
            continue
        out.append(s)
    return out


def lease_view(expires_utc: datetime | None, now_utc: datetime, skew_s: int = SKEW_S) -> str:
    if expires_utc is None:
        return "missing"
    delta = (expires_utc - now_utc).total_seconds()
    if abs(delta) <= skew_s:
        return "ambiguous"
    if delta > skew_s:
        return "held"
    return "expired"
