"""Sprint 12 P1 — Secondary TX Hygiene + Symlink Defense source-inspection tests.

Covers:
- Fix 4: init_pragmas wal_autocheckpoint aligned to 200 (removes 4000 override on startup conn)
- Fix 2: heartbeat_stale_alert.ps1 symlink-resolve block present

See: reports/sprint_12_p1_secondary_tx_hygiene_draft_20260426.md
"""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fix 4: init_pragmas wal_autocheckpoint consistency
# ---------------------------------------------------------------------------

def test_init_pragmas_wal_autocheckpoint_200():
    """init_pragmas() must set wal_autocheckpoint=200, not 4000.

    MR !269 added per-connection PRAGMA wal_autocheckpoint=200 in
    get_db_connection(). The startup sequence in telegram_bot.py opens a
    connection (→ sets 200) then calls init_pragmas() on it (→ was 4000,
    overriding the 200). This test enforces the aligned value.
    """
    from agt_equities.db import init_pragmas
    source = inspect.getsource(init_pragmas)

    assert "wal_autocheckpoint=200" in source or "wal_autocheckpoint = 200" in source, (
        "init_pragmas must set PRAGMA wal_autocheckpoint=200 to be consistent with get_db_connection()"
    )
    assert "wal_autocheckpoint=4000" not in source, (
        "init_pragmas must NOT set wal_autocheckpoint=4000 (overrides the per-connection 200)"
    )


def test_init_pragmas_runtime_sets_200(tmp_path):
    """Calling init_pragmas on a live connection confirms the pragma takes effect."""
    from agt_equities.db import get_db_connection, init_pragmas
    db = tmp_path / "init_pragmas_test.db"
    with closing(get_db_connection(db_path=db)) as conn:
        init_pragmas(conn)
        val = conn.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]
    assert val == 200, f"init_pragmas must result in wal_autocheckpoint=200; got {val}"


# ---------------------------------------------------------------------------
# Fix 2: heartbeat_stale_alert.ps1 symlink-resolve defense
# ---------------------------------------------------------------------------

def test_heartbeat_stale_alert_symlink_resolve_present():
    """heartbeat_stale_alert.ps1 must contain symlink-resolve block after $DB_PATH assignment.

    Defense-in-depth: if machine-level AGT_DB_PATH ever points at a symlink
    (e.g. after NSSM reinstall), the script resolves to the real path before
    opening the SQLite connection, preventing WAL-split false HEARTBEAT_STALE.
    """
    ps1_path = Path(__file__).parent.parent / "scripts" / "heartbeat_stale_alert.ps1"
    assert ps1_path.exists(), f"heartbeat_stale_alert.ps1 not found at {ps1_path}"
    source = ps1_path.read_text(encoding="utf-8")

    db_path_line = next(
        (i for i, ln in enumerate(source.splitlines()) if "$DB_PATH = $env:AGT_DB_PATH" in ln),
        None,
    )
    assert db_path_line is not None, "heartbeat_stale_alert.ps1 must contain '$DB_PATH = $env:AGT_DB_PATH'"

    lines_after = source.splitlines()[db_path_line:]
    search_window = "\n".join(lines_after[:6])
    assert "Get-Item" in search_window, (
        "Symlink-resolve block (Get-Item ...) must appear within 5 lines of $DB_PATH assignment"
    )
    assert "$_symTarget" in search_window, (
        "Symlink-resolve block must set $_symTarget within 5 lines of $DB_PATH assignment"
    )
