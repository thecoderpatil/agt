# Phase A piece 4 Gate 2 -- verify runner cannot write to prod state.
# Checks: inheritance disabled, Authenticated Users absent, SYSTEM/Administrators intact.
# Run as Administrator after running ci_acl_apply.ps1.

$ProdStatePath = "C:\AGT_Runtime\state"

if (-not (Test-Path $ProdStatePath)) {
    Write-Error "ACL VERIFY: $ProdStatePath does not exist -- nothing to verify."
    exit 1
}

$acl = Get-Acl $ProdStatePath

# Check 1: inheritance is disabled (AreAccessRulesProtected = true means disabled).
if ($acl.AreAccessRulesProtected -eq $false) {
    Write-Error "ACL VERIFY FAIL: inheritance still enabled on $ProdStatePath"
    exit 1
}
Write-Host "ACL VERIFY: inheritance disabled -- OK"

# Check 2: Authenticated Users absent (no Modify path for gitlab-runner-svc).
$authUsersRules = $acl.Access | Where-Object {
    $_.IdentityReference.Value -like "*Authenticated Users*"
}
if ($authUsersRules.Count -gt 0) {
    Write-Error "ACL VERIFY FAIL: Authenticated Users still has access on $ProdStatePath"
    $authUsersRules | Format-Table IdentityReference, FileSystemRights, AccessControlType
    exit 1
}
Write-Host "ACL VERIFY: Authenticated Users absent -- OK"

# Check 3: SYSTEM still has FullControl (prod NSSM services write here).
$systemRule = $acl.Access | Where-Object {
    ($_.IdentityReference.Value -eq "NT AUTHORITY\SYSTEM") -and
    ($_.AccessControlType -eq "Allow")
}
if (-not $systemRule) {
    Write-Error "ACL VERIFY FAIL: SYSTEM access missing -- prod services would break"
    exit 1
}
Write-Host "ACL VERIFY: SYSTEM FullControl present -- OK"

# Check 4: Administrators still has FullControl (operator access).
$adminRule = $acl.Access | Where-Object {
    ($_.IdentityReference.Value -like "*Administrators*") -and
    ($_.AccessControlType -eq "Allow")
}
if (-not $adminRule) {
    Write-Error "ACL VERIFY FAIL: Administrators access missing"
    exit 1
}
Write-Host "ACL VERIFY: Administrators FullControl present -- OK"

Write-Host ""
Write-Host "ACL VERIFY PASS:"
$acl.Access | Format-Table IdentityReference, FileSystemRights, AccessControlType, IsInherited
exit 0
