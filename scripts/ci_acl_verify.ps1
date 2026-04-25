# Phase A piece 4 — ACL verification for CI containment contract.
# Confirms the GitLab Runner service account (NT AUTHORITY\SYSTEM) DENY
# is in place on the prod state path. Run as Administrator after
# running ci_acl_apply.ps1.
#
# Sentinel: ACL verification.

$RunnerAccount = "NT AUTHORITY\SYSTEM"
$ProdStatePath = "C:\AGT_Runtime\state"

if (-not (Test-Path $ProdStatePath)) {
    Write-Error "ACL VERIFY: $ProdStatePath does not exist — nothing to verify."
    exit 1
}

$Acl = Get-Acl $ProdStatePath
$DenyEntries = $Acl.Access | Where-Object {
    $_.IdentityReference -like "*SYSTEM*" -and $_.AccessControlType -eq "Deny"
}

if ($DenyEntries.Count -eq 0) {
    Write-Error "ACL VERIFY FAIL: no DENY entry for $RunnerAccount on $ProdStatePath"
    Write-Host "Run scripts/ci_acl_apply.ps1 as Administrator to apply the DENY ACE."
    exit 1
}

Write-Host "ACL VERIFY PASS: $($DenyEntries.Count) DENY entries for $RunnerAccount on $ProdStatePath"
$DenyEntries | ForEach-Object {
    Write-Host "  Rights=$($_.FileSystemRights) Type=$($_.AccessControlType) Inherited=$($_.IsInherited)"
}

# Functional write probe. Note: if running as Admin (not as the runner
# account itself), the probe may succeed — Admin can override DENY.
# The ACL output above is the canonical verification signal.
$probe = Join-Path $ProdStatePath ".acl_verify_probe"
try {
    $null = New-Item -Path $probe -ItemType File -Force -ErrorAction Stop
    Remove-Item $probe -Force
    Write-Host "NOTE: current process (Admin) can write — expected; DENY targets SYSTEM not Admin."
} catch {
    Write-Host "Write probe blocked: $($_.Exception.Message)"
    Write-Host "(If running as SYSTEM or runner account, this confirms ACL is working.)"
}
exit 0
