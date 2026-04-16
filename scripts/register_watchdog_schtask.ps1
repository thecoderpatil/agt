<#
.SYNOPSIS
One-shot schtasks registrar for bot_liveness_watchdog.ps1. MR #2.

.DESCRIPTION
Registers a scheduled task "AGT_Bot_Liveness_Watchdog" that runs the
watchdog every 5 minutes, 7 days a week. The watchdog itself is cheap
(single SQLite read + optional INSERT), so we skip RTH gating and let
it always run - if the bot is down outside RTH that's still useful
signal, and a stale row that persists past RTH open would otherwise
get noticed by the first human /report anyway.

Usage (from an elevated PowerShell):
    .\scripts\register_watchdog_schtask.ps1

Idempotent: deletes any prior task with the same name first.

.NOTES
Does NOT modify boot_desk.bat (project: boot_desk.bat is on the
absolute-do-not-touch list). Operator runs this once after deploy.
#>

param(
    [string]$TaskName = "AGT_Bot_Liveness_Watchdog",
    [string]$ScriptPath = "C:\AGT_Telegram_Bridge\scripts\bot_liveness_watchdog.ps1",
    [int]   $IntervalMinutes = 5
)

if (-not (Test-Path $ScriptPath)) {
    Write-Error "Watchdog script not found: $ScriptPath"
    exit 1
}

# Idempotent: remove any pre-existing task with the same name.
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed prior task '$TaskName'"
    }
} catch {
    Write-Host "No prior task to remove."
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

# Repeat every N minutes, indefinitely, starting 1 min from now.
$start = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -User $env:USERNAME `
    -Description "AGT bot heartbeat liveness watchdog - MR #2" | Out-Null

Write-Host "Registered '$TaskName' - every $IntervalMinutes min starting $start"
Write-Host "Log: C:\AGT_Telegram_Bridge\logs\bot_liveness_watchdog.log"
