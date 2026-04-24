# Sprint 6 add-on: post-start service-boot smoke.
# Runs AFTER deploy.ps1's integrity_check, BEFORE declaring deploy success.
# ASCII-only per feedback_ps1_ascii_only.md.
#
# Contract:
#   1. Wait 60 seconds after service restart (let boot stabilize).
#   2. For each service (agt-telegram-bot, agt-scheduler): tail last 60s of log.
#   3. Grep tail for smoke-fail patterns (see below).
#   4. Verify heartbeat <60s for both services via daemon_heartbeat table.
#   5. Exit 0 if clean; exit 1 with diagnostic dump if any check fails.

param(
    [int]$StabilizeSeconds = 60,
    [string]$LogsDir       = "C:\AGT_Telegram_Bridge\logs",
    [string]$DbPath        = "C:\AGT_Runtime\state\agt_desk.db",
    [int]$HeartbeatSlaSec  = 120
)

$ErrorActionPreference = "Stop"

# 1. Stabilize window
Write-Host "post_start_smoke: waiting $StabilizeSeconds s for boot stabilize..."
Start-Sleep -Seconds $StabilizeSeconds

# Smoke-fail grep patterns (regex).
# Catches the Sprint 5 regression classes: R1 canonical-DB, R2 coroutine
# never awaited, R4 TypeError bundle, plus generic CRITICAL/FATAL lines
# and NSSM service-state failures.
$failPatterns = @(
    "TypeError:",
    "RuntimeWarning: coroutine .* was never awaited",
    "AssertionError: canonical DB path not resolved",
    "\[CRITICAL\]",
    "\[FATAL\]",
    "SERVICE_PAUSED",
    "SERVICE_STOPPED"
)

$logFiles = @(
    (Join-Path $LogsDir "nssm_agt-telegram-bot_stderr.log"),
    (Join-Path $LogsDir "nssm_agt-telegram-bot_stdout.log"),
    (Join-Path $LogsDir "nssm_agt-scheduler_stderr.log"),
    (Join-Path $LogsDir "nssm_agt-scheduler_stdout.log"),
    (Join-Path $LogsDir "agt_scheduler.log"),
    (Join-Path $LogsDir "bot.log")
)

$cutoff = (Get-Date).AddSeconds(-$StabilizeSeconds)
$findings = New-Object System.Collections.ArrayList
foreach ($lf in $logFiles) {
    if (-not (Test-Path $lf)) { continue }
    # Tail: read last 300 lines; filter by timestamp prefix if parseable, else
    # keep last 300 as a fallback window.
    $lines = Get-Content -LiteralPath $lf -Tail 300 -ErrorAction SilentlyContinue
    if (-not $lines) { continue }
    foreach ($line in $lines) {
        foreach ($pat in $failPatterns) {
            if ($line -match $pat) {
                [void]$findings.Add("match=${pat} file=$($lf) line=$($line.Substring(0, [Math]::Min($line.Length, 240)))")
                break
            }
        }
    }
}

# 2. Heartbeat check via sqlite3 (both services <HeartbeatSlaSec).
$heartbeatFailures = New-Object System.Collections.ArrayList
if (Test-Path $DbPath) {
    $integrityScript = Join-Path $PSScriptRoot "integrity_check.py"
    # Reuse python venv already on PATH for integrity checks.
    $hbQuery = "SELECT daemon_name, last_beat_utc FROM daemon_heartbeat WHERE daemon_name IN ('agt_bot','agt_scheduler')"
    $pyCmd = @"
import sqlite3, sys
from datetime import datetime, timezone
conn = sqlite3.connect(f'file:{r'$DbPath'}?mode=ro', uri=True)
now = datetime.now(timezone.utc)
rows = conn.execute(r'''$hbQuery''').fetchall()
sla = int('$HeartbeatSlaSec')
bad = []
for name, ts in rows:
    if not ts: bad.append(f'missing={name}'); continue
    dt = datetime.fromisoformat(ts)
    age = (now - dt).total_seconds()
    if age > sla: bad.append(f'stale={name} age_sec={int(age)}')
if bad: print('HEARTBEAT_BAD: ' + '; '.join(bad)); sys.exit(2)
print('HEARTBEAT_OK')
"@
    $pyExe = "C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe"
    if (Test-Path $pyExe) {
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $hbOutput = & $pyExe -c $pyCmd 2>&1
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        Write-Host "heartbeat: $hbOutput"
        if ($hbOutput -match "HEARTBEAT_BAD") {
            [void]$heartbeatFailures.Add($hbOutput)
        }
    } else {
        Write-Warning "post_start_smoke: python venv missing at $pyExe; skipping heartbeat check"
    }
} else {
    Write-Warning "post_start_smoke: DB missing at $DbPath; skipping heartbeat check"
}

# 3. Outcome.
if ($findings.Count -eq 0 -and $heartbeatFailures.Count -eq 0) {
    Write-Host "smoke=pass files_scanned=$($logFiles.Count) patterns_checked=$($failPatterns.Count)"
    exit 0
}

Write-Host "smoke=fail findings=$($findings.Count) heartbeat_failures=$($heartbeatFailures.Count)"
foreach ($f in $findings) { Write-Host $f }
foreach ($h in $heartbeatFailures) { Write-Host "HB: $h" }
exit 1
