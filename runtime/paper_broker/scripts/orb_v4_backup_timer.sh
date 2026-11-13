#!/usr/bin/env bash
# ORB v4 PAPER box backup ticker. Weekdays 06:01-13:25 PT every 5 minutes.
# Never touches the LIVE v3 timer. Stop with: $0 stop
set -u
SELF=$(readlink -f "$0")
REPO=/workspace/orb-v4-paper
RUN=$REPO/run
LOCK=$RUN/orb_v4_backup_timer.daemon.lock
PIDF=$RUN/orb_v4_backup_timer.pid
STOPF=$RUN/orb_v4_backup_timer.STOP
HB=$RUN/backup_timer_heartbeat.json
LOG=$RUN/logs/orb_v4_backup_timer.log
ROLE=$REPO/paper_broker/scripts/orb_v4_role.sh
export TZ=America/Los_Angeles ORB_BROKER_MODE=paper
# Fail closed without v4 paper keys (inherited, never written).
keys_ok(){ [ -n "${ORB_V4_ALPACA_PAPER_KEY_ID:-}" ] && [ -n "${ORB_V4_ALPACA_PAPER_SECRET_KEY:-}" ]; }
running(){ [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; }
log(){ echo "$(date '+%F %T') PT [backup] $*" >> "$LOG"; }
mkdir -p "$RUN/logs"
in_window(){
  python3 - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
now=datetime.now(ZoneInfo('America/Los_Angeles'))
ok = now.weekday()<5 and (now.hour, now.minute) >= (6,1) and (now.hour, now.minute) <= (13,25)
raise SystemExit(0 if ok else 1)
PY
}
next_sleep(){
  # Bash-only wake calculation. A hung python3 here skipped ticks for 17 and
  # 61 minutes on 2026-11-12 after the 06:20 and 06:51 idles. Cap at 5 minutes.
  local now_s min sec mins_to target close ts d
  now_s=$(date +%s)
  min=$((10#$(date +%M)))
  sec=$((10#$(date +%S)))
  mins_to=$((5 - (min % 5)))
  if [ "$mins_to" -le 0 ]; then mins_to=5; fi
  target=$((now_s + mins_to * 60 - sec))
  # 12:48 lets the backup cover a dead primary's 12:46 exit before the 12:50
  # PT MOC cutoff (2026-11-13 fix: the backup now waits 90 s for YOPC first).
  for close in "12:46:00" "12:48:00" "12:55:00" "13:05:00"; do
    ts=$(date -d "today $close" +%s 2>/dev/null || true)
    if [ -n "${ts:-}" ] && [ "$ts" -gt "$now_s" ] && [ "$ts" -lt "$target" ]; then
      target=$ts
    fi
  done
  d=$((target - now_s))
  if [ "$d" -lt 5 ]; then d=5; fi
  if [ "$d" -gt 300 ]; then d=300; fi
  printf '%s\n' "$d"
}
run_tick(){
  # Hard wall-clock budget (8 min, or sooner if a close slot is imminent).
  # The tick runs in a new session so a hung engine child is killed with it.
  local budget tpid wrc rc
  budget=$(PYTHONPATH="$REPO" "$REPO/venv-paper/bin/python" -m paper_broker.failover.tick_budget --seconds 2>/dev/null || true)
  case "$budget" in
    ''|*[!0-9]*) budget=480 ;;
  esac
  log "tick start budget_s=$budget"
  setsid bash "$ROLE" Backup >>"$LOG" 2>&1 &
  tpid=$!
  PYTHONPATH="$REPO" "$REPO/venv-paper/bin/python" -m paper_broker.failover.tick_budget --watch "$tpid" >/dev/null 2>&1
  wrc=$?
  wait "$tpid" 2>/dev/null
  rc=$?
  if [ "$wrc" -eq 124 ]; then
    log "tick timeout budget_s=$budget — killed hung tick and released leftover lock"
    PYTHONPATH="$REPO" "$REPO/venv-paper/bin/python" -m paper_broker.failover.tick_budget --release-locks >/dev/null 2>&1 || true
  elif [ "${rc:-0}" -ne 0 ]; then
    log "tick rc=$rc"
  fi
}

case "${1:-status}" in
  start)
    keys_ok || { echo "REFUSED: ORB_V4_ALPACA_PAPER_* env not set"; exit 3; }
    rm -f "$STOPF"
    nohup setsid flock -n "$LOCK" bash "$SELF" supervise >/dev/null 2>&1 < /dev/null &
    sleep 2
    if ! running; then
      log "start failed: supervisor did not come up (flock lock busy?)"
      echo "FAIL: supervisor did not start"
      exit 1
    fi
    bash "$SELF" status
    ;;
  supervise)
    echo $$ > "$PIDF"
    log "supervisor start pid=$$"
    export ORB_V4_ENGINE_ALLOW=1
    jump=0
    while [ ! -f "$STOPF" ]; do
      if in_window; then
        # 2026-11-13: the box VM froze 12:46-13:17 PT. A sleep that ends far
        # later by wall clock (date +%s) than asked means a freeze/resume: the
        # next tick idles (and hands back a lease it holds) instead of trading.
        export ORB_V4_CLOCK_JUMP_S="$jump"
        if [ "$jump" -ge 60 ]; then log "clock jump ${jump}s after sleep (VM freeze/resume) - tick will idle"; fi
        run_tick
        timeout 5s python3 -c "import json,time; from pathlib import Path; Path('$HB').write_text(json.dumps({'alive_epoch':int(time.time()),'alive_pt':__import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/Los_Angeles')).isoformat(),'role':'backup'}))" || log "heartbeat write skipped or timed out"
      fi
      secs=$(next_sleep || true); case "$secs" in ''|*[!0-9]*) secs=30 ;; esac; if [ "$secs" -gt 300 ]; then secs=300; fi
      if [ "$jump" -ge 60 ]; then secs=30; fi
      t_sleep=$(date +%s); sleep "$secs"; t_wake=$(date +%s)
      jump=$(( t_wake - t_sleep - secs )); if [ "$jump" -lt 0 ]; then jump=0; fi
    done
    rm -f "$PIDF"
    log "supervisor exit"
    ;;
  stop)
    touch "$STOPF"
    if [ -f "$PIDF" ]; then
      pid=$(cat "$PIDF")
      pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
      if [ -n "${pgid:-}" ]; then
        kill -- "-$pgid" 2>/dev/null || true
      fi
      kill "$pid" 2>/dev/null || true
    fi
    # flock -n holds the lock; a leftover sleep child can keep the fd open after bash is killed.
    pkill -f "flock -n $LOCK" 2>/dev/null || true
    rm -f "$PIDF"
    i=0
    while [ $i -lt 10 ]; do
      if command -v fuser >/dev/null && fuser "$LOCK" >/dev/null 2>&1; then
        sleep 0.2
        i=$((i+1))
      else
        break
      fi
    done
    echo stopped
    ;;
  ensure)
    # Idempotent boot/session hook. flock in start() prevents a second supervisor.
    # If v4 paper keys are not in this environment, skip (do not print or write them).
    if running; then
      echo "already running supervisor pid $(cat $PIDF)"
      exit 0
    fi
    if ! keys_ok; then
      log "ensure: v4 paper keys not in environment — not starting"
      echo "ensure: keys not in environment (not starting)"
      exit 0
    fi
    log "ensure: not running — starting"
    bash "$SELF" start
    ;;
  status)
    if running; then echo "running supervisor pid $(cat $PIDF)"; else echo "not running"; fi
    cat "$HB" 2>/dev/null; echo
    tail -5 "$LOG" 2>/dev/null
    ;;
  *) echo "usage: $0 start|stop|status|ensure"; exit 2 ;;
esac
