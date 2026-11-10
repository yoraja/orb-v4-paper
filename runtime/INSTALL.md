# ORB v4 PAPER — YOPC primary, box automatic backup

This overlay is not the dashboard. Do not copy it onto the GitHub Pages checkout
(`index.html`, `strategy.html`, `heartbeat.json`, `.nojekyll`). Install it into
the existing PAPER runtime directories. It does not deploy itself, start tasks,
or send orders.

Implemented and tested here: role decisions, git compare-and-swap fencing,
close/entry guards, carry awareness, and the install refusals. Not done here:
cutting the hosts over, creating the `ownership` branch, or calling Alpaca.

## What changes

- YOPC runs the primary grid: scan `06:00:20`–`12:45:20` PT every 15 minutes
  (signal-only before `07:30`), exit `12:46`, eod/carry `12:55`, verify `13:05`,
  existing post-close check `13:20`.
- Box trades only after YOPC misses at least two slots and the ownership lease
  is clearly free. A healthy or still-leased primary leaves box idle.
- Publish goes only with a confirmed lease. Box must not overwrite a newer YOPC
  liveness heartbeat.
- Settings stay the current v4 book: 14 symbols, 1.5% risk, 16% cap, 6
  concurrent, cash-only, −2R halt, entries through 12:45, OR-mid stop, 2.5R,
  2-session carry. See `daily/docs/rules_orb_v4_paper.json`.

## Ownership prerequisite (fail closed without it)

Per-host claim files are not a lock. Both hosts need the existing Pages git
credential (the one that already pushes `yoraja/orb-v4-paper`) and this remote:

```text
ORB_V4_OWNERSHIP_REMOTE=https://github.com/yoraja/orb-v4-paper.git
ORB_V4_OWNERSHIP_BRANCH=ownership
```

The lease file is `ownership.json` on branch `ownership`. From a host that
already has the Pages git credential (nothing new is written to the command
line):

```text
python runtime/deploy/bootstrap_ownership.py --remote https://github.com/yoraja/orb-v4-paper.git
```

The first run pushes a released bootstrap document. A later run sees
`ownership.json` and does not replace it, including a lease that is already
held. Do not invent a credential, tunnel, or shared folder. Until a fencing
token is confirmed on that branch, the runner submits nothing.

Account pin remains paper `J7D0` at `https://paper-api.alpaca.markets`. Never
pass `--live` or `--arm`.

## Do not touch

- The dashboard repo root files listed above.
- Any LIVE tree, LIVE task, or the shared `trading-strategy` environment.
- `ORB v4 PAPER Recovery Close 20261109` and its scripts. The installer only
  compares hashes and exits if they differ:
  - `orb-v4-recovery-close-20261109.py` `58a8ff246eb12f561f49ca59a91a89f5c518518717c424ca22f17cf5898d1fa6`
  - `orb-v4-recovery-close-20261109.ps1` `306e7ad8ff2eadc921df72c016aefba70016ef252e077b3c5f5e5d5db20e6c3d`
  - `task-ORB-v4-PAPER-Recovery-Close-20261109.xml` `d8d3d276772a17e495fbb5ac66b334537cfc5be9b28049d6f6c5ea337a298a84`

## PAPER-isolated interpreter

On the box, `.venv` is a symlink into the shared trading-strategy environment.
Do not `pip install` through it.

```powershell
$v = 'C:\Users\yogjr\.orb-v4-paper'
# stop if .venv is a symlink; leave the link in place
py -3.12 -m venv "$v\venv-paper"
& "$v\venv-paper\Scripts\python.exe" -m pip install -r "$v\incoming\requirements-paper-freeze.txt"
```

Use the existing freeze already on the host. This overlay's own package is
stdlib. Set `ORB_V4_PY` to `venv-paper\Scripts\python.exe`.

## Install onto the existing runtime

1. Copy `dist/orb-v4-paper-yopc-primary.tar.gz` to the host and check the
   `.sha256` file.
2. Extract into a staging directory, not into the Pages checkout and not over
   `paper-secrets.json`, `run\state`, or the recovery scripts.
3. Copy `runtime/paper_broker/failover` to
   `C:\Users\yogjr\.orb-v4-paper\code\paper_broker\failover` (YOPC) and the
   equivalent PAPER code directory on the box.
4. Copy `runtime/deploy/orb-v4-role.ps1` to `C:\Users\yogjr\.orb-v4-paper\`.
5. From `code`, run `python -m paper_broker.failover.cli self-test`.
6. Dry-run a backup plan with no engine launch:

```powershell
$env:ORB_V4_ENGINE_ALLOW = '0'
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\yogjr\.orb-v4-paper\orb-v4-role.ps1 -Role Backup -DryRun
```

7. Patch the installed engine only if `runtime/deploy/apply_hooks.py` finds the
   known anchors in `paper_broker/orb_v4_paper.py` (sha256
   `97c073d4b5a85d6cbcfeeae53a0c283f3b23b803a9cf4d11f4e66b5c04f23fe7` on the
   probed hosts). Exit and flatten anchors are unchanged. The submit anchor is
   `o = client.submit_order(req)`. The hook inserts `before_submit()` above
   that line and does not rename `o`. If any anchor is missing, the patcher
   raises and leaves the file unchanged.
8. Carry awareness for both hosts is
   `paper_broker/failover/carry_expected_20261106.json` (AAPL, MSFT, CSCO,
   META, WMT, with MSFT stop 529.53 and target 546.75). It does not submit
   those orders.

## Cutover (operator, not this repo)

1. Stop the box `orb_v4_timer.py` daemon. Do not stop the LIVE timer.
2. Disable the YOPC task `ORB v4 PAPER Failover Watchdog` (it still assumes
   box is primary). Do not delete it until the new primary has a confirmed day.
3. Register one task per host. `register-tasks.ps1` prints the definitions
   with no changes. `-Register` performs the registration and never touches
   `ORB v4 PAPER Recovery Close 20261109`.

```powershell
# YOPC
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\yogjr\.orb-v4-paper\register-tasks.ps1 -Register -Role Primary
# box
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\yogjr\.orb-v4-paper\register-tasks.ps1 -Register -Role Backup
```

Equivalent `schtasks` commands, with `C:\Users\yogjr\.orb-v4-paper\orb-v4-role.ps1`
as the role script:

```text
schtasks /Create /TN "ORB v4 PAPER Primary" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 05:55 /RL LIMITED /F /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\Users\yogjr\.orb-v4-paper\orb-v4-role.ps1\" -Role Primary"
schtasks /Create /TN "ORB v4 PAPER Backup" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 06:01 /RI 5 /DU 07:25 /RL LIMITED /F /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\Users\yogjr\.orb-v4-paper\orb-v4-role.ps1\" -Role Backup"
```

If `schtasks` rejects `/RI` on a weekly trigger, use `-Register -Role Backup`
instead. That path sets a 5-minute repetition for 7 hours 25 minutes.
4. Leave the Monday recovery task registered and unmodified.

Rollback: disable the two new tasks, start the box PAPER daemon, re-enable the
old YOPC watchdog. The `ownership` branch can stay; idle processes do not order.

## Untested on Windows

The suite runs on Linux with a local git bare repo and fake brokers. These host
steps were not executed: Task Scheduler registration, `py -3.12 -m venv`,
PowerShell `ParseExact` (the format string is pinned and the Python parser is
tested), toast notifications, the Windows git credential helper, and stopping
the live box daemon. No Alpaca call was made.
