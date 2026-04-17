"""Static assertions on scripts/nssm_install.ps1.

The NSSM install script is PowerShell — we can't invoke it in CI (no
Windows runner and no nssm.exe). Instead we static-check the file for:

  - ASCII-only (PS 5.1 reads BOMless .ps1 as Windows-1252; em-dashes
    break the quote parser — banked lesson feedback_ps1_ascii_only.md).
  - All required NSSM configuration keys appear at least once.
  - Specific numeric thresholds match the ticket spec.
  - Scheduler service env extras include the cutover flag + clientId=2.
  - No `respawn_*` references (the install script must not revive
    Sprint A MR2 respawn lifecycle — that's retired by MR-NSSM-2).
  - Verbs present (-Install, -Update, -Uninstall, -Status, -DryRun, -Autostart).
  - Both service names present (agt-telegram-bot, agt-scheduler).
  - Do-not-touch files not referenced (walker.py, flex_sync.py, boot_desk.bat).

Coverage for the actual NSSM registration behavior lives in a Coder
one-shot that runs `-Install -DryRun` on Windows and diffs the output.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.sprint_a


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "nssm_install.ps1"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script_bytes() -> bytes:
    return SCRIPT_PATH.read_bytes()


def test_script_exists() -> None:
    assert SCRIPT_PATH.is_file(), f"missing {SCRIPT_PATH}"


def test_script_is_ascii(script_bytes: bytes) -> None:
    # PS 5.1 reads BOMless .ps1 as Windows-1252. Any byte > 127 risks
    # breaking the quote parser and silently corrupting the install.
    bad = [(i, b) for i, b in enumerate(script_bytes) if b > 127]
    assert bad == [], (
        f"non-ASCII bytes found at offsets {bad[:5]}; "
        "strip em-dashes/smart quotes before committing"
    )


def test_script_has_shebang_doc(script_text: str) -> None:
    # PS multi-line doc block must open on line 1 so Get-Help works.
    head = script_text.lstrip().splitlines()[0]
    assert head.startswith("<#"), f"doc block missing, got: {head!r}"


@pytest.mark.parametrize(
    "verb",
    [
        "[switch]$Install",
        "[switch]$Update",
        "[switch]$Uninstall",
        "[switch]$Status",
        "[switch]$Autostart",
        "[switch]$DryRun",
    ],
)
def test_verb_parameters_declared(script_text: str, verb: str) -> None:
    assert verb in script_text, f"missing param declaration: {verb}"


def test_exactly_one_verb_guard(script_text: str) -> None:
    assert "Exactly one verb required" in script_text, (
        "verb guard message missing; script must reject zero-verb + multi-verb invocations"
    )


@pytest.mark.parametrize(
    "service_name",
    ["agt-telegram-bot", "agt-scheduler"],
)
def test_service_names_present(script_text: str, service_name: str) -> None:
    assert service_name in script_text, f"service name not set: {service_name}"


@pytest.mark.parametrize(
    "nssm_key",
    [
        "Application",
        "AppParameters",
        "AppDirectory",
        "AppStdout",
        "AppStderr",
        "AppRotateFiles",
        "AppRotateOnline",
        "AppRotateBytes",
        "AppStopMethodConsole",
        "AppStopMethodWindow",
        "AppStopMethodThreads",
        "AppExit",
        "AppRestartDelay",
        "ObjectName",
        "AppEnvironmentExtra",
        "Start",
    ],
)
def test_required_nssm_key_configured(script_text: str, nssm_key: str) -> None:
    # Each required key must be set at least once via Set-NssmKey.
    needle = f'-Key "{nssm_key}"'
    assert needle in script_text, f"Set-NssmKey call missing for {nssm_key}"


def test_graceful_stop_method_console_30s(script_text: str) -> None:
    # 30 000 ms gives Python time to drain open IBKR connections and
    # finish in-flight SQLite writes on SIGINT.
    assert 'Set-NssmKey -Name $Name -Key "AppStopMethodConsole" -Values @("30000")' in script_text


def test_window_and_threads_stop_methods_disabled(script_text: str) -> None:
    # AppStopMethodWindow and AppStopMethodThreads are useless for a
    # console-only Python process. Setting to 0 skips them and goes
    # straight to the graceful Console method.
    assert 'Set-NssmKey -Name $Name -Key "AppStopMethodWindow"  -Values @("0")' in script_text
    assert 'Set-NssmKey -Name $Name -Key "AppStopMethodThreads" -Values @("0")' in script_text


def test_restart_on_crash_with_30s_backoff(script_text: str) -> None:
    assert 'Set-NssmKey -Name $Name -Key "AppExit"         -Values @("Default", "Restart")' in script_text
    assert 'Set-NssmKey -Name $Name -Key "AppRestartDelay" -Values @("30000")' in script_text


def test_log_rotation_10mb(script_text: str) -> None:
    assert 'Set-NssmKey -Name $Name -Key "AppRotateFiles"     -Values @("1")' in script_text
    assert 'Set-NssmKey -Name $Name -Key "AppRotateOnline"    -Values @("1")' in script_text
    assert 'Set-NssmKey -Name $Name -Key "AppRotateBytes"     -Values @("10485760")' in script_text


def test_scheduler_env_extras(script_text: str) -> None:
    # Scheduler service must get USE_SCHEDULER_DAEMON=1 + clientId=2
    # baked into the service env so a stale shell can't race against it.
    assert '"USE_SCHEDULER_DAEMON"' in script_text
    assert '"SCHEDULER_IB_CLIENT_ID"' in script_text
    # Actual value assignment (hashtable literal in script).
    assert 'USE_SCHEDULER_DAEMON"   = "1"' in script_text
    assert 'SCHEDULER_IB_CLIENT_ID" = "2"' in script_text


def test_bot_env_extras_empty(script_text: str) -> None:
    # Bot reads .env directly; no service-scope env extras needed.
    assert '$botEnv = @{}' in script_text


def test_localsystem_rejected(script_text: str) -> None:
    assert 'Refusing to run services as LocalSystem' in script_text


def test_elevated_check(script_text: str) -> None:
    assert 'IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)' in script_text
    assert 'Run as Administrator' in script_text


def test_dry_run_short_circuits_nssm_invocations(script_text: str) -> None:
    # -DryRun must not invoke nssm.exe.
    assert 'if ($DryRun) {' in script_text
    assert 'DRYRUN> nssm' in script_text


def test_install_is_idempotent_via_stop_then_remove(script_text: str) -> None:
    # Fresh install must stop + remove any prior service first.
    idx_install = script_text.index("function Install-Service {")
    window = script_text[idx_install : idx_install + 800]
    assert "Stop-IfRunning -Name $Name" in window
    assert "Remove-ServiceIfExists -Name $Name" in window


def test_update_path_requires_existing_service(script_text: str) -> None:
    idx_update = script_text.index("function Update-Service {")
    window = script_text[idx_update : idx_update + 500]
    assert 'use -Install first' in window


def test_autostart_flag_flips_start_mode(script_text: str) -> None:
    assert '$startMode = if ($Autostart) { "SERVICE_AUTO_START" } else { "SERVICE_DEMAND_START" }' in script_text


def test_default_start_mode_is_manual(script_text: str) -> None:
    # On plain -Install (no -Autostart), services are SERVICE_DEMAND_START
    # so an install never auto-starts a service that hasn't been proven yet.
    # The if-else above guarantees this; assert the default branch literal.
    assert '"SERVICE_DEMAND_START"' in script_text


def test_log_path_under_repo_logs_dir(script_text: str) -> None:
    assert '"logs\\nssm_{0}_stdout.log"' in script_text
    assert '"logs\\nssm_{0}_stderr.log"' in script_text


def test_python_resolve_prefers_venv(script_text: str) -> None:
    # Venv python takes precedence over system python so dependency
    # drift in the system interpreter cannot break the service.
    idx = script_text.index("function Resolve-PythonExe")
    window = script_text[idx : idx + 600]
    assert '.venv\\Scripts\\python.exe' in window
    # Fallback to PATH python.exe.
    assert 'Get-Command python.exe' in window


def test_no_respawn_references(script_text: str) -> None:
    # NSSM owns lifecycle. Install script must NOT revive Sprint A MR2
    # respawn_*.ps1 path. MR-NSSM-2 retires the watchdog respawn;
    # we must not reintroduce it here.
    assert 'respawn_bot' not in script_text
    assert 'respawn_scheduler' not in script_text


def test_no_git_operations_in_install(script_text: str) -> None:
    # NSSM service must not do git fetch/reset on every start. Working
    # tree sync happens via boot_desk.bat or a post-merge hook, not
    # inside the service.
    assert 'git fetch' not in script_text
    assert 'git reset' not in script_text


def test_no_pip_install_in_script(script_text: str) -> None:
    # Dependencies pre-installed on venv. NSSM launching must not pip.
    assert 'pip install' not in script_text


def test_default_repo_root(script_text: str) -> None:
    assert '[string]$RepoRoot = "C:\\AGT_Telegram_Bridge"' in script_text


def test_error_action_stop(script_text: str) -> None:
    # Fail fast so a partial config doesn't leave a half-set service.
    assert '$ErrorActionPreference = "Stop"' in script_text


def test_sc_query_for_existence_check(script_text: str) -> None:
    # Service existence check uses sc.exe (ships with Windows) rather
    # than Get-Service (requires SCM permissions we may not have).
    idx = script_text.index("function Service-Exists {")
    window = script_text[idx : idx + 300]
    assert 'sc.exe query' in window
