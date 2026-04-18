"""ADR-013 Dispatch B — verify schema + backfill + view."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_with_incidents() -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        # Minimal incidents table matching prod schema (column names per
        # reference_incidents_schema.md memory: detected_at / last_action_at).
        conn.execute("""
            CREATE TABLE incidents (
                incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                invariant_name TEXT NOT NULL,
                detected_at TIMESTAMP NOT NULL,
                last_action_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'open',
                payload TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO incidents (invariant_name, detected_at, status) VALUES (?, datetime('now'), 'open')",
            [
                ("NO_PHANTOM_FILLS",),                  # internal default
                ("UNKNOWN_ACCT_1190",),                 # backfill → broker
                ("NO_MISSING_DAEMON_HEARTBEAT",),       # backfill → vendor
                ("NO_MISSING_DAEMON_HEARTBEAT",),       # backfill → vendor (second row)
                ("WALKER_RECONCILIATION_DRIFT",),       # internal default
            ],
        )
        conn.commit()
    yield p
    p.unlink(missing_ok=True)


def _migrate(db_path: Path) -> dict:
    from scripts.migrate_incidents_dual_ledger import run
    return run(db_path=db_path)


@pytest.mark.sprint_a
def test_alters_add_columns(tmp_db_with_incidents):
    _migrate(tmp_db_with_incidents)
    with sqlite3.connect(tmp_db_with_incidents) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()]
    for c in ("fault_source", "severity_tier", "burn_weight"):
        assert c in cols


@pytest.mark.sprint_a
def test_migration_idempotent(tmp_db_with_incidents):
    first = _migrate(tmp_db_with_incidents)
    second = _migrate(tmp_db_with_incidents)
    assert first["alters_applied"] == 3
    assert second["alters_applied"] == 0


@pytest.mark.sprint_a
def test_backfill_classifies_external_faults(tmp_db_with_incidents):
    _migrate(tmp_db_with_incidents)
    with sqlite3.connect(tmp_db_with_incidents) as conn:
        broker = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE fault_source = 'broker'"
        ).fetchone()[0]
        vendor = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE fault_source = 'vendor'"
        ).fetchone()[0]
        internal = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE fault_source = 'internal'"
        ).fetchone()[0]
    assert broker == 1
    assert vendor == 2
    assert internal == 2


@pytest.mark.sprint_a
def test_view_separates_internal_from_external(tmp_db_with_incidents):
    _migrate(tmp_db_with_incidents)
    with sqlite3.connect(tmp_db_with_incidents) as conn:
        row = conn.execute("SELECT internal_burn, external_burn FROM v_error_budget_72h").fetchone()
    internal_burn, external_burn = row
    # 2 internal incidents × default burn_weight 10 = 20.
    assert internal_burn == 20
    # 1 broker (10) + 2 vendor (1 each) = 12.
    assert external_burn == 12
