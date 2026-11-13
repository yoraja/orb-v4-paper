"""Reproduce Fri 2026-11-13 and prove the fixes. No broker, no network.

Real sequence that day (PT):
  12:30:22  YOPC acquires epoch 30 (scan 1230), TTL 12 min; heartbeat scan 1230 ~12:31.
  12:45:02  YOPC tick runs 18 s before the 12:45:20 fire time -> nothing due (12:45 was
            excluded from the +20 s wait).
  12:46:03  Box backup: 12:31 heartbeat is older than GRACE_MIN (8 min) and the 12:46
            exit fired 3 s ago -> 'stale and critical' -> takeover with 0 missed slots.
  12:46:04  Box wins the CAS (epoch 31, exit TTL 16 min -> 13:02:04); YOPC cas_lost.
  12:46:05  Box VM frozen until ~13:17 (tick watchdog could not run).
  12:47-13:01 YOPC: lease_held_elsewhere (no way to reclaim) -> exit 1246 and eod 1255 never ran.
  13:02:02  YOPC: lease_expiry_ambiguous rc 2. 13:05 verify ran -> rc 1 (carries not booked).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_broker.failover import schedule  # noqa: E402
from paper_broker.failover import transport as transport_mod  # noqa: E402
from paper_broker.failover.cli import _clock_jump_s  # noqa: E402
from paper_broker.failover.fence import holds_fence  # noqa: E402
from paper_broker.failover.runner import AccountContext, Heartbeat, Runner  # noqa: E402
from paper_broker.failover.schedule import Slot, regular_grid, run_order  # noqa: E402
from paper_broker.failover.transport import MemoryTransport  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
DAY = (2026, 11, 13)


def pt(hh, mm, ss=0):
    return datetime(*DAY, hh, mm, ss, tzinfo=PT)


def utc(dt):
    return dt.astimezone(timezone.utc).isoformat()


def yopc_hb(when, slot="1230", kind="scan"):
    return Heartbeat(host="yopc", ts_utc=when.astimezone(timezone.utc), kind=kind, slot=slot,
                     rc=0, ok=True, trading=True, date="2026-11-13")


COMPLETED_MORNING = [f"2026-11-13 scan {s.hhmm}" for s in regular_grid(pt(0, 0).date())
                     if s.kind == "scan" and s.hhmm <= "1230"]


class Nov13(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.t = MemoryTransport()
        transport_mod.set_transport_factory(lambda: self.t)
        self.calls: list[str] = []
        self._pub = patch("paper_broker.failover.dashboard_publish.schedule_publish", return_value="stubbed")
        self._pub.start()
        for k in ("ORB_V4_FENCE_TOKEN", "ORB_V4_FENCE_REQUIRED", "ORB_V4_CLOCK_JUMP_S"):
            os.environ.pop(k, None)
        # Lease exactly as YOPC left it after the 12:30 scan (epoch 30).
        self.t._commit({
            "schema": 1, "track": "v4paper", "account_pin_last4": "J7D0", "primary_host": "yopc",
            "owner": "yopc", "epoch": 30, "token": "30:yopc:yopc-cli", "instance": "yopc-cli",
            "status": "holding", "date": "2026-11-13", "slot": "1230", "kind": "scan",
            "acquired_utc": utc(pt(12, 30, 22)), "renewed_utc": utc(pt(12, 30, 22)),
            "expires_utc": utc(pt(12, 42, 22)), "reason": "primary", "completed": list(COMPLETED_MORNING),
        })

    def tearDown(self):
        self._pub.stop()
        transport_mod.set_transport_factory(None)
        for k in ("ORB_V4_FENCE_TOKEN", "ORB_V4_FENCE_REQUIRED", "ORB_V4_CLOCK_JUMP_S"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def ex(self, slot, token):
        self.calls.append(f"{token.split(':')[1]} {slot.kind} {slot.hhmm}")
        return {"rc": 0, "orders": 0}

    def runner(self, role, now, hb=None, jump=0.0, wall=None, mono=None, state=None):
        host = "yopc" if role == "primary" else "box"
        kw = {}
        if wall is not None:
            kw = {"wall": wall, "mono": mono}
        return Runner(role=role, host=host, instance=f"{host}-cli", transport=self.t,
                      state_dir=state or (self.root / host), now=now, slots=regular_grid(now.date()),
                      heartbeat=hb, account=AccountContext(), clock_jump_s=jump, **kw)

    def box_takes_lease_like_1246(self):
        """Force the real 12:46:04 box lease (epoch 31) to test recovery from it."""
        doc = dict(self.t.doc)
        doc.update({"owner": "box", "epoch": 31, "token": "31:box:box-cli", "instance": "box-cli",
                    "slot": "1245", "kind": "scan", "reason": "takeover",
                    "acquired_utc": utc(pt(12, 46, 4)), "renewed_utc": utc(pt(12, 46, 4)),
                    "expires_utc": utc(pt(13, 2, 4))})
        self.t._commit(doc)

    # ---- 1. the backup must not take over while YOPC is fresh
    def test_backup_idles_at_1246_with_fresh_yopc(self):
        d = self.runner("backup", pt(12, 46, 3), hb=yopc_hb(pt(12, 31, 23))).run(self.ex)
        self.assertEqual((d.action, d.reason), ("idle", "primary_healthy"))
        self.assertEqual(self.t.doc["owner"], "yopc")
        self.assertEqual(self.calls, [])
        # Even with the YOPC heartbeat unpublished, YOPC's own 12:30 lease write is liveness.
        d2 = self.runner("backup", pt(12, 44, 0), hb=yopc_hb(pt(11, 0, 0), slot="1100")).run(self.ex)
        self.assertEqual((d2.action, d2.reason), ("idle", "primary_healthy"))

    # ---- 2. YOPC fires the 12:45 scan at +20 s and keeps the lease through the close
    def test_yopc_keeps_lease_1245_through_1305(self):
        d = self.runner("primary", pt(12, 45, 21)).run(self.ex)
        self.assertEqual(d.action, "run")
        self.assertEqual(self.calls, ["yopc scan 1245"])
        b = self.runner("backup", pt(12, 46, 3), hb=yopc_hb(pt(12, 31, 23))).run(self.ex)
        self.assertEqual((b.action, b.reason), ("idle", "lease_held_elsewhere"))
        self.runner("primary", pt(12, 46, 2)).run(self.ex)
        self.runner("primary", pt(12, 55, 2)).run(self.ex)
        self.runner("primary", pt(13, 5, 2)).run(self.ex)
        self.assertEqual(self.calls, ["yopc scan 1245", "yopc exit 1246", "yopc eod 1255", "yopc verify 1305"])
        self.assertEqual(self.t.doc["owner"], "yopc")

    # ---- 3. today's exact failure: box holds an unexpired lease and freezes
    def test_yopc_reclaims_from_frozen_box_and_runs_close_slots(self):
        self.box_takes_lease_like_1246()
        d = self.runner("primary", pt(12, 47, 2)).run(self.ex)
        self.assertEqual((d.action, d.reason), ("run", "primary_reclaim"))
        # exit runs before the late 12:45 scan sharing the tick
        self.assertEqual(self.calls, ["yopc exit 1246", "yopc scan 1245"])
        self.assertEqual(self.t.doc["owner"], "yopc")
        self.assertEqual(self.t.doc["epoch"], 32)
        # the frozen box's old token is fenced off
        os.environ["ORB_V4_FENCE_REQUIRED"] = "1"
        os.environ["ORB_V4_FENCE_TOKEN"] = "31:box:box-cli"
        self.assertFalse(holds_fence(pt(12, 48).astimezone(timezone.utc)))
        self.runner("primary", pt(12, 55, 2)).run(self.ex)
        self.runner("primary", pt(13, 5, 2)).run(self.ex)
        self.assertEqual(self.calls[2:], ["yopc eod 1255", "yopc verify 1305"])

    def test_yopc_not_stuck_on_ambiguous_box_lease_for_close_slots(self):
        self.box_takes_lease_like_1246()
        # Box lease expires 13:02:04: YOPC at 13:02:02 is inside the 20 s skew band.
        # Nothing is due then -> idle, not fail_closed rc 2.
        d = self.runner("primary", pt(13, 2, 2)).run(self.ex)
        self.assertEqual(d.action, "idle")
        self.assertNotEqual(d.reason, "lease_expiry_ambiguous")
        # If the eod were still open in the skew band it is reclaimed.
        doc = dict(self.t.doc)
        doc["expires_utc"] = utc(pt(12, 55, 10))
        self.t._commit(doc)
        d2 = self.runner("primary", pt(12, 55, 2)).run(self.ex)
        self.assertEqual((d2.action, d2.reason), ("run", "primary_reclaim"))
        self.assertIn("yopc eod 1255", self.calls)

    def test_yopc_reclaims_expired_box_lease(self):
        self.box_takes_lease_like_1246()
        doc = dict(self.t.doc)
        doc["expires_utc"] = utc(pt(12, 50))
        self.t._commit(doc)
        d = self.runner("primary", pt(12, 55, 2)).run(self.ex)
        self.assertEqual(d.action, "run")
        self.assertIn("yopc eod 1255", self.calls)

    # ---- 4. box resumes after the freeze
    def test_box_resume_releases_lease_and_never_takes_over(self):
        self.box_takes_lease_like_1246()
        d = self.runner("backup", pt(13, 17, 21), hb=yopc_hb(pt(12, 31, 23)), jump=1860).run(self.ex)
        self.assertEqual((d.action, d.reason), ("handback", "clock_jump_release"))
        self.assertEqual(self.t.doc["status"], "released")
        self.assertEqual(self.calls, [])
        # Not holding + jump -> idle even if YOPC looks dead.
        d2 = self.runner("backup", pt(13, 18, 0), hb=yopc_hb(pt(9, 0), slot="0900"), jump=1860).run(self.ex)
        self.assertEqual((d2.action, d2.reason), ("idle", "clock_jump_idle"))
        self.assertEqual(self.calls, [])

    def test_mid_tick_clock_jump_fails_closed_before_any_slot(self):
        # The tick starts, the VM freezes 31 min: wall advances, monotonic does not.
        walls = iter([1000.0, 1000.0 + 1860])
        monos = iter([50.0, 51.0])
        r = self.runner("primary", pt(12, 46, 2), wall=lambda: next(walls), mono=lambda: next(monos))
        d = r.run(self.ex)
        self.assertEqual((d.action, d.reason), ("fail_closed", "clock_jump_mid_tick"))
        self.assertEqual(self.calls, [])

    # ---- 5. the backup still covers a truly dead primary, with the exit first
    def test_backup_covers_dead_yopc_close_slots(self):
        # YOPC died after its 12:30 scan; it never ran 12:45 or 12:46.
        hb = yopc_hb(pt(12, 31, 23))
        d = self.runner("backup", pt(12, 46, 3), hb=hb).run(self.ex)
        self.assertEqual((d.action, d.reason), ("idle", "primary_healthy"))
        # 12:48 supervisor tick: exit 2 min overdue and not completed -> cover it
        # before the 12:50 PT MOC cutoff, exit first.
        d = self.runner("backup", pt(12, 48, 3), hb=hb).run(self.ex)
        self.assertEqual((d.action, d.reason), ("run", "takeover"))
        self.assertEqual(self.calls, ["box exit 1246", "box scan 1245"])
        self.runner("backup", pt(12, 55, 5), hb=hb).run(self.ex)
        self.runner("backup", pt(13, 5, 5), hb=hb).run(self.ex)
        self.assertEqual(self.calls[2:], ["box eod 1255", "box verify 1305"])

    def test_backup_covers_exit_before_moc_cutoff_when_yopc_dies_at_close(self):
        # YOPC ran the 12:45 scan (lease renewed 12:45:21) and then died.
        doc = dict(self.t.doc)
        doc.update({"renewed_utc": utc(pt(12, 45, 21)), "acquired_utc": utc(pt(12, 45, 21)),
                    "expires_utc": utc(pt(12, 47, 30)), "slot": "1245",
                    "completed": list(COMPLETED_MORNING) + ["2026-11-13 scan 1245"]})
        self.t._commit(doc)
        hb = yopc_hb(pt(12, 46, 0), slot="1245")
        d = self.runner("backup", pt(12, 47, 0), hb=hb).run(self.ex)
        self.assertEqual(d.action, "idle")  # exit only 60 s old: give YOPC its turn
        d = self.runner("backup", pt(12, 48, 3), hb=hb).run(self.ex)
        self.assertEqual((d.action, d.reason), ("run", "takeover"))
        self.assertEqual(self.calls, ["box exit 1246"])  # before the 12:50 MOC cutoff

    def test_backup_does_not_preempt_yopc_that_renewed_for_exit(self):
        doc = dict(self.t.doc)
        doc.update({"status": "released", "renewed_utc": utc(pt(12, 46, 5)), "acquired_utc": utc(pt(12, 45, 21)),
                    "expires_utc": utc(pt(12, 46, 30)),
                    "completed": list(COMPLETED_MORNING) + ["2026-11-13 scan 1245", "2026-11-13 exit 1246"]})
        self.t._commit(doc)
        d = self.runner("backup", pt(12, 48, 3), hb=yopc_hb(pt(12, 31, 23))).run(self.ex)
        self.assertEqual(d.action, "idle")
        self.assertEqual(self.calls, [])

    def test_critical_grace_lets_primary_tick_first(self):
        doc = dict(self.t.doc)
        doc.update({"renewed_utc": utc(pt(12, 20)), "acquired_utc": utc(pt(12, 20)), "expires_utc": utc(pt(12, 32))})
        self.t._commit(doc)
        hb = yopc_hb(pt(12, 20, 50), slot="1215")
        # Stale (26 min), only 1 missed slot; exit fired 30 s ago -> wait for YOPC.
        d = self.runner("backup", pt(12, 46, 30), hb=hb).run(self.ex)
        self.assertEqual(d.action, "idle")
        d = self.runner("backup", pt(12, 47, 35), hb=hb).run(self.ex)
        self.assertEqual((d.action, d.reason), ("run", "takeover"))

    def test_morning_dead_primary_takeover_unchanged(self):
        self.t._commit({"schema": 1, "owner": "yopc", "epoch": 33, "token": "33:yopc:yopc-cli",
                        "instance": "yopc-cli", "status": "holding", "renewed_utc": utc(pt(13, 5) - timedelta(days=3)),
                        "acquired_utc": utc(pt(13, 5) - timedelta(days=3)),
                        "expires_utc": utc(pt(13, 15) - timedelta(days=3)), "completed": []})
        hb = yopc_hb(pt(13, 5) - timedelta(days=3), slot="1305", kind="verify")
        d = self.runner("backup", pt(6, 16, 0), hb=hb).run(self.ex)
        self.assertEqual(d.action, "idle")  # only 06:00 past grace -> 1 missed
        d = self.runner("backup", pt(6, 23, 30), hb=hb).run(self.ex)
        self.assertEqual((d.action, d.reason), ("run", "takeover"))

    # ---- helpers
    def test_run_order_close_first(self):
        s = [Slot(pt(12, 45), "scan"), Slot(pt(12, 46), "exit")]
        self.assertEqual([x.kind for x in run_order(s)], ["exit", "scan"])

    def test_clock_jump_probe(self):
        state = self.root / "probe"
        self.assertEqual(_clock_jump_s(state, "box"), 0.0)
        import json, time
        p = state / "clock_probe_box.json"
        p.write_text(json.dumps({"offset": (time.time() - time.monotonic()) - 1860}))
        self.assertGreater(_clock_jump_s(state, "box"), 1800)
        self.assertLess(_clock_jump_s(state, "box"), 5)
        os.environ["ORB_V4_CLOCK_JUMP_S"] = "1867"
        self.assertEqual(_clock_jump_s(state, "box", probe=False), 1867.0)

    def test_wrapper_waits_for_1245_fire(self):
        ps1 = (ROOT / "deploy" / "orb-v4-role.ps1").read_text(encoding="utf-8")
        self.assertIn("$n.Minute -le 45", ps1)
        sh = (ROOT / "paper_broker" / "scripts" / "orb_v4_backup_timer.sh").read_text(encoding="utf-8")
        self.assertIn("ORB_V4_CLOCK_JUMP_S", sh)
        self.assertIn('"12:48:00"', sh)


if __name__ == "__main__":
    unittest.main(verbosity=1)
