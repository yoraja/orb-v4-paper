"""Reliability: HTTP timeouts, idempotent submit lookup, and tick wall-clock budget.

No broker, no network, no orders.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_broker.failover.http_timeouts import (
    CONNECT_S,
    READ_S,
    TIMEOUT,
    install_runtime_timeouts,
    lookup_order_by_client_id,
    submit_order_idempotent,
)
from paper_broker.failover.tick_budget import (
    PREEMPT_BUFFER_S,
    TICK_WALL_S,
    TIMEOUT_RC,
    release_stale_track_lock,
    run_process_group,
    tick_deadline,
    tick_timeout_seconds,
)

PT = ZoneInfo("America/Los_Angeles")


def pt(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=PT)


class FakeReq:
    def __init__(self, coid="orbv4p-20261112-0745-CSCO"):
        self.client_order_id = coid


class FakeClient:
    def __init__(self, *, submit_exc=None, lookup=None, lookup_exc=None):
        self.submits = 0
        self.lookups: list[str] = []
        self._submit_exc = submit_exc
        self._lookup = lookup
        self._lookup_exc = lookup_exc

    def submit_order(self, req):
        self.submits += 1
        if self._submit_exc:
            raise self._submit_exc
        return {"id": "new", "client_order_id": getattr(req, "client_order_id", None)}

    def get_order_by_client_id(self, coid):
        self.lookups.append(coid)
        if self._lookup_exc:
            raise self._lookup_exc
        return self._lookup


class TickBudgetTests(unittest.TestCase):
    def test_scan_just_before_exit_is_preempted(self):
        now = pt(2026, 11, 12, 12, 45, 20)
        deadline = tick_deadline(now, kind="scan")
        self.assertEqual(deadline, pt(2026, 11, 12, 12, 46) - __import__("datetime").timedelta(seconds=PREEMPT_BUFFER_S))
        self.assertLess(deadline, pt(2026, 11, 12, 12, 46))
        self.assertLess(tick_timeout_seconds(now, kind="scan"), 60)

    def test_exit_gets_full_wall_clock(self):
        now = pt(2026, 11, 12, 12, 46, 0)
        deadline = tick_deadline(now, kind="exit")
        self.assertEqual(deadline, now + __import__("datetime").timedelta(seconds=TICK_WALL_S))
        self.assertEqual(tick_timeout_seconds(now, kind="exit"), TICK_WALL_S)

    def test_eod_and_verify_also_get_full_wall_clock(self):
        for kind, when in (("eod", pt(2026, 11, 12, 12, 55)), ("verify", pt(2026, 11, 12, 13, 5))):
            self.assertEqual(
                tick_timeout_seconds(when, kind=kind),
                TICK_WALL_S,
            )

    def test_mid_morning_scan_uses_eight_minute_cap(self):
        now = pt(2026, 11, 12, 10, 0, 0)
        self.assertEqual(tick_timeout_seconds(now, kind="scan"), TICK_WALL_S)

    def test_inside_preempt_buffer_dies_before_close(self):
        now = pt(2026, 11, 12, 12, 45, 50)
        deadline = tick_deadline(now, kind="scan")
        self.assertLess(deadline, pt(2026, 11, 12, 12, 46, 0))
        self.assertGreaterEqual(deadline, now)
        self.assertLessEqual(tick_timeout_seconds(now, kind="scan"), 10)

    def test_hung_process_times_out_and_next_command_runs(self):
        hung = [sys.executable, "-c", "import time; time.sleep(30)"]
        t0 = time.monotonic()
        rc_hang = run_process_group(hung, timeout_s=1)
        elapsed = time.monotonic() - t0
        self.assertEqual(rc_hang, TIMEOUT_RC)
        self.assertLess(elapsed, 8)
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "next_slot.txt"
            ok = [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path(r'{marker}').write_text('ran')",
            ]
            rc_next = run_process_group(ok, timeout_s=5)
            self.assertEqual(rc_next, 0)
            self.assertEqual(marker.read_text(), "ran")

    def test_release_lock_when_not_held(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "orb_v4_paper.lock"
            path.write_text("stale pid 1\n", encoding="utf-8")
            status = release_stale_track_lock(path)
            self.assertEqual(status, "released")
            self.assertEqual(path.read_text(encoding="utf-8"), "")


class SubmitIdempotencyTests(unittest.TestCase):
    def test_timeout_recovers_by_client_order_id_without_resubmit(self):
        existing = {"id": "ord-1", "client_order_id": "orbv4p-20261112-0745-CSCO", "status": "accepted"}
        client = FakeClient(submit_exc=TimeoutError("read timed out"), lookup=existing)
        got = submit_order_idempotent(client, FakeReq())
        self.assertIs(got, existing)
        self.assertEqual(client.submits, 1)
        self.assertEqual(client.lookups, ["orbv4p-20261112-0745-CSCO"])

    def test_disconnect_lookup_miss_does_not_resubmit(self):
        client = FakeClient(
            submit_exc=ConnectionError("Remote end closed connection without response"),
            lookup=None,
        )
        with self.assertRaises(ConnectionError):
            submit_order_idempotent(client, FakeReq())
        self.assertEqual(client.submits, 1)
        self.assertEqual(client.lookups, ["orbv4p-20261112-0745-CSCO"])

    def test_lookup_failure_does_not_resubmit(self):
        client = FakeClient(
            submit_exc=TimeoutError("timed out"),
            lookup_exc=TimeoutError("lookup timed out"),
        )
        with self.assertRaises(TimeoutError):
            submit_order_idempotent(client, FakeReq())
        self.assertEqual(client.submits, 1)
        self.assertEqual(len(client.lookups), 1)

    def test_non_timeout_error_is_not_looked_up(self):
        client = FakeClient(submit_exc=ValueError("bad request"))
        with self.assertRaises(ValueError):
            submit_order_idempotent(client, FakeReq())
        self.assertEqual(client.submits, 1)
        self.assertEqual(client.lookups, [])

    def test_successful_submit_does_not_lookup(self):
        client = FakeClient()
        got = submit_order_idempotent(client, FakeReq())
        self.assertEqual(got["id"], "new")
        self.assertEqual(client.submits, 1)
        self.assertEqual(client.lookups, [])

    def test_lookup_helper_swallows_getter_errors(self):
        client = FakeClient(lookup_exc=RuntimeError("broker down"))
        self.assertIsNone(lookup_order_by_client_id(client, "orbv4p-x"))
        self.assertIsNone(lookup_order_by_client_id(client, ""))


class InstallTimeoutTests(unittest.TestCase):
    def test_install_is_idempotent_and_sets_requests_default_timeout(self):
        install_runtime_timeouts()
        install_runtime_timeouts()
        import requests
        self.assertTrue(getattr(requests.sessions.Session.request, "_orb_v4_timeout", False))
        self.assertEqual(TIMEOUT, (CONNECT_S, READ_S))
        self.assertEqual(CONNECT_S, 10.0)
        self.assertEqual(READ_S, 25.0)
        captured = {}

        def fake_send(request, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise RuntimeError("stop-before-network")

        sess = requests.Session()
        sess.send = fake_send  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            sess.get("http://127.0.0.1:1")
        self.assertEqual(captured.get("timeout"), TIMEOUT)



class TimeoutHookTests(unittest.TestCase):
    def test_timeout_install_hook_is_idempotent(self):
        from deploy.apply_hooks import apply_timeout_install
        src = "import paper_broker.run_orb_v3_paper as eng\n\nX = 1\n"
        once = apply_timeout_install(src)
        self.assertIn("install_runtime_timeouts()", once)
        self.assertEqual(once.count("install_runtime_timeouts()"), 1)
        twice = apply_timeout_install(once)
        self.assertEqual(twice, once)

if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(verbosity=2) is None else 0)
