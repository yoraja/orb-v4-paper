# Register ORB v4 PAPER scheduled tasks. Paper only.
# Without -Register this only prints the definitions and schtasks commands.
# With -Register it registers exactly one role on this host.
# It never creates, changes, or deletes 'ORB v4 PAPER Recovery Close 20261109'.
# It never passes --live or --arm.
[CmdletBinding()]
param(
  [switch]$Register,
  [ValidateSet('', 'Primary', 'Backup')][string]$Role = ''
)
$ErrorActionPreference = 'Stop'
$Recovery = 'ORB v4 PAPER Recovery Close 20261109'
$PrimaryName = 'ORB v4 PAPER Primary'
$BackupName = 'ORB v4 PAPER Backup'
foreach ($name in @($PrimaryName, $BackupName)) {
  if ($name -eq $Recovery) { throw "refusing to touch $Recovery" }
}
$Base = if ($env:ORB_V4_BASE) { $env:ORB_V4_BASE } else { 'C:\Users\yogjr\.orb-v4-paper' }
$RoleScript = Join-Path $Base 'orb-v4-role.ps1'
$Pwsh = 'powershell.exe'
# Primary runs every minute in yogjr's desktop session: launch it hidden but waited, so no console pops up
# and Task Scheduler still tracks the real run (IgnoreNew, 12-minute limit) and exit code.
$HiddenWait = Join-Path $Base 'run-hidden-wait.vbs'
$PrimaryTr = "wscript.exe //B //Nologo `"$HiddenWait`" `"$RoleScript`" -Role Primary"
$BackupTr = "$Pwsh -NoProfile -ExecutionPolicy Bypass -File `"$RoleScript`" -Role Backup"

Write-Output "YOPC primary task: $PrimaryName weekdays 05:55 local PT, every 1 minute for 7 hours 30 minutes (primary-daemon is a one-shot tick, not a loop). $PrimaryTr"
Write-Output "Box backup task: $BackupName weekdays 06:01 local PT, every 5 minutes for 7 hours 25 minutes. $BackupTr"
Write-Output "Do not modify or unregister: $Recovery"
Write-Output 'After cutover, disable (do not delete) ORB v4 PAPER Failover Watchdog on YOPC and stop only the PAPER box orb_v4_timer.py daemon.'
Write-Output 'Grid: scan 06:00:20-12:45:20 PT every 15m (signal-only before 07:30), exit 12:46, eod 12:55, verify 13:05, post-close 13:20.'
Write-Output "schtasks primary: schtasks /Create /TN `"$PrimaryName`" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 05:55 /RI 1 /DU 07:30 /TR `"$PrimaryTr`" /RL LIMITED /F"
Write-Output "schtasks backup: schtasks /Create /TN `"$BackupName`" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 06:01 /RI 5 /DU 07:25 /TR `"$BackupTr`" /RL LIMITED /F"
Write-Output 'If /RI is rejected on WEEKLY, create the backup task in Task Scheduler with a 5-minute repetition for 7 hours 25 minutes, or pass -Register -Role Backup to this script.'

if (-not $Register) {
  Write-Output 'Listing only. Re-run with -Register -Role Primary on YOPC, or -Register -Role Backup on the box, after INSTALL.md prechecks.'
  exit 0
}
if (-not $Role) { throw 'FAIL CLOSED: -Register requires -Role Primary or -Role Backup' }
if (-not (Test-Path $RoleScript)) { throw "FAIL CLOSED: role script missing: $RoleScript" }

$taskName = if ($Role -eq 'Primary') { $PrimaryName } else { $BackupName }
if ($taskName -eq $Recovery) { throw "refusing to touch $Recovery" }
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$RoleScript`" -Role $Role"
$at = if ($Role -eq 'Primary') { '5:55AM' } else { '6:01AM' }

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
  $bak = Join-Path $Base ("task-" + ($taskName -replace ' ', '_') + ".bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.xml')
  Export-ScheduledTask -TaskName $taskName | Set-Content $bak -Encoding Unicode
  Write-Output "previous task exported to $bak"
}

$codeDir = Join-Path $Base 'code'
if ($Role -eq 'Primary') {
  if (-not (Test-Path $HiddenWait)) { throw "FAIL CLOSED: hidden launcher missing: $HiddenWait" }
  $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "//B //Nologo `"$HiddenWait`" `"$RoleScript`" -Role Primary" -WorkingDirectory $codeDir
} else {
  $action = New-ScheduledTaskAction -Execute $Pwsh -Argument $arguments -WorkingDirectory $codeDir
}
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $at
# primary-daemon / backup-tick are one-shot plan/run, not a loop. Repeat through the session.
# 1-minute primary ticks stay inside scan (10 min) and exit (3 min) late windows.
# ExecutionTimeLimit 12 min lets a slow scan finish but kills a hung tick so IgnoreNew cannot block the rest of the day.
if ($Role -eq 'Primary') {
  $rep = New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Hours 7 -Minutes 30)
  $trigger.Repetition = $rep.Repetition
}
if ($Role -eq 'Backup') {
  $rep = New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 7 -Minutes 25)
  $trigger.Repetition = $rep.Repetition
}
$limit = if ($Role -eq 'Primary') { New-TimeSpan -Minutes 12 } else { New-TimeSpan -Minutes 25 }
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit $limit
$principal = $null
foreach ($srcName in @('ORB v4 PAPER Failover Watchdog', 'ORB Box Heartbeat Check')) {
  if ($srcName -eq $Recovery) { continue }
  $src = Get-ScheduledTask -TaskName $srcName -ErrorAction SilentlyContinue
  if ($src) { $principal = $src.Principal; break }
}
if (-not $principal) {
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "ORB v4 PAPER $Role. Paper only. Does not replace $Recovery." | Out-Null
$registered = Get-ScheduledTask -TaskName $taskName
if ($registered.TaskName -eq $Recovery) { throw "refusing to touch $Recovery" }
Write-Output "$taskName : $($registered.State)"
exit 0
