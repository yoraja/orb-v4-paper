"""Direct #grok-trades alerts for ORB v4 PAPER jobs (YOPC primary or box backup).

Disabled by default: daily/docs/ops_orb_v4_paper.json slack_alerts_enabled=false.
This module never posts unless that flag is true AND a v4-owned webhook is set
in ORB_V4_SLACK_WEBHOOK_URL. It never reads LIVE ORB v3 Slack credentials,
never prints or logs the webhook value, and never posts heartbeats or routine fills.

Events (deduped once per event[+key] per PT day):
  halt, auth, failed_flatten, failed_verify, failover, unfilled_exit
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from paper_broker import v4_paths as P

PT = ZoneInfo("America/Los_Angeles")
PREFIX = P.SLACK_PREFIX
CHANNEL = P.SLACK_CHANNEL
WEBHOOK_ENV = "ORB_V4_SLACK_WEBHOOK_URL"
OPS_NAME = "ops_orb_v4_paper.json"
ALLOWED = frozenset({
    "halt",
    "auth",
    "failed_flatten",
    "failed_verify",
    "failover",
    "unfilled_exit",
})
# Keep names imported by call sites without repeating strings there.
HALT = "halt"
AUTH = "auth"
FAILED_FLATTEN = "failed_flatten"
FAILED_VERIFY = "failed_verify"
FAILOVER = "failover"
UNFILLED_EXIT = "unfilled_exit"

def _ops_path() -> Path:
    override = (os.environ.get("ORB_V4_OPS_FILE") or "").strip()
    if override:
        return Path(override)
    return P.HOME / "daily" / "docs" / OPS_NAME


def load_ops() -> dict[str, Any]:
    path = _ops_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def alerts_enabled() -> bool:
    ops = load_ops()
    val = ops.get("slack_alerts_enabled", False)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)


def _webhook_url() -> str:
    """Return the v4 webhook or ''. Never log or return a LIVE/shared name's value."""
    return (os.environ.get(WEBHOOK_ENV) or "").strip()


def _today() -> str:
    return datetime.now(PT).date().isoformat()


def _dedupe_path() -> Path:
    return P.STATE / "slack_alert_dedupe.json"


def _load_dedupe() -> dict[str, Any]:
    path = _dedupe_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dedupe(data: dict[str, Any]) -> None:
    path = _dedupe_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def dedupe_key(event: str, key: str | None, day: str | None = None) -> str:
    day = day or _today()
    extra = f":{key}" if key else ""
    return f"{day}:{event}{extra}"


def _already_sent(event: str, key: str | None) -> bool:
    data = _load_dedupe()
    return dedupe_key(event, key) in data


def _mark_sent(event: str, key: str | None, result: str) -> None:
    data = _load_dedupe()
    data[dedupe_key(event, key)] = {"result": result, "pt": datetime.now(PT).isoformat()}
    _save_dedupe(data)


def _prefix(text: str) -> str:
    t = (text or "").strip()
    if t.startswith(PREFIX):
        return t
    return f"{PREFIX} {t}".strip()


def _post(url: str, text: str) -> str:
    # The URL is never written to logs or exception messages we return.
    payload = json.dumps({"text": text, "channel": CHANNEL}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if int(code) >= 300:
                return f"http_{code}"
            return "ok"
    except urllib.error.HTTPError as e:
        return f"http_{e.code}"
    except Exception as e:
        return f"error_{type(e).__name__}"


def maybe_alert(
    event: str,
    text: str,
    *,
    key: str | None = None,
    dry_run: bool = False,
    opener=None,
) -> dict[str, Any]:
    """Attempt a direct Slack post. Safe to call from trading paths: never raises.

    opener, if provided, is a urllib-style opener used only by tests (Slack is mocked).
    When slack_alerts_enabled is false, this returns without any network call.
    """
    out: dict[str, Any] = {
        "event": event,
        "posted": False,
        "reason": "",
        "deduped": False,
        "enabled": False,
    }
    try:
        if event not in ALLOWED:
            out["reason"] = "unknown_event"
            return out
        if dry_run:
            out["reason"] = "dry_run"
            return out
        if not alerts_enabled():
            out["reason"] = "disabled"
            return out
        out["enabled"] = True
        if _already_sent(event, key):
            out["reason"] = "deduped"
            out["deduped"] = True
            return out
        url = _webhook_url()
        if not url:
            out["reason"] = "missing_webhook"
            return out
        body = _prefix(text)
        if opener is not None:
            payload = json.dumps({"text": body, "channel": CHANNEL}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            opener(req, timeout=8)
            status = "ok"
        else:
            status = _post(url, body)
        if status == "ok":
            _mark_sent(event, key, status)
            out["posted"] = True
            out["reason"] = "ok"
        else:
            out["reason"] = status
        return out
    except Exception:
        out["reason"] = "swallowed"
        return out
