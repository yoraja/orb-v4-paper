"""Minute-by-minute no-order failover simulation for Mon 2026-11-16 (and Tue 11-17 close),
using the deployed Runner with an in-memory lease seeded from the real ownership.json.
YOPC: Task Scheduler tick every minute 05:55-13:25, scan minutes wait to +21 s (orb-v4-role.ps1).
Box: supervisor tick every 5 min 06:01-13:25 plus 12:46/12:55/13:05. No broker, no orders."""
import json, sys, tempfile, subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
sys.path.insert(0, "/workspace/orb-v4-paper")
from paper_broker.failover.runner import Runner, Heartbeat, AccountContext
from paper_broker.failover.schedule import regular_grid
from paper_broker.failover.transport import MemoryTransport
from paper_broker.failover import transport as tm
PT = ZoneInfo("America/Los_Angeles")
seed = json.loads(subprocess.run(["git", "-C", "/workspace/orb-v4-paper/run/ownership-git", "show", "origin/ownership:ownership.json"],
                                 capture_output=True, text=True, check=True).stdout)

def sim(day, yopc_alive, box_freeze=None, label=""):
    t = MemoryTransport(); t._commit(seed); tm.set_transport_factory(lambda: t)
    tmp = Path(tempfile.mkdtemp())
    runs, owners, box_dec, hb = [], [], [], {"v": None}
    def ex(host):
        def f(slot, token):
            runs.append((host, slot.kind, slot.hhmm))
            return {"rc": 0, "orders": 0}
        return f
    d0 = datetime(*day, tzinfo=PT)
    events = []
    m = d0.replace(hour=5, minute=55)
    while m <= d0.replace(hour=13, minute=25):
        ty = m + timedelta(seconds=2)
        if m.minute % 15 == 0 and (6, 0) <= (m.hour, m.minute) <= (12, 45):
            ty = m + timedelta(seconds=21)
        events.append((ty, "yopc"))
        if (m.hour, m.minute) >= (6, 1) and (m.minute % 5 == 1 or (m.hour, m.minute) in ((12, 46), (12, 48), (12, 55), (13, 5))):
            events.append((m + timedelta(seconds=3), "box"))
        m += timedelta(minutes=1)
    for when, host in sorted(events):
        if host == "yopc":
            if not yopc_alive(when):
                continue
            n_before = len(runs)
            r = Runner(role="primary", host="yopc", instance="yopc-cli", transport=t, state_dir=tmp / "yopc",
                       now=when, slots=regular_grid(when.date()), heartbeat=None, account=AccountContext())
            d = r.run(ex("yopc"))
            if len(runs) > n_before:
                last = runs[-1]
                hb["v"] = Heartbeat(host="yopc", ts_utc=(when + timedelta(seconds=60)).astimezone(timezone.utc),
                                    kind=last[1], slot=last[2], rc=0, ok=True, trading=True, date=f"{when:%Y-%m-%d}")
        else:
            jump = 0.0
            if box_freeze and box_freeze[0] <= when < box_freeze[1]:
                continue
            if box_freeze and when >= box_freeze[1] and not box_dec[-1:][0][3].startswith("resume"):
                jump = (box_freeze[1] - box_freeze[0]).total_seconds()
            seen = hb["v"] if hb["v"] and hb["v"].ts_utc <= when.astimezone(timezone.utc) else None
            r = Runner(role="backup", host="box", instance="box-cli", transport=t, state_dir=tmp / "box",
                       now=when, slots=regular_grid(when.date()), heartbeat=seen or Heartbeat(host="yopc", ts_utc=datetime(2026, 11, 13, 21, 33, tzinfo=timezone.utc), kind="verify", slot="1305", rc=0, ok=True, trading=True, date="2026-11-13"),
                       account=AccountContext(), clock_jump_s=jump)
            d = r.run(ex("box"))
            box_dec.append((f"{when:%H:%M:%S}", d.action, d.reason, "resume" if jump else ""))
        owners.append((f"{when:%H:%M}", (t.doc or {}).get("owner"), (t.doc or {}).get("status")))
    tm.set_transport_factory(None)
    by = {}
    for h, k, s in runs:
        by.setdefault(h, []).append(f"{k} {s}")
    print(f"\n######## {label}")
    for h in ("yopc", "box"):
        print(f"{h} ran {len(by.get(h, []))} slots: {', '.join(by.get(h, []))}")
    acts = {}
    for _, a, rsn, _x in box_dec:
        acts[f"{a}:{rsn}"] = acts.get(f"{a}:{rsn}", 0) + 1
    print("box decisions:", acts)
    print("box non-idle:", [b for b in box_dec if b[1] != "idle"][:8])
    hold = {}
    for _, o, st in owners:
        if st == "holding":
            hold[o] = hold.get(o, 0) + 1
    print("lease-holding samples by owner:", hold)
    grid = [s for s in regular_grid(datetime(*day).date())]
    done = {f"{k} {s}" for _, k, s in runs}
    print("all 31 slots ran exactly once:", len(runs) == len(grid) and all(f"{s.kind} {s.hhmm}" in done for s in grid),
          f"({len(runs)} runs / {len(grid)} slots)")

with patch("paper_broker.failover.dashboard_publish.schedule_publish", return_value="stub"):
    sim((2026, 11, 16), lambda w: True, label="Mon 11-16: YOPC healthy all day")
    sim((2026, 11, 16), lambda w: not (w.hour, w.minute) >= (9, 5) or (w.hour, w.minute) >= (10, 30),
        label="Mon 11-16: YOPC down 09:05-10:29, back 10:30")
    sim((2026, 11, 16), lambda w: (w.hour, w.minute) < (12, 32), label="Mon 11-16: YOPC dies 12:32 (close slots)")
    sim((2026, 11, 17), lambda w: True, label="Tue 11-17: YOPC healthy (day-2 exits at 12:46/12:55)")
    # Fri 11-13 replay: YOPC at 12:45 with the fix, box frozen 12:46-13:17
    sim((2026, 11, 17), lambda w: True, box_freeze=(datetime(2026, 11, 17, 12, 46, 30, tzinfo=PT), datetime(2026, 11, 17, 13, 17, tzinfo=PT)),
        label="Tue 11-17: box VM frozen 12:46-13:17 (Fri replay)")
