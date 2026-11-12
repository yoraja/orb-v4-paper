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
  python3 - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
now=datetime.now(ZoneInfo('America/Los_Angeles'))
# sleep until the next 5-minute boundary, at least 5s, at most 60s past a tick
minute = (now.minute // 5 + 1) * 5
delta = timedelta(minutes=minute-now.minute, seconds=-now.second, microseconds=-now.microsecond)
if minute >= 60:
    delta = timedelta(hours=1, minutes=-now.minute, seconds=-now.second, microseconds=-now.microsecond)
secs=max(5, int(delta.total_seconds()))
print(secs)
PY
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
    while [ ! -f "$STOPF" ]; do
      if in_window; then
        log "tick start"
        bash "$ROLE" Backup >>"$LOG" 2>&1 || log "tick rc=$?"
        python3 -c "import json,time; from pathlib import Path; Path('$HB').write_text(json.dumps({'alive_epoch':int(time.time()),'alive_pt':__import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/Los_Angeles')).isoformat(),'role':'backup'}))"
      fi
      sleep "$(next_sleep)"
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
