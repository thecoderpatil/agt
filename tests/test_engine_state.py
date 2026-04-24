"""Sprint 6 Mega-MR 5 — engine_state repo tests."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = [pytest.mark.sprint_a, pytest.mark.agt_tripwire_exempt]


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "engine_state.db"
    # Empty placeholder so the migration's get_db_connection succeeds.
    with closing(sqlite3.connect(str(db))) as c:
        c.execute("SELECT 1")
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass
    from scripts.migrate_engine_state import run
    run(db_path=db)
    return db


def test_migration_creates_table_and_seeds_four_engines(seeded_db):
    with closing(sqlite3.connect(str(seeded_db))) as conn:
        rows = conn.execute(
            "SELECT engine, canary_step, halted FROM engine_state ORDER BY engine"
        ).fetchall()
    engines = [r[0] for r in rows]
    assert engines == ["entry", "exit", "harvest", "roll"]
    assert all(r[1] == "paper" for r in rows)
    assert all(r[2] == 0 for r in rows)


def test_migration_is_idempotent(seeded_db):
    from scripts.migrate_engine_state import run
    run(db_path=seeded_db)
    run(db_path=seeded_db)
    with closing(sqlite3.connect(str(seeded_db))) as conn:
        count = conn.execute("SELECT COUNT(*) FROM engine_state").fetchone()[0]
    assert count == 4


def test_get_state_returns_row_for_known_engine(seeded_db):
    from agt_equities.engine_state import get_state
    state = get_state("exit", db_path=seeded_db)
    assert state is not None
    assert state["engine"] == "exit"
    assert state["canary_step"] == "paper"
    assert state["halted"] == 0


def test_get_state_rejects_unknown_engine(seeded_db):
    from agt_equities.engine_state import get_state
    with pytest.raises(ValueError, match="engine must be one of"):
        get_state("bogus", db_path=seeded_db)


def test_halt_engine_flips_halted_flag(seeded_db):
    from agt_equities.engine_state import halt_engine, get_state
    ok = halt_engine("exit", reason="test_trip", db_path=seeded_db)
    assert ok is True
    state = get_state("exit", db_path=seeded_db)
    assert state["halted"] == 1
    assert state["halted_reason"] == "test_trip"
    assert state["halted_at_utc"] is not None


def test_halt_engine_requires_reason(seeded_db):
    from agt_equities.engine_state import halt_engine
    with pytest.raises(ValueError, match="reason is required"):
        halt_engine("exit", reason="", db_path=seeded_db)


def test_resume_engine_clears_halt_fields(seeded_db):
    from agt_equities.engine_state import halt_engine, resume_engine, get_state
    halt_engine("roll", reason="test_trip", db_path=seeded_db)
    ok = resume_engine("roll", db_path=seeded_db)
    assert ok is True
    state = get_state("roll", db_path=seeded_db)
    assert state["halted"] == 0
    assert state["halted_reason"] is None
    assert state["halted_at_utc"] is None


def test_advance_canary_step_paper_to_canary_1(seeded_db):
    from agt_equities.engine_state import advance_canary_step, get_state
    ok = advance_canary_step(
        "exit", from_step="paper", to_step="canary_1", db_path=seeded_db,
    )
    assert ok is True
    assert get_state("exit", db_path=seeded_db)["canary_step"] == "canary_1"


def test_advance_canary_step_rejects_invalid_pair(seeded_db):
    from agt_equities.engine_state import advance_canary_step
    with pytest.raises(ValueError, match="invalid forward advance"):
        advance_canary_step(
            "exit", from_step="paper", to_step="live", db_path=seeded_db,
        )


def test_advance_canary_step_rejects_backward(seeded_db):
    from agt_equities.engine_state import advance_canary_step
    with pytest.raises(ValueError, match="invalid forward advance"):
        advance_canary_step(
            "exit", from_step="canary_2", to_step="canary_1", db_path=seeded_db,
        )


def test_any_engine_in_prior_canary_false_when_all_paper(seeded_db):
    from agt_equities.engine_state import any_engine_in_prior_canary
    assert any_engine_in_prior_canary("entry", db_path=seeded_db) is False


def test_any_engine_in_prior_canary_true_when_exit_halted(seeded_db):
    from agt_equities.engine_state import halt_engine, any_engine_in_prior_canary
    halt_engine("exit", reason="test", db_path=seeded_db)
    # entry is after exit in sequence; exit halted should trip guard for entry
    assert any_engine_in_prior_canary("entry", db_path=seeded_db) is True


def test_any_engine_in_prior_canary_exit_has_no_priors(seeded_db):
    """exit is the first engine in the sequence; nothing can be prior."""
    from agt_equities.engine_state import any_engine_in_prior_canary
    assert any_engine_in_prior_canary("exit", db_path=seeded_db) is False


def test_list_all_returns_four_rows(seeded_db):
    from agt_equities.engine_state import list_all
    rows = list_all(db_path=seeded_db)
    assert len(rows) == 4
    assert {r["engine"] for r in rows} == {"exit", "roll", "harvest", "entry"}
