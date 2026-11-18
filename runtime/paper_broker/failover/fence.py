"""Fencing-token check performed again immediately before any broker submit."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from paper_broker.failover.transport import TransportError, make_transport_from_env


def holds_fence(now: datetime | None = None) -> bool:
    token = (os.environ.get("ORB_V4_FENCE_TOKEN") or "").strip()
    if not token:
        return False
    try:
        current = make_transport_from_env().read()
    except TransportError:
        return False
    doc = current.doc or {}
    if doc.get("token") != token or doc.get("status") != "holding":
        return False
    try:
        exp = datetime.fromisoformat(str(doc.get("expires_utc")))
    except Exception:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now <= exp


def assert_fence() -> None:
    """Fail closed. A lost, expired, or unreadable token must not submit."""
    if os.environ.get("ORB_V4_FENCE_REQUIRED") != "1":
        return
    if not holds_fence():
        raise SystemExit("FAIL CLOSED: fencing token is not the confirmed owner")


def before_submit() -> None:
    """Hook the existing PAPER engine calls immediately before submit_order."""
    assert_fence()
