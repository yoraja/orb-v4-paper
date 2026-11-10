# Prints the task definitions to register by hand. This script does not register them
# unless -Register is passed on the host, and it never touches the Monday recovery task.
[CmdletBinding()]
param([switch]$Register)
$ErrorActionPreference = 'Stop'
Write-Output 'YOPC primary: weekdays 05:55 PT, ORB v4 PAPER Primary, runs orb-v4-role.ps1 -Role Primary until after 13:20 post-close.'
Write-Output 'Box backup: weekdays 06:01 PT every 5 minutes for 7h25m, ORB v4 PAPER Backup, runs orb-v4-role.ps1 -Role Backup.'
Write-Output 'Do not modify or unregister: ORB v4 PAPER Recovery Close 20261109'
Write-Output 'Disable (do not delete) after cutover: ORB v4 PAPER Failover Watchdog on YOPC, and the box orb_v4_timer.py daemon.'
Write-Output 'Grid: scan 06:00:20-12:45:20 PT every 15m (signal-only before 07:30), exit 12:46, eod 12:55, verify 13:05, post-close 13:20.'
if (-not $Register) { Write-Output 'Dry listing only. Re-run with -Register on the target host after INSTALL.md prechecks.'; exit 0 }
throw 'Register is a host step. Do not run it from the cloud agent or against a LIVE tree.'
