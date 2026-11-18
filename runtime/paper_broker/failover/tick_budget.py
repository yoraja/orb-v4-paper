"""Per-tick wall-clock budget so a hung scan cannot block later slots.

Reliability only: no strategy, risk, sizing, or order-type changes.

A tick is capped at TICK_WALL_S (8 minutes). Scan ticks are also preempted
PREEMPT_BUFFER_S seconds before the next close slot (12:46 exit, 12:55 eod,
13:05 verify) so those slots can start on time.

Lease renewal during a long tick is unnecessary: the cap (8 min) is shorter
than every lease TTL (scan 12 min, exit/eod 16 min, verify 10 min). The
runner already renews the lease at the start of each tick. A killed tick
returns before the lease lapses, and the next tick renews it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from paper_broker.failover.schedule import regular_grid

PT = ZoneInfo("America/Los_Angeles")
TICK_WALL_S = 8 * 60
PREEMPT_BUFFER_S = 15
CLOSE_KINDS = ("exit", "eod", "verify")
TIMEOUT_RC = 124
LOCK_NAME = "orb_v4_paper.lock"


def close_slots_on(day) -> list:
    return [s for s in regular_grid(day) if s.kind in CLOSE_KINDS]


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=PT)
    return now


def tick_deadline(now: datetime, *, kind: str | None = None) -> datetime:
    """Absolute PT deadline for this tick."""
    now = _aware(now)
    hard = now + timedelta(seconds=TICK_WALL_S)
    if kind in CLOSE_KINDS:
        return hard
    upcoming = [s for s in close_slots_on(now.date()) if s.when > now]
    if not upcoming:
        return hard
    nxt = upcoming[0]
    preempt = nxt.when - timedelta(seconds=PREEMPT_BUFFER_S)
    if preempt > now:
        return min(hard, preempt)
    # Inside the preemption buffer but the close slot has not fired yet:
    # die just before it fires so the close tick can start.
    if nxt.when > now:
        almost = nxt.when - timedelta(seconds=1)
        return almost if almost > now else now + timedelta(seconds=1)
    return hard


def tick_timeout_seconds(now: datetime, *, kind: str | None = None) -> int:
    now = _aware(now)
    seconds = int((tick_deadline(now, kind=kind) - now).total_seconds())
    return max(1, seconds)


def track_lock_path() -> Path:
    home = Path(os.environ.get("ORB_V4_HOME") or ".")
    run = Path(os.environ.get("ORB_V4_RUN") or (home / "run"))
    return run / "locks" / LOCK_NAME


def release_stale_track_lock(path: Path | None = None) -> str:
    """Release the track lock only if it is not held by a live process.

    The lock is an advisory fcntl/msvcrt lock on the file; it is released
    automatically when the holder process dies. This helper is a safety net
    so a leftover file cannot look stuck after a tick was killed.
    """
    path = Path(path or track_lock_path())
    if not path.exists():
        return "missing"
    try:
        fh = open(path, "a+")
    except OSError:
        return "unreadable"
    try:
        if os.name == "nt":
            import msvcrt
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return "held"
            try:
                fh.seek(0)
                fh.truncate()
            finally:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            return "released"
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "held"
        try:
            fh.seek(0)
            fh.truncate()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return "released"
    finally:
        fh.close()


def kill_process_group(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            return


def run_process_group(
    argv: Sequence[str],
    *,
    env: dict | None = None,
    cwd: str | None = None,
    timeout_s: int,
) -> int:
    """Run argv in a new process group. Kill the group on timeout. Return rc or 124."""
    proc = subprocess.Popen(
        list(argv),
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        return int(proc.wait(timeout=timeout_s) or 0)
    except subprocess.TimeoutExpired:
        kill_process_group(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_process_group(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        release_stale_track_lock()
        return TIMEOUT_RC


def main(argv: list[str] | None = None) -> int:
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="tick_budget")
    ap.add_argument("--seconds", action="store_true", help="print tick timeout seconds for now")
    ap.add_argument("--kind", default=None)
    ap.add_argument("--release-locks", action="store_true")
    ap.add_argument("--watch", type=int, default=0, help="pid to wait on until the deadline")
    args = ap.parse_args(argv)
    now = datetime.now(PT)
    if args.release_locks:
        print(release_stale_track_lock())
        return 0
    if args.watch:
        timeout_s = tick_timeout_seconds(now, kind=args.kind)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                os.kill(args.watch, 0)
            except (ProcessLookupError, OSError):
                return 0
            time.sleep(0.2)
        kill_process_group(args.watch)
        release_stale_track_lock()
        return TIMEOUT_RC
    if args.seconds:
        print(tick_timeout_seconds(now, kind=args.kind))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
