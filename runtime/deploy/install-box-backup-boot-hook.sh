#!/usr/bin/env bash
# Install the ORB v4 PAPER box backup auto-restart hook.
# Paper only. Does not touch LIVE ORB v3 timers, the v3 bashrc line, or box-secrets.json.
# Does not print or write secrets. Idempotent. Usage: install | uninstall | status
set -euo pipefail
export TZ=America/Los_Angeles
REPO=/workspace/orb-v4-paper
TIMER="$REPO/paper_broker/scripts/orb_v4_backup_timer.sh"
MARKER="# ORB v4 PAPER box backup watchdog (does not touch the LIVE v3 timer line above)"
HOOK="[ -x $TIMER ] && $TIMER ensure >/dev/null 2>&1"
LINE="$MARKER"$'\n'"$HOOK"
BASHRC="${HOME:-/home/box}/.bashrc"
AUTOSTART_DIR="${HOME:-/home/box}/.config/autostart"
DESKTOP_SRC="$REPO/deploy/orb-v4-paper-backup.desktop"
DESKTOP_DST="$AUTOSTART_DIR/orb-v4-paper-backup.desktop"
UNIT_SRC="$REPO/deploy/orb-v4-paper-backup.service"
INSTALLED="$REPO/run/state/backup_boot_hook_installed.txt"
mkdir -p "$REPO/run/state" "$REPO/run/logs"

log(){ echo "$(date '+%F %T') PT [backup-hook] $*" | tee -a "$REPO/run/logs/backup_boot_hook.log"; }

systemd_is_init(){
  [ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" = "systemd" ]
}

install_bashrc(){
  touch "$BASHRC"
  if grep -Fqx "$HOOK" "$BASHRC" && grep -Fqx "$MARKER" "$BASHRC"; then
    log "bashrc hook already present"
    return 0
  fi
  # Drop a stale copy of just the command line, then append the marked pair.
  if grep -Fqx "$HOOK" "$BASHRC"; then
    local tmp
    tmp="$(mktemp)"
    grep -Fv "$HOOK" "$BASHRC" > "$tmp"
    mv "$tmp" "$BASHRC"
  fi
  printf '\n%s\n' "$LINE" >> "$BASHRC"
  log "installed bashrc hook in $BASHRC"
}

remove_bashrc(){
  [ -f "$BASHRC" ] || return 0
  local tmp
  tmp="$(mktemp)"
  grep -Fv "$HOOK" "$BASHRC" | grep -Fv "$MARKER" > "$tmp"
  mv "$tmp" "$BASHRC"
  log "removed bashrc hook from $BASHRC"
}

install_autostart(){
  mkdir -p "$AUTOSTART_DIR"
  cp -f "$DESKTOP_SRC" "$DESKTOP_DST"
  log "installed desktop autostart $DESKTOP_DST (harmless if this desktop does not process XDG autostart)"
}

remove_autostart(){
  rm -f "$DESKTOP_DST"
  log "removed $DESKTOP_DST"
}

install_systemd(){
  if ! systemd_is_init; then
    log "systemd is not PID 1 (init=$(ps -p 1 -o comm= | tr -d ' ')); not enabling a unit"
    return 0
  fi
  if systemctl --user status >/dev/null 2>&1; then
    mkdir -p "${HOME}/.config/systemd/user"
    cp -f "$UNIT_SRC" "${HOME}/.config/systemd/user/orb-v4-paper-backup.service"
    systemctl --user daemon-reload
    systemctl --user enable --now orb-v4-paper-backup.service
    log "enabled systemd user unit orb-v4-paper-backup.service"
    return 0
  fi
  if command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
    sudo cp -f "$UNIT_SRC" /etc/systemd/system/orb-v4-paper-backup.service
    sudo systemctl daemon-reload
    sudo systemctl enable orb-v4-paper-backup.service
    log "enabled systemd system unit orb-v4-paper-backup.service"
    return 0
  fi
  log "systemd is init but no user bus and no passwordless sudo; unit not enabled"
}

remove_systemd(){
  if systemctl --user status >/dev/null 2>&1; then
    systemctl --user disable --now orb-v4-paper-backup.service 2>/dev/null || true
    rm -f "${HOME}/.config/systemd/user/orb-v4-paper-backup.service"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  if [ -f /etc/systemd/system/orb-v4-paper-backup.service ] && command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
    sudo systemctl disable --now orb-v4-paper-backup.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/orb-v4-paper-backup.service
    sudo systemctl daemon-reload 2>/dev/null || true
  fi
}

status(){
  echo "init_pid1=$(ps -p 1 -o comm= | tr -d ' ')"
  echo "systemd_is_init=$(systemd_is_init && echo yes || echo no)"
  echo "crontab=$(command -v crontab || echo missing)"
  if [ -f "$BASHRC" ] && grep -Fqx "$HOOK" "$BASHRC"; then echo "bashrc_hook=present"; else echo "bashrc_hook=absent"; fi
  if [ -f "$DESKTOP_DST" ]; then echo "xdg_autostart=present"; else echo "xdg_autostart=absent"; fi
  echo "timer=$TIMER"
  bash "$TIMER" status | head -2
}

case "${1:-install}" in
  install)
    [ -x "$TIMER" ] || { echo "FAIL: timer script missing: $TIMER"; exit 2; }
    install_bashrc
    install_autostart
    install_systemd
    status > "$INSTALLED"
    date "+%F %T %Z" >> "$INSTALLED"
    status
    ;;
  uninstall)
    remove_bashrc
    remove_autostart
    remove_systemd
    rm -f "$INSTALLED"
    status
    ;;
  status) status ;;
  *) echo "usage: $0 install|uninstall|status"; exit 2 ;;
esac
