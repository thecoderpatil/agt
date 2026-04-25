# Phase A piece 4 — GitLab Runner service account ACL deny on prod state.
# Run as Administrator. One-time setup; persists in NTFS DACL.
#
# Runner service account: NT AUTHORITY\SYSTEM (LocalSystem).
# Confirmed by: Get-CimInstance Win32_Service -Filter "Name='gitlab-runner'"
#               StartName = LocalSystem
#
# IMPORTANT: NT AUTHORITY\SYSTEM holds SeBackupPrivilege / SeRestorePrivilege.
# These privileges allow bypassing DACL checks ONLY when the caller explicitly
# requests backup/restore access flags in the Win32 CreateFile call.
# Python's sqlite3 module uses normal CreateFile flags — no backup semantics —
# so the DENY ACE IS effective against Python-level db writes from CI jobs.
# Admin-level Win32 operations with explicit backup flags may still bypass.
# This is defense-in-depth, not a hard security boundary.
#
# GitLab Runner service account ACL deny on prod state.

$RunnerAccount = "NT AUTHORITY\SYSTEM"
$ProdStatePath = "C:\AGT_Runtime\state"

if (-not (Test-Path $ProdStatePath)) {
    Write-Error "Prod state path $ProdStatePath does not exist — abort."
    exit 1
}

# Show current ACL before modification for audit log.
Write-Host "Current ACL for ${ProdStatePath}:"
icacls "$ProdStatePath"
Write-Host ""

# Apply DENY ACE. Flags:
#   (OI) = ObjectInherit  — applies to files in the dir
#   (CI) = ContainerInherit — applies to subdirs
#   W    = Write permissions
#   D    = Delete
#   DC   = DeleteChild (delete items inside the dir)
$icaclsArgs = @("$ProdStatePath", "/deny", "${RunnerAccount}:(OI)(CI)(W,D,DC)")
Write-Host "Applying: icacls $($icaclsArgs -join ' ')"
& icacls @icaclsArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "icacls failed with exit code $LASTEXITCODE"
    exit 1
}

Write-Host ""
Write-Host "ACL applied. Next steps:"
Write-Host "  1. Run: .\scripts\ci_acl_verify.ps1"
Write-Host "  2. Add AGT_CI_ACL_ENFORCED=true to C:\GitLab-Runner\config.toml environment array"
Write-Host "  3. Restart gitlab-runner service: Restart-Service gitlab-runner"
exit 0
