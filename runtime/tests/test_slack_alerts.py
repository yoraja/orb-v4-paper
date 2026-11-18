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


# Isolate state and ops from the live paper runtime. Point the alerts file
# at a temp path so a real host file is never read during tests.
td = Path(tempfile.mkdtemp(prefix="v4-slack-test-"))
run = td / "run"
run.mkdir()
os.environ["ORB_V4_HOME"] = str(ROOT)
os.environ["ORB_V4_RUN"] = str(run)
os.environ.pop("ORB_V4_SLACK_WEBHOOK_URL", None)
os.environ.pop("ORB_V4_OPS_FILE", None)
# Non-existent default for this process unless a test sets a real temp file.
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(td / "missing-slack-alerts.json")

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
# Keep the alerts-file override so tests never touch the host path.
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(td / "missing-slack-alerts.json")
importlib.reload(sa)
check(sa.alerts_enabled() is False, "repo ops flag slack_alerts_enabled defaults to false")
check(sa.ALERTS_FILE_ENV == "ORB_V4_SLACK_ALERTS_FILE", "alerts file env name")
check(sa.DEFAULT_ALERTS_FILE == Path(r"C:\Users\yogjr\.grok-alerts\slack-alerts.json"),
      "default alerts file is the shared YOPC path")

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


def no_url_leaked(result, *extra):
    blob = json.dumps(result, default=str) + " " + " ".join(str(x) for x in extra)
    return "hooks.example.test" not in blob and "webhook_url" not in str(result.get("reason") or "")


# Disabled: even with a file and env webhook present, no network call.
good_file = td / "slack-alerts.json"
good_file.write_text(json.dumps({
    "channel": "C0C0HCTTJBU",
    "webhook_url": "https://hooks.example.test/mock-file",
}), encoding="utf-8")
os.environ["ORB_V4_OPS_FILE"] = str(ops_disabled)
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(good_file)
os.environ["ORB_V4_SLACK_WEBHOOK_URL"] = "https://hooks.example.test/mock-disabled"
importlib.reload(sa)
calls.clear()
r = sa.maybe_alert("halt", "daily -2R halt", key="halt", opener=opener_factory(calls))
check(r["posted"] is False and r["reason"] == "disabled", f"disabled hook does not post: {r}")
check(calls == [], "disabled hook does not call opener")
check(no_url_leaked(r), "disabled result does not include the webhook")

# Enabled, no file, no env: missing webhook.
os.environ["ORB_V4_OPS_FILE"] = str(ops_enabled)
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(td / "missing-slack-alerts.json")
os.environ.pop("ORB_V4_SLACK_WEBHOOK_URL", None)
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
importlib.reload(sa)
r = sa.maybe_alert("auth", "bad keys", opener=opener_factory(calls))
check(r["posted"] is False and r["reason"] == "missing_webhook", f"enabled without webhook: {r}")
check(calls == [], "missing webhook does not call opener")

# Enabled + shared file (correct channel): posts from the file, env ignored.
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(good_file)
os.environ["ORB_V4_SLACK_WEBHOOK_URL"] = "https://hooks.example.test/mock-env-should-not-win"
importlib.reload(sa)
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
calls.clear()
r = sa.maybe_alert("failed_verify", "after-close check: MSFT due still open", key="verify", opener=opener_factory(calls))
check(r["posted"] is True and r["reason"] == "ok", f"file webhook posts: {r}")
check(len(calls) == 1, f"one opener call, got {len(calls)}")
if calls:
    check(calls[0]["url"] == "https://hooks.example.test/mock-file", "opener used the file webhook, not env")
    payload = json.loads(calls[0]["body"])
    check(payload["text"].startswith("ORB v4 PAPER"), f"prefix applied: {payload['text']!r}")
    check(payload["channel"] == "C0C0HCTTJBU", "channel is #grok-trades id")
check(no_url_leaked(r), "success result does not include the webhook")
r2 = sa.maybe_alert("failed_verify", "after-close check: MSFT due still open", key="verify", opener=opener_factory(calls))
check(r2["deduped"] is True and r2["posted"] is False, f"same event same day is deduped: {r2}")
check(len(calls) == 1, "dedupe does not call opener again")

