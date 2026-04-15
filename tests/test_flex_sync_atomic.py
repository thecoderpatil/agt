"""Sprint A / Unit A3 — flex_sync single-atomic-transaction tests.

Covers DT Q2 ruling (2026-04-14): flex_sync.run_sync must execute its data
side as a single ``BEGIN IMMEDIATE`` ... ``COMMIT``. A failure in any
section must roll back ALL section upserts AND the master_log_sync
status='success' update, while leaving the audit row (status='running' →
status='error') in place so the failure is observable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

from agt_equities import flex_sync
from agt_equities.db import get_db_connection
from agt_equities.schema import register_master_log_tables, register_operational_tables


# ---------------------------------------------------------------------------
# Test DB fixture — isolated, never touches prod.
# ---------------------------------------------------------------------------

# Three trivial test-only tables. Keeps the A3 atomic-transaction invariant
# decoupled from production schema — the target-table choice is incidental;
# what matters is that three upserts land atomically and roll back together.
_A3_TEST_TABLES = [
    """CREATE TABLE IF NOT EXISTS a3_test_section_one (
        transaction_id TEXT PRIMARY KEY,
        payload TEXT,
        last_synced_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS a3_test_section_two (
        transaction_id TEXT PRIMARY KEY,
        payload TEXT,
        last_synced_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS a3_test_section_three (
        transaction_id TEXT PRIMARY KEY,
        payload TEXT,
        last_synced_at TEXT NOT NULL
    )""",
]


@pytest.fixture
def flex_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "agt_flex_test.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000;")
    try:
        # master_log_sync + walker_warnings_log come from the real schema so
        # run_sync's audit-row writes hit valid tables.
        register_master_log_tables(conn)
        register_operational_tables(conn)
        for ddl in _A3_TEST_TABLES:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()

    # Redirect flex_sync._get_db at the module level. flex_sync still owns
    # its own DB_PATH constant — A3 keeps that wart out of scope, so we
    # patch the connection factory directly.
    def _factory():
        c = sqlite3.connect(db, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout = 15000;")
        return c

    monkeypatch.setattr(flex_sync, "_get_db", _factory)
    return db


# ---------------------------------------------------------------------------
# Synthetic section data — minimal valid shape for _upsert_rows.
# ---------------------------------------------------------------------------

def _three_synthetic_sections() -> list[dict]:
    """Three sections targeting A3 test-only tables.

    Decouples the atomic-transaction assertion from production schema —
    what's under test is rollback semantics across three upserts, not the
    contents of any particular master_log_* table.
    """
    return [
        {
            "table": "a3_test_section_one",
            "rows": [{"transaction_id": "T1", "payload": "one"}],
            "pk_cols": ["transaction_id"],
            "account_id": "U_TEST_1",
        },
        {
            "table": "a3_test_section_two",
            "rows": [{"transaction_id": "T2", "payload": "two"}],
            "pk_cols": ["transaction_id"],
            "account_id": "U_TEST_1",
        },
        {
            "table": "a3_test_section_three",
            "rows": [{"transaction_id": "T3", "payload": "three"}],
            "pk_cols": ["transaction_id"],
            "account_id": "U_TEST_1",
        },
    ]


def _disable_walker_and_side_effects(monkeypatch):
    """Suppress walker + post-success side effects so tests stay hermetic."""
    monkeypatch.setattr(flex_sync, "_persist_walker_warnings",
                        lambda conn, sync_id: None)
    # parse_flex_xml is overridden per-test below.
    # Side effects (desk_state, archive_handoffs, git push) all live in
    # try/except blocks inside run_sync and only fire post-commit on the
    # success path. They will fail and be swallowed in the error tests
    # without affecting assertions.


# ---------------------------------------------------------------------------
# Happy path — atomic commit lands all sections + status='success'.
# ---------------------------------------------------------------------------

def test_run_sync_happy_path_commits_all(flex_db: Path, monkeypatch):
    _disable_walker_and_side_effects(monkeypatch)
    monkeypatch.setattr(flex_sync, "parse_flex_xml",
                        lambda _b: _three_synthetic_sections())

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")
    assert result.status == "success"
    assert result.sections_processed == 3
    assert result.rows_received == 3
    assert result.rows_inserted == 3

    conn = sqlite3.connect(flex_db)
    try:
        n_trades = conn.execute("SELECT COUNT(*) FROM a3_test_section_one").fetchone()[0]
        n_corp = conn.execute("SELECT COUNT(*) FROM a3_test_section_two").fetchone()[0]
        n_xfer = conn.execute("SELECT COUNT(*) FROM a3_test_section_three").fetchone()[0]
        assert (n_trades, n_corp, n_xfer) == (1, 1, 1)
        n_sync = conn.execute(
            "SELECT COUNT(*) FROM master_log_sync WHERE status='success'"
        ).fetchone()[0]
        assert n_sync == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A3 invariant — failure mid-section rolls back ALL sections + status update.
# ---------------------------------------------------------------------------

def test_run_sync_atomic_rollback_on_mid_section_failure(flex_db: Path, monkeypatch):
    _disable_walker_and_side_effects(monkeypatch)
    monkeypatch.setattr(flex_sync, "parse_flex_xml",
                        lambda _b: _three_synthetic_sections())

    real_upsert = flex_sync._upsert_rows
    state = {"calls": 0}

    def _failing_upsert(conn, table, rows, pk_cols, now):
        state["calls"] += 1
        if state["calls"] == 3:
            raise RuntimeError("simulated section-3 fault")
        return real_upsert(conn, table, rows, pk_cols, now)

    monkeypatch.setattr(flex_sync, "_upsert_rows", _failing_upsert)

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")

    assert result.status == "error"
    assert "simulated section-3 fault" in (result.error_message or "")
    assert state["calls"] == 3

    conn = sqlite3.connect(flex_db)
    try:
        # ATOMIC ROLLBACK — sections 1 and 2 must NOT have persisted.
        n_trades = conn.execute("SELECT COUNT(*) FROM a3_test_section_one").fetchone()[0]
        n_corp = conn.execute("SELECT COUNT(*) FROM a3_test_section_two").fetchone()[0]
        n_xfer = conn.execute("SELECT COUNT(*) FROM a3_test_section_three").fetchone()[0]
        assert (n_trades, n_corp, n_xfer) == (0, 0, 0), \
            "A3 invariant violated: partial-section data persisted across rollback"

        # Audit row survives — running → error in a separate small txn.
        sync_rows = conn.execute(
            "SELECT status, error_message FROM master_log_sync WHERE sync_id=?",
            (result.sync_id,),
        ).fetchall()
        assert len(sync_rows) == 1
        assert sync_rows[0][0] == "error"
        assert "simulated section-3 fault" in (sync_rows[0][1] or "")

        # No spurious 'success' rows.
        n_success = conn.execute(
            "SELECT COUNT(*) FROM master_log_sync WHERE status='success'"
        ).fetchone()[0]
        assert n_success == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Walker-warning failure must NOT roll the success commit back.
# ---------------------------------------------------------------------------

def test_walker_warning_failure_is_non_fatal(flex_db: Path, monkeypatch):
    """Per A3 design: walker_warnings sits inside the txn but its failure
    is caught and swallowed so the section data still commits."""
    monkeypatch.setattr(flex_sync, "parse_flex_xml",
                        lambda _b: _three_synthetic_sections())

    def _broken_walker(conn, sync_id):
        raise RuntimeError("walker exploded")
    monkeypatch.setattr(flex_sync, "_persist_walker_warnings", _broken_walker)

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")
    assert result.status == "success"

    conn = sqlite3.connect(flex_db)
    try:
        n_trades = conn.execute("SELECT COUNT(*) FROM a3_test_section_one").fetchone()[0]
        assert n_trades == 1
        n_success = conn.execute(
            "SELECT COUNT(*) FROM master_log_sync WHERE status='success'"
        ).fetchone()[0]
        assert n_success == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit row exists even when XML parsing blows up before any data work.
# ---------------------------------------------------------------------------

def test_audit_row_survives_parse_failure(flex_db: Path, monkeypatch):
    _disable_walker_and_side_effects(monkeypatch)

    def _broken_parse(_b):
        raise RuntimeError("xml parse fault")
    monkeypatch.setattr(flex_sync, "parse_flex_xml", _broken_parse)

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")
    assert result.status == "error"
    assert "xml parse fault" in (result.error_message or "")

    conn = sqlite3.connect(flex_db)
    try:
        rows = conn.execute(
            "SELECT status, error_message FROM master_log_sync WHERE sync_id=?",
            (result.sync_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "error"
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# A5d.b — FLEX_SYNC_DIGEST alert enqueue tests
# ---------------------------------------------------------------------------

def test_run_sync_enqueues_flex_sync_digest_on_success(flex_db: Path, monkeypatch):
    """A5d.b: a successful run_sync must post a FLEX_SYNC_DIGEST alert
    onto the cross_daemon_alerts bus with the right payload shape."""
    _disable_walker_and_side_effects(monkeypatch)
    monkeypatch.setattr(flex_sync, "parse_flex_xml",
                        lambda _b: _three_synthetic_sections())

    captured: list[dict] = []

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    # Patch the source-of-truth attribute the lazy import resolves to.
    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")
    assert result.status == "success"

    digests = [c for c in captured if c["kind"] == "FLEX_SYNC_DIGEST"]
    assert len(digests) == 1, f"expected 1 FLEX_SYNC_DIGEST, got {captured}"
    rec = digests[0]
    assert rec["severity"] == "info"
    p = rec["payload"]
    assert p["sync_id"] == result.sync_id
    assert p["mode"] == "oneshot"
    assert p["sections_processed"] == result.sections_processed
    assert p["rows_received"] == result.rows_received
    assert p["rows_inserted"] == result.rows_inserted


def test_run_sync_does_not_enqueue_on_error(flex_db: Path, monkeypatch):
    """A5d.b: when the sync fails, no FLEX_SYNC_DIGEST should be enqueued.
    The error path runs the audit-update small-txn but skips the success-only
    enqueue block."""
    _disable_walker_and_side_effects(monkeypatch)

    def _broken_parse(_b):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(flex_sync, "parse_flex_xml", _broken_parse)

    captured: list[dict] = []
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda kind, payload, **kw: captured.append({"kind": kind, **kw}),
    )

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")
    assert result.status == "error"
    assert all(c["kind"] != "FLEX_SYNC_DIGEST" for c in captured), (
        f"unexpected enqueue on error path: {captured}"
    )


def test_run_sync_enqueue_failure_does_not_crash(flex_db: Path, monkeypatch):
    """A5d.b: a raise inside enqueue_alert must NOT propagate out of
    run_sync. The data txn already committed; an alert-bus failure is
    best-effort and must be logged + swallowed."""
    _disable_walker_and_side_effects(monkeypatch)
    monkeypatch.setattr(flex_sync, "parse_flex_xml",
                        lambda _b: _three_synthetic_sections())

    def boom(*a, **kw):
        raise RuntimeError("simulated alert-bus down")

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", boom)

    result = flex_sync.run_sync(flex_sync.SyncMode.ONESHOT, xml_bytes=b"<x/>")
    # Sync success must stand even when the alert enqueue blows up.
    assert result.status == "success"
