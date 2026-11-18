"""Portable date/time formatting for ORB v4 PAPER.

Windows libc rejects Unix strftime hyphen-padding flags (day, month, hour)
("Invalid format string"). These helpers use datetime attributes or
lstrip("0") instead, and produce the same unpadded text on Linux and Windows.
"""
from __future__ import annotations

from datetime import date, datetime


def weekday_mon_day(d: date | datetime) -> str:
    """'Tue Nov 10' (no leading zero on the day)."""
    return f"{d.strftime('%a %b')} {d.day}"


def weekday_mon_day_year(d: date | datetime) -> str:
    """'Tue Nov 10, 2026'."""
    return f"{d.strftime('%a %b')} {d.day}, {d.year}"


def time_ampm(dt: datetime, *, seconds: bool = False) -> str:
    """'1:05 PM' or '1:05:00 PM' without a leading zero on the hour."""
    fmt = "%I:%M:%S %p" if seconds else "%I:%M %p"
    return dt.strftime(fmt).lstrip("0")


def fmt_pt(dt: datetime, *, seconds: bool = False) -> str:
    """'Tue Nov 10, 2026 1:05 PM PT'."""
    return f"{weekday_mon_day_year(dt)} {time_ampm(dt, seconds=seconds)} PT"


def fmt_pt_time(dt: datetime, *, seconds: bool = False) -> str:
    """'1:05:00 PM PT'."""
    return f"{time_ampm(dt, seconds=seconds)} PT"


def fmt_pt_date(dt: date | datetime) -> str:
    """'Tue Nov 10, 2026'."""
    return weekday_mon_day_year(dt)


def fmt_mdy_pt(dt: datetime) -> str:
    """'11/10/2026 1:05 PM PT'."""
    return f"{dt.month}/{dt.day}/{dt.year} {time_ampm(dt)} PT"


def fmt_mon_day_year_time_pt(dt: datetime) -> str:
    """'Nov 10, 2026 1:05 PM PT'."""
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} {time_ampm(dt)} PT"


def fmt_weekday_mon_day_time_pt(dt: datetime) -> str:
    """'Tue Nov 10, 1:05 PM PT'."""
    return f"{dt.strftime('%a %b')} {dt.day}, {time_ampm(dt)} PT"
