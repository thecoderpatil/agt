"""Sprint 8 Mega-MR 3 — decision_outcomes schema tightening tests (DR B6)."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _seed_base_schema(db: Path) -> None:
    """Seed base decision_outcomes table (Sprint 6 MR !229 migration)."""
    from scripts.migrate_decision_outcomes import run as base_migrate
    base_migrate(db_path=db)


def _seed_full_schema(db: Path) -> None:
    """Seed base + Sprint 8 tightening columns."""
    _seed_base_schema(db)
    from scripts.migrate_decision_outcomes_tightening import run as tighten_migrate
    tighten_migrate(db_path=db)


@pytest.fixture
def base_db(tmp_path, monkeypatch):
    """Fresh DB with only the base decision_outcomes schema (pre-tightening)."""
    db = tmp_path / "base.db"
    with closing(sqlite3.connect(str(db))) as c:
        c.execute("SELECT 1")
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass
    _seed_base_schema(db)
    return db


@pytest.fixture
def tightened_db(tmp_path, monkeypatch):
    """Fresh DB with base + tightening migration applied."""
    db = tmp_path / "tightened.db"
    with closing(sqlite3.connect(str(db))) as c:
        c.execute("SELECT 1")
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass
    _seed_full_schema(db)
    return db


@pytest.fixture(autouse=True)
def _reset_warning_state():
    """Clear the module-level warning state so each test starts fresh."""
    from agt_equities import decision_outcome_repo
    decision_outcome_repo._warning_state.clear()
    yield
    decision_outcome_repo._warning_state.clear()


# ---- Migration tests -------------------------------------------------------


def test_migration_adds_three_columns_idempotent(base_db):
    """Apply tightening migration; re-apply; no error; three new columns present."""
    from scripts.migrate_decision_outcomes_tightening import run as migrate
    # First apply
    result1 = migrate(db_path=base_db)
    assert sorted(result1["added"]) == [
        "config_hash", "kill_switch_invocation_ref", "triggering_rule_id",
    ]
    assert result1["skipped_existing"] == []
    # Re-apply
    result2 = migrate(db_path=base_db)
    assert result2["added"] == []
    assert sorted(result2["skipped_existing"]) == [
        "config_hash", "kill_switch_invocation_ref", "triggering_rule_id",
    ]

    with closing(sqlite3.connect(str(base_db))) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_outcomes)")}
    assert "config_hash" in cols
    assert "triggering_rule_id" in cols
    assert "kill_switch_invocation_ref" in cols


def test_migration_backfills_pre_migration_unknown_for_legacy_rows(base_db):
    """Existing rows get 'pre_migration_unknown' in the two required columns."""
    # Pre-insert a row on the BASE schema (no new cols yet).
    with closing(sqlite3.connect(str(base_db))) as conn:
        conn.execute(
            """
            INSERT INTO decision_outcomes (
                decision_id, engine, canary_step, fire_timestamp_utc,
                account_id, household, gate_snapshot_json, inputs_json
            ) VALUES ('legacy-1', 'exit', 'paper', '2026-04-01T00:00:00+00:00',
                      'U1', 'T', '{}', '{}')
            """
        )
        conn.commit()
    # Apply tightening
    from scripts.migrate_decision_outcomes_tightening import run as migrate
    migrate(db_path=base_db)
    # Legacy row must have the sentinel backfilled
    with closing(sqlite3.connect(str(base_db))) as conn:
        ch, tri, ks = conn.execute(
            "SELECT config_hash, triggering_rule_id, kill_switch_invocation_ref "
            "FROM decision_outcomes WHERE decision_id = ?",
            ("legacy-1",),
        ).fetchone()
    assert ch == "pre_migration_unknown"
    assert tri == "pre_migration_unknown"
    assert ks is None  # kill_switch NULL-allowed, no backfill


# ---- Repo tests ------------------------------------------------------------


def test_record_decision_outcome_config_hash_captured(tightened_db):
    from agt_equities.decision_outcome_repo import record_fire
    cfg = {"rule_version": "v9", "leverage_limit": 1.50}
    cfg_hash = hashlib.sha256(
        json.dumps(cfg, sort_keys=True).encode()
    ).hexdigest()[:16]
    record_fire(
        decision_id="d-cfg",
        engine="exit",
        canary_step="paper",
        account_id="U1", household="T",
        gate_snapshot={"g": "pass"}, inputs={"x": 1},
        config_hash=cfg_hash,
        triggering_rule_id="csp_screener_pass",
        db_path=tightened_db,
    )
    with closing(sqlite3.connect(str(tightened_db))) as conn:
        row = conn.execute(
            "SELECT config_hash, triggering_rule_id, kill_switch_invocation_ref "
            "FROM decision_outcomes WHERE decision_id = ?",
            ("d-cfg",),
        ).fetchone()
    assert row == (cfg_hash, "csp_screener_pass", None)


def test_record_decision_outcome_triggering_rule_id_captured(tightened_db):
    from agt_equities.decision_outcome_repo import record_fire
    record_fire(
        decision_id="d-rule",
        engine="roll",
        canary_step="canary_1",
        account_id="U1", household="T",
        gate_snapshot={}, inputs={},
        config_hash="abc123def4560000",
        triggering_rule_id="NO_BELOW_BASIS_CC",
        db_path=tightened_db,
    )
    with closing(sqlite3.connect(str(tightened_db))) as conn:
        tri = conn.execute(
            "SELECT triggering_rule_id FROM decision_outcomes WHERE decision_id = ?",
            ("d-rule",),
        ).fetchone()[0]
    assert tri == "NO_BELOW_BASIS_CC"


def test_record_decision_outcome_kill_switch_ref_nullable(tightened_db):
    from agt_equities.decision_outcome_repo import record_fire
    # Without kill_switch_invocation_ref — None is valid.
    record_fire(
        decision_id="d-noks",
        engine="entry",
        canary_step="paper",
        account_id="U1", household="T",
        gate_snapshot={}, inputs={},
        config_hash="aaaaaaaaaaaaaaaa",
        triggering_rule_id="test_rule",
        db_path=tightened_db,
    )
    # With kill_switch_invocation_ref
    record_fire(
        decision_id="d-ks",
        engine="harvest",
        canary_step="paper",
        account_id="U1", household="T",
        gate_snapshot={}, inputs={},
        config_hash="bbbbbbbbbbbbbbbb",
        triggering_rule_id="test_rule",
        kill_switch_invocation_ref="ks-evt-42",
        db_path=tightened_db,
    )
    with closing(sqlite3.connect(str(tightened_db))) as conn:
        rows = dict(conn.execute(
            "SELECT decision_id, kill_switch_invocation_ref FROM decision_outcomes "
            "WHERE decision_id IN ('d-noks', 'd-ks')"
        ).fetchall())
    assert rows["d-noks"] is None
    assert rows["d-ks"] == "ks-evt-42"


def test_repo_emits_warning_once_per_day_when_caller_omits_required_fields(
    tightened_db, caplog
):
    from agt_equities.decision_outcome_repo import record_fire
    caplog.set_level(logging.WARNING, logger="agt_equities.decision_outcome_repo")
    # First call — should warn
    record_fire(
        decision_id="d-w1",
        engine="exit", canary_step="paper",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=tightened_db,
    )
    warn_count_first = sum(
        1 for rec in caplog.records
        if "caller omitted required fields" in rec.getMessage()
    )
    assert warn_count_first == 1
    # Second call same day — should NOT warn again
    record_fire(
        decision_id="d-w2",
        engine="exit", canary_step="paper",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=tightened_db,
    )
    warn_count_second = sum(
        1 for rec in caplog.records
        if "caller omitted required fields" in rec.getMessage()
    )
    assert warn_count_second == 1, "warning must be once-per-day"
    # Both rows get the sentinel value written
    with closing(sqlite3.connect(str(tightened_db))) as conn:
        rows = conn.execute(
            "SELECT decision_id, config_hash, triggering_rule_id "
            "FROM decision_outcomes WHERE decision_id IN ('d-w1', 'd-w2') "
            "ORDER BY decision_id"
        ).fetchall()
    assert rows[0] == ("d-w1", "caller_did_not_provide", "caller_did_not_provide")
    assert rows[1] == ("d-w2", "caller_did_not_provide", "caller_did_not_provide")


def test_migration_creates_indexes(base_db):
    """Tightening migration creates the two expected indexes."""
    from scripts.migrate_decision_outcomes_tightening import run as migrate
    migrate(db_path=base_db)
    with closing(sqlite3.connect(str(base_db))) as conn:
        idxs = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='decision_outcomes'"
            )
        }
    assert "idx_decision_outcomes_triggering_rule" in idxs
    assert "idx_decision_outcomes_kill_switch" in idxs
