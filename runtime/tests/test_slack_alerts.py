"""Slack alert hook for ORB v4 PAPER — Slack is always mocked. Never posts.
Run: python tests/test_slack_alerts.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fails = 0


def check(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m)
    fails += 0 if c else 1


# Isolate state and ops from the live paper runtime.
td = Path(tempfile.mkdtemp(prefix="v4-slack-test-"))
run = td / "run"
run.mkdir()
os.environ["ORB_V4_HOME"] = str(ROOT)
os.environ["ORB_V4_RUN"] = str(run)
os.environ.pop("ORB_V4_SLACK_WEBHOOK_URL", None)
# Make sure a leftover enabled flag cannot leak in from the developer env.
os.environ.pop("ORB_V4_OPS_FILE", None)

ops_disabled = td / "ops-disabled.json"
ops_disabled.write_text(json.dumps({
    "slack_alerts_enabled": False,
    "slack_channel": "C0C0HCTTJBU",
    "slack_prefix": "ORB v4 PAPER",
    "webhook_env": "ORB_V4_SLACK_WEBHOOK_URL",
}), encoding="utf-8")
ops_enabled = td / "ops-enabled.json"
ops_enabled.write_text(json.dumps({
    "slack_alerts_enabled": True,
    "slack_channel": "C0C0HCTTJBU",
    "slack_prefix": "ORB v4 PAPER",
    "webhook_env": "ORB_V4_SLACK_WEBHOOK_URL",
}), encoding="utf-8")

# v4_paths creates dirs from ORB_V4_RUN at import time — import after env set.
import importlib
import paper_broker.v4_paths as v4_paths
importlib.reload(v4_paths)
import paper_broker.slack_alerts as sa
importlib.reload(sa)

# Default repo ops file is disabled.
os.environ.pop("ORB_V4_OPS_FILE", None)
importlib.reload(sa)
check(sa.alerts_enabled() is False, "repo ops flag slack_alerts_enabled defaults to false")

calls = []


def opener_factory(store):
    class Resp:
        status = 200

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def opener(req, timeout=8):
        body = req.data.decode() if isinstance(req.data, (bytes, bytearray)) else ""
        store.append({"url": req.full_url, "body": body, "timeout": timeout, "method": req.get_method()})
        return Resp()

    return opener


# Disabled: even with a webhook present, no network call.
os.environ["ORB_V4_OPS_FILE"] = str(ops_disabled)
os.environ["ORB_V4_SLACK_WEBHOOK_URL"] = "https://hooks.example.test/mock-disabled"
importlib.reload(sa)
calls.clear()
r = sa.maybe_alert("halt", "daily -2R halt", key="halt", opener=opener_factory(calls))
check(r["posted"] is False and r["reason"] == "disabled", f"disabled hook does not post: {r}")
check(calls == [], "disabled hook does not call opener")

# Enabled but missing webhook: no post, no exception.
os.environ["ORB_V4_OPS_FILE"] = str(ops_enabled)
os.environ.pop("ORB_V4_SLACK_WEBHOOK_URL", None)
# fresh dedupe state
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
importlib.reload(sa)
r = sa.maybe_alert("auth", "bad keys", opener=opener_factory(calls))
check(r["posted"] is False and r["reason"] == "missing_webhook", f"enabled without webhook: {r}")
check(calls == [], "missing webhook does not call opener")

# Enabled + webhook + mock: posts once, prefix applied, then deduped.
os.environ["ORB_V4_SLACK_WEBHOOK_URL"] = "https://hooks.example.test/mock-enabled"
importlib.reload(sa)
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
calls.clear()
r = sa.maybe_alert("failed_verify", "after-close check: MSFT due still open", key="verify", opener=opener_factory(calls))
check(r["posted"] is True and r["reason"] == "ok", f"enabled mock post: {r}")
check(len(calls) == 1, f"one opener call, got {len(calls)}")
if calls:
    check(calls[0]["url"] == "https://hooks.example.test/mock-enabled", "mock opener saw the test URL only")
    payload = json.loads(calls[0]["body"])
    check(payload["text"].startswith("ORB v4 PAPER"), f"prefix applied: {payload['text']!r}")
    check(payload["channel"] == "C0C0HCTTJBU", "channel is #grok-trades id")
r2 = sa.maybe_alert("failed_verify", "after-close check: MSFT due still open", key="verify", opener=opener_factory(calls))
check(r2["deduped"] is True and r2["posted"] is False, f"same event same day is deduped: {r2}")
check(len(calls) == 1, "dedupe does not call opener again")

# A different event the same day still posts.
r3 = sa.maybe_alert("failover", "box backup takeover", key="takeover", opener=opener_factory(calls))
check(r3["posted"] is True, f"different event posts: {r3}")
check(len(calls) == 2, "second event calls opener")

# dry_run never posts even when enabled
r4 = sa.maybe_alert("halt", "halt", key="halt-dry", dry_run=True, opener=opener_factory(calls))
check(r4["reason"] == "dry_run" and r4["posted"] is False, f"dry_run: {r4}")
check(len(calls) == 2, "dry_run does not call opener")

# unknown event rejected
r5 = sa.maybe_alert("heartbeat", "still alive", opener=opener_factory(calls))
check(r5["reason"] == "unknown_event" and r5["posted"] is False, "heartbeats are rejected")
check(len(calls) == 2, "heartbeat does not call opener")

# Hook must not consult non-v4 env names
check(sa.WEBHOOK_ENV == "ORB_V4_SLACK_WEBHOOK_URL", "webhook env is v4-owned")
src = Path(sa.__file__).read_text(encoding="utf-8")
check("ORB_V3" not in src and ".orb-v3" not in src, "hook source does not reference LIVE v3 paths")

# maybe_alert never raises if opener blows up
def bad_opener(req, timeout=8):
    raise RuntimeError("network down")

r6 = sa.maybe_alert("failed_flatten", "flatten FAILED CSCO", key="CSCO", opener=bad_opener)
check(r6["posted"] is False and r6["reason"] in {"error_RuntimeError", "swallowed"}, f"opener error swallowed: {r6}")

print("ALL SLACK ALERT HOOK CHECKS PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
