# Box backup auto-restart

The box backup supervisor (`paper_broker/scripts/orb_v4_backup_timer.sh`) is a nohup/`flock` process. It dies when the box VM/pod recycles. This hook restarts it.

On this box, systemd is not PID 1 (`/tini -- /pod-daemon`) and cron is not installed, so the installer uses the same class of hook as LIVE v3's timer: an idempotent `ensure` line in `~/.bashrc`, plus an XDG autostart entry. `ensure` starts the supervisor only when v4 paper keys are already in the environment; it never prints or writes secrets and never touches `box-secrets.json`. The `flock` lock allows only one supervisor.

Install: `bash /workspace/orb-v4-paper/deploy/install-box-backup-boot-hook.sh install`

If systemd is later PID 1, the same installer enables `deploy/orb-v4-paper-backup.service`.
