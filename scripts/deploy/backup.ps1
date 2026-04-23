# SQLite hot backup via Python sqlite3.backup() — WAL-safe, no external binary required.
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

$venv_python = "C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe"
$backup_script = (Resolve-Path (Join-Path $PSScriptRoot "..\backup_db.py")).Path

& $venv_python $backup_script $DbPath $out
if ($LASTEXITCODE -ne 0) { throw "backup_db.py failed (exit $LASTEXITCODE)" }

$size = (Get-Item $out).Length
Write-Host "Backup OK: $out ($([math]::Round($size/1MB,2)) MB)"

# Retention: keep last 30 timestamped backups
Get-ChildItem $BackupDir -Filter "agt_desk_*.db" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue
