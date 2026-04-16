<#
.SYNOPSIS
AGT bot liveness watchdog. MR #2.

.DESCRIPTION
Reads the most recent 'agt_bot' heartbeat from daemon_heartbeat, computes
age in seconds. If older than STALE_SECS (default 180), enqueues a crit
alert on cross_daemon_alerts so the bot's own drain loop surfaces it to
Telegram + Gmail drafts. If the row is missing entirely (age=NULL) the
watchdog also enqueues.

Registered by scripts/register_watchdog_schtask.ps1 to run every 5 min
during RTH. Safe to run outside RTH - the only side effect on a stale
detection is a row in cross_daemon_alerts, which is cheap.

Reads DB read-only. Writes are guarded by a single INSERT with
busy_timeout=15000 per FU-A convention. Never touches IB. Never touches
the bot process directly.

.NOTES
- Exit 0: healthy or alert enqueued successfully.
- Exit 1: could not open DB (disk or path problem - log + return).
- Exit 2: alert insert failed after DB read succeeded.
#>

param(
    [string]$DbPath    = "C:\AGT_Telegram_Bridge\agt_desk.db",
    [int]   $StaleSecs = 180,
    [string]$LogFile   = "C:\AGT_Telegram_Bridge\logs\bot_liveness_watchdog.log"
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

if ($sqlite) {
    try {
        $null = & $sqlite.Path $DbPath ".timeout 15000" $insertQ 2>$null
    } catch {
        Write-Log "ERROR alert_insert sqlite3 $_"
        exit 2
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
    } catch {
        try {
            $out = & python -c $pyIns $DbPath $insertQ 2>$null
        } catch {
            Write-Log "ERROR alert_insert no_python"
            exit 2
        }
    }
}

Write-Log "STALE age_sec=$ageSec -- BOT_STALE crit alert enqueued"
exit 0
