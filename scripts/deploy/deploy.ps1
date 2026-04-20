# Atomic 3-slot deploy: staging -> current -> previous. Reversible via rollback.ps1.

param(

    [Parameter(Mandatory=$true)][string]$SourcePath,

    [string]$RuntimeRoot = "C:\AGT_Runtime",

    [switch]$SkipBackup,

    [switch]$SkipServiceRestart

)

$ErrorActionPreference = "Stop"



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

    nssm stop agt-telegram-bot | Out-Null

    nssm stop agt-scheduler | Out-Null

    Start-Sleep -Seconds 3

}



# 4. Atomic 3-slot rotation

if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }

Move-Item $current $previous

Move-Item $staging $current



# 5. Start services (scheduler first — telegram_bot depends on scheduler heartbeat)

if (-not $SkipServiceRestart) {

    nssm start agt-scheduler | Out-Null

    Start-Sleep -Seconds 2

    nssm start agt-telegram-bot | Out-Null

}



Write-Host "Deploy complete at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). Rollback target: $previous"

