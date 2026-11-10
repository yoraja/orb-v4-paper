"""Operator entry points. Refuses --live and --arm. Does not submit orders itself.

``plan`` prints the decision and does not trade. ``primary-daemon`` and
``backup-tick`` are what the Windows tasks call on the hosts. They fail closed
when the ownership remote is unset or the fencing token is not confirmed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_broker.failover.runner import PIN_LAST4, AccountContext, Runner
from paper_broker.failover.schedule import PT, regular_grid
from paper_broker.failover.transport import OwnershipPrerequisite, make_transport_from_env

REFUSED = ("--live", "--arm", "-live", "-arm")


def _refuse_flags(argv: list[str]) -> None:
    for arg in argv:
        if arg in REFUSED or arg.startswith("--live=") or arg.startswith("--arm="):
            raise SystemExit(f"FAIL CLOSED: refusing {arg}")


def _account_from_env() -> AccountContext:
    last4 = (os.environ.get("ORB_V4_ACCOUNT_LAST4") or PIN_LAST4).strip()
    base = (os.environ.get("ORB_V4_BASE_URL") or "https://paper-api.alpaca.markets").strip()
    return AccountContext(last4=last4, base_url=base, clock_open=None)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="paper_broker.failover.cli", description="ORB v4 PAPER role runner (paper only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "backup-tick", "primary-daemon"):
        sp = sub.add_parser(name)
        sp.add_argument("--role", choices=("primary", "backup"))
        sp.add_argument("--host", choices=("yopc", "box"))
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--now", default=None, help="YYYY-MM-DDTHH:MM PT (tests and dry-run only)")
    sub.add_parser("self-test")
    return ap


def _now(value: str | None, dry_run: bool) -> datetime:
    if value:
        if not dry_run:
            raise SystemExit("FAIL CLOSED: --now requires --dry-run")
        return datetime.fromisoformat(value).replace(tzinfo=PT)
    return datetime.now(PT)


def _decision_payload(decision) -> dict:
    return {
        "action": decision.action,
        "reason": decision.reason,
        "role": decision.role,
        "host": decision.host,
        "missed": decision.missed,
        "daemon_dead": decision.daemon_dead,
        "slots": [s.key for s in decision.slots],
        "fence_confirmed": decision.fence_confirmed,
        "publish_allowed": decision.publish_allowed,
        "entry_permitted": decision.entry_permitted,
        "orders_submitted": decision.orders_submitted,
        "dry_run": decision.dry_run,
        "handback": decision.handback,
        "prerequisite": decision.prerequisite,
    }


def cmd_plan(args) -> int:
    role = args.role or ("primary" if args.cmd == "primary-daemon" else "backup")
    host = args.host or ("yopc" if role == "primary" else "box")
    if role == "primary" and host != "yopc":
        print("FAIL CLOSED: primary host must be yopc")
        return 2
    if role == "backup" and host != "box":
        print("FAIL CLOSED: backup host must be box")
        return 2
    now = _now(args.now, args.dry_run or args.cmd == "plan")
    try:
        transport = make_transport_from_env()
    except OwnershipPrerequisite as e:
        print("FAIL CLOSED: ownership prerequisite")
        print(str(e))
        return 2
    state = Path(os.environ.get("ORB_V4_RUN") or (Path.home() / ".orb-v4-paper" / "run")) / "state"
    runner = Runner(
        role=role,
        host=host,
        instance=os.environ.get("ORB_V4_INSTANCE") or f"{host}-cli",
        transport=transport,
        state_dir=state,
        now=now,
        slots=regular_grid(now.date()),
        heartbeat=None if role == "primary" else _unreadable_heartbeat(),
        account=_account_from_env(),
    )
    # backup-tick without a heartbeat reader fail-closes inside plan (heartbeat_unreadable)
    # unless the operator injected ORB_V4_HEARTBEAT_JSON (no network in this module).
    if role == "backup":
        runner.heartbeat = _load_heartbeat()
    dry = bool(args.cmd == "plan" or args.dry_run)
    if dry:
        decision = runner.plan(dry_run=True)
    else:
        decision = runner.run(_engine_executor, dry_run=False)
    print(json.dumps(_decision_payload(decision), indent=1))
    if decision.action == "fail_closed" and decision.reason in ("ownership_prerequisite", "lease_unreadable", "heartbeat_unreadable", "ambiguous_timeout", "lease_expiry_ambiguous"):
        return 2
    return 0


def _unreadable_heartbeat():
    from paper_broker.failover.runner import Heartbeat
    return Heartbeat(error="not provided")


def _load_heartbeat():
    if os.environ.get("ORB_V4_HEARTBEAT_JSON"):
        return _heartbeat_from_env()
    if os.environ.get("ORB_V4_ALLOW_HEARTBEAT_FETCH") == "1":
        from paper_broker.failover.heartbeat_fetch import fetch_heartbeat
        return fetch_heartbeat()
    from paper_broker.failover.runner import Heartbeat
    return Heartbeat(error="heartbeat not provided to this process")


def _heartbeat_from_env():
    from paper_broker.failover.runner import Heartbeat
    raw = os.environ.get("ORB_V4_HEARTBEAT_JSON")
    if not raw:
        return Heartbeat(error="heartbeat not provided to this process")
    doc = json.loads(raw)
    ts = doc.get("ts_utc")
    return Heartbeat(
        host=doc.get("host"),
        ts_utc=datetime.fromisoformat(ts) if ts else None,
        kind=doc.get("kind"),
        slot=doc.get("slot"),
        rc=doc.get("rc"),
        ok=doc.get("ok"),
        trading=doc.get("trading"),
        date=doc.get("date"),
        error=doc.get("error"),
    )


def _engine_executor(slot, token: str) -> dict:
    """Call the already-installed PAPER engine. Never adds --live or --arm.

    The cloud tree has no engine and does not set ORB_V4_ENGINE_ALLOW, so this
    returns without a subprocess. The host role script sets the allow flag only
    after INSTALL.md prechecks.
    """
    if os.environ.get("ORB_V4_ENGINE_ALLOW") != "1":
        return {"rc": 2, "orders": 0, "detail": "engine launch not allowed in this process"}
    import importlib.util
    import subprocess
    if importlib.util.find_spec("paper_broker.orb_v4_paper") is None:
        return {"rc": 2, "orders": 0, "detail": "installed engine paper_broker.orb_v4_paper not found"}
    env = dict(os.environ)
    env["ORB_V4_FENCE_TOKEN"] = token
    env["ORB_V4_FENCE_REQUIRED"] = "1"
    env["ORB_SLOT"] = slot.hhmm
    env["ORB_BROKER_MODE"] = "paper"
    for flag in ("--live", "--arm"):
        if flag in env.get("ORB_V4_ENGINE_ARGS", ""):
            return {"rc": 2, "orders": 0, "detail": "refusing live/arm"}
    proc = subprocess.run(
        [sys.executable, "-m", "paper_broker.orb_v4_paper", slot.kind],
        env=env,
        cwd=os.environ.get("ORB_V4_HOME") or None,
    )
    return {"rc": int(proc.returncode or 0), "orders": 0, "detail": "engine exit"}


def cmd_self_test() -> int:
    from paper_broker.failover.schedule import PARSE_EXACT_FORMAT, broken_format_rejects, parse_exact_slot
    got = parse_exact_slot("2026-11-06", "1045")
    if got.hour != 10 or got.minute != 45:
        print("FAIL parse")
        return 1
    if not broken_format_rejects("2026-11-06", "1045"):
        print("FAIL broken format should reject")
        return 1
    if PARSE_EXACT_FORMAT != "yyyy-MM-ddHHmm":
        print("FAIL format string")
        return 1
    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _refuse_flags(argv)
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return cmd_self_test()
    if args.cmd == "plan":
        args.dry_run = True
    return cmd_plan(args)


if __name__ == "__main__":
    raise SystemExit(main())
