"""Sprint 6 add-on: post_start_smoke.ps1 PowerShell AST parse guard.

Mirrors the R3 regression guard pattern from Sprint 6 MR 1 but scoped
to post_start_smoke.ps1 so failures in this specific script surface
cleanly in CI. The broader R3 test at
``test_sprint6_r3_deploy_script_parse.py`` also covers this file by
walking ``scripts/deploy/`` — this test exists for explicit ownership
and for the boot-smoke ship report's verifiability.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent
SMOKE_PS1 = REPO / "scripts" / "deploy" / "post_start_smoke.ps1"


def _powershell_available() -> bool:
    return shutil.which("powershell") is not None or shutil.which("pwsh") is not None


def _powershell_cmd() -> str:
    return "powershell" if shutil.which("powershell") else "pwsh"


def test_post_start_smoke_ps1_exists():
    assert SMOKE_PS1.exists(), f"post_start_smoke.ps1 missing at {SMOKE_PS1}"


def test_post_start_smoke_ps1_is_ascii_only():
    """Per feedback_ps1_ascii_only.md — PowerShell scripts must be ASCII.

    The R3 em-dash incident in rollback.ps1 (Sprint 6 MR 1) is the anchor
    for this rule. Encoding bugs are silent until a CI runner with a
    different default code page trips on them.
    """
    raw = SMOKE_PS1.read_bytes()
    for i, b in enumerate(raw):
        if b > 0x7F:
            snippet = raw[max(0, i - 30): i + 30].decode("utf-8", errors="replace")
            raise AssertionError(
                f"Non-ASCII byte 0x{b:02x} at offset {i}; context: {snippet!r}"
            )


@pytest.mark.skipif(
    not _powershell_available(),
    reason="PowerShell not on PATH (CI Linux runner)",
)
def test_post_start_smoke_ps1_parses_cleanly():
    """PowerShell Language Parser must report zero parse errors."""
    ps_cmd = (
        "$errs = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SMOKE_PS1}', [ref]$null, [ref]$errs); "
        "if ($errs) { $errs | ForEach-Object { Write-Output ('ERROR: ' + $_.Message) } }; "
        "Write-Output ('COUNT: ' + $errs.Count)"
    )
    result = subprocess.run(
        [_powershell_cmd(), "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"PS invocation failed: {result.stderr}"
    assert "COUNT: 0" in result.stdout, (
        f"Parse errors present in post_start_smoke.ps1:\n{result.stdout}"
    )
