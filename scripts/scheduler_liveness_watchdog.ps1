<#
.SYNOPSIS
AGT scheduler daemon liveness watchdog. Sprint A MR2.

.DESCRIPTION
Reads the latest 'agt_scheduler' heartbeat from daemon_heartbeat, computes
age in seconds. If older than StaleSecs (default 120, matches 2 missed
60s beats per DT Q3 90s TTL with margin), enqueues a crit alert on
cross_daemon_alerts AND invokes scripts\respawn_scheduler.ps1 to spawn
a new daemon. If the row is missing entirely (age=NULL) the watchdog
both enqueues and respawns.

Registered by scripts/register_watchdog_schtask.ps1 as
AGT_Scheduler_Liveness_Watchdog running every 5 min. Respawn is guarded
by a process-existence check (Get-CimInstance Win32_Process) so repeat
ticks while the daemon initializes do not fork a second instance and
collide on clientId=2.

Reads DB read-only. Writes are guarded by busy_timeout=15000 per FU-A
convention. Never touches IB. Never touches the bot process.

.NOTES
- Exit 0: healthy, or alert+respawn succeeded.
- Exit 1: could not open DB.
- Exit 2: alert insert failed after DB read.
- Exit 3: respawn failed (alert was still enqueued).
#>

param(
    [string]$DbPath       = "C:\AGT_Telegram_Bridge\agt_desk.db",
    [int]   $StaleSecs    = 120,
    [string]$LogFile      = "C:\AGT_Telegram_Bridge\logs\scheduler_liveness_watchdog.log",
    [string]$RespawnScript = "C:\AGT_Telegram_Bridge\scripts\respawn_scheduler.ps1",
    [string]$DaemonName   = "agt_scheduler"
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

if (-not (Test-Path $DbPath)) {
    Write-Log "ERROR db_missing path=$DbPath"
    exit 1
}

# Read heartbeat age via python (read-only URI). sqlite3.exe fallback
# omitted here because the scheduler watchdog always ships alongside
# agt_scheduler.py which requires Python anyway.
$py = @"
import sqlite3, sys
try:
    conn = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
    conn.execute('PRAGMA busy_timeout=15000')
    row = conn.execute(
        "SELECT CAST((julianday('now') - julianday(last_beat_utc)) * 86400.0 AS INTEGER), "
        "COALESCE(pid, -1) FROM daemon_heartbeat WHERE daemon_name=?",
        (sys.argv[2],),
    ).fetchone()
    if row is None:
        print('-1|-1')
    else:
        print(f'{row[0] if row[0] is not None else -1}|{row[1]}')
except Exception as e:
    print(f'ERR|{e}')
    sys.exit(1)
"@

$out = $null
try { $out = & py -3 -c $py $DbPath $DaemonName 2>$null } catch {}
if ([string]::IsNullOrWhiteSpace($out)) {
    try { $out = & python -c $py $DbPath $DaemonName 2>$null } catch {}
}
if ([string]::IsNullOrWhiteSpace($out)) {
    Write-Log "ERROR no_python_or_read_failed"
    exit 1
}

$parts = ($out.Trim() -split '\|')
if ($parts.Length -lt 2) { $parts = ($out.Trim() -split '\s+') }
try { $ageSec = [int]$parts[0] } catch { $ageSec = -1 }
try { $pid_   = [int]$parts[1] } catch { $pid_   = -1 }

Write-Log "watchdog read age_sec=$ageSec pid=$pid_ daemon=$DaemonName"

if ($ageSec -ge 0 -and $ageSec -le $StaleSecs) {
    exit 0
}

# ---------------------------------------------------------------------------
# Stale or missing. Enqueue crit alert on cross_daemon_alerts.
# ---------------------------------------------------------------------------
$reason = if ($ageSec -lt 0) { "heartbeat row missing" } else { "heartbeat age ${ageSec}s > ${StaleSecs}s" }
$subject = "Scheduler liveness watchdog: $reason"
$body    = "pid_in_row=$pid_ age_sec=$ageSec threshold=${StaleSecs}s"

$subjectEsc = $subject -replace '"','\"'
$bodyEsc    = $body    -replace '"','\"'
$payload    = "{""subject"":""$subjectEsc"",""body"":""$bodyEsc"",""watchdog"":""scheduler_liveness"",""age_sec"":$ageSec}"
$payloadSql = $payload.Replace("'", "''")

$nowTs   = [int][double]::Parse((Get-Date -UFormat %s))
$insertQ = "INSERT INTO cross_daemon_alerts(created_ts, kind, severity, payload_json, status, attempts) VALUES ($nowTs, 'SCHEDULER_STALE', 'crit', '$payloadSql', 'pending', 0);"

$pyIns = @"
import sqlite3, sys
q = sys.argv[2]
try:
    conn = sqlite3.connect(sys.argv[1])
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute(q)
    conn.commit()
    conn.close()
except Exception as e:
    print(f'ERR {e}')
    sys.exit(1)
"@

$insertFailed = $false
try {
    $ret = & py -3 -c $pyIns $DbPath $insertQ 2>&1
    if ($LASTEXITCODE -ne 0) { $insertFailed = $true }
} catch {
    try {
        $ret = & python -c $pyIns $DbPath $insertQ 2>&1
        if ($LASTEXITCODE -ne 0) { $insertFailed = $true }
    } catch { $insertFailed = $true }
}

if ($insertFailed) {
    Write-Log "ERROR alert_insert_failed"
    # Still attempt respawn; alerting is best-effort.
}

Write-Log "STALE age_sec=$ageSec -- SCHEDULER_STALE crit alert enqueued=$(-not $insertFailed)"

# ---------------------------------------------------------------------------
# Respawn guard: skip if an agt_scheduler.py process is already live.
# ---------------------------------------------------------------------------
$existing = $null
try {
    $existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'agt_scheduler\.py' }
} catch {
    Write-Log "WARN cim_query_failed: $($_.Exception.Message)"
}

if ($existing) {
    $procPids = ($existing | ForEach-Object { $_.ProcessId }) -join ','
    Write-Log "respawn skipped: agt_scheduler.py already running pid=$procPids (heartbeat stalled but process live)"
    exit 0
}

if (-not (Test-Path $RespawnScript)) {
    Write-Log "ERROR respawn_script_missing path=$RespawnScript"
    exit 3
}

try {
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","`"$RespawnScript`"" `
        -WindowStyle Hidden `
        -PassThru | Out-Null
    Write-Log "respawn invoked: $RespawnScript"
    exit 0
} catch {
    Write-Log "ERROR respawn_invoke_failed: $($_.Exception.Message)"
    exit 3
}
