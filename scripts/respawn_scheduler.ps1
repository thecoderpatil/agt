<#
.SYNOPSIS
Headless scheduler respawn invoked by scheduler_liveness_watchdog.ps1.
Sprint A MR2.

.DESCRIPTION
Replicates scripts\boot_scheduler.bat WITHOUT the interactive pause so
the watchdog can respawn the daemon cleanly. Forces USE_SCHEDULER_DAEMON=1
and SCHEDULER_IB_CLIENT_ID=2 in process scope.

Flow:
  1. cd C:\AGT_Telegram_Bridge (no git reset -- bot owns working tree sync)
  2. Activate .venv if present
  3. set USE_SCHEDULER_DAEMON=1, SCHEDULER_IB_CLIENT_ID=2
  4. python agt_scheduler.py (detached)

The spawned python process keeps running after this script exits.
Watchdog's Get-CimInstance process-existence check prevents double-spawn
and therefore a clientId=2 collision at the IB gateway.

.NOTES
WARNING: during the pre-MR4 observation window, if the bot still has
USE_SCHEDULER_DAEMON=0 in its env, this respawn will start a scheduler
that double-executes the gated jobs. Operator discipline per .env.example.
#>

param(
    [string]$RepoRoot = "C:\AGT_Telegram_Bridge",
    [string]$LogFile  = "C:\AGT_Telegram_Bridge\logs\respawn_scheduler.log"
)

$ErrorActionPreference = "Continue"

function Write-Log {
    param([string]$msg)
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    $line = "$ts  $msg"
    try {
        $dir = Split-Path -Parent $LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
    } catch {}
    Write-Host $line
}

Set-Location $RepoRoot

# No git reset here: the bot (boot_desk.bat) owns working-tree sync.
# Competing resets from two daemons corrupts the tree.

$venvActivate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    try {
        . $venvActivate
        Write-Log "venv activated"
    } catch {
        Write-Log "WARN venv_activate_failed: $($_.Exception.Message)"
    }
} else {
    Write-Log "no .venv -- using system Python"
}

$exe = Join-Path $RepoRoot "agt_scheduler.py"
if (-not (Test-Path $exe)) {
    Write-Log "ERROR agt_scheduler_missing path=$exe"
    exit 1
}

# Cutover flag + client id injected via parent-env. Start-Process
# inherits the parent PowerShell session env by default.
$env:USE_SCHEDULER_DAEMON   = "1"
$env:SCHEDULER_IB_CLIENT_ID = "2"
Write-Log "env: USE_SCHEDULER_DAEMON=1 SCHEDULER_IB_CLIENT_ID=2"

try {
    $proc = Start-Process -FilePath "python" `
        -ArgumentList @("agt_scheduler.py") `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden `
        -PassThru
    Write-Log "scheduler respawned pid=$($proc.Id)"
    exit 0
} catch {
    Write-Log "ERROR spawn_failed: $($_.Exception.Message)"
    exit 2
}
