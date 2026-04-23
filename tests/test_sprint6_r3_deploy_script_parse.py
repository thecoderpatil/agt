"""
tests/test_sprint6_r3_deploy_script_parse.py

Sprint 6 Mega-MR 1 §1C — R3 regression guard.

Regression context: Sprint 5 R3 was the `deploy.ps1` inline-Python
tokenizer error. A nested-quoted inline `python -c "..."` block was
appended to deploy.ps1 and failed the PowerShell parser at runtime —
deploy halted before reaching the atomic rotation. Fixed in MR !225 by
relocating the integrity check into a standalone `integrity_check.py`
script.

This guard asserts: EVERY `.ps1` file under `scripts/deploy/` parses
cleanly under the PowerShell Language Parser (zero parse errors).

Uses `[System.Management.Automation.Language.Parser]::ParseFile` via
subprocess. Skips gracefully if `powershell` is not on PATH (CI Linux
runners without PowerShell installed).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO / "scripts" / "deploy"


def _powershell_available() -> bool:
    return shutil.which("powershell") is not None or shutil.which("pwsh") is not None


def _powershell_cmd() -> str:
    if shutil.which("powershell"):
        return "powershell"
    return "pwsh"


def _parse_ps1(script_path: Path) -> tuple[int, str]:
    """Return (error_count, stderr) from PowerShell AST parse."""
    ps_cmd = (
        "$errs = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script_path}', [ref]$null, [ref]$errs); "
        "if ($errs) { "
        "  $errs | ForEach-Object { Write-Output ('ERROR: ' + $_.Message) }; "
        "  Write-Output ('COUNT: ' + $errs.Count) "
        "} else { "
        "  Write-Output 'COUNT: 0' "
        "}"
    )
    result = subprocess.run(
        [_powershell_cmd(), "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + "\n" + result.stderr
    count = 0
    for line in output.splitlines():
        if line.strip().startswith("COUNT:"):
            try:
                count = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return count, output


@pytest.mark.skipif(not _powershell_available(), reason="PowerShell not on PATH")
def test_r3_all_deploy_ps1_files_parse_cleanly():
    ps1_files = sorted(DEPLOY_DIR.glob("*.ps1"))
    assert ps1_files, (
        f"Expected at least one .ps1 file in {DEPLOY_DIR}. Did the deploy "
        "directory move?"
    )
    failures: list[tuple[Path, int, str]] = []
    for script in ps1_files:
        err_count, output = _parse_ps1(script)
        if err_count != 0:
            failures.append((script, err_count, output))

    assert not failures, (
        "Sprint 6 R3: PowerShell parse errors in scripts/deploy/:\n"
        + "\n".join(
            f"  {f[0].name}: {f[1]} error(s)\n{f[2]}"
            for f in failures
        )
    )
