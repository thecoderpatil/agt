"""ADR-020 Phase A piece 4 — CI containment contract unit tests.

Seven tests covering:
- ci_containment_assert: passes on test DB, fails on wrong path, fails on unset env.
- ci_db_canary: proves write succeeds on a tmp SQLite DB.
- ci_window_check: RTH blocked, Flex blocked, off-hours passes.

All @pytest.mark.sprint_a. No IB, network, or prod DB dependency.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Import the script modules via sys.path (scripts/ has no __init__.py;
# project root is on sys.path via pytest rootdir).
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.ci_containment_assert import (  # noqa: E402
    assert_ci_isolation,
)
from scripts.ci_db_canary import write_canary_probe  # noqa: E402
from scripts.ci_window_check import is_sensitive_window  # noqa: E402

import scripts.ci_containment_assert as _assert_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Assertion tests
# ---------------------------------------------------------------------------


@pytest.mark.sprint_a
def test_assert_ci_isolation_passes_on_test_db(monkeypatch, tmp_path):
    """Happy path: AGT_DB_PATH resolves to the configured CI test DB.

    We monkeypatch EXPECTED_CI_DB_PATH to tmp_path so the test does not
    require C:\\GitLab-Runner to exist on every dev box.
    """
    fake_db = tmp_path / "agt_desk.db"
    fake_db_resolved = fake_db.resolve()
    monkeypatch.setenv("AGT_DB_PATH", str(fake_db))
    monkeypatch.setattr(_assert_mod, "EXPECTED_CI_DB_PATH", fake_db_resolved)
    monkeypatch.setattr(_assert_mod, "PROD_DB_FORBIDDEN_PATHS", [])
    monkeypatch.delenv("AGT_CI_ACL_ENFORCED", raising=False)
    # Should complete without raising SystemExit
    assert_ci_isolation()


@pytest.mark.sprint_a
def test_assert_ci_isolation_fails_on_prod_db(monkeypatch, tmp_path):
    """AGT_DB_PATH resolves to a path that != EXPECTED_CI_DB_PATH -> SystemExit(1)."""
    wrong_db = tmp_path / "wrong.db"
    monkeypatch.setenv("AGT_DB_PATH", str(wrong_db))
    # EXPECTED points somewhere else
    monkeypatch.setattr(_assert_mod, "EXPECTED_CI_DB_PATH", tmp_path / "right.db")
    monkeypatch.setattr(_assert_mod, "PROD_DB_FORBIDDEN_PATHS", [])
    monkeypatch.delenv("AGT_CI_ACL_ENFORCED", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        assert_ci_isolation()
    assert exc_info.value.code == 1


@pytest.mark.sprint_a
def test_assert_ci_isolation_fails_on_unset_env(monkeypatch):
    """AGT_DB_PATH unset -> SystemExit(1) with clear message."""
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    monkeypatch.delenv("AGT_CI_ACL_ENFORCED", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        assert_ci_isolation()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Canary test
# ---------------------------------------------------------------------------


@pytest.mark.sprint_a
def test_canary_probe_writes_test_db(monkeypatch, tmp_path):
    """Canary inserts a probe row into a writable tmp DB and the row is verifiable."""
    db_path = tmp_path / "canary_test.db"
    monkeypatch.setenv("AGT_DB_PATH", str(db_path))
    monkeypatch.setenv("CI_RUNNER_ID", "runner-unit-test-42")
    monkeypatch.setenv("CI_JOB_ID", "job-unit-test-1")

    write_canary_probe()

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT * FROM _ci_canary").fetchall()
    conn.close()

    assert len(rows) == 1, f"Expected 1 probe row, got {len(rows)}"
    assert rows[0][1] == "runner-unit-test-42"
    assert rows[0][2] == "job-unit-test-1"


# ---------------------------------------------------------------------------
# Window-check tests
# ---------------------------------------------------------------------------


@pytest.mark.sprint_a
def test_window_check_rth_blocked():
    """10:00 ET on a weekday (Friday) falls inside RTH -> sensitive, 'RTH window block'."""
    now_et = datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("US/Eastern"))
    is_sensitive, window = is_sensitive_window(now_et)
    assert is_sensitive is True
    assert "RTH" in window


@pytest.mark.sprint_a
def test_window_check_flex_blocked():
    """17:05 ET on a weekday falls inside the Flex sync window -> sensitive."""
    now_et = datetime(2026, 4, 24, 17, 5, tzinfo=ZoneInfo("US/Eastern"))
    is_sensitive, window = is_sensitive_window(now_et)
    assert is_sensitive is True
    assert "Flex" in window


@pytest.mark.sprint_a
def test_window_check_off_hours_passes():
    """22:00 ET on a weekday is outside all sensitive windows -> not sensitive."""
    now_et = datetime(2026, 4, 24, 22, 0, tzinfo=ZoneInfo("US/Eastern"))
    is_sensitive, window = is_sensitive_window(now_et)
    assert is_sensitive is False
    assert window == "off-hours"
