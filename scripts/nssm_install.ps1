<#
.SYNOPSIS
NSSM service lifecycle for agt-telegram-bot + agt-scheduler.

.DESCRIPTION
Idempotent installer/updater for AGT's two long-running Python daemons
under NSSM supervision. Replaces manual `python telegram_bot.py` /
`python agt_scheduler.py` + Sprint A MR2 watchdog respawn with
OS-managed services that auto-restart on crash.

Backlog: .claude-cowork-notes.md "NSSM service conversion" ticket,
Yash-approved 2026-04-17, priority-queued ahead of ADR-007 Step 7.

Services:
  agt-telegram-bot       python.exe telegram_bot.py      (UI + handlers + clientId=1)
  agt-scheduler          python.exe agt_scheduler.py     (clientId=2 + heartbeat + invariants)

Verbs (exactly one required):
  -Install    Stop + remove + re-create both services. Default manual start.
              Add -Autostart to set SERVICE_AUTO_START (system boot).
  -Update     Reconfigure existing services in place.
  -Uninstall  Stop and remove both services. Idempotent.
  -Status     Print nssm get output for both services.

Modifiers:
  -Autostart  Set Start=SERVICE_AUTO_START on -Install/-Update. Default is manual.
  -DryRun     Echo every nssm.exe command; execute nothing.
  -User       Windows user to run services as. Default: current user.
              LocalSystem is supported (see -AllowLocalSystem); the -User
              path requires -Password (SecureString). On MR1.5 verification
              we confirmed codebase cleanliness for SYSTEM: no *.py refs to
              user-profile / OneDrive paths, and .env + .gitlab-token grant
              NT AUTHORITY\SYSTEM Full Control.
  -Password   SecureString password for -User. Required on -Install/-Update
              because NSSM refuses to set ObjectName without a password.
              If omitted, the script prompts via Read-Host -AsSecureString.
              NSSM won't accept a one-arg ObjectName for any non-built-in
              account (rc=6 "requires both a username and password").
              Note: Windows 10+ Microsoft-account-linked local accounts may
              be rejected by ChangeServiceConfig even when LogonUser accepts
              the same credential (local MSA cache drift). If -User auth
              fails on such an account, fall back to -AllowLocalSystem or
              refresh the local cache by signing back in with the online
              MSA password.
  -AllowLocalSystem
              Opt-in to install services as built-in LocalSystem. Bypasses
              the Assert-User-NotLocalSystem guard, forces ObjectName to
              "LocalSystem", and skips the -Password resolver entirely.
              Used for MR1.5 cutover on this box after MSA credentials
              blocked the -User path.
  -RepoRoot   Default C:\AGT_Telegram_Bridge.
  -PythonExe  Override python.exe path.

NSSM settings applied to both services:
  AppDirectory             $RepoRoot
  AppStdout/AppStderr      $RepoRoot\logs\nssm_<svc>_{stdout,stderr}.log
  AppRotateFiles           1
  AppRotateOnline          1
  AppRotateBytes           10485760  (10 MB)
  AppStopMethodConsole     30000     (30s Ctrl+C for graceful stop)
  AppStopMethodWindow      0
  AppStopMethodThreads     0
  AppExit Default          Restart
  AppRestartDelay          60000     (60s backoff)
  ObjectName               $User
  Start                    SERVICE_DEMAND_START | SERVICE_AUTO_START

Scheduler-only AppEnvironmentExtra:
  USE_SCHEDULER_DAEMON=1
  SCHEDULER_IB_CLIENT_ID=2

.NOTES
Must run elevated (service registration requires admin).

Graceful shutdown: AppStopMethodConsole 30000 sends CTRL+C which Python
maps to SIGINT. Both daemons have handlers:
  - telegram_bot.py: PTB post_shutdown(_graceful_shutdown)
  - agt_scheduler.py: signal.signal(SIGINT/SIGTERM, _signal_handler)

Does NOT touch boot_desk.bat, walker.py, flex_sync.py.