# A different event the same day still posts.
r3 = sa.maybe_alert("failover", "box backup takeover", key="takeover", opener=opener_factory(calls))
check(r3["posted"] is True, f"different event posts: {r3}")
check(len(calls) == 2, "second event calls opener")

# Wrong channel in the file: refuse, and do not fall back to env.
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
wrong = td / "wrong-channel.json"
wrong.write_text(json.dumps({
    "channel": "C0000000000",
    "webhook_url": "https://hooks.example.test/mock-wrong-channel",
}), encoding="utf-8")
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(wrong)
os.environ["ORB_V4_SLACK_WEBHOOK_URL"] = "https://hooks.example.test/mock-env-fallback"
importlib.reload(sa)
n = len(calls)
r_wrong = sa.maybe_alert("halt", "daily -2R halt", key="halt-wrong", opener=opener_factory(calls))
check(r_wrong["posted"] is False and r_wrong["reason"] == "wrong_channel",
      f"wrong file channel is refused: {r_wrong}")
check(len(calls) == n, "wrong channel does not call opener or fall back to env")
check(no_url_leaked(r_wrong), "wrong-channel result does not include the webhook")

# Unreadable / invalid file: refuse, no env fallback.
bad = td / "bad.json"
bad.write_text("not-json", encoding="utf-8")
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(bad)
importlib.reload(sa)
r_bad = sa.maybe_alert("auth", "bad keys", key="auth-bad", opener=opener_factory(calls))
check(r_bad["posted"] is False and r_bad["reason"] == "alerts_file_unreadable",
      f"unreadable file refused: {r_bad}")
check(len(calls) == n, "unreadable file does not call opener")

# Missing file, env fallback works (box-side path).
os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(td / "missing-slack-alerts.json")
os.environ["ORB_V4_SLACK_WEBHOOK_URL"] = "https://hooks.example.test/mock-enabled"
importlib.reload(sa)
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
calls.clear()
r_env = sa.maybe_alert("failed_verify", "after-close check: MSFT due still open", key="verify-env", opener=opener_factory(calls))
check(r_env["posted"] is True and r_env["reason"] == "ok", f"env fallback posts: {r_env}")
check(len(calls) == 1 and calls[0]["url"] == "https://hooks.example.test/mock-enabled",
      "opener used the env webhook when the file is absent")

# dry_run never posts even when enabled
r4 = sa.maybe_alert("halt", "halt", key="halt-dry", dry_run=True, opener=opener_factory(calls))
check(r4["reason"] == "dry_run" and r4["posted"] is False, f"dry_run: {r4}")
check(len(calls) == 1, "dry_run does not call opener")

# unknown event rejected
r5 = sa.maybe_alert("heartbeat", "still alive", opener=opener_factory(calls))
check(r5["reason"] == "unknown_event" and r5["posted"] is False, "heartbeats are rejected")
check(len(calls) == 1, "heartbeat does not call opener")

# Hook must not consult LIVE paths
src = Path(sa.__file__).read_text(encoding="utf-8")
check("ORB_V3" not in src and ".orb-v3" not in src, "hook source does not reference LIVE v3 paths")
check(sa.WEBHOOK_ENV == "ORB_V4_SLACK_WEBHOOK_URL", "webhook env is v4-owned")

# maybe_alert never raises if opener blows up
def bad_opener(req, timeout=8):
    raise RuntimeError("network down")

os.environ["ORB_V4_SLACK_ALERTS_FILE"] = str(good_file)
os.environ.pop("ORB_V4_SLACK_WEBHOOK_URL", None)
importlib.reload(sa)
if sa._dedupe_path().exists():
    sa._dedupe_path().unlink()
r6 = sa.maybe_alert("failed_flatten", "flatten FAILED CSCO", key="CSCO", opener=bad_opener)
check(r6["posted"] is False and r6["reason"] in {"error_RuntimeError", "swallowed"}, f"opener error swallowed: {r6}")
check(no_url_leaked(r6), "error result does not include the webhook")

print("ALL SLACK ALERT HOOK CHECKS PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
