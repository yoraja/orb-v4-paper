"""No-order Monday/Tuesday engine simulation for the 3 carries. Dry-run only, guarded client,
isolated ORB_V4_RUN copy (no writes to the live state). Reads Alpaca paper ..J7D0 (read-only)."""
import json, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.environ.get("ORB_V4_HOME", "/workspace/orb-v4-paper"))
os.environ["ORB_V4_FENCE_REQUIRED"] = "1"; os.environ["ORB_V4_FENCE_TOKEN"] = ""
import paper_broker.orb_v4_paper as orb
from paper_broker import v4_keys
PT = ZoneInfo("America/Los_Angeles")
MUT = {"submit_order", "cancel_order_by_id", "cancel_orders", "close_position", "close_all_positions", "replace_order_by_id"}
class G:
    def __init__(s, c): s._c = c
    def __getattr__(s, n):
        if n in MUT:
            raise RuntimeError(f"refused {n}")
        return getattr(s._c, n)
v4_keys.apply()
client = G(orb.make_client())
orb.make_client = lambda: client
acct = client.get_account()
print("account ..%s cash=%s equity=%s" % (str(acct.account_number)[-4:], acct.cash, acct.equity))
pos = orb.eng.list_positions(client)
params = orb.eng.v3_params()
open_n = orb.eng.count_open_orb_positions({p["symbol"]: p for p in pos}, list(params["universe"]))
print("positions:", [(p["symbol"], p["qty"]) for p in pos])
print(f"concurrent gate Monday: open={open_n}/{params['max_concurrent']} slots_left={params['max_concurrent']-open_n} cash_only={params.get('cash_only')} cash={float(acct.cash):.2f}")
cb = orb.load_carry()
print("carry book:", {s: (r["side"], r["qty"], r["entry_date"], r["planned_exit"]) for s, r in cb["positions"].items()})
def at(s):
    orb.eng._SIM_NOW = datetime.fromisoformat(s).replace(tzinfo=PT)
    orb.set_calendar(None)
for when, fn in (("2026-11-16T12:46", orb.cmd_exit), ("2026-11-16T12:55", orb.cmd_eod),
                 ("2026-11-17T12:46", orb.cmd_exit), ("2026-11-17T12:55", orb.cmd_eod)):
    at(when)
    print(f"\n===== {when} PT {fn.__name__} (dry-run) =====")
    rc = fn(dry_run=True, client=client)
    runs = sorted(orb.P.RUNS.glob("*_dry_*.json"), key=lambda p: p.stat().st_mtime)
    d = json.loads(runs[-1].read_text())
    keep = {k: d.get(k) for k in ("due", "results", "actions", "next_session") if k in d}
    print("rc", rc, json.dumps(keep, default=str)[:1500])
