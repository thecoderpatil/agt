# Restore previous deploy. Quarantines (does not delete) the failed bridge-current for forensics.
param(
    [string]$RuntimeRoot = "C:\AGT_Runtime",
    [switch]$SkipServiceRestart
)
$ErrorActionPreference = "Stop"

# Sprint 6 R3 fix: previous version used U+2014 em-dash characters inside
# string literals. File saved as UTF-8 without BOM; PowerShell 5.1 read the
# 3-byte em-dash as CP1252 garbage and the tokenizer failed with
# "Unexpected token 'cannot'". Replaced with ASCII hyphens.
#
# Sprint 6 R5: same nssm SERVICE_STOP_PENDING stderr issue that hit
# deploy.ps1 (2026-04-23 17:56 ET) would hit rollback.ps1 on any real
# recovery invocation. Same Wait-ServiceState / Invoke-NssmStop pattern
# applied here so rollback is R5-safe too.
function Wait-ServiceState {
    param(
        [Parameter(Mandatory=$true)][string]$ServiceName,
        [Parameter(Mandatory=$true)][string]$ExpectedState,
        [int]$TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $queryOutput = sc.exe query $ServiceName 2>&1
        $stateLine = $queryOutput | Select-String -Pattern "STATE"
        if ($stateLine) {
            $currentState = ($stateLine.Line -replace '.*:\s*\d+\s+', '').Trim()
            if ($currentState -eq $ExpectedState) {
                return $true
            }
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Invoke-NssmStop {
    param([Parameter(Mandatory=$true)][string]$ServiceName)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        nssm stop $ServiceName 2>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if (-not (Wait-ServiceState -ServiceName $ServiceName -ExpectedState "STOPPED" -TimeoutSeconds 30)) {
        throw "Sprint 6 R5: service $ServiceName failed to reach STOPPED within 30s"
    }
}

function Invoke-NssmStart {
    param([Parameter(Mandatory=$true)][string]$ServiceName)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        nssm start $ServiceName 2>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if (-not (Wait-ServiceState -ServiceName $ServiceName -ExpectedState "RUNNING" -TimeoutSeconds 30)) {
        throw "Sprint 6 R5: service $ServiceName failed to reach RUNNING within 30s"
    }
}

$current    = Join-Path $RuntimeRoot "bridge-current"
$previous   = Join-Path $RuntimeRoot "bridge-previous"
$quarantine = Join-Path $RuntimeRoot ("bridge-failed-" + (Get-Date -Format "yyyyMMdd_HHmmss"))

if (-not (Test-Path $previous)) { throw "No previous deploy at $previous -- cannot roll back." }
if (-not (Test-Path $current))  { throw "No current deploy at $current -- abnormal state, surface to Architect." }

if (-not $SkipServiceRestart) {
    Invoke-NssmStop -ServiceName "agt-telegram-bot"
    Invoke-NssmStop -ServiceName "agt-scheduler"
    Start-Sleep -Seconds 3
}

Move-Item $current $quarantine
Move-Item $previous $current

if (-not $SkipServiceRestart) {
    Invoke-NssmStart -ServiceName "agt-scheduler"
    Start-Sleep -Seconds 2
    Invoke-NssmStart -ServiceName "agt-telegram-bot"
}

Write-Host "Rollback complete. Failed deploy quarantined at $quarantine. Investigate before next deploy."
