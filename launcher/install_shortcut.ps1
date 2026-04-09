# install_shortcut.ps1 — Create "AGT Cure Console" desktop shortcut
# Run: powershell -ExecutionPolicy Bypass -File launcher\install_shortcut.ps1
# Idempotent: deletes existing shortcut if present, recreates.

$ErrorActionPreference = "Stop"

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "AGT Cure Console.lnk"
$TargetPath = "C:\AGT_Telegram_Bridge\launcher\AGT_Cure.vbs"
$IconPath = "C:\AGT_Telegram_Bridge\launcher\agt_icon.ico"
$WorkingDir = "C:\AGT_Telegram_Bridge"

# Delete existing shortcut if present
if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
    Write-Host "Removed existing shortcut: $ShortcutPath"
}

# Create shortcut
$WScript = New-Object -ComObject WScript.Shell
$Shortcut = $WScript.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.WorkingDirectory = $WorkingDir
$Shortcut.WindowStyle = 7  # 7 = minimized
$Shortcut.Description = "Launch AGT Cure Console (FastAPI deck + browser)"

if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
} else {
    # Fallback: use a built-in Windows icon (green globe)
    $Shortcut.IconLocation = "%SystemRoot%\System32\shell32.dll,13"
}

$Shortcut.Save()
Write-Host ""
Write-Host "Desktop shortcut created: $ShortcutPath"
Write-Host "  Target: $TargetPath"
Write-Host "  Icon:   $IconPath"
Write-Host "  Action: Double-click to start Cure Console (no console flash)"
Write-Host ""