Tests via tests/test_nssm_install_script.py (static assertions on this
script's contents). No live NSSM registration in CI; deferred to Coder.
#>

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$Status,

    [switch]$Autostart,
    [switch]$DryRun,

    [string]$RepoRoot = "C:\AGT_Telegram_Bridge",
    [string]$PythonExe = "",
    [string]$User = $env:USERNAME,
    [SecureString]$Password,
    [switch]$AllowLocalSystem,

    [string]$BotServiceName = "agt-telegram-bot",
    [string]$SchedulerServiceName = "agt-scheduler"
)

# Populated lazily from $Password (or Read-Host) inside Invoke-Main when we
# know we need it. Kept as a plaintext string because NSSM is invoked via
# Start-Process + ArgumentList, which requires plain args. Zeroed on exit.
$script:PlainPassword = $null

$ErrorActionPreference = "Stop"

# Verb guard: exactly one verb required.
$verbs = @($Install, $Update, $Uninstall, $Status) | Where-Object { $_ }
if ($verbs.Count -ne 1) {
    Write-Host "Exactly one verb required: -Install | -Update | -Uninstall | -Status" -ForegroundColor Red
    Write-Host "Use Get-Help .\scripts\nssm_install.ps1 -Full for details."
    exit 2
}

# -------------------------------------------------------------------- logging

function Write-Banner {
    param([string]$msg)
    Write-Host ""
    Write-Host ("==> {0}" -f $msg) -ForegroundColor Cyan
}

function Write-Info {
    param([string]$msg)
    Write-Host ("  [info]  {0}" -f $msg)
}

function Write-Warn {
    param([string]$msg)
    Write-Host ("  [warn]  {0}" -f $msg) -ForegroundColor Yellow
}

function Write-Err {
    param([string]$msg)
    Write-Host ("  [err]   {0}" -f $msg) -ForegroundColor Red
}

# ---------------------------------------------------------------- nssm helpers

function Assert-Elevated {
    $wid = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $prin = New-Object System.Security.Principal.WindowsPrincipal($wid)
    $isAdmin = $prin.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Err "This script must run in an elevated PowerShell (Run as Administrator)."
        exit 3
    }
    Write-Info "elevated session confirmed"
}

function Assert-NssmOnPath {
    $nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if (-not $nssm) {
        Write-Err "nssm.exe not on PATH."
        Write-Err "Install NSSM 2.24+ from https://nssm.cc/download and re-run."
        exit 4
    }
    Write-Info ("nssm.exe: {0}" -f $nssm.Path)
}

function Assert-User-NotLocalSystem {
    $isLocalSystem = ($User -eq "LocalSystem" -or $User -eq "NT AUTHORITY\SYSTEM")
    if ($AllowLocalSystem) {
        if (-not $isLocalSystem) {
            $script:User = 'LocalSystem'
        }
        Write-Warn "service account: LocalSystem (AllowLocalSystem opt-in)"
        Write-Warn "  Skips ObjectName password step (built-in account). Verified"
        Write-Warn "  codebase-clean for MR1.5: no user-profile / OneDrive refs in"
        Write-Warn "  *.py; .env + .gitlab-token grant SYSTEM Full Control."
        return
    }
    if ($isLocalSystem) {
        Write-Err "Refusing to run services as LocalSystem without -AllowLocalSystem opt-in."
        Write-Err "Pass -User <domain\user> + -Password, or pass -AllowLocalSystem."
        exit 5
    }
    Write-Info ("service account: {0}" -f $User)
}

function Resolve-Password {
    # Populates $script:PlainPassword from the $Password SecureString param,
    # or prompts interactively if not supplied. Plaintext lives only for the
    # duration of the Install/Update run and is zeroed in the finally block
    # of Invoke-Main.
    if ($AllowLocalSystem) {
        $script:PlainPassword = $null
        Write-Info "password: not required (LocalSystem)"
        return
    }
    if ($DryRun) {
        $script:PlainPassword = "<REDACTED-DRYRUN>"
        Write-Info "password: <REDACTED-DRYRUN> (DryRun mode)"
        return
    }
    $sec = $Password
    if (-not $sec) {
        Write-Info ("password: prompting for {0}" -f $User)
        $sec = Read-Host -Prompt ("Password for {0}" -f $User) -AsSecureString
    }
    if (-not $sec -or $sec.Length -eq 0) {
        Write-Err "Password required on -Install/-Update (NSSM rc=6 on one-arg ObjectName)."
        exit 9
    }
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        $script:PlainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Write-Info ("password: captured ({0} chars)" -f $script:PlainPassword.Length)
}

