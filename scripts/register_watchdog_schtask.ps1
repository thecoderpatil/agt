<#
.SYNOPSIS
One-shot schtasks registrar for watchdogs. MR #2 + Sprint A MR2.

.DESCRIPTION
Registers two scheduled tasks, each running every 5 minutes 7 days a week:

  1. AGT_Bot_Liveness_Watchdog       -> scripts\bot_liveness_watchdog.ps1
  2. AGT_Scheduler_Liveness_Watchdog -> scripts\scheduler_liveness_watchdog.ps1

The watchdogs themselves are cheap (single SQLite read + optional INSERT +
optional respawn), so we skip RTH gating and let them always run. A stale
row that persists past RTH open would otherwise get noticed by the first
human /report anyway.

Usage (from an elevated PowerShell):
    .\scripts\register_watchdog_schtask.ps1

Idempotent: deletes any prior task with each name first.

.NOTES
Does NOT modify boot_desk.bat (absolute do-not-touch list). Operator runs
this once after deploy, or again after any watchdog path change.
#>

param(
    [string]$BotTaskName         = "AGT_Bot_Liveness_Watchdog",
    [string]$BotScriptPath       = "C:\AGT_Telegram_Bridge\scripts\bot_liveness_watchdog.ps1",
    [string]$SchedulerTaskName   = "AGT_Scheduler_Liveness_Watchdog",
    [string]$SchedulerScriptPath = "C:\AGT_Telegram_Bridge\scripts\scheduler_liveness_watchdog.ps1",
    [int]   $IntervalMinutes     = 5
)

function Register-WatchdogTask {
    param(
        [Parameter(Mandatory)] [string]$TaskName,
        [Parameter(Mandatory)] [string]$ScriptPath,
        [Parameter(Mandatory)] [int]   $IntervalMinutes,
        [Parameter(Mandatory)] [string]$Description
    )

    if (-not (Test-Path $ScriptPath)) {
        Write-Error "Watchdog script not found: $ScriptPath"
        return $false
    }

    # Idempotent: remove any pre-existing task with the same name.
    try {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Removed prior task '$TaskName'"
        }
    } catch {
        Write-Host "No prior task to remove for '$TaskName'."
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
        -Description $Description | Out-Null

    Write-Host "Registered '$TaskName' - every $IntervalMinutes min starting $start"
    return $true
}

$okBot = Register-WatchdogTask `
    -TaskName        $BotTaskName `
    -ScriptPath      $BotScriptPath `
    -IntervalMinutes $IntervalMinutes `
    -Description     "AGT bot heartbeat liveness watchdog - MR #2 + Sprint A MR2 respawn"

$okSched = Register-WatchdogTask `
    -TaskName        $SchedulerTaskName `
    -ScriptPath      $SchedulerScriptPath `
    -IntervalMinutes $IntervalMinutes `
    -Description     "AGT scheduler daemon liveness watchdog - Sprint A MR2"

if ($okBot)   { Write-Host "Log: C:\AGT_Telegram_Bridge\logs\bot_liveness_watchdog.log" }
if ($okSched) { Write-Host "Log: C:\AGT_Telegram_Bridge\logs\scheduler_liveness_watchdog.log" }

if (-not $okBot -or -not $okSched) {
    Write-Error "One or more watchdog registrations failed - see errors above."
    exit 1
}
exit 0
