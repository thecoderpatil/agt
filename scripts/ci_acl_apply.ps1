# Phase A piece 4 Gate 2 -- break inheritance + remove Authenticated Users grant.
# Replaces the SYSTEM-target DENY approach from MR !263 (incident-causing).
# Runner identity: gitlab-runner-svc (dedicated local user, not LocalSystem).
# Run as Administrator on C:\AGT_Runtime\state before flipping AGT_CI_ACL_ENFORCED=true.

$ProdStatePath = "C:\AGT_Runtime\state"

if (-not (Test-Path $ProdStatePath)) {
    Write-Error "Prod state path $ProdStatePath does not exist -- abort."
    exit 1
}

# Pre-flight: confirm gitlab-runner-svc exists (Gate 1 must be complete).
$existingUser = Get-LocalUser -Name "gitlab-runner-svc" -ErrorAction SilentlyContinue
if (-not $existingUser) {
    Write-Error "gitlab-runner-svc local user not found -- Gate 1 incomplete; abort."
    exit 1
}

# Show current ACL before modification.
Write-Host "Current ACL for ${ProdStatePath}:"
icacls "$ProdStatePath"
Write-Host ""

# Step 1: Break inheritance, copy inherited ACEs as explicit.
$acl = Get-Acl $ProdStatePath
$acl.SetAccessRuleProtection($true, $true)
Set-Acl -Path $ProdStatePath -AclObject $acl
Write-Host "Inheritance broken; inherited ACEs copied as explicit."

# Step 2: Remove all Authenticated Users ACEs (closes Modify path for gitlab-runner-svc).
$acl = Get-Acl $ProdStatePath
$authRules = $acl.Access | Where-Object {
    $_.IdentityReference.Value -like "*Authenticated Users*"
}
Write-Host "Removing $($authRules.Count) Authenticated Users ACE(s)..."
foreach ($rule in $authRules) {
    $acl.RemoveAccessRule($rule) | Out-Null
    Write-Host "  Removed: $($rule.IdentityReference) $($rule.FileSystemRights)"
}
Set-Acl -Path $ProdStatePath -AclObject $acl
Write-Host ""

# Result: SYSTEM and Administrators retain FullControl; Users gets RX only.
# gitlab-runner-svc (Authenticated Users member) can no longer write.
Write-Host "ACL applied. Final state:"
icacls "$ProdStatePath"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Run: .\scripts\ci_acl_verify.ps1"
Write-Host "  2. Add AGT_CI_ACL_ENFORCED=true to C:\GitLab-Runner\config.toml environment array"
Write-Host "  3. Restart gitlab-runner service: Restart-Service gitlab-runner"
exit 0
