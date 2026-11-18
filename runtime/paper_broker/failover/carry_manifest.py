"""Shared five-name carry awareness for both PAPER hosts.

Stopped and target prices are filled only when the broker did not return
bracket legs, and only for fields present in the manifest. Missing stops stay
null. This file is not an account credential and not an order instruction.
"""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = Path(__file__).with_name("carry_expected_20261106.json")
PIN_LAST4 = "J7D0"
RECOVERY_DATE = "2026-11-09"
RECOVERY_SYMBOLS = ("CSCO", "META", "WMT")
UNTOUCHED_BY_RECOVERY = ("AAPL", "MSFT")


def load_manifest(path: Path | None = None) -> dict:
    return json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))


def recovery_coid(symbol: str) -> str:
    return f"orbv4p-xrec-{RECOVERY_DATE.replace('-', '')}-{symbol}"


def overlay_manifest(carry_book: dict, manifest: dict | None = None) -> list[str]:
    man = manifest if manifest is not None else load_manifest()
    filled: list[str] = []
    for sym, rec in (carry_book.get("positions") or {}).items():
        src = (man.get("positions") or {}).get(sym)
        if not src:
            continue
        for key in ("plan_stop", "plan_target", "entry_price"):
            if rec.get(key) in (None, "") and src.get(key) not in (None, ""):
                rec[key] = src[key]
                rec[f"{key}_source"] = "carry_manifest"
                filled.append(f"{sym}.{key}")
        if not rec.get("planned_exit") and src.get("planned_exit"):
            rec["planned_exit"] = src["planned_exit"]
            filled.append(f"{sym}.planned_exit")
        if not rec.get("entry_date") and src.get("entry_date"):
            rec["entry_date"] = src["entry_date"]
            filled.append(f"{sym}.entry_date")
    return filled
