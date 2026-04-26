# Atomic 3-slot deploy: staging -> current -> previous. Reversible via rollback.ps1.

param(

    [Parameter(Mandatory=$true)][string]$SourcePath,

    [string]$RuntimeRoot = "C:\AGT_Runtime",

    [switch]$SkipBackup,

    [switch]$SkipServiceRestart

)

$ErrorActionPreference = "Stop"



# Sprint 6 R5: tolerate NSSM SERVICE_STOP_PENDING / SERVICE_START_PENDING
# stderr without tripping ErrorActionPreference=Stop. NSSM emits transient
# state-transition lines to stderr even on healthy calls; pre-R5 deploy.ps1
# aborted mid-sequence when it hit them (2026-04-23 17:56 ET incident). Fix
# scopes EAP=Continue around each nssm call, redirects stderr, then polls
# `sc.exe query` for the expected service state with a 30s timeout so the
# script hard-fails if the service genuinely never transitions.
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



$current  = Join-Path $RuntimeRoot "bridge-current"

$staging  = Join-Path $RuntimeRoot "bridge-staging"

$previous = Join-Path $RuntimeRoot "bridge-previous"



if (-not (Test-Path $SourcePath)) { throw "Source path missing: $SourcePath" }

if (-not (Test-Path $current))    { throw "bridge-current missing: $current. Run Phase 2 initial seed first." }


$CanonicalEnvFile = "C:\AGT_Runtime\state\.env"
$CanonicalDbDir = "C:\AGT_Runtime\state"

if (-not (Test-Path $CanonicalEnvFile)) {
    Write-Error "Canonical env file missing at $CanonicalEnvFile. Create it before deploying."
    exit 1
}
if (-not (Test-Path $CanonicalDbDir)) {
    Write-Error "Canonical state dir missing at $CanonicalDbDir. Create it before deploying."
    exit 1
}



# 1. Pre-flight DB backup (skippable for dev iterations — never skip in prod)

if (-not $SkipBackup) {

    & "$PSScriptRoot\backup.ps1" -Label "pre_deploy"

}



# 2. Clean staging, robocopy source -> staging

if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }

New-Item -ItemType Directory -Path $staging -Force | Out-Null



$excludeDirs = @(

    ".git", ".worktrees", ".venv", "reports", "outputs", "logs",

    "__pycache__", ".pytest_cache", ".cursor", ".vscode", ".idea",

    "node_modules", ".mypy_cache", ".ruff_cache"

)

$excludeFiles = @(

    "agt_desk.db", "agt_desk.db-wal", "agt_desk.db-shm",

    ".gitlab-token", ".claude-cowork-notes.md",

    "HANDOFF_ARCHITECT_latest.md", ".git"

)



$roboArgs = @($SourcePath, $staging, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:2", "/W:2")

$roboArgs += "/XD"

$roboArgs += $excludeDirs

$roboArgs += "/XF"

$roboArgs += $excludeFiles



robocopy @roboArgs | Out-Null

# robocopy exit 0-7 = success (1=files copied, 2=extras, 3=both, etc); 8+ = failure

if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit $LASTEXITCODE)" }




# 3. Stop services (telegram_bot first — it reads scheduler state)

if (-not $SkipServiceRestart) {

    Invoke-NssmStop -ServiceName "agt-telegram-bot"

    Invoke-NssmStop -ServiceName "agt-scheduler"

    Start-Sleep -Seconds 3

}



# 4. Atomic 3-slot rotation

if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }

Move-Item $current $previous

Move-Item $staging $current



# 5. Start services (scheduler first — telegram_bot depends on scheduler heartbeat)

if (-not $SkipServiceRestart) {

    Invoke-NssmStart -ServiceName "agt-scheduler"

    Start-Sleep -Seconds 2

    Invoke-NssmStart -ServiceName "agt-telegram-bot"

}



# 6. Sprint 5 MR C: PRAGMA integrity_check post-start.
#    Sprint 4 pre-sprint gate observed a transient "database disk image is
#    malformed" during a _check_invariants_tick probe that self-resolved on
#    PRAGMA integrity_check. This hook catches the same class proactively —
#    if integrity is not 'ok' after deploy, halt with non-zero exit so the
#    operator doesn't silently ship on top of a corrupt DB.

if (-not $SkipServiceRestart) {

    $dbPath = Join-Path $CanonicalDbDir "agt_desk.db"

    if (Test-Path $dbPath) {

        Start-Sleep -Seconds 5

        $integrityScript = Join-Path $PSScriptRoot "integrity_check.py"

        $integrityResult = & python $integrityScript $dbPath

        $integrityExit = $LASTEXITCODE

        Write-Host "PRAGMA integrity_check: $integrityResult"

        if ($integrityExit -ne 0) {

            Write-Error "Sprint 5 MR C: integrity_check returned non-ok. Deploy halted."

            exit $integrityExit

        }

    } else {

        Write-Warning "PRAGMA integrity_check skipped: DB path missing ($dbPath)"

    }

}



# 7. Sprint 6 add-on: post-start service-boot smoke.
# Runs AFTER integrity_check, BEFORE declaring deploy success. Catches
# R1/R2/R4-class boot regressions at deploy-time against the REAL service
# in the REAL OS environment. Auto-rolls back on smoke failure.

if (-not $SkipServiceRestart) {

    $smokeScript = Join-Path $PSScriptRoot "post_start_smoke.ps1"

    if (Test-Path $smokeScript) {

        & powershell -NoProfile -ExecutionPolicy Bypass -File $smokeScript

        $smokeExit = $LASTEXITCODE

        if ($smokeExit -ne 0) {

            Write-Error "post_start_smoke FAILED (exit $smokeExit). Rolling back via rollback.ps1"

            $rollbackScript = Join-Path $PSScriptRoot "rollback.ps1"

            if (Test-Path $rollbackScript) {

                & powershell -NoProfile -ExecutionPolicy Bypass -File $rollbackScript

            }

            exit 1

        }

    } else {

        Write-Warning "post_start_smoke.ps1 missing at $smokeScript; skipping (not a deploy-blocker yet)"

    }

}

# Step 8: Assert machine-level AGT_DB_PATH is the canonical real path.
# Regression guard: if AGT_DB_PATH points at the symlink, the heartbeat alert
# task (runs as SYSTEM, inherits machine env) fires false HEARTBEAT_STALE every
# ~30 min (WAL split — see contention_source_recon_20260426.md).
# Non-fatal: warn loudly but do not abort deploy.
$_canonDbPath = Join-Path $CanonicalDbDir "agt_desk.db"
$_machineDbPath = [System.Environment]::GetEnvironmentVariable("AGT_DB_PATH", "Machine")
if ($_machineDbPath -ne $_canonDbPath) {
    Write-Warning "AGT_DB_PATH machine env='$_machineDbPath'; expected='$_canonDbPath'"
    Write-Warning "Run: [System.Environment]::SetEnvironmentVariable('AGT_DB_PATH','$_canonDbPath','Machine')"
} else {
    Write-Host "AGT_DB_PATH machine env: OK"
}

Write-Host "Deploy complete at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). Rollback target: $previous"