function Clear-Password {
    if ($null -ne $script:PlainPassword) {
        # Best-effort overwrite before releasing the reference. .NET strings
        # are immutable so a previous copy may linger in GC; this is the
        # tightest we can do in a script context.
        $script:PlainPassword = ("X" * $script:PlainPassword.Length)
        $script:PlainPassword = $null
        [System.GC]::Collect()
    }
}

function Resolve-PythonExe {
    if ($PythonExe -and (Test-Path $PythonExe)) {
        return $PythonExe
    }
    $venv = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venv) {
        return $venv
    }
    $sys = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($sys) {
        return $sys.Path
    }
    Write-Err ("python.exe not found. Tried: -PythonExe, {0}, PATH." -f $venv)
    Write-Err "Provide -PythonExe <path> or create the venv at RepoRoot\.venv."
    exit 6
}

function Ensure-LogsDir {
    $d = Join-Path $RepoRoot "logs"
    if (Test-Path $d) {
        return
    }
    if ($DryRun) {
        Write-Host ("    DRYRUN> mkdir {0}" -f $d)
    } else {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-Info ("created logs dir: {0}" -f $d)
    }
}

function ConvertTo-Win32Arg {
    # Escapes a single arg per CommandLineToArgvW rules so that CreateProcess
    # hands the child the exact intended string. Required because
    # Start-Process -ArgumentList in Windows PowerShell 5.1 naively joins the
    # array with spaces and does not quote args containing spaces/quotes.
    # That mangled the password arg on -Install, producing NSSM rc=6
    # "account name is invalid or password is invalid".
    param([string]$Arg)
    if ($null -eq $Arg) { return '""' }
    if ($Arg -eq '')    { return '""' }
    if ($Arg -notmatch '[\s"]') { return $Arg }

    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append('"')
    $i = 0
    while ($i -lt $Arg.Length) {
        $nBs = 0
        while ($i -lt $Arg.Length -and $Arg[$i] -eq '\') {
            $nBs++; $i++
        }
        if ($i -eq $Arg.Length) {
            [void]$sb.Append('\' * ($nBs * 2))
        } elseif ($Arg[$i] -eq '"') {
            [void]$sb.Append('\' * ($nBs * 2 + 1))
            [void]$sb.Append('"')
            $i++
        } else {
            [void]$sb.Append('\' * $nBs)
            [void]$sb.Append($Arg[$i])
            $i++
        }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Invoke-Nssm {
    param([string[]]$NssmArgs)

    # Redact the password arg for any log output. ObjectName is followed by
    # User then Password; redact the 2nd arg after "ObjectName".
    $displayArgs = @($NssmArgs)
    for ($i = 0; $i -lt $displayArgs.Count; $i++) {
        if ($displayArgs[$i] -eq 'ObjectName' -and $i + 2 -lt $displayArgs.Count) {
            $displayArgs[$i + 2] = '<REDACTED>'
        }
    }

    if ($DryRun) {
        Write-Host ("    DRYRUN> nssm {0}" -f ($displayArgs -join " "))
        return 0
    }

    # Build a properly Win32-escaped single command-line string and feed it
    # through System.Diagnostics.Process (not Start-Process, which re-joins
    # the array naively in PS 5.1).
    $escaped = $NssmArgs | ForEach-Object { ConvertTo-Win32Arg $_ }
    $cmdLine = ($escaped -join ' ')

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'nssm.exe'
    $psi.Arguments = $cmdLine
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow = $true

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()
    $out = $p.StandardOutput.ReadToEnd()
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()

    if ($out) { Write-Host ("    {0}" -f $out.TrimEnd()) }
    if ($err) { Write-Host ("    {0}" -f $err.TrimEnd()) -ForegroundColor Yellow }
    return $p.ExitCode
}

function Service-Exists {
    param([string]$Name)
    $null = sc.exe query $Name 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Stop-IfRunning {
    param([string]$Name)
    if (-not (Service-Exists -Name $Name)) {
        return
    }
    Write-Info ("stopping {0} (if running)" -f $Name)
    Invoke-Nssm @("stop", $Name) | Out-Null
}

function Remove-ServiceIfExists {
    param([string]$Name)
    if (-not (Service-Exists -Name $Name)) {
        Write-Info ("{0} not installed, nothing to remove" -f $Name)
        return
    }
    Write-Info ("removing {0}" -f $Name)
    Invoke-Nssm @("remove", $Name, "confirm") | Out-Null
}

# --------------------------------------------------------------- set helpers

function Set-NssmKey {
    param(
        [string]$Name,
        [string]$Key,
        [string[]]$Values
    )
    $args = @("set", $Name, $Key) + $Values
    $rc = Invoke-Nssm -NssmArgs $args
    if ($rc -ne 0 -and -not $DryRun) {
        Write-Err ("nssm set {0} {1} failed rc={2}" -f $Name, $Key, $rc)
        exit 10
    }
}

function Configure-Service {
    param(
        [string]$Name,
        [string]$Py,
        [string]$Script,
        [hashtable]$EnvExtra
    )

    $stdoutLog = Join-Path $RepoRoot ("logs\nssm_{0}_stdout.log" -f $Name)
    $stderrLog = Join-Path $RepoRoot ("logs\nssm_{0}_stderr.log" -f $Name)

    Set-NssmKey -Name $Name -Key "Application"    -Values @($Py)
    Set-NssmKey -Name $Name -Key "AppParameters"  -Values @($Script)
    Set-NssmKey -Name $Name -Key "AppDirectory"   -Values @($RepoRoot)

    Set-NssmKey -Name $Name -Key "AppStdout"          -Values @($stdoutLog)
    Set-NssmKey -Name $Name -Key "AppStderr"          -Values @($stderrLog)
    Set-NssmKey -Name $Name -Key "AppRotateFiles"     -Values @("1")
    Set-NssmKey -Name $Name -Key "AppRotateOnline"    -Values @("1")
    Set-NssmKey -Name $Name -Key "AppRotateBytes"     -Values @("10485760")

    Set-NssmKey -Name $Name -Key "AppStopMethodConsole" -Values @("30000")
    Set-NssmKey -Name $Name -Key "AppStopMethodWindow"  -Values @("0")
    Set-NssmKey -Name $Name -Key "AppStopMethodThreads" -Values @("0")

    Set-NssmKey -Name $Name -Key "AppExit"         -Values @("Default", "Restart")
    Set-NssmKey -Name $Name -Key "AppRestartDelay" -Values @("60000")

    if ($AllowLocalSystem) {
        # Built-in LocalSystem -- no password. NSSM accepts bare 'LocalSystem'
        # as ObjectName.
        Set-NssmKey -Name $Name -Key "ObjectName" -Values @("LocalSystem")
    } else {
        if (-not $script:PlainPassword) {
            Write-Err "Configure-Service called without a resolved password; this is a bug."
            exit 8
        }
        Set-NssmKey -Name $Name -Key "ObjectName" -Values @($User, $script:PlainPassword)
    }

    if ($EnvExtra -and $EnvExtra.Count -gt 0) {
        $lines = @()
        foreach ($k in $EnvExtra.Keys) {
            $lines += ("{0}={1}" -f $k, $EnvExtra[$k])
        }
        $joined = [string]::Join("`r`n", $lines)
        Set-NssmKey -Name $Name -Key "AppEnvironmentExtra" -Values @($joined)
    } else {
        Set-NssmKey -Name $Name -Key "AppEnvironmentExtra" -Values @("")
    }

    $startMode = if ($Autostart) { "SERVICE_AUTO_START" } else { "SERVICE_DEMAND_START" }
    Set-NssmKey -Name $Name -Key "Start" -Values @($startMode)

    Write-Info ("{0}: configured (start={1}, python={2}, script={3})" -f $Name, $startMode, $Py, $Script)
}

function Install-Service {
    param(
        [string]$Name,
        [string]$Py,
        [string]$Script,
        [hashtable]$EnvExtra
    )
    Write-Banner ("Installing {0}" -f $Name)

    Stop-IfRunning -Name $Name
    Remove-ServiceIfExists -Name $Name

    $rc = Invoke-Nssm -NssmArgs @("install", $Name, $Py, $Script)
    if ($rc -ne 0 -and -not $DryRun) {
        Write-Err ("nssm install {0} failed rc={1}" -f $Name, $rc)
        exit 11
    }
    Configure-Service -Name $Name -Py $Py -Script $Script -EnvExtra $EnvExtra
}

function Update-Service {
    param(
        [string]$Name,
        [string]$Py,
        [string]$Script,
        [hashtable]$EnvExtra
    )
    Write-Banner ("Updating {0}" -f $Name)

    if (-not (Service-Exists -Name $Name)) {
        Write-Warn ("{0} does not exist; use -Install first." -f $Name)
        exit 12
    }

    Stop-IfRunning -Name $Name
    Configure-Service -Name $Name -Py $Py -Script $Script -EnvExtra $EnvExtra
    Write-Info ("{0}: update complete. Start manually with: nssm start {0}" -f $Name)
}

function Uninstall-Service {
    param([string]$Name)
    Write-Banner ("Uninstalling {0}" -f $Name)
    Stop-IfRunning -Name $Name
    Remove-ServiceIfExists -Name $Name
}

function Status-Service {
    param([string]$Name)
    Write-Banner ("Status: {0}" -f $Name)
    if (-not (Service-Exists -Name $Name)) {
        Write-Warn ("{0} not installed." -f $Name)
        return
    }
    $keys = @(
        "Application", "AppParameters", "AppDirectory",
        "AppStdout", "AppStderr",
        "AppRotateFiles", "AppRotateOnline", "AppRotateBytes",
        "AppStopMethodConsole", "AppStopMethodWindow", "AppStopMethodThreads",
        "AppExit", "AppRestartDelay",
        "ObjectName", "Start",
        "AppEnvironmentExtra"
    )
    foreach ($k in $keys) {
        Invoke-Nssm -NssmArgs @("get", $Name, $k) | Out-Null
    }
    $null = sc.exe query $Name
}

# -------------------------------------------------------------------- main

function Invoke-Main {
    Assert-Elevated
    Assert-NssmOnPath

    if ($Uninstall) {
        Uninstall-Service -Name $BotServiceName
        Uninstall-Service -Name $SchedulerServiceName
        Write-Banner "Uninstall complete."
        return
    }

    if ($Status) {
        Status-Service -Name $BotServiceName
        Status-Service -Name $SchedulerServiceName
        return
    }

    Assert-User-NotLocalSystem
    Resolve-Password
    $py = Resolve-PythonExe
    Write-Info ("python: {0}" -f $py)
    Ensure-LogsDir

    $botScript   = Join-Path $RepoRoot "telegram_bot.py"
    $schedScript = Join-Path $RepoRoot "agt_scheduler.py"
    if (-not (Test-Path $botScript)) {
        Write-Err ("missing: {0}" -f $botScript); exit 7
    }
    if (-not (Test-Path $schedScript)) {
        Write-Err ("missing: {0}" -f $schedScript); exit 7
    }

    $schedEnv = @{
        "USE_SCHEDULER_DAEMON"   = "1"
        "SCHEDULER_IB_CLIENT_ID" = "2"
    }
    $botEnv = @{}

    if ($Install) {
        Install-Service -Name $BotServiceName       -Py $py -Script $botScript   -EnvExtra $botEnv
        Install-Service -Name $SchedulerServiceName -Py $py -Script $schedScript -EnvExtra $schedEnv
        Write-Banner "Install complete."
        Write-Host ""
        Write-Info "Services are NOT started. Start manually with:"
        Write-Host "    nssm start $BotServiceName"
        Write-Host "    nssm start $SchedulerServiceName"
        if (-not $Autostart) {
            Write-Host ""
            Write-Info "Start mode: manual. Re-run with -Autostart after services are proven."
        }
        return
    }

    if ($Update) {
        Update-Service -Name $BotServiceName       -Py $py -Script $botScript   -EnvExtra $botEnv
        Update-Service -Name $SchedulerServiceName -Py $py -Script $schedScript -EnvExtra $schedEnv
        Write-Banner "Update complete."
        return
    }
}

try {
    Invoke-Main
    exit 0
} catch {
    Write-Err ("fatal: {0}" -f $_.Exception.Message)
    exit 1
} finally {
    Clear-Password
}
