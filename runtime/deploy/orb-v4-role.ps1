# ORB v4 PAPER role wrapper. Paper only. YOPC is primary. Box is the backup.
# Does not register or edit 'ORB v4 PAPER Recovery Close 20261109'.
# Does not pass --live or --arm. Does not call a chat webhook.
# Slot stamps use the preserved ParseExact fix: 'yyyy-MM-ddHHmm' (no space, no colon).
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][ValidateSet('Primary','Backup','SelfTest')][string]$Role,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
foreach ($a in @($args) + $PSBoundParameters.Values) {
  if ("$a" -match '^(--live|--arm|-live|-arm)$') { throw "FAIL CLOSED: refusing $a" }
}
function Convert-V4SlotStamp([string]$day, [string]$hhmm) {
  # 2026-11-06 fix. 'yyyy-MM-dd HH:mm' throws on '2026-11-061045' and blocked takeover slots.
  return [DateTime]::ParseExact($day + $hhmm, 'yyyy-MM-ddHHmm', $null)
}
$Base = if ($env:ORB_V4_BASE) { $env:ORB_V4_BASE } else { 'C:\Users\yogjr\.orb-v4-paper' }
$Py = Join-Path $Base 'venv-paper\Scripts\python.exe'
if (-not (Test-Path $Py)) { throw "PAPER venv-paper python missing ($Py). Do not use a symlinked .venv." }
$env:ORB_BROKER_MODE = 'paper'
$env:ORB_V4_ENGINE_ALLOW = '1'
if ($Role -eq 'Backup') { $env:ORB_V4_ALLOW_HEARTBEAT_FETCH = '1' }
$env:ORB_V4_HOME = Join-Path $Base 'code'
$env:ORB_V4_RUN = Join-Path $Base 'run'
$env:PYTHONUTF8 = '1'
$env:TZ = 'America/Los_Angeles'
if ($Role -eq 'SelfTest') {
  & $Py -m paper_broker.failover.cli self-test
  exit $LASTEXITCODE
}
if ($Role -eq 'Primary') {
  $env:ORB_V4_ROLE = 'primary'
  $env:ORB_V4_HOST = 'yopc'
  Remove-Item Env:ORB_V4_NO_PUBLISH -ErrorAction SilentlyContinue
  & $Py -m paper_broker.failover.cli primary-daemon --role primary --host yopc $(if ($DryRun) { '--dry-run' })
  exit $LASTEXITCODE
}
$env:ORB_V4_ROLE = 'backup'
$env:ORB_V4_HOST = 'box'
& $Py -m paper_broker.failover.cli backup-tick --role backup --host box $(if ($DryRun) { '--dry-run' })
exit $LASTEXITCODE
