# scripts/scheduled/register_friday_jobs.ps1
#
# One-shot registration of the Friday 2026-04-24 scheduled agents:
#   - AGT_followup_a_flex_backfill_20260424  @ 07:33 ET
#   - AGT_sprint7_first_fire_20260424        @ 18:42 ET
#
# Both run as SYSTEM (same principal as the NSSM services). Each script
# self-deletes its schtasks entry after running so this registration is
# one-shot — no cleanup required afterwards.
#
# Safe to re-run: schtasks /create /f overwrites existing entries.

$ErrorActionPreference = 'Stop'

$Venv = 'C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe'
$Scripts = 'C:\AGT_Telegram_Bridge\.worktrees\coder\scripts\scheduled'

$Jobs = @(
    @{
        Name   = 'AGT_followup_a_flex_backfill_20260424'
        Script = "$Scripts\followup_a_flex_backfill_2026_04_24.py"
        Time   = '07:33'
        Date   = '04/24/2026'
    },
    @{
        Name   = 'AGT_sprint7_first_fire_20260424'
        Script = "$Scripts\sprint7_first_fire_observation_2026_04_24.py"
        Time   = '18:42'
        Date   = '04/24/2026'
    }
)

foreach ($job in $Jobs) {
    $name   = $job.Name
    $script = $job.Script
    $time   = $job.Time
    $date   = $job.Date

    if (-not (Test-Path $script)) {
        Write-Error "Script not found: $script"
        exit 1
    }

    # schtasks /tr expects a single string. Each script uses absolute paths
    # internally so we don't need to cd — invoke venv python + script directly.
    # Wrap the two tokens in embedded double-quotes to survive schtasks parsing.
    $tr = "`"$Venv`" `"$script`""

    Write-Host "Registering $name at $date $time ..."
    schtasks /create `
        /sc once `
        /tn $name `
        /tr $tr `
        /st $time `
        /sd $date `
        /ru SYSTEM `
        /rl HIGHEST `
        /f
    if ($LASTEXITCODE -ne 0) {
        Write-Error "schtasks /create failed for $name (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
}

Write-Host ""
Write-Host "Registered. Verify with: schtasks /query /tn AGT_followup_a_flex_backfill_20260424"
Write-Host "                       and: schtasks /query /tn AGT_sprint7_first_fire_20260424"
