"""Read the public PAPER heartbeat. GET only. No credentials. No writes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.request import Request, urlopen

URLS = (
    "https://yoraja.github.io/orb-v4-paper/heartbeat.json",
    "https://raw.githubusercontent.com/yoraja/orb-v4-paper/main/heartbeat.json",
)


def fetch_heartbeat(opener=None):
    from paper_broker.failover.runner import Heartbeat
    open_fn = opener or urlopen
    errors = []
    best = None
    for url in URLS:
        try:
            req = Request(url, headers={"Cache-Control": "no-cache", "Accept": "application/json"})
            with open_fn(req, timeout=20) as resp:
                doc = json.loads(resp.read().decode())
            if doc.get("track") != "v4paper":
                raise ValueError(f"unexpected track {doc.get('track')!r}")
            ts = datetime.fromisoformat(str(doc["ts_utc"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            hb = Heartbeat(
                host=doc.get("host"),
                ts_utc=ts,
                kind=doc.get("kind"),
                slot=str(doc.get("slot") or ""),
                rc=doc.get("rc"),
                ok=doc.get("ok"),
                trading=doc.get("trading"),
                date=doc.get("date"),
            )
            if best is None or (hb.ts_utc and best.ts_utc and hb.ts_utc > best.ts_utc):
                best = hb
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}")
    if best is None:
        return Heartbeat(error=" | ".join(errors) or "heartbeat unreadable")
    return best
