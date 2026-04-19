# Restore previous deploy. Quarantines (does not delete) the failed bridge-current for forensics.
param(
    [string]$RuntimeRoot = "C:\AGT_Runtime",
    [switch]$SkipServiceRestart
)
$ErrorActionPreference = "Stop"

$current    = Join-Path $RuntimeRoot "bridge-current"
$previous   = Join-Path $RuntimeRoot "bridge-previous"
$quarantine = Join-Path $RuntimeRoot ("bridge-failed-" + (Get-Date -Format "yyyyMMdd_HHmmss"))

if (-not (Test-Path $previous)) { throw "No previous deploy at $previous — cannot roll back." }
if (-not (Test-Path $current))  { throw "No current deploy at $current — abnormal state, surface to Architect." }

if (-not $SkipServiceRestart) {
    nssm stop agt-telegram-bot | Out-Null
    nssm stop agt-scheduler | Out-Null
    Start-Sleep -Seconds 3
}

Move-Item $current $quarantine
Move-Item $previous $current

if (-not $SkipServiceRestart) {
    nssm start agt-scheduler | Out-Null
    Start-Sleep -Seconds 2
    nssm start agt-telegram-bot | Out-Null
}

Write-Host "Rollback complete. Failed deploy quarantined at $quarantine. Investigate before next deploy."
