"""Sprint 6 Mega-MR 4A — decision_outcome_repo tests."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


def _seed_schema(db: Path) -> None:
    from scripts.migrate_decision_outcomes import run as migrate
    migrate(db_path=db)
    # Sprint 8 MR 3 (DR B6): apply tightening so new columns exist.
    from scripts.migrate_decision_outcomes_tightening import run as tighten
    tighten(db_path=db)


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "decision_outcomes.db"
    # Create empty file so get_db_connection can open it
    with closing(sqlite3.connect(str(db))) as c:
        c.execute("SELECT 1")
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass
    _seed_schema(db)
    return db


def test_migration_is_idempotent(seeded_db):
    """Running the migration twice does not fail."""
    from scripts.migrate_decision_outcomes import run
    run(db_path=seeded_db)
    # Still usable
    with closing(sqlite3.connect(str(seeded_db))) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_outcomes'"
        )
        assert cur.fetchone() is not None


def test_record_fire_inserts_row(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire
    record_fire(
        decision_id="d-1",
        engine="exit",
        canary_step="paper",
        account_id="U12345",
        household="TEST",
        gate_snapshot={"g1": "pass", "g2": "pass"},
        inputs={"ticker": "AAPL", "strike": 150.0},
        db_path=seeded_db,
    )
    with closing(sqlite3.connect(str(seeded_db))) as conn:
        row = conn.execute(
            "SELECT engine, canary_step, account_id FROM decision_outcomes WHERE decision_id = ?",
            ("d-1",),
        ).fetchone()
    assert row == ("exit", "paper", "U12345")


def test_record_fire_is_idempotent_on_decision_id(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire
    for _ in range(3):
        record_fire(
            decision_id="d-2",
            engine="roll",
            canary_step="canary_1",
            account_id="U12345",
            household="TEST",
            gate_snapshot={},
            inputs={},
            db_path=seeded_db,
        )
    with closing(sqlite3.connect(str(seeded_db))) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM decision_outcomes WHERE decision_id = ?", ("d-2",)
        ).fetchone()[0]
    assert count == 1


def test_record_fire_rejects_bad_engine(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire
    with pytest.raises(ValueError, match="engine must be one of"):
        record_fire(
            decision_id="d-3",
            engine="bogus",
            canary_step="paper",
            account_id="U1",
            household="T",
            gate_snapshot={},
            inputs={},
            db_path=seeded_db,
        )


def test_record_fire_rejects_bad_canary_step(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire
    with pytest.raises(ValueError, match="canary_step must be one of"):
        record_fire(
            decision_id="d-4",
            engine="exit",
            canary_step="nonsense",
            account_id="U1",
            household="T",
            gate_snapshot={},
            inputs={},
            db_path=seeded_db,
        )


def test_update_fill_outcome_attaches_json(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire, update_fill_outcome
    record_fire(
        decision_id="d-5", engine="harvest", canary_step="paper",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    ok = update_fill_outcome(
        decision_id="d-5",
        fill_outcome={"filled_qty": 100, "avg_price": 50.25},
        db_path=seeded_db,
    )
    assert ok is True
    with closing(sqlite3.connect(str(seeded_db))) as conn:
        fill_json, updated_at = conn.execute(
            "SELECT fill_outcome_json, updated_at FROM decision_outcomes WHERE decision_id = ?",
            ("d-5",),
        ).fetchone()
    assert "100" in fill_json and "50.25" in fill_json
    assert updated_at is not None


def test_update_fill_outcome_missing_row_returns_false(seeded_db):
    from agt_equities.decision_outcome_repo import update_fill_outcome
    assert update_fill_outcome(
        decision_id="d-nonexistent", fill_outcome={"x": 1}, db_path=seeded_db,
    ) is False


def test_update_reconciliation_attaches_json(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire, update_reconciliation
    record_fire(
        decision_id="d-6", engine="entry", canary_step="canary_2",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    ok = update_reconciliation(
        decision_id="d-6",
        reconciliation_delta={"delta_pnl": -3.50, "delta_qty": 0},
        db_path=seeded_db,
    )
    assert ok is True


def test_get_fires_by_engine_returns_chronological(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire, get_fires_by_engine
    record_fire(
        decision_id="d-a", engine="exit", canary_step="paper",
        fire_timestamp_utc="2026-04-20T10:00:00+00:00",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    record_fire(
        decision_id="d-b", engine="exit", canary_step="paper",
        fire_timestamp_utc="2026-04-22T10:00:00+00:00",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    record_fire(
        decision_id="d-c", engine="roll", canary_step="paper",  # different engine
        fire_timestamp_utc="2026-04-23T10:00:00+00:00",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    fires = get_fires_by_engine(engine="exit", db_path=seeded_db)
    assert len(fires) == 2
    assert fires[0]["decision_id"] == "d-b"
    assert fires[1]["decision_id"] == "d-a"


def test_get_fires_by_engine_since_utc_filters(seeded_db):
    from agt_equities.decision_outcome_repo import record_fire, get_fires_by_engine
    record_fire(
        decision_id="d-old", engine="exit", canary_step="paper",
        fire_timestamp_utc="2026-04-10T10:00:00+00:00",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    record_fire(
        decision_id="d-new", engine="exit", canary_step="paper",
        fire_timestamp_utc="2026-04-22T10:00:00+00:00",
        account_id="U1", household="T", gate_snapshot={}, inputs={},
        db_path=seeded_db,
    )
    fires = get_fires_by_engine(
        engine="exit",
        since_utc="2026-04-15T00:00:00+00:00",
        db_path=seeded_db,
    )
    assert len(fires) == 1
    assert fires[0]["decision_id"] == "d-new"
