<#
.SYNOPSIS
AGT bot liveness watchdog. MR #2 + Sprint A MR2 respawn.

.DESCRIPTION
Reads the most recent 'agt_bot' heartbeat from daemon_heartbeat, computes
age in seconds. If older than StaleSecs (default 180), enqueues a crit
alert on cross_daemon_alerts so the bot's own drain loop surfaces it to
Telegram + Gmail drafts. If the row is missing entirely (age=NULL) the
watchdog also enqueues. If the alert was enqueued AND no python process
is running telegram_bot.py, this script then respawns the bot via
scripts\respawn_bot.ps1 (Sprint A MR2 addition -- was enqueue-only in
the original MR #2 ship).

Registered by scripts/register_watchdog_schtask.ps1 to run every 5 min
during RTH. Safe to run outside RTH - the only side effect on a stale
detection is a row in cross_daemon_alerts + a headless respawn.

Reads DB read-only. Writes are guarded by a single INSERT with
busy_timeout=15000 per FU-A convention. Never touches IB. Respawn is
guarded by a process-existence check (Get-CimInstance Win32_Process) so
repeat ticks while init is in flight do not fork a second instance and
collide on the singleton lockfile.

.NOTES
- Exit 0: healthy, or alert+respawn succeeded (or respawn skipped due to
  live process).
- Exit 1: could not open DB.
- Exit 2: alert insert failed after DB read.
- Exit 3: respawn invocation failed (alert was still enqueued).
#>

param(
    [string]$DbPath        = "C:\AGT_Telegram_Bridge\agt_desk.db",
    [int]   $StaleSecs     = 180,
    [string]$LogFile       = "C:\AGT_Telegram_Bridge\logs\bot_liveness_watchdog.log",
    [string]$RespawnScript = "C:\AGT_Telegram_Bridge\scripts\respawn_bot.ps1"
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

# Prefer sqlite3.exe in PATH; fall back to py -c invocation.
$sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
if ($sqlite) {
    $readQ = "SELECT COALESCE(CAST((julianday('now') - julianday(last_beat_utc)) * 86400.0 AS INTEGER), -1), COALESCE(pid, -1) FROM daemon_heartbeat WHERE daemon_name='agt_bot';"
    try {
        $out = & $sqlite.Path $DbPath $readQ 2>$null
    } catch {
        Write-Log "ERROR sqlite_read $_"
        exit 1
    }
} else {
    # Python fallback - uses uri read-only for safety.
    $py = @"
import sqlite3, sys
try:
    conn = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
    conn.execute('PRAGMA busy_timeout=15000')
    row = conn.execute(
        \"SELECT CAST((julianday('now') - julianday(last_beat_utc)) * 86400.0 AS INTEGER), \"
        \"COALESCE(pid, -1) FROM daemon_heartbeat WHERE daemon_name='agt_bot'\"
    ).fetchone()
    if row is None:
        print('-1|-1')
    else:
        print(f'{row[0] if row[0] is not None else -1}|{row[1]}')
except Exception as e:
    print(f'ERR|{e}')
    sys.exit(1)
"@
    try {
        $out = & py -3 -c $py $DbPath 2>$null
    } catch {
        try {
            $out = & python -c $py $DbPath 2>$null
        } catch {
            Write-Log "ERROR no_sqlite_or_python"
            exit 1
        }
    }
}

if ([string]::IsNullOrWhiteSpace($out)) {
    # No row at all - treat as stale.
    $ageSec = -1
    $pid_   = -1
} else {
    $parts = ($out.Trim() -split '\|')
    if ($parts.Length -lt 2) { $parts = ($out.Trim() -split '\t') }
    if ($parts.Length -lt 2) { $parts = ($out.Trim() -split '\s+') }
    try {
        $ageSec = [int]$parts[0]
    } catch { $ageSec = -1 }
    try {
        $pid_ = [int]$parts[1]
    } catch { $pid_ = -1 }
}

Write-Log "watchdog read age_sec=$ageSec pid=$pid_"

if ($ageSec -ge 0 -and $ageSec -le $StaleSecs) {
    # Healthy - no alert.
    exit 0
}

# Stale or missing - enqueue a crit alert.
$reason = if ($ageSec -lt 0) { "heartbeat row missing" } else { "heartbeat age ${ageSec}s > ${StaleSecs}s" }
$subject = "Bot liveness watchdog: $reason"
$body    = "pid_in_row=$pid_ age_sec=$ageSec threshold=${StaleSecs}s"

# Escape single quotes for SQL literal.
$subjectSql = $subject.Replace("'", "''")
$bodySql    = $body.Replace("'", "''")
$payload    = "{\"subject\":\"$($subject -replace '"','\"')\",\"body\":\"$($body -replace '"','\"')\",\"watchdog\":\"bot_liveness\",\"age_sec\":$ageSec}"
$payloadSql = $payload.Replace("'", "''")

$nowTs      = [int][double]::Parse((Get-Date -UFormat %s))

$insertQ = "INSERT INTO cross_daemon_alerts(created_ts, kind, severity, payload_json, status, attempts) VALUES ($nowTs, 'BOT_STALE', 'crit', '$payloadSql', 'pending', 0);"

$insertFailed = $false
if ($sqlite) {
    try {
        $null = & $sqlite.Path $DbPath ".timeout 15000" $insertQ 2>$null
        if ($LASTEXITCODE -ne 0) { $insertFailed = $true }
    } catch {
        Write-Log "ERROR alert_insert sqlite3 $_"
        $insertFailed = $true
    }
} else {
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
    try {
        $out = & py -3 -c $pyIns $DbPath $insertQ 2>$null
        if ($LASTEXITCODE -ne 0) { $insertFailed = $true }
    } catch {
        try {
            $out = & python -c $pyIns $DbPath $insertQ 2>$null
            if ($LASTEXITCODE -ne 0) { $insertFailed = $true }
        } catch {
            Write-Log "ERROR alert_insert no_python"
            $insertFailed = $true
        }
    }
}

Write-Log "STALE age_sec=$ageSec -- BOT_STALE crit alert enqueued=$(-not $insertFailed)"

# ---------------------------------------------------------------------------
# Sprint A MR2: respawn the bot if no python telegram_bot.py is running.
# Guarded by Get-CimInstance so repeat ticks during init don't fork a
# second instance (singleton lockfile would abort the dupe anyway, but
# noisy log spam).
# ---------------------------------------------------------------------------
$existing = $null
try {
    $existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'telegram_bot\.py' }
} catch {
    Write-Log "WARN cim_query_failed: $($_.Exception.Message)"
}

if ($existing) {
    $procPids = ($existing | ForEach-Object { $_.ProcessId }) -join ','
    Write-Log "respawn skipped: telegram_bot.py already running pid=$procPids (heartbeat stalled but process live)"
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
