"""ORB v4 PAPER failover package.

This namespace is the YOPC-primary / box-backup overlay. It does not replace
the dashboard site and it does not contain broker credentials.
"""
from __future__ import annotations

try:
    from paper_broker.failover.http_timeouts import install_runtime_timeouts
    install_runtime_timeouts()
except Exception:
    pass
