# Sprint 8 Mega-MR 5 — local PSScriptAnalyzer wrapper.
# Run before pushing if you touched any .ps1 files. No CI dependency;
# just a developer convenience.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/lint_powershell.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/lint_powershell.ps1 -Path ./scripts/deploy
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/lint_powershell.ps1 -Severity Error

param(
    [string]$Path = "./scripts",
    [ValidateSet("Information","Warning","Error")][string[]]$Severity = @("Warning","Error")
)

$ErrorActionPreference = "Stop"

if (-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)) {
    Write-Host "PSScriptAnalyzer not installed. Run:"
    Write-Host "  Install-Module PSScriptAnalyzer -Force -Scope CurrentUser -AllowClobber"
    exit 2
}

Import-Module PSScriptAnalyzer
$findings = Invoke-ScriptAnalyzer -Path $Path -Recurse -Severity $Severity
if ($null -eq $findings -or $findings.Count -eq 0) {
    Write-Host "lint_powershell: clean (Path=$Path Severity=$Severity)"
    exit 0
}

$findings | Format-Table -AutoSize RuleName, Severity, ScriptName, Line, Message
Write-Host ""
Write-Host ("lint_powershell: {0} finding(s) across Severity={1}" -f $findings.Count, $Severity)
exit 1
