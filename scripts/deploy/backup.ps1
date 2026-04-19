# SQLite hot backup via VACUUM INTO — safe under WAL, does not block writers.
param(
    [string]$DbPath = "C:\AGT_Telegram_Bridge\agt_desk.db",
    [string]$BackupDir = "C:\AGT_Runtime\backups",
    [string]$Label = ""
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $DbPath)) { throw "DB not found at $DbPath" }
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$suffix = if ($Label) { "_$Label" } else { "" }
$out = Join-Path $BackupDir "agt_desk_${ts}${suffix}.db"
$outForwardSlash = $out.Replace('\','/')

# Locate sqlite3.exe — prefer venv shim, fall back to PATH
$sqlite = "C:\AGT_Telegram_Bridge\.venv\Scripts\sqlite3.exe"
if (-not (Test-Path $sqlite)) { $sqlite = "sqlite3.exe" }

& $sqlite $DbPath "VACUUM INTO '$outForwardSlash';"
if ($LASTEXITCODE -ne 0) { throw "VACUUM INTO failed (exit $LASTEXITCODE)" }

$size = (Get-Item $out).Length
Write-Host "Backup OK: $out ($([math]::Round($size/1MB,2)) MB)"

# Retention: keep last 30 timestamped backups
Get-ChildItem $BackupDir -Filter "agt_desk_*.db" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue
