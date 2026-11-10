"""Close and entry decisions that keep a recovery order from reversing a position.

Normal exits use ``orbv4p-x-<YYYYMMDD>-<SYM>`` (MOC) or
``orbv4p-xmkt-<YYYYMMDD>-<SYM>`` (market fallback). The one-shot recovery task
uses ``orbv4p-xrec-20261109-<SYM>`` and is not modified here. While that order
is open or partially filled, the scheduler must not cancel it and must not
submit a second close.

Bracket stop/limit legs are not a pending close. A working market close is
left alone. Close quantity is the live absolute position, never the original
carry quantity.
"""
from __future__ import annotations

from datetime import date, datetime

RECOVERY_PREFIX = "orbv4p-xrec-"
EXIT_PREFIXES = ("orbv4p-x-", "orbv4p-xmkt-", "orbv4p-xah-")
OPEN_STATUSES = {
    "new", "accepted", "pending_new", "accepted_for_bidding",
    "partially_filled", "held", "pending_replace", "pending_cancel",
}


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _status(order) -> str:
    raw = str(_get(order, "status", "") or "")
    if "." in raw:
        raw = raw.split(".")[-1]
    return raw.strip().lower()


def _coid(order) -> str:
    return str(_get(order, "client_order_id", "") or "")


def _side(order) -> str:
    raw = str(_get(order, "side", "") or "").lower()
    if "buy" in raw:
        return "buy"
    if "sell" in raw:
        return "sell"
    return raw


def _symbol(order) -> str:
    return str(_get(order, "symbol", "") or "")


def _tif(order) -> str:
    return str(_get(order, "time_in_force", "") or "").split(".")[-1].lower()


def _otype(order) -> str:
    return str(_get(order, "type", "") or _get(order, "order_type", "") or "").split(".")[-1].lower()


def position_side(position) -> str | None:
    if not position:
        return None
    qty = float(_get(position, "qty", 0) or 0)
    side = str(_get(position, "side", "") or "").lower()
    if qty == 0:
        return None
    if qty < 0 or "short" in side:
        return "short"
    return "long"


def close_side_for(side: str) -> str:
    if side == "short":
        return "buy"
    if side == "long":
        return "sell"
    raise ValueError(f"unknown position side {side!r}")


def is_open_order(order) -> bool:
    return _status(order) in OPEN_STATUSES


def market_close_coid(symbol: str, day: date | datetime) -> str:
    d = day.date() if isinstance(day, datetime) else day
    return f"orbv4p-xmkt-{d:%Y%m%d}-{symbol}"


def moc_close_coid(symbol: str, day: date | datetime) -> str:
    d = day.date() if isinstance(day, datetime) else day
    return f"orbv4p-x-{d:%Y%m%d}-{symbol}"


def _is_bracket_leg(order) -> bool:
    coid = _coid(order)
    if coid.startswith(RECOVERY_PREFIX) or coid.startswith(EXIT_PREFIXES):
        return False
    if _tif(order) == "cls":
        return False
    if _otype(order) in ("market", "mkt"):
        return False
    if _get(order, "stop_price", None) not in (None, "", 0):
        return True
    return _otype(order) in ("stop", "stop_limit", "limit", "trailing_stop")


def decide_close(record: dict, position, open_orders, *, today: date | None = None) -> dict:
    """``submit`` is the only action that may send a close."""
    sym = str(record.get("symbol") or "")
    have = position_side(position)
    if have is None:
        return {"action": "skip_flat", "symbol": sym, "submit": False}
    recorded = record.get("side")
    if recorded and recorded != have:
        return {
            "action": "refuse_flip",
            "symbol": sym,
            "submit": False,
            "have": have,
            "record_side": recorded,
        }
    want = close_side_for(have)
    qty = abs(float(_get(position, "qty", 0) or 0))
    if qty <= 0:
        return {"action": "skip_flat", "symbol": sym, "submit": False}
    pending = [o for o in (open_orders or []) if _symbol(o) == sym and is_open_order(o)]
    for order in pending:
        coid = _coid(order)
        if coid.startswith(RECOVERY_PREFIX):
            return {
                "action": "recovery_close_pending",
                "symbol": sym,
                "submit": False,
                "coid": coid,
                "status": _status(order),
                "filled_qty": float(_get(order, "filled_qty", 0) or 0),
                "order_qty": _get(order, "qty"),
                "close_side": want,
            }
    for order in pending:
        coid = _coid(order)
        if coid.startswith(EXIT_PREFIXES) or _tif(order) == "cls" or (
            _side(order) == want and not _is_bracket_leg(order)
        ):
            return {
                "action": "pending_close",
                "symbol": sym,
                "submit": False,
                "coid": coid,
                "status": _status(order),
                "filled_qty": float(_get(order, "filled_qty", 0) or 0),
                "close_side": want,
            }
    day = today or date.today()
    return {
        "action": "submit",
        "symbol": sym,
        "submit": True,
        "qty": qty,
        "side": want,
        "position_side": have,
        "moc_coid": moc_close_coid(sym, day),
        "market_coid": market_close_coid(sym, day),
    }


def entry_block(symbol: str, signal: dict | None, positions: dict) -> str | None:
    position = (positions or {}).get(symbol)
    have = position_side(position)
    if have is None:
        return None
    bracket = (signal or {}).get("bracket") or {}
    want = bracket.get("side") or (signal or {}).get("first_break_side")
    if want in ("long", "short") and want != have:
        return "refuse_flip"
    return "already_in_position"
