"""Portable date formatting for ORB v4 PAPER (Windows-safe). Run:
  python tests/test_portable_strftime.py
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_broker.ptime import (
    fmt_mdy_pt,
    fmt_mon_day_year_time_pt,
    fmt_pt,
    fmt_pt_date,
    fmt_pt_time,
    fmt_weekday_mon_day_time_pt,
    weekday_mon_day,
)

fails = 0


def check(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m)
    fails += 0 if c else 1


PT = ZoneInfo("America/Los_Angeles")
# Single-digit day and hour, and a 10th (no leading-zero strip of "10")
d = date(2026, 11, 10)
d2 = date(2026, 1, 5)
dt = datetime(2026, 11, 10, 13, 5, 6, tzinfo=PT)  # 1:05:06 PM
dt10 = datetime(2026, 11, 10, 10, 5, 0, tzinfo=PT)  # 10:05 AM

check(weekday_mon_day(d) == "Tue Nov 10", f"weekday_mon_day Nov 10 got {weekday_mon_day(d)!r}")
check(weekday_mon_day(d2) == "Mon Jan 5", f"weekday_mon_day Jan 5 got {weekday_mon_day(d2)!r}")
check(fmt_pt_date(d) == "Tue Nov 10, 2026", f"fmt_pt_date got {fmt_pt_date(d)!r}")
check(fmt_pt_time(dt, seconds=True) == "1:05:06 PM PT", f"fmt_pt_time 1pm got {fmt_pt_time(dt, seconds=True)!r}")
check(fmt_pt_time(dt10, seconds=False) == "10:05 AM PT", f"fmt_pt_time 10am must keep 10, got {fmt_pt_time(dt10)!r}")
check(fmt_pt(dt) == "Tue Nov 10, 2026 1:05 PM PT", f"fmt_pt got {fmt_pt(dt)!r}")
check(fmt_mdy_pt(dt) == "11/10/2026 1:05 PM PT", f"fmt_mdy_pt got {fmt_mdy_pt(dt)!r}")
check(fmt_mon_day_year_time_pt(dt) == "Nov 10, 2026 1:05 PM PT", fmt_mon_day_year_time_pt(dt))
check(fmt_weekday_mon_day_time_pt(dt) == "Tue Nov 10, 1:05 PM PT", fmt_weekday_mon_day_time_pt(dt))

# The 13:05 verify message format must not use %- flags
msg = f"ORB v4 PAPER 📊 EOD {weekday_mon_day(d)}: equity $49,799.22"
check(msg == "ORB v4 PAPER 📊 EOD Tue Nov 10: equity $49,799.22", f"verify line got {msg!r}")
check("%-" not in msg, "verify line has no %- flag")

# On this platform, %-d may or may not work; the helper must work either way.
try:
    broken = d.strftime("%a %b %-d")
    check(True, f"native %-d happens to work here ({broken!r}); helper still used")
except ValueError as e:
    check("Invalid format string" in str(e) or "error" in str(e).lower(),
          f"native %-d raises as on Windows: {type(e).__name__}: {e}")
    check(weekday_mon_day(d) == "Tue Nov 10", "helper works after native %-d failed")

# Source scan: v4 PAPER runtime files must not use non-portable %- flags.
# Skip leftover v3 dashboard, backups, venv, and pre-hook copies.
SKIP_PARTS = (
    "/venv", "\\venv", "/.venv", "site-packages", "/backup", "\\backup",
    "refresh_orb_dashboard.py", "orb_v4_paper.py.pre-hooks",
    "yopc-plan/originals", "yopc-plan/v3-paper-disable",
    "yopc-plan\\originals", "yopc-plan\\v3-paper-disable",
    "/run/", "\\run\\", "_v3_ops_removed",
)
pat = re.compile(r"%-[dmHIyWwUj]|:%[a-zA-Z ]*%-")
hits = []
for path in list((ROOT / "paper_broker").rglob("*.py")) + list((ROOT / "deploy").rglob("*")) + list((ROOT / "yopc-plan" / "v4-postclose").rglob("*.py")):
    if not path.is_file():
        continue
    s = str(path)
    if any(part in s for part in SKIP_PARTS):
        continue
    if path.suffix not in {".py", ".ps1", ".sh"} and path.name != "orb-v4-role.ps1":
        if path.suffix:
            continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if pat.search(line):
            hits.append(f"{path.relative_to(ROOT)}:{i}:{line.strip()}")
check(hits == [], "no non-portable %- strftime in v4 PAPER runtime\n  " + "\n  ".join(hits))

# Engine, when present in this tree, must use the portable helper.
engine = ROOT / "paper_broker" / "orb_v4_paper.py"
if engine.is_file():
    import ast
    src = engine.read_text(encoding="utf-8")
    ast.parse(src)
    check("weekday_mon_day(today)" in src, "cmd_verify uses weekday_mon_day")
    check("%-d" not in src, "orb_v4_paper.py has no %-d")
else:
    check(True, "overlay tree has no engine file; helper is shipped for the host copy")

print("ALL PORTABLE STRFTIME CHECKS PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
