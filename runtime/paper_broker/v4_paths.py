"""ORB v4 PAPER — single source of paths for this isolated copy.

ORB_V4_HOME   code root (default: this repo)
ORB_V4_RUN    runtime root for state/logs/locks/box_runs (default: <home>/run)
Nothing here points at /workspace/trading-strategy or /workspace/orb-v3-sync.
"""
from __future__ import annotations
import os
from pathlib import Path

HOME = Path(os.environ.get("ORB_V4_HOME") or Path(__file__).resolve().parents[1])
RUN = Path(os.environ.get("ORB_V4_RUN") or (HOME / "run"))
STATE = RUN / "state"
LOGS = RUN / "logs"
LOCKS = RUN / "locks"
RUNS = RUN / "box_runs"
CACHE = RUN / "cache"
for _d in (STATE, LOGS, LOCKS, RUNS, CACHE):
    _d.mkdir(parents=True, exist_ok=True)

TAG = "orbv4p"           # client_order_id prefix for every v4 order
SLACK_PREFIX = "ORB v4 PAPER"
SLACK_CHANNEL = "C0C0HCTTJBU"  # #grok-trades
RULES = HOME / "daily" / "docs" / "rules_orb_v4_paper.json"
CARRY_STATE = STATE / "carry_state.json"
