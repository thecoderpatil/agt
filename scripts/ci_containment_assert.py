"""Phase A piece 4 -- CI containment contract assertion.

Refuses to start the test suite if the runtime configuration could allow
CI code to mutate prod state. Run as the first step of every CI job's
before_script.

Failure exits with code 1 + message on stderr. Success is silent.
Phase A piece 4 -- CI containment contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Canonical CI test DB path (config.toml environment line).
EXPECTED_CI_DB_PATH = Path(r"C:\GitLab-Runner\test_data\agt_desk.db").resolve()

# Paths CI must NEVER write to (forbidden prod-state paths).
PROD_DB_FORBIDDEN_PATHS: list[Path] = [
    Path(r"C:\AGT_Runtime\state\agt_desk.db").resolve(),
    Path(r"C:\AGT_Telegram_Bridge\agt_desk.db").resolve(),  # legacy dev fixture
]

# Directories the ACL probe verifies the runner cannot write to.
# Scoped to actual prod runtime state -- C:\AGT_Telegram_Bridge is excluded
# (Architect dev worktree; may carry broader Authenticated Users grants that
# are outside Gate 2 scope and should not block CI enforcement).
PROD_STATE_PROBE_DIRS: list[Path] = [
    Path(r"C:\AGT_Runtime\state").resolve(),
]


def _check_env_var() -> Path:
    """Assert AGT_DB_PATH is set and resolves to the canonical CI test DB."""
    raw = os.environ.get("AGT_DB_PATH")
    if not raw:
        _fail("AGT_DB_PATH is unset -- test suite cannot proceed without CI DB path")
    resolved = Path(raw).resolve()
    if resolved != EXPECTED_CI_DB_PATH:
        _fail(
            f"AGT_DB_PATH={resolved} does not match expected "
            f"CI test DB {EXPECTED_CI_DB_PATH}. "
            f"Check config.toml environment line."
        )
    return resolved


def _check_forbidden_paths(resolved: Path) -> None:
    """Assert AGT_DB_PATH does not point at any prod-state DB path."""
    for forbidden in PROD_DB_FORBIDDEN_PATHS:
        if resolved == forbidden:
            _fail(
                f"AGT_DB_PATH resolves to prod DB {resolved} -- "
                f"CI containment hard block"
            )


def _check_acl_probe() -> None:
    """Probe runner write access to prod-state dirs. Gated by AGT_CI_ACL_ENFORCED=true.

    Uses PROD_STATE_PROBE_DIRS (not PROD_DB_FORBIDDEN_PATHS parents) to avoid probing
    paths outside Gate 2 scope. Runner must not be able to write to any probe dir.
    """
    if os.environ.get("AGT_CI_ACL_ENFORCED", "").lower() != "true":
        return
    for prod_dir in PROD_STATE_PROBE_DIRS:
        if not prod_dir.exists():
            continue
        probe = prod_dir / ".ci_acl_probe"
        try:
            probe.touch()
            probe.unlink()
            _fail(
                f"runner can write to prod-state path {prod_dir} -- "
                f"ACL not enforced. Run scripts/ci_acl_apply.ps1 as Admin."
            )
        except (PermissionError, OSError):
            pass  # Expected: runner lacks write on prod state path.


def _fail(msg: str) -> None:
    print(f"CI CONTAINMENT FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def assert_ci_isolation() -> None:
    """Run all containment checks. Raises SystemExit(1) on any failure."""
    resolved = _check_env_var()
    _check_forbidden_paths(resolved)
    _check_acl_probe()


def main() -> int:
    try:
        assert_ci_isolation()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"CI CONTAINMENT FAIL (unexpected): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
