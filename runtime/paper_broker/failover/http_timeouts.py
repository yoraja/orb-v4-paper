"""Connect/read timeouts and idempotent submit recovery for the v4 runtime.

Reliability only: no strategy, risk, sizing, or order-type changes.

Every Alpaca REST call and every bar-data fetch should use about 10s connect
and 20–30s read, with bounded retries. Order POSTs are never retried. If a
submit times out or drops after the request may have reached the broker,
look up the order by client_order_id and do not submit again.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

CONNECT_S = 10.0
READ_S = 25.0
TIMEOUT = (CONNECT_S, READ_S)
# Bounded retries for connect / "other" failures only. read=0 and status=0
# so a request that may have reached Alpaca is never resent.
RETRY_TOTAL = 3
RETRY_CONNECT = 3
RETRY_BACKOFF = 0.5

_INSTALLED = False
_log = logging.getLogger("orb_v4.http_timeouts")


def _is_timeout_or_disconnect(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    tokens = (
        "timeout",
        "timed out",
        "time-out",
        "disconnected",
        "connection aborted",
        "connection reset",
        "remotely closed",
        "remote end closed",
        "readtimeout",
        "connecttimeout",
        "endpointconnection",
        "protocolerror",
        "broken pipe",
    )
    if any(t in name for t in ("timeout", "disconnect", "aborted", "reset")):
        return True
    return any(t in text for t in tokens)


def lookup_order_by_client_id(client: Any, client_order_id: str) -> Any | None:
    """Return the broker order for this client_order_id, or None. Never submits."""
    if not client_order_id or client is None:
        return None
    getter = getattr(client, "get_order_by_client_id", None)
    if callable(getter):
        try:
            return getter(client_order_id)
        except Exception as exc:
            _log.warning("lookup by client_order_id %s failed: %s", client_order_id, type(exc).__name__)
            return None
    return None


def submit_order_idempotent(
    client: Any,
    req: Any,
    *,
    submit_fn: Callable[..., Any] | None = None,
) -> Any:
    """Submit once. On timeout/disconnect after the POST may have been sent,
    look up by client_order_id and return that order. Never POST again."""
    submit = submit_fn or client.submit_order
    coid = getattr(req, "client_order_id", None) or (req.get("client_order_id") if isinstance(req, dict) else None)
    try:
        return submit(req)
    except Exception as exc:
        if not _is_timeout_or_disconnect(exc):
            raise
        found = lookup_order_by_client_id(client, str(coid or ""))
        if found is not None:
            _log.warning(
                "submit timed out or dropped; recovered order via client_order_id=%s (no resubmit)",
                coid,
            )
            return found
        _log.warning(
            "submit timed out or dropped; no order found for client_order_id=%s (not resubmitting)",
            coid,
        )
        raise


def _patch_requests_timeout() -> None:
    try:
        import requests.sessions as sessions
    except Exception:
        return
    if getattr(sessions.Session.request, "_orb_v4_timeout", False):
        return
    original = sessions.Session.request

    def request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("timeout", TIMEOUT)
        return original(self, method, url, **kwargs)

    request._orb_v4_timeout = True  # type: ignore[attr-defined]
    sessions.Session.request = request  # type: ignore[assignment]


def _patch_urllib_timeout() -> None:
    try:
        import urllib.request as ureq
    except Exception:
        return
    if getattr(ureq.urlopen, "_orb_v4_timeout", False):
        return
    original = ureq.urlopen
    _missing = object()

    def urlopen(url, data=None, timeout=_missing, *args, **kwargs):  # type: ignore[no-untyped-def]
        if timeout is _missing:
            timeout = READ_S
        return original(url, data, timeout, *args, **kwargs)

    urlopen._orb_v4_timeout = True  # type: ignore[attr-defined]
    ureq.urlopen = urlopen  # type: ignore[assignment]


def _patch_yfinance_timeout() -> None:
    try:
        import yfinance as yf
    except Exception:
        return
    download = getattr(yf, "download", None)
    if download is None or getattr(download, "_orb_v4_timeout", False):
        return

    def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("timeout", READ_S)
        return download(*args, **kwargs)

    wrapped._orb_v4_timeout = True  # type: ignore[attr-defined]
    yf.download = wrapped


def _patch_trading_client_submit() -> None:
    try:
        from alpaca.trading.client import TradingClient
    except Exception:
        return
    original = getattr(TradingClient, "submit_order", None)
    if original is None or getattr(original, "_orb_v4_idempotent", False):
        return

    def submit_order(self, req, *args, **kwargs):  # type: ignore[no-untyped-def]
        return submit_order_idempotent(
            self,
            req,
            submit_fn=lambda request: original(self, request, *args, **kwargs),
        )

    submit_order._orb_v4_idempotent = True  # type: ignore[attr-defined]
    TradingClient.submit_order = submit_order  # type: ignore[assignment]


def _harden_open_sessions() -> None:
    """Best-effort: attach urllib3 connect retries (never retry POSTs that may have landed)."""
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except Exception:
        return

    def retry() -> "Retry":
        return Retry(
            total=RETRY_TOTAL,
            connect=RETRY_CONNECT,
            read=0,
            status=0,
            other=RETRY_CONNECT,
            redirect=0,
            backoff_factor=RETRY_BACKOFF,
            allowed_methods=None,
            raise_on_status=False,
        )

    try:
        from paper_broker import paper_trader as pt
    except Exception:
        pt = None
    if pt is None or getattr(pt, "_harden_session", None) is None:
        return
    if getattr(pt._harden_session, "_orb_v4_timeout", False):
        return
    original = pt._harden_session

    def _harden_session(client):  # type: ignore[no-untyped-def]
        client = original(client)
        try:
            sess = getattr(client, "_session", None)
            if sess is not None:
                sess.mount("https://", HTTPAdapter(max_retries=retry()))
        except Exception:
            pass
        return client

    _harden_session._orb_v4_timeout = True  # type: ignore[attr-defined]
    pt._harden_session = _harden_session


def install_runtime_timeouts() -> None:
    """Idempotent. Safe to call from any v4 process; no network, no secrets."""
    global _INSTALLED
    if _INSTALLED:
        return
    _patch_requests_timeout()
    _patch_urllib_timeout()
    _patch_yfinance_timeout()
    _patch_trading_client_submit()
    _harden_open_sessions()
    _INSTALLED = True


# Backwards-friendly alias
install = install_runtime_timeouts
