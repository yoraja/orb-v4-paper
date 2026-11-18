"""Who may push the public heartbeat.

Only a confirmed lease owner may publish a trading heartbeat. A backup must
not overwrite a newer YOPC liveness heartbeat: that heartbeat is how YOPC
asks for handback without trading.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def should_publish(local: dict, remote: dict | None, *, takeover_since_utc: datetime | None = None) -> bool:
    if not local:
        return False
    if remote is None:
        return True
    remote_ts = _ts(remote.get("ts_utc"))
    local_ts = _ts(local.get("ts_utc"))
    if local.get("host") != "yopc" and remote.get("host") == "yopc" and remote_ts is not None:
        since = _ts(takeover_since_utc)
        if since is not None and remote_ts > since:
            return False
        if local_ts is not None and remote_ts > local_ts:
            return False
    return True
