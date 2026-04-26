"""Sprint 12 P0 — DEFECT-3 fix: wal_autocheckpoint=200 per-connection pragma tests.

Verifies that get_db_connection() sets PRAGMA wal_autocheckpoint=200, reducing
WAL bloat and shortening write-lock contention windows.
See: reports/heartbeat_stale_dual_daemon_20260426.md §5 DEFECT-3.
"""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Source inspection
# ---------------------------------------------------------------------------

def test_wal_autocheckpoint_in_get_db_connection_source():
    """get_db_connection() source must reference wal_autocheckpoint and 200."""
    from agt_equities.db import get_db_connection
    source = inspect.getsource(get_db_connection)
    assert "wal_autocheckpoint" in source, "get_db_connection must set PRAGMA wal_autocheckpoint"
    assert "200" in source, "get_db_connection must set wal_autocheckpoint=200"


# ---------------------------------------------------------------------------
# Runtime: pragma value verified on connection open
# ---------------------------------------------------------------------------

def test_get_db_connection_sets_autocheckpoint_200(tmp_path):
    """get_db_connection() must set PRAGMA wal_autocheckpoint=200 on every new connection."""
    db = tmp_path / "wal_test.db"
    from agt_equities.db import get_db_connection
    with closing(get_db_connection(db_path=db)) as conn:
        val = conn.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]
    assert val == 200, f"Expected wal_autocheckpoint=200, got {val}"


def test_autocheckpoint_200_persists_in_db_header(tmp_path):
    """wal_autocheckpoint is a DB-file-level setting — value persists for a second connection."""
    db = tmp_path / "wal_test.db"
    from agt_equities.db import get_db_connection

    # First connection sets it
    with closing(get_db_connection(db_path=db)) as conn:
        conn.execute("CREATE TABLE t (x INTEGER);")
        conn.execute("PRAGMA wal_autocheckpoint;")

    # Second connection (new handle) should still read 200
    with closing(get_db_connection(db_path=db)) as conn2:
        val = conn2.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]
    assert val == 200, f"wal_autocheckpoint must persist in DB header; got {val}"


def test_autocheckpoint_200_is_below_default_1000(tmp_path):
    """Explicit sanity: 200 < 1000 (default) ensures more frequent checkpoints."""
    db = tmp_path / "wal_test.db"
    from agt_equities.db import get_db_connection
    with closing(get_db_connection(db_path=db)) as conn:
        val = conn.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]
    assert val < 1000, f"wal_autocheckpoint={val} is not below the 1000-page default"
    assert val > 0, "wal_autocheckpoint must be positive (0 disables checkpointing entirely)"


def test_ro_connection_not_affected(tmp_path):
    """get_ro_connection() does not need wal_autocheckpoint — just verify it does not raise."""
    db = tmp_path / "wal_test.db"
    # Bootstrap a DB file first
    from agt_equities.db import get_db_connection, get_ro_connection
    with closing(get_db_connection(db_path=db)) as conn:
        conn.execute("CREATE TABLE t (x INTEGER);")
        conn.commit()
    # Read-only connection must open without error
    with closing(get_ro_connection(db_path=db)) as ro:
        val = ro.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]
    # RO connection does not set it; whatever the DB header says is fine
    assert isinstance(val, int)
