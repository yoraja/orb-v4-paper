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
$Secrets = Join-Path $Base 'paper-secrets.json'
if (-not $env:ORB_V4_PAPER_KEYS_FILE) { $env:ORB_V4_PAPER_KEYS_FILE = $Secrets }
if (-not $env:ORB_V4_OWNERSHIP_REMOTE) { $env:ORB_V4_OWNERSHIP_REMOTE = 'https://github.com/yoraja/orb-v4-paper.git' }
if (-not $env:ORB_V4_OWNERSHIP_BRANCH) { $env:ORB_V4_OWNERSHIP_BRANCH = 'ownership' }
if (-not $env:ORB_V4_OWNERSHIP_WORKDIR) { $env:ORB_V4_OWNERSHIP_WORKDIR = Join-Path $Base 'run\ownership-git' }
$env:ORB_V4_ENGINE_ALLOW = '1'
if ($Role -eq 'Backup') { $env:ORB_V4_ALLOW_HEARTBEAT_FETCH = '1' }
$env:ORB_V4_HOME = Join-Path $Base 'code'
$env:ORB_V4_RUN = Join-Path $Base 'run'
if (-not $env:ORB_V4_PAGES) { $env:ORB_V4_PAGES = Join-Path $Base 'pages' }
if (-not $env:ORB_DASHBOARD_PASSWORD) {
  $DashPw = Join-Path $Base 'dashboard-password.txt'
  if (Test-Path $DashPw) { $env:ORB_DASHBOARD_PASSWORD = (Get-Content -LiteralPath $DashPw -Raw).Trim() }
}
$env:PYTHONUTF8 = '1'
$env:TZ = 'America/Los_Angeles'
# Task Scheduler often starts with an empty WorkingDirectory (cwd = System32).
# paper_broker is not installed in venv-paper, so python -m needs ORB_V4_HOME on
# sys.path. This was the 2026-11-11 05:55 PT Primary failure (exit 1, no logs).
if ($env:PYTHONPATH) {
  $env:PYTHONPATH = "$($env:ORB_V4_HOME);$($env:PYTHONPATH)"
} else {
  $env:PYTHONPATH = $env:ORB_V4_HOME
}
Set-Location -LiteralPath $env:ORB_V4_HOME

function Write-RoleLog([string]$msg) {
  try {
    $dir = Join-Path $Base 'logs'
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') PT $msg"
    Add-Content -LiteralPath (Join-Path $dir "role-$Role.log") -Value $line -Encoding UTF8
  } catch {}
}

function Get-V4TickBudgetSeconds {
  # 8-minute cap, or sooner if a close slot is imminent. Fallback 480 if the helper fails.
  $budget = 480
  try {
    $out = & $Py -m paper_broker.failover.tick_budget --seconds
    if ($LASTEXITCODE -eq 0 -and "$out" -match '^[0-9]+$') { $budget = [int]$out }
  } catch {}
  return $budget
}

function Invoke-V4Tick([string[]]$PyArgs, [string]$label) {
  $budget = Get-V4TickBudgetSeconds
  Write-RoleLog "start $label budget_s=$budget cwd=$(Get-Location) py=$Py home=$($env:ORB_V4_HOME)"
  $p = Start-Process -FilePath $Py -ArgumentList $PyArgs -WorkingDirectory $env:ORB_V4_HOME -PassThru -NoNewWindow
  if (-not $p.WaitForExit($budget * 1000)) {
    Write-RoleLog "tick timeout after ${budget}s — killing pid=$($p.Id) and children"
    try { & taskkill /PID $p.Id /T /F | Out-Null } catch {
      try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    try { & $Py -m paper_broker.failover.tick_budget --release-locks | Out-Null } catch {}
    Write-RoleLog "$label timeout exit=124"
    exit 124
  }
  $rc = $p.ExitCode
  Write-RoleLog "$label exit=$rc"
  exit $rc
}

Write-RoleLog "wrapper start Role=$Role DryRun=$DryRun cwd=$(Get-Location) py=$Py home=$($env:ORB_V4_HOME)"
if ($Role -eq 'SelfTest') {
  & $Py -m paper_broker.failover.cli self-test
  $rc = $LASTEXITCODE
  Write-RoleLog "self-test exit=$rc"
  exit $rc
}
if ($Role -eq 'Primary') {
  $env:ORB_V4_ROLE = 'primary'
  $env:ORB_V4_HOST = 'yopc'
  Remove-Item Env:ORB_V4_NO_PUBLISH -ErrorAction SilentlyContinue
  $pyArgs = @('-m', 'paper_broker.failover.cli', 'primary-daemon', '--role', 'primary', '--host', 'yopc')
  if ($DryRun) { $pyArgs += '--dry-run' }
  Invoke-V4Tick $pyArgs 'primary-daemon'
}
$env:ORB_V4_ROLE = 'backup'
$env:ORB_V4_HOST = 'box'
$pyArgs = @('-m', 'paper_broker.failover.cli', 'backup-tick', '--role', 'backup', '--host', 'box')
if ($DryRun) { $pyArgs += '--dry-run' }
Invoke-V4Tick $pyArgs 'backup-tick'
