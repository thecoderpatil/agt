# heartbeat_stale_alert.ps1 -- MR !88 external observer.
#
# NSSM (MR1.5) owns bot + scheduler restart. The invariants tick
# (NO_MISSING_DAEMON_HEARTBEAT in agt_equities/invariants/checks.py)
# writes structured incidents when a heartbeat goes stale. But the
# tick runs inside bot-or-scheduler; if both daemons are down at once,
# no tick runs, no incident is written, and the user gets no signal.
#
# This script is scheduled every 5 min via AGT_Heartbeat_Stale_Alert
# under NT AUTHORITY\SYSTEM. It is the only observer with no runtime
# dependency on either Python daemon. If it sees either daemon heartbeat
# older than 300s (or missing), it posts directly to the Telegram Bot
# API, bypassing the cross_daemon_alerts bus (which requires the bot's
# drain loop to be alive -- exactly what we can't assume here).
#
# Dependencies:
#   - venv python for read-only SQLite (no native SQLite in PS 5.1)
#   - .env for TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID (canonical env var
#     name used throughout the codebase: telegram_bot.py:77,
#     vrp_veto.py:725). The architect's MR2 dispatch said CHAT_ID, but
#     CHAT_ID is not the value used anywhere else -- using USER_ID here
#     matches the existing Telegram API body pattern and the actual
#     key present in .env.
#   - api.telegram.org reachable via HTTPS. If blocked, log + non-zero
#     exit; schtask retries on next 5-min tick.
#
# ASCII-only per feedback_ps1_ascii_only.md.

$ErrorActionPreference = 'Continue'

$LogPath = 'C:\AGT_Telegram_Bridge\logs\heartbeat_stale_alert.log'
$EnvPath = 'C:\AGT_Telegram_Bridge\.env'
$PyPath  = 'C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe'
$StaleThresholdSec = 300

function Write-AlertLog {
    param([string]$Message)
    $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Message)
    Add-Content -Path $LogPath -Value $line -ErrorAction SilentlyContinue
}

function Read-EnvKey {
    param([string]$Key)
    if (-not (Test-Path $EnvPath)) { return $null }
    foreach ($rawLine in Get-Content $EnvPath) {
        $trimmed = $rawLine.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $eqIdx = $trimmed.IndexOf('=')
        if ($eqIdx -lt 1) { continue }
        $k = $trimmed.Substring(0, $eqIdx).Trim()
        if ($k -ne $Key) { continue }
        $v = $trimmed.Substring($eqIdx + 1).Trim()
        if ($v.Length -ge 2) {
            $first = $v[0]
            $last  = $v[$v.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $v = $v.Substring(1, $v.Length - 2)
            }
        }
        return $v
    }
    return $null
}

try {
    Write-AlertLog '=== start ==='

    $token  = Read-EnvKey 'TELEGRAM_BOT_TOKEN'
    $userId = Read-EnvKey 'TELEGRAM_USER_ID'
    if (-not $token -or -not $userId) {
        Write-AlertLog 'FAIL: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID missing from .env'
        exit 2
    }
    if (-not (Test-Path $PyPath)) {
        Write-AlertLog ('FAIL: venv python not found at ' + $PyPath)
        exit 3
    }

    # Query heartbeat ages via venv python (no native SQLite in PS 5.1).
    # Outputs one line per target: "<daemon_name>|<age_s_or_MISSING>".
    # NOTE: writing to a temp .py file then invoking `python <file>` rather
    # than `python -c "<script>"`. PS 5.1 strips embedded double-quotes when
    # passing -c args, which mangles the SQLite URI and the f-string. The
    # same class of quoting bug that bit MR1.5's NSSM Invoke-Nssm -- fix is
    # the same: avoid the -c path entirely.
    $pyScript = @'
import sqlite3
conn = sqlite3.connect("file:C:/AGT_Telegram_Bridge/agt_desk.db?mode=ro", uri=True)
for name in ("agt_bot", "agt_scheduler"):
    r = conn.execute(
        "SELECT CAST((julianday('now') - julianday(last_beat_utc)) * 86400 AS INT) "
        "FROM daemon_heartbeat WHERE daemon_name=?",
        (name,),
    ).fetchone()
    age = r[0] if r else None
    print(f"{name}|{age if age is not None else 'MISSING'}")
'@

    $pyFile = Join-Path $env:TEMP ('hb_stale_query_{0}.py' -f (Get-Random))
    Set-Content -Path $pyFile -Value $pyScript -Encoding ASCII
    try {
        $queryOut = & $PyPath $pyFile 2>&1
    } finally {
        Remove-Item $pyFile -Force -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0) {
        Write-AlertLog ('FAIL: python query exit=' + $LASTEXITCODE + ' output=' + ($queryOut -join ' | '))
        exit 4
    }
    Write-AlertLog ('query: ' + ($queryOut -join ' ; '))

    $alerts = @()
    foreach ($rawRow in $queryOut) {
        $parts = ([string]$rawRow) -split '\|', 2
        if ($parts.Count -ne 2) { continue }
        $daemon = $parts[0].Trim()
        $ageRaw = $parts[1].Trim()
        if ($ageRaw -eq 'MISSING') {
            $alerts += ('HEARTBEAT_MISSING daemon=' + $daemon)
            continue
        }
        [int]$ageInt = 0
        if (-not [int]::TryParse($ageRaw, [ref]$ageInt)) { continue }
        if ($ageInt -gt $StaleThresholdSec) {
            $alerts += ('HEARTBEAT_STALE daemon=' + $daemon + ' age=' + $ageInt + 's')
        }
    }

    if ($alerts.Count -eq 0) {
        Write-AlertLog 'ok: all heartbeats within threshold'
        exit 0
    }

    $postFailed = $false
    $url = 'https://api.telegram.org/bot' + $token + '/sendMessage'
    foreach ($alertText in $alerts) {
        $stamp = Get-Date -Format 'o'
        $text = $alertText + ' at ' + $stamp
        $body = (@{ chat_id = $userId; text = $text } | ConvertTo-Json -Compress)
        Write-AlertLog ('POST: ' + $text)
        try {
            $resp = Invoke-RestMethod -Uri $url -Method Post `
                -ContentType 'application/json' -Body $body -TimeoutSec 15
            Write-AlertLog ('ok: telegram ok=' + $resp.ok)
        } catch {
            Write-AlertLog ('FAIL: telegram post error: ' + $_.Exception.Message)
            $postFailed = $true
        }
    }

    if ($postFailed) { exit 5 }
    exit 0
} catch {
    Write-AlertLog ('FATAL: ' + $_.Exception.Message)
    exit 1
}
