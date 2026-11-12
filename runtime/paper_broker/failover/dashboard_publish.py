"""Non-blocking PAPER dashboard / heartbeat publish.

A publish failure must never delay or fail a trading slot. Only a confirmed
lease owner should call this (primary after a slot, or box backup while it
holds the lease after a takeover). A host must not overwrite a newer
heartbeat from the other host. This module never passes --live or --arm and
does not change the dashboard layout.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_broker.failover.publish_policy import should_publish

log = logging.getLogger("orb_v4_publish")
_push_lock = threading.Lock()

KIND_OK = {"scan", "exit", "eod", "verify"}


def _state_dir() -> Path:
    if os.environ.get("ORB_V4_RUN"):
        return Path(os.environ["ORB_V4_RUN"]) / "state"
    from paper_broker import v4_paths as P
    return P.STATE


def _home() -> Path:
    return Path(os.environ.get("ORB_V4_HOME") or Path(__file__).resolve().parents[2])


def _run_dir() -> Path:
    if os.environ.get("ORB_V4_RUN"):
        return Path(os.environ["ORB_V4_RUN"])
    from paper_broker import v4_paths as P
    return P.RUN


def _keys_label() -> str:
    try:
        from paper_broker import v4_keys
        return "ok" if v4_keys.present() else "missing"
    except Exception:
        return "n/a"


def build_heartbeat(host: str, *, kind: str | None = None, slot: str | None = None,
                    date: str | None = None, rc: int = 0) -> dict:
    return {
        "track": "v4paper",
        "host": host,
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind or "alive",
        "slot": slot,
        "date": date,
        "rc": rc,
        "keys": _keys_label(),
    }


def write_local_heartbeat(doc: dict, path: Path | None = None) -> Path:
    dest = path or (_state_dir() / "heartbeat.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc), encoding="utf-8")
    return dest


def _remote_heartbeat(fetch=None) -> dict | None:
    try:
        from paper_broker.failover.heartbeat_fetch import fetch_heartbeat
        hb = (fetch or fetch_heartbeat)()
        if hb is None or getattr(hb, "error", None) or getattr(hb, "ts_utc", None) is None:
            return None
        ts = hb.ts_utc
        return {
            "host": hb.host,
            "ts_utc": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "kind": hb.kind,
            "trading": hb.trading,
        }
    except Exception:
        return None


def _slot_fields(slot: Any) -> tuple[str | None, str | None, str | None]:
    if slot is None:
        return None, None, None
    kind = getattr(slot, "kind", None) or (slot.get("kind") if isinstance(slot, dict) else None)
    hhmm = getattr(slot, "hhmm", None) or (slot.get("slot") if isinstance(slot, dict) else None)
    when = getattr(slot, "when", None)
    date = f"{when:%Y-%m-%d}" if when is not None else (slot.get("date") if isinstance(slot, dict) else None)
    return kind, hhmm, date


def _append_log(msg: str) -> None:
    try:
        logf = _run_dir() / "logs" / "dashboard_publish.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        with logf.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def _push() -> str:
    """Run refresh_v4_dashboard.py --push. Never raises."""
    with _push_lock:
        try:
            home = _home()
            script = home / "paper_broker" / "scripts" / "refresh_v4_dashboard.py"
            if not script.is_file():
                _append_log(f"push skipped: missing {script}")
                return "missing_script"
            py = os.environ.get("ORB_V4_PY") or sys.executable
            env = dict(os.environ)
            env["ORB_BROKER_MODE"] = "paper"
            env.pop("ORB_LIVE_SECRETS", None)
            args = [str(py), str(script), "--push"]
            if any(a in ("--live", "--arm", "-live", "-arm") for a in args):
                return "refused_live"
            r = subprocess.run(
                args,
                cwd=str(home),
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            last = tail[-1][:240] if tail else ""
            _append_log(f"push rc={r.returncode} {last}")
            return f"dashboard rc={r.returncode}"
        except Exception as e:
            _append_log(f"push error {type(e).__name__}")
            return f"error:{type(e).__name__}"


def schedule_publish(
    *,
    host: str,
    role: str,
    publish_allowed: bool,
    slot: Any = None,
    rc: int = 0,
    takeover_since_utc=None,
    dry_run: bool = False,
    wait: bool = False,
    fetch=None,
    pusher=None,
) -> str:
    """Write the local heartbeat and start a background Pages push. Never raises."""
    try:
        if dry_run:
            return "skipped_dry_run"
        if not publish_allowed:
            return "skipped_not_allowed"
        if role == "backup" and not publish_allowed:
            return "skipped_not_allowed"
        kind, hhmm, date = _slot_fields(slot)
        # Only real slot ticks may become the public liveness heartbeat.
        # The 2026-11-12 06:31 recovery flatten published kind=alive from the
        # box and the backup treated it as a healthy primary at 06:35 PT.
        if kind not in KIND_OK:
            _append_log(f"publish skipped: kind={kind!r} is not a slot tick")
            return "skipped_not_a_slot"
        local = build_heartbeat(host, kind=kind, slot=hhmm, date=date, rc=rc)
        write_local_heartbeat(local)
        remote = _remote_heartbeat(fetch=fetch)
        if role == "backup" and remote is None:
            # Do not risk overwriting a newer YOPC heartbeat we failed to read.
            _append_log("publish skipped: backup remote heartbeat unreadable")
            return "skipped_remote_unreadable"
        if not should_publish(local, remote, takeover_since_utc=takeover_since_utc):
            _append_log("publish skipped: newer remote heartbeat")
            return "skipped_newer_remote"
        if os.environ.get("ORB_V4_NO_PUBLISH") and role != "primary":
            _append_log("publish skipped: ORB_V4_NO_PUBLISH")
            return "skipped_no_publish"
        worker = pusher or _push
        thread = threading.Thread(target=worker, name="orb-v4-pages-publish", daemon=True)
        thread.start()
        if wait:
            thread.join(timeout=700)
        return "scheduled"
    except Exception as e:
        try:
            log.exception("schedule_publish failed")
            _append_log(f"schedule_publish error {type(e).__name__}")
        except Exception:
            pass
        return f"error:{type(e).__name__}"
