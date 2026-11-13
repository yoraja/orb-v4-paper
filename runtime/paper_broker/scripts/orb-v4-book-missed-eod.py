"""Book today's v4 entries into the carry book exactly as the 12:55 eod would, with NO orders.

Use only when the 12:55 eod slot was missed (2026-11-13: box backup froze holding the lease).
Runs the engine's own cmd_eod code path with a guarded Alpaca client:
  - any submit / cancel / close / replace call raises (no broker mutation possible),
  - the engine flatten() is replaced by a refusal (if earnings would force a flatten, this
    stops and reports instead of trading),
  - the fence is required with an empty token, so the engine's before_submit hook fails closed,
  - run summary is written locally, nothing is queued to the Slack outbox.
Reads keys via paper_broker.v4_keys (never printed). Checks the v4 account pin. Paper only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["ORB_BROKER_MODE"] = "paper"
os.environ["ORB_V4_FENCE_REQUIRED"] = "1"
os.environ["ORB_V4_FENCE_TOKEN"] = ""

for a in sys.argv[1:]:
    if a in ("--live", "--arm"):
        raise SystemExit("FAIL CLOSED: refusing " + a)

import paper_broker.orb_v4_paper as orb  # noqa: E402
from paper_broker import v4_keys  # noqa: E402
from paper_broker import v4_paths as P  # noqa: E402

MUTATING = {"submit_order", "cancel_order_by_id", "cancel_orders", "close_position",
            "close_all_positions", "replace_order_by_id", "exercise_options_position"}


class RefusedMutation(RuntimeError):
    pass


class GuardedClient:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in MUTATING:
            def _refuse(*a, **k):
                raise RefusedMutation(f"no-order booking: refused client.{name}")
            return _refuse
        return getattr(self._inner, name)


def _refuse_flatten(client, syms, *, dry_run, log):
    raise RefusedMutation(f"no-order booking: eod would flatten {syms} (earnings rule) — stopping, report to Yogesh")


def _no_cancel(client, symbols, log):
    open_syms = orb.eng._open_order_symbols(client)
    hit = sorted(set(symbols) & set(open_syms or ()))
    if open_syms is None or hit:
        raise RefusedMutation(f"no-order booking: open orders exist for {hit or symbols} — not cancelling")
    return []


RUN_FILES: list[Path] = []


def _write_run_local(cmd, summary, msgs, *, dry_run):
    now = orb._now()
    tag = f"{cmd}_booked_post_close_{now:%Y%m%d_%H%M}_{datetime.now().strftime('%H%M%S%f')}"
    summary.update({"cmd": cmd, "dry_run": False, "no_orders": True, "pt": now.isoformat(),
                    "how": "orb-v4-book-missed-eod.py (engine cmd_eod path, guarded client, no orders)",
                    "slack_messages": msgs, "relay_required": False})
    f = P.RUNS / f"{tag}.json"
    f.write_text(json.dumps(summary, indent=1, default=str))
    RUN_FILES.append(f)
    for m in msgs:
        print("[not sent] " + m)
    return f


def main() -> int:
    v4_keys.apply()
    raw = orb.make_client()
    pin = json.loads((P.STATE / "v4_account_pin.json").read_text())
    acct = raw.get_account()
    if hashlib.sha256(str(acct.id).encode()).hexdigest() != pin["account_id_sha256"]:
        print("FAIL CLOSED: keys do not resolve to the pinned v4 account")
        return 2
    print(f"account ..{str(acct.account_number)[-4:]} (pinned) · carry book {P.CARRY_STATE}")
    client = GuardedClient(raw)
    orb.make_client = lambda: client
    orb.flatten = _refuse_flatten
    orb.eng.cancel_open_orders_for = _no_cancel
    orb.write_run = _write_run_local
    lock = orb.eng.acquire_track_lock(timeout_s=60)
    if lock is None:
        print("LOCKED: another v4 run holds the lock — not booking")
        return 5
    try:
        rc = orb.cmd_eod(dry_run=False, client=client)
    except RefusedMutation as e:
        print(f"STOPPED (no orders): {e}")
        return 4
    cb = orb.load_carry()
    print(json.dumps({"eod_rc": rc, "carry": cb.get("positions"), "run_files": [str(f) for f in RUN_FILES]}, indent=1, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
