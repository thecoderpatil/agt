<#
.SYNOPSIS
Headless bot respawn invoked by bot_liveness_watchdog.ps1. Sprint A MR2.

.DESCRIPTION
Replicates the boot sequence of boot_desk.bat WITHOUT the interactive
pause so the watchdog can respawn the bot cleanly. Does NOT modify
boot_desk.bat (that file is on the absolute-do-not-touch list).

Flow:
  1. cd C:\AGT_Telegram_Bridge
  2. git fetch origin main + git reset --hard origin/main (matches
     boot_desk.bat SSOT policy)
  3. Activate .venv if present
  4. python telegram_bot.py (detached)

The spawned python process keeps running after this script exits.
Watchdog's process-existence check (Get-CimInstance Win32_Process)
prevents double-spawn on next 5-min tick while init is in flight.

.NOTES
Standalone so the watchdog can invoke it via Start-Process -WindowStyle Hidden
without dragging a console window onto the user's screen.
#>

param(
    [string]$RepoRoot = "C:\AGT_Telegram_Bridge",
    [string]$LogFile  = "C:\AGT_Telegram_Bridge\logs\respawn_bot.log"
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

# Sync to origin/main before respawn, matching boot_desk.bat policy.
Write-Log "respawn_bot: git fetch + reset --hard origin/main"
& git fetch origin main --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Log "WARN git_fetch_failed exit=$LASTEXITCODE -- proceeding with current tree"
}
& git reset --hard origin/main --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Log "WARN git_reset_failed exit=$LASTEXITCODE -- proceeding"
}

# Activate venv if available.
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

# Spawn bot detached. Parent (this script) exits immediately.
$exe = Join-Path $RepoRoot "telegram_bot.py"
if (-not (Test-Path $exe)) {
    Write-Log "ERROR telegram_bot_missing path=$exe"
    exit 1
}

try {
    $proc = Start-Process -FilePath "python" `
        -ArgumentList @("telegram_bot.py") `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden `
        -PassThru
    Write-Log "bot respawned pid=$($proc.Id)"
    exit 0
} catch {
    Write-Log "ERROR spawn_failed: $($_.Exception.Message)"
    exit 2
}
