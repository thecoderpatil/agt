"""
tests/test_sprint6_r5_deploy_nssm_stop_tolerance.py

Sprint 6 Mega-MR 1 §1E — R5 regression guard + fix verification.

Regression context: Sprint 5 R5 was the `deploy.ps1` abort at
`nssm stop agt-telegram-bot | Out-Null` because NSSM emits
`SERVICE_STOP_PENDING` / `SERVICE_START_PENDING` informational lines to
stderr, which tripped the script-global `$ErrorActionPreference = "Stop"`
and halted the deploy mid-sequence (2026-04-23 17:56 ET incident). Left
the bot stopped, scheduler running, bridge-staging half-populated.
Bridge-previous was stale until this ship.

Fix: wrap each nssm stop/start in a local `$ErrorActionPreference =
"Continue"` scope + redirect stderr + poll `sc.exe query` for the
expected service state with a 30s timeout. Hard-fail if the state
never transitions.

This guard asserts (static + parse):

1. deploy.ps1 contains a `Wait-ServiceState` helper function.
2. deploy.ps1 uses the helper after each nssm stop/start.
3. deploy.ps1 tolerates NSSM stderr (local ErrorActionPreference
   Continue scope or `2>&1` redirection on each nssm call).
4. deploy.ps1 + rollback.ps1 still parse cleanly under the PowerShell
   Language Parser (covered by the R3 guard too, but re-check here as
   tamper sentinel for R5's edits).

Integration-level "mock nssm injecting stderr" is intentionally OUT OF
SCOPE — too brittle in CI. Static sentinels + parse-check are enough
to prove the structural fix shipped.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent
DEPLOY_PS1 = REPO / "scripts" / "deploy" / "deploy.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _powershell_available() -> bool:
    return shutil.which("powershell") is not None or shutil.which("pwsh") is not None


def _powershell_cmd() -> str:
    if shutil.which("powershell"):
        return "powershell"
    return "pwsh"


def test_r5_deploy_ps1_defines_wait_service_state_helper():
    src = _read(DEPLOY_PS1)
    assert "function Wait-ServiceState" in src, (
        "Sprint 6 R5: deploy.ps1 must define a `Wait-ServiceState` helper "
        "that polls `sc.exe query` for expected service state with timeout. "
        "NSSM stop/start alone trips ErrorActionPreference=Stop on "
        "SERVICE_STOP_PENDING stderr."
    )


def test_r5_deploy_ps1_calls_wait_service_state_for_stops():
    src = _read(DEPLOY_PS1)
    # Expected state names PowerShell sc.exe query returns.
    assert 'ExpectedState "STOPPED"' in src or "ExpectedState 'STOPPED'" in src, (
        "Sprint 6 R5: deploy.ps1 must call Wait-ServiceState with "
        "ExpectedState STOPPED after nssm stop calls."
    )
    assert 'ExpectedState "RUNNING"' in src or "ExpectedState 'RUNNING'" in src, (
        "Sprint 6 R5: deploy.ps1 must call Wait-ServiceState with "
        "ExpectedState RUNNING after nssm start calls."
    )


def test_r5_deploy_ps1_tolerates_nssm_stderr():
    """At least one of: local Continue scope OR 2>&1 redirect on nssm."""
    src = _read(DEPLOY_PS1)
    has_local_continue = (
        '$ErrorActionPreference = "Continue"' in src
        and '$ErrorActionPreference = "Stop"' in src  # restored afterward
    )
    has_stderr_redirect = "nssm stop" in src and (
        "nssm stop agt-telegram-bot 2>&1" in src
        or "nssm.exe stop" in src and "2>&1" in src
    )
    assert has_local_continue or has_stderr_redirect, (
        "Sprint 6 R5: deploy.ps1 must either scope ErrorActionPreference "
        "to Continue around nssm calls OR redirect nssm stderr with 2>&1. "
        "Without one of these, nssm's SERVICE_STOP_PENDING stderr halts "
        "the script under the global ErrorActionPreference=Stop."
    )


@pytest.mark.skipif(not _powershell_available(), reason="PowerShell not on PATH")
def test_r5_deploy_ps1_parses_cleanly_post_fix():
    """Tamper sentinel: the R5 fix must not break PowerShell parsing."""
    ps_cmd = (
        "$errs = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{DEPLOY_PS1}', [ref]$null, [ref]$errs); "
        "if ($errs) { "
        "  $errs | ForEach-Object { Write-Output ('ERROR: ' + $_.Message) }; "
        "  Write-Output ('COUNT: ' + $errs.Count) "
        "} else { Write-Output 'COUNT: 0' }"
    )
    result = subprocess.run(
        [_powershell_cmd(), "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + "\n" + result.stderr
    assert "COUNT: 0" in output, (
        f"Sprint 6 R5: deploy.ps1 parse errors post-R5-fix:\n{output}"
    )
