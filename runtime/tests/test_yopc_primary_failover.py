"""YOPC-primary / box-backup proofs. No broker, no network, no day-state on the host."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_broker.failover import close_guard, schedule
from paper_broker.failover.carry_manifest import load_manifest, overlay_manifest, recovery_coid
from deploy.apply_hooks import apply_engine_hooks, assert_not_dashboard, recovery_status
from paper_broker.failover.cli import main as cli_main
from paper_broker.failover.fence import assert_fence, holds_fence
from paper_broker.failover.heartbeat_fetch import fetch_heartbeat
from paper_broker.failover.publish_policy import should_publish
from paper_broker.failover.runner import AccountContext, Heartbeat, Runner, order_ids_for
from paper_broker.failover.schedule import Slot, mark_outcome, parse_exact_slot, regular_grid
from paper_broker.failover.transport import GitBranchTransport, MemoryTransport, set_transport_factory as set_factory
from paper_broker.failover.venv_guard import SharedVenvRefused, assert_paper_python, inspect_venv

# fence.set_transport_factory was wrong; the factory lives on transport.
from paper_broker.failover import fence as fence_mod
from paper_broker.failover import transport as transport_mod

PT = ZoneInfo("America/Los_Angeles")
EVIDENCE = {}


def pt(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=PT)


def hb(when, **kw):
    base = dict(host="yopc", kind="scan", slot=f"{when:%H%M}", rc=0, ok=True, trading=True, date=f"{when:%Y-%m-%d}")
    base.update(kw)
    return Heartbeat(ts_utc=when.astimezone(timezone.utc), **base)


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.transport = MemoryTransport()
        transport_mod.set_transport_factory(lambda: self.transport)
        os.environ.pop("ORB_V4_FENCE_TOKEN", None)
        os.environ.pop("ORB_V4_FENCE_REQUIRED", None)
        os.environ.pop("ORB_V4_OWNERSHIP_REMOTE", None)
        os.environ.pop("ORB_V4_ENGINE_ALLOW", None)
        os.environ.pop("ORB_V4_ALLOW_HEARTBEAT_FETCH", None)
        self.calls = []

    def tearDown(self):
        transport_mod.set_transport_factory(None)
        os.environ.pop("ORB_V4_FENCE_TOKEN", None)
        os.environ.pop("ORB_V4_FENCE_REQUIRED", None)
        self.tmp.cleanup()

    def executor(self, slot, token):
        self.calls.append(slot.key)
        orders = 1 if schedule.entry_permitted(slot) or slot.kind in ("exit", "eod") else 0
        return {"rc": 0, "orders": orders, "token": token}

    def runner(self, role, when, *, instance=None, heartbeat=None, account=None, state=None):
        host = "yopc" if role == "primary" else "box"
        return Runner(
            role=role,
            host=host,
            instance=instance or f"{host}-1",
            transport=self.transport,
            state_dir=state or (self.root / role),
            now=when,
            slots=regular_grid(when.date()),
            heartbeat=heartbeat,
            account=account or AccountContext(),
        )

    def test_parse_exact_fix_and_failed_slot_not_successful(self):
        got = parse_exact_slot("2026-11-06", "1045")
        self.assertEqual(got, pt(2026, 11, 6, 10, 45))
        self.assertTrue(schedule.broken_format_rejects("2026-11-06", "1045"))
        self.assertRaises(ValueError, parse_exact_slot, "2026-11-06", "10:45")
        slot = Slot(got, "scan")
        self.assertFalse(schedule.is_successful(self.root, slot))
        self.assertEqual(mark_outcome(self.root, slot, 1, detail="parse or broker failure"), "fail")
        self.assertFalse(schedule.is_successful(self.root, slot))
        self.assertTrue((self.root / "slot_scan_20261106_1045.fail").is_file())
        self.assertFalse((self.root / "slot_scan_20261106_1045.ok").exists())
        hb_fail = {"ok": False, "trading": True, "kind": "scan", "slot": "1045", "date": "2026-11-06", "rc": 1}
        self.assertFalse(schedule.heartbeat_covers_slot(hb_fail, slot))
        EVIDENCE["parse_exact"] = "yyyy-MM-ddHHmm accepts 2026-11-061045; spaced format rejects; rc 1 is not success"

    def test_grid_phases_and_settings(self):
        day = datetime(2026, 11, 6).date()
        slots = regular_grid(day)
        scans = [s for s in slots if s.kind == "scan"]
        self.assertEqual(len(scans), 28)
        self.assertEqual(scans[0].hhmm, "0600")
        self.assertEqual(scans[-1].hhmm, "1245")
        self.assertEqual([(s.hhmm, s.kind) for s in slots if s.kind != "scan"],
                         [("1246", "exit"), ("1255", "eod"), ("1305", "verify")])
        self.assertEqual(schedule.postclose_slot(day).hhmm, "1320")
        self.assertEqual(schedule.phase_for(scans[2]), "signal_only")  # 06:30
        self.assertEqual(schedule.phase_for(next(s for s in scans if s.hhmm == "0730")), "arm")
        self.assertFalse(schedule.entry_permitted(scans[2]))
        rules = json.loads((ROOT / "daily" / "docs" / "rules_orb_v4_paper.json").read_text())
        r = rules["rules"]
        self.assertEqual(r["universe"], ["NVDA", "TSLA", "INTC", "PLTR", "AMD", "AAPL", "AMZN", "GOOGL", "MU", "MSFT", "META", "AVGO", "WMT", "CSCO"])
        self.assertEqual(r["risk_frac_per_trade"], 0.015)
        self.assertEqual(r["max_notional_frac_per_symbol"], 0.16)
        self.assertEqual(r["max_concurrent"], 6)
        self.assertTrue(r["cash_only"])
        self.assertEqual(r["daily_loss_limit_R"], -2.0)
        self.assertEqual(r["no_entry_after_pt"], "12:45")
        self.assertEqual(r["stop_loss"], "Opening-range midpoint")
        self.assertEqual(r["profit_target"], "2.5R")
        self.assertEqual(r["v4_carry"]["hold_trading_days"], 2)
        self.assertEqual(r["client_order_id_prefix"], "orbv4p")
        EVIDENCE["settings"] = "14 symbols, 1.5% risk, 16% cap, 6 concurrent, cash-only, -2R, entries to 12:45, OR-mid, 2.5R, 2-day carry"

    def test_monday_signal_only_submits_no_entry(self):
        when = pt(2026, 11, 9, 6, 30, 30)
        decision = self.runner("primary", when).run(self.executor)
        self.assertEqual(decision.action, "run")
        self.assertFalse(decision.entry_permitted)
        self.assertEqual(decision.orders_submitted, 0)
        self.assertEqual(self.calls, ["2026-11-09 scan 0630"])
        EVIDENCE["monday_signal_only"] = "2026-11-09 06:30 scan runs with zero entry orders"

    def test_primary_healthy_backup_submits_nothing(self):
        when = pt(2026, 11, 6, 10, 20)
        backup = self.runner("backup", when, heartbeat=hb(pt(2026, 11, 6, 10, 16)))
        decision = backup.run(self.executor)
        self.assertEqual(decision.action, "idle")
        self.assertEqual(decision.reason, "primary_healthy")
        self.assertEqual(decision.orders_submitted, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transport.writes, 0)
        EVIDENCE["healthy_primary"] = "backup idle, zero CAS writes, zero orders"

    def test_stale_heartbeat_with_live_lease_does_not_take_over(self):
        when = pt(2026, 11, 6, 10, 41)
        self.transport.doc = {
            "status": "holding", "owner": "yopc", "instance": "yopc-a",
            "token": "1:yopc:yopc-a", "epoch": 1,
            "expires_utc": (when + timedelta(minutes=10)).astimezone(timezone.utc).isoformat(),
        }
        self.transport.sha = "held"
        decision = self.runner("backup", when, heartbeat=hb(pt(2026, 11, 6, 9, 50))).run(self.executor)
        self.assertTrue(decision.daemon_dead)
        self.assertGreaterEqual(decision.missed, 2)
        self.assertEqual(decision.action, "idle")
        self.assertEqual(decision.reason, "lease_held_elsewhere")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transport.writes, 0)
        EVIDENCE["slow_primary"] = "stale heartbeat plus unexpired YOPC lease leaves backup idle"

    def test_missed_primary_takeover_exactly_once_and_failed_retry_is_not_success(self):
        when = pt(2026, 11, 6, 10, 46)
        heartbeat = hb(pt(2026, 11, 6, 10, 7))
        first = self.runner("backup", when, instance="box-1", heartbeat=heartbeat).run(self.executor)
        self.assertTrue(first.daemon_dead)
        self.assertEqual(first.action, "run")
        self.assertEqual(first.reason, "takeover")
        self.assertEqual(self.calls, ["2026-11-06 scan 1045"])
        self.assertEqual(first.orders_submitted, 1)
        self.assertIn("2026-11-06 scan 1045", self.transport.doc.get("completed") or [])
        again = self.runner("backup", when + timedelta(minutes=1), instance="box-1", heartbeat=heartbeat).run(self.executor)
        self.assertEqual(again.action, "idle")
        self.assertEqual(again.orders_submitted, 0)
        self.assertEqual(self.calls, ["2026-11-06 scan 1045"])
        other = self.runner("backup", when + timedelta(minutes=1), instance="box-2", heartbeat=heartbeat).run(self.executor)
        self.assertEqual(other.action, "idle")
        self.assertEqual(other.reason, "lease_held_elsewhere")
        self.assertEqual(len(self.calls), 1)
        # A failed attempt is not completed and is not an .ok marker.
        fail_transport = MemoryTransport()
        self.transport = fail_transport
        calls = []

        def fail_exec(slot, token):
            calls.append(slot.key)
            return {"rc": 1, "orders": 0}

        when2 = pt(2026, 11, 6, 11, 1)
        failed = self.runner("backup", when2, instance="box-9", heartbeat=hb(pt(2026, 11, 6, 10, 7)),
                             state=self.root / "fail").run(fail_exec)
        self.assertEqual(failed.action, "run")
        slot = next(s for s in regular_grid(when2.date()) if s.hhmm == "1100")
        self.assertFalse(schedule.is_successful(self.root / "fail", slot))
        self.assertNotIn(slot.key, fail_transport.doc.get("completed") or [])
        EVIDENCE["takeover_once"] = "10:46 takeover of 1045 submits once; second tick and second instance do not; rc 1 is not success"

    def test_two_backup_cas_exactly_one_winner(self):
        when = pt(2026, 11, 6, 10, 46)
        heartbeat = hb(pt(2026, 11, 6, 10, 7))
        results = []

        def go(instance):
            results.append(self.runner("backup", when, instance=instance, heartbeat=heartbeat,
                                       state=self.root / instance).plan())

        threads = [threading.Thread(target=go, args=(name,)) for name in ("box-a", "box-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        runs = [d for d in results if d.action == "run" and d.fence_confirmed]
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.transport.doc["owner"], "box")
        EVIDENCE["cas_once"] = "two backup planners, one confirmed lease"

    def test_ambiguous_timeout_and_skew_fail_closed(self):
        when = pt(2026, 11, 6, 10, 46)
        heartbeat = hb(pt(2026, 11, 6, 10, 7))
        self.transport.mode = "ambiguous_stale"
        stale = self.runner("backup", when, heartbeat=heartbeat).run(self.executor)
        self.assertEqual(stale.action, "fail_closed")
        self.assertEqual(stale.reason, "ambiguous_timeout")
        self.assertEqual(stale.orders_submitted, 0)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.transport.doc is None or self.transport.doc.get("owner") != "box")
        self.transport = MemoryTransport()
        self.transport.mode = "ambiguous_unreadable"
        unread = self.runner("backup", when, heartbeat=heartbeat, state=self.root / "u").run(self.executor)
        self.assertEqual(unread.action, "fail_closed")
        self.assertEqual(unread.orders_submitted, 0)
        self.transport = MemoryTransport()
        self.transport.doc = {
            "status": "holding", "owner": "yopc", "instance": "yopc-a", "epoch": 3,
            "token": "3:yopc:yopc-a",
            "expires_utc": (when + timedelta(seconds=10)).astimezone(timezone.utc).isoformat(),
        }
        self.transport.sha = "edge"
        skewed = self.runner("backup", when, heartbeat=heartbeat, state=self.root / "skew").run(self.executor)
        self.assertEqual(skewed.action, "fail_closed")
        self.assertEqual(skewed.reason, "lease_expiry_ambiguous")
        self.assertEqual(self.transport.writes, 0)
        self.assertEqual(self.calls, [])
        EVIDENCE["ambiguous"] = "unconfirmed push and expiry inside the skew band submit nothing"

    def test_handback_then_primary_does_not_repeat_completed_slot(self):
        when = pt(2026, 11, 6, 10, 46)
        heartbeat = hb(pt(2026, 11, 6, 10, 7))
        backup = self.runner("backup", when, instance="box-1", heartbeat=heartbeat)
        first = backup.run(self.executor)
        self.assertEqual(first.orders_submitted, 1)
        later = pt(2026, 11, 6, 10, 52)
        returned = hb(later, kind="alive", slot=None, trading=False, ok=True)
        hand = self.runner("backup", later, instance="box-1", heartbeat=returned, state=self.root / "backup").run(self.executor)
        self.assertEqual(hand.action, "handback")
        self.assertEqual(hand.orders_submitted, 0)
        self.assertEqual(self.transport.doc["status"], "released")
        self.assertIn("2026-11-06 scan 1045", self.transport.doc["completed"])
        primary = self.runner("primary", later + timedelta(seconds=30), instance="yopc-b", state=self.root / "yopc").run(self.executor)
        self.assertNotIn("2026-11-06 scan 1045", [s.key for s in primary.slots] if primary.slots else [])
        self.assertEqual(primary.orders_submitted, 0)
        self.assertEqual(self.calls, ["2026-11-06 scan 1045"])
        EVIDENCE["handback"] = "backup releases on a newer YOPC heartbeat and the completed slot is not traded again"

    def test_crash_restart_old_token_cannot_submit(self):
        when = pt(2026, 11, 6, 10, 46)
        primary = self.runner("primary", when, instance="yopc-a").run(self.executor)
        self.assertEqual(primary.action, "run")
        token = primary.fence_token
        self.transport.doc["expires_utc"] = (when - timedelta(minutes=2)).astimezone(timezone.utc).isoformat()
        # Clear completed so the backup has a due slot, then let it acquire.
        self.transport.doc["completed"] = []
        self.transport.doc["status"] = "holding"
        backup = self.runner("backup", when + timedelta(minutes=20), instance="box-1",
                             heartbeat=hb(pt(2026, 11, 6, 10, 7)), state=self.root / "box").run(self.executor)
        self.assertEqual(backup.action, "run")
        os.environ["ORB_V4_FENCE_REQUIRED"] = "1"
        os.environ["ORB_V4_FENCE_TOKEN"] = token
        self.assertFalse(holds_fence(when.astimezone(timezone.utc)))
        with self.assertRaises(SystemExit):
            assert_fence()
        restarted = self.runner("primary", when + timedelta(minutes=20), instance="yopc-b",
                                state=self.root / "restart").run(self.executor)
        self.assertEqual(restarted.action, "idle")
        self.assertEqual(restarted.reason, "lease_held_elsewhere")
        EVIDENCE["crash_restart"] = "expired primary token fails closed; restarted primary does not trade over the backup lease"

    def test_dry_run_does_not_write_lease_or_orders(self):
        when = pt(2026, 11, 6, 10, 46)
        decision = self.runner("backup", when, heartbeat=hb(pt(2026, 11, 6, 10, 7))).run(self.executor, dry_run=True)
        self.assertEqual(decision.action, "would_run")
        self.assertEqual(decision.orders_submitted, 0)
        self.assertEqual(self.transport.writes, 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(any((self.root / "backup").glob("takeover_*")))
        EVIDENCE["dry_run"] = "dry-run performs no CAS write and no order"

    def test_unreadable_lease_or_heartbeat_fail_closed(self):
        when = pt(2026, 11, 6, 10, 46)
        self.transport.mode = "read_error"
        decision = self.runner("backup", when, heartbeat=hb(when)).run(self.executor)
        self.assertEqual(decision.action, "fail_closed")
        self.assertEqual(decision.reason, "lease_unreadable")
        self.assertEqual(self.calls, [])
        self.transport.mode = "ok"
        blind = self.runner("backup", when, heartbeat=Heartbeat(error="timeout")).run(self.executor)
        self.assertEqual(blind.reason, "heartbeat_unreadable")
        self.assertEqual(blind.orders_submitted, 0)
        EVIDENCE["read_errors"] = "unreadable lease or heartbeat does not take over"

    def test_wrong_account_date_session_refuse(self):
        when = pt(2026, 11, 6, 10, 46)
        heartbeat = hb(pt(2026, 11, 6, 10, 7))
        wrong_acct = self.runner("backup", when, heartbeat=heartbeat, account=AccountContext(last4="CL96"),
                                 state=self.root / "acct").run(self.executor)
        self.assertEqual(wrong_acct.reason, "refuse_account")
        self.assertEqual(self.transport.writes, 0)
        wrong_date = self.runner(
            "backup", when, heartbeat=heartbeat,
            account=AccountContext(session_date=pt(2026, 11, 8, 10, 0)),
            state=self.root / "date",
        ).run(self.executor)
        self.assertEqual(wrong_date.reason, "refuse_date")
        self.assertEqual(self.transport.writes, 0)
        closed = self.runner(
            "backup", when, heartbeat=heartbeat,
            account=AccountContext(clock_open=False),
            state=self.root / "sess",
        ).run(self.executor)
        self.assertEqual(closed.reason, "refuse_session")
        self.assertEqual(self.calls, [])
        live = self.runner(
            "backup", when, heartbeat=heartbeat,
            account=AccountContext(base_url="https://api.alpaca.markets", live_flag=True),
            state=self.root / "live",
        ).run(self.executor)
        self.assertEqual(live.reason, "refuse_live")
        self.assertEqual(self.calls, [])
        EVIDENCE["guards"] = "wrong account, date, closed session, and live URL submit nothing and do not take the lease"

    def test_close_pending_partial_completed_and_no_flip(self):
        day = datetime(2026, 11, 9).date()
        short = {"symbol": "CSCO", "qty": -78, "side": "short"}
        pending = {"symbol": "CSCO", "side": "buy", "qty": "78", "status": "new",
                   "client_order_id": "orbv4p-xrec-20261109-CSCO", "filled_qty": 0}
        decision = close_guard.decide_close({"symbol": "CSCO", "side": "short", "qty": 78}, short, [pending], today=day)
        self.assertEqual(decision["action"], "recovery_close_pending")
        self.assertFalse(decision["submit"])
        partial_pos = {"symbol": "CSCO", "qty": -48, "side": "short"}
        partial = dict(pending, status="partially_filled", filled_qty=30)
        decision = close_guard.decide_close({"symbol": "CSCO", "side": "short", "qty": 78}, partial_pos, [partial], today=day)
        self.assertEqual(decision["action"], "recovery_close_pending")
        self.assertFalse(decision["submit"])
        done = close_guard.decide_close({"symbol": "CSCO", "side": "short", "qty": 0}, None, [], today=day)
        self.assertEqual(done["action"], "skip_flat")
        residual = close_guard.decide_close({"symbol": "CSCO", "side": "short", "qty": 78}, partial_pos, [], today=day)
        self.assertTrue(residual["submit"])
        self.assertEqual(residual["qty"], 48)
        self.assertEqual(residual["side"], "buy")
        self.assertEqual(residual["moc_coid"], "orbv4p-x-20261109-CSCO")
        self.assertNotEqual(residual["moc_coid"], recovery_coid("CSCO"))
        other = {"symbol": "WMT", "side": "sell", "qty": "62", "status": "new", "client_order_id": "manual-close", "type": "market"}
        blocked = close_guard.decide_close({"symbol": "WMT", "side": "long", "qty": 62},
                                           {"symbol": "WMT", "qty": 62, "side": "long"}, [other], today=day)
        self.assertEqual(blocked["action"], "pending_close")
        leg = {"symbol": "WMT", "side": "sell", "status": "held", "type": "stop", "stop_price": 100, "client_order_id": ""}
        allowed = close_guard.decide_close({"symbol": "WMT", "side": "long", "qty": 62},
                                           {"symbol": "WMT", "qty": 62, "side": "long"}, [leg], today=day)
        self.assertTrue(allowed["submit"])
        self.assertEqual(close_guard.entry_block("CSCO", {"bracket": {"side": "long"}}, {"CSCO": short}), "refuse_flip")
        self.assertEqual(close_guard.entry_block("CSCO", {"bracket": {"side": "short"}}, {"CSCO": short}), "already_in_position")
        flipped = close_guard.decide_close({"symbol": "CSCO", "side": "long", "qty": 78}, short, [], today=day)
        self.assertEqual(flipped["action"], "refuse_flip")
        ids_a = order_ids_for("NVDA", day, "1045")
        ids_b = order_ids_for("NVDA", day, "1045")
        self.assertEqual(ids_a, ids_b)
        self.assertNotEqual(ids_a["entry"], ids_a["moc"])
        self.assertNotEqual(ids_a["moc"], ids_a["recovery"])
        broker = {}

        def submit(coid, **payload):
            if coid in broker:
                return "duplicate"
            broker[coid] = payload
            return "accepted"

        self.assertEqual(submit(ids_a["entry"], side="buy"), "accepted")
        self.assertEqual(submit(ids_b["entry"], side="buy"), "duplicate")
        self.assertEqual(len(broker), 1)
        EVIDENCE["closes"] = "recovery pending/partial blocks a second close; residual close keeps the side; duplicate entry id is rejected; flip is refused"

    def test_carry_manifest_keeps_known_levels_only(self):
        man = load_manifest()
        self.assertEqual(set(man["positions"]), {"AAPL", "MSFT", "CSCO", "META", "WMT"})
        self.assertEqual(man["positions"]["MSFT"]["plan_stop"], 529.53)
        self.assertEqual(man["positions"]["MSFT"]["plan_target"], 546.75)
        self.assertEqual(man["positions"]["MSFT"]["entry_price"], 534.98)
        self.assertIsNone(man["positions"]["AAPL"]["plan_stop"])
        book = {"positions": {
            "MSFT": {"symbol": "MSFT", "side": "long", "qty": 15, "plan_stop": None, "plan_target": None, "entry_price": None},
            "AAPL": {"symbol": "AAPL", "side": "short", "qty": 23, "plan_stop": 300, "plan_target": None},
        }}
        overlay_manifest(book, man)
        self.assertEqual(book["positions"]["MSFT"]["plan_stop"], 529.53)
        self.assertEqual(book["positions"]["MSFT"]["qty"], 15)
        self.assertEqual(book["positions"]["AAPL"]["plan_stop"], 300)
        self.assertIsNone(book["positions"]["AAPL"]["plan_target"])
        EVIDENCE["carry"] = "five-name book; MSFT stop/target filled when blank; existing AAPL stop kept; missing targets stay null"

    def test_publish_and_venv_and_hooks(self):
        since = pt(2026, 11, 6, 10, 46).astimezone(timezone.utc)
        remote = {"host": "yopc", "ts_utc": pt(2026, 11, 6, 10, 50).astimezone(timezone.utc).isoformat(), "trading": False}
        local = {"host": "box", "ts_utc": pt(2026, 11, 6, 10, 51).astimezone(timezone.utc).isoformat()}
        self.assertFalse(should_publish(local, remote, takeover_since_utc=since))
        self.assertTrue(should_publish({"host": "yopc", "ts_utc": remote["ts_utc"]}, None))
        home = self.root / "paper-home"
        home.mkdir()
        (home / ".venv").symlink_to(self.root / "trading-strategy" / ".venv")
        info = inspect_venv(home)
        self.assertTrue(info["symlink"])
        self.assertFalse(info["usable_for_pip"])
        outside = self.root / "trading-strategy" / ".venv" / "bin" / "python"
        with self.assertRaises(SharedVenvRefused):
            assert_paper_python(outside, home)
        dash = self.root / "pages"
        dash.mkdir()
        (dash / "index.html").write_text("x", encoding="utf-8")
        (dash / "strategy.html").write_text("x", encoding="utf-8")
        with self.assertRaises(Exception):
            assert_not_dashboard(dash)
        src = (
            'for r in sorted(due, key=lambda x: x["symbol"]):\n'
            '        if r["symbol"] in have_moc:\n'
            '            pass\n'
            '    if flat_syms:\n'
            '        fo = flatten(client, sorted(flat_syms), dry_run=dry_run, log=log)\n'
            '        submitted = client.submit_order(req)\n'
        )
        patched = apply_engine_hooks(src)
        self.assertIn("decide_close", patched)
        self.assertIn("before_submit", patched)
        with self.assertRaises(Exception):
            apply_engine_hooks("no anchors here")
        rec = self.root / "orb-v4-recovery-close-20261109.py"
        rec.write_text("changed\n", encoding="utf-8")
        self.assertEqual(recovery_status(rec), "mismatch")
        self.assertEqual(rec.read_text(encoding="utf-8"), "changed\n")
        EVIDENCE["publish_venv_hooks"] = "box does not overwrite a newer YOPC liveness heartbeat; symlink venv and dashboard root are refused; recovery bytes are not rewritten"

    def test_git_cas_two_hosts_one_owner(self):
        bare = self.root / "bare.git"
        subprocess.run(["git", "init", "--bare", "-b", "ownership", str(bare)], check=True, capture_output=True, text=True)
        a = GitBranchTransport(str(bare), self.root / "work-a")
        b = GitBranchTransport(str(bare), self.root / "work-b")
        doc = {"epoch": 1, "owner": "yopc", "token": "1:yopc:a", "status": "holding"}
        first = a.cas_write("", doc)
        self.assertEqual(first.status, "ok", first.detail)
        second = b.cas_write("", {"epoch": 1, "owner": "box", "token": "1:box:b", "status": "holding"})
        self.assertEqual(second.status, "rejected", second.detail)
        seen = b.read()
        self.assertEqual(seen.doc["owner"], "yopc")
        self.assertEqual(seen.doc["token"], "1:yopc:a")
        EVIDENCE["git_cas"] = "local bare repo compare-and-swap: second host's create is rejected and the first token remains"

    def test_heartbeat_parse_uses_injected_opener_only(self):
        seen = []

        class Resp:
            def read(self):
                return json.dumps({
                    "track": "v4paper", "host": "yopc", "ts_utc": "2026-11-06T18:07:00+00:00",
                    "kind": "scan", "slot": "1000", "rc": 0, "ok": True, "date": "2026-11-06",
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(req, timeout=20):
            seen.append(req.get_method() + " " + req.full_url)
            return Resp()

        got = fetch_heartbeat(opener)
        self.assertEqual(got.host, "yopc")
        self.assertTrue(all(item.startswith("GET ") for item in seen))
        self.assertTrue(all("heartbeat.json" in item for item in seen))
        EVIDENCE["heartbeat_get"] = "public heartbeat reader is GET-only and tested with a fake opener"

    def test_cli_refuses_live_and_missing_remote_without_orders(self):
        with self.assertRaises(SystemExit):
            cli_main(["self-test", "--live"])
        rc = cli_main(["self-test"])
        self.assertEqual(rc, 0)
        transport_mod.set_transport_factory(None)
        os.environ["ORB_V4_RUN"] = str(self.root / "cli-run")
        rc = cli_main(["plan", "--role", "backup", "--host", "box"])
        self.assertEqual(rc, 2)
        self.assertFalse((Path.home() / ".orb-v4-paper").exists())
        text = Path(ROOT / "deploy" / "orb-v4-role.ps1").read_text(encoding="utf-8")
        self.assertIn("[DateTime]::ParseExact($day + $hhmm, 'yyyy-MM-ddHHmm', $null)", text)
        self.assertNotIn("slack", text.lower())
        joined = "\n".join(p.read_text(encoding="utf-8").lower() for p in (ROOT / "paper_broker" / "failover").glob("*.py"))
        self.assertNotIn("slack.com", joined)
        self.assertNotIn("chat.postmessage", joined)
        EVIDENCE["cli"] = "CLI self-test passes; --live is refused; plan without the ownership remote exits 2"

    def test_role_script_and_package_do_not_touch_dashboard_names(self):
        names = sorted(p.name for p in ROOT.rglob("*") if p.is_file())
        self.assertNotIn("index.html", names)
        self.assertNotIn("strategy.html", names)
        self.assertNotIn("heartbeat.json", names)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FailoverTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    out = ROOT / "tests" / "evidence" / "mock_evidence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"passed": result.wasSuccessful(), "evidence": EVIDENCE}, indent=1), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
