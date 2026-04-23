"""Sprint 6 Mega-MR 4B — incidents error-budget columns tests.

Migration adds `error_budget_tier` + `budget_consumed_pct` to the
existing incidents table, backfilling `error_budget_tier` from the
string `severity` column. Register() accepts the two new kwargs.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = [pytest.mark.sprint_a, pytest.mark.agt_tripwire_exempt]


def _bare_incidents_schema(db: Path) -> None:
    """Minimal pre-MR-4B incidents schema (no error_budget_tier)."""
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            """
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_key TEXT NOT NULL,
                invariant_id TEXT,
                severity TEXT NOT NULL,
                scrutiny_tier TEXT NOT NULL,
                status TEXT NOT NULL,
                detector TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                last_action_at TEXT NOT NULL,
                closed_at TEXT,
                consecutive_breaches INTEGER NOT NULL DEFAULT 1,
                observed_state TEXT,
                desired_state TEXT,
                confidence REAL,
                mr_iid INTEGER,
                ddiff_url TEXT,
                rejection_history TEXT
            )
            """
        )
        # Also the mirror table register() writes to.
        conn.execute(
            """
            CREATE TABLE remediation_incidents (
                incident_key TEXT PRIMARY KEY,
                detector TEXT,
                detected_at TEXT,
                status TEXT,
                updated_at TEXT
            )
            """
        )
        # Seed some historic rows with different severities.
        rows = [
            ("k1", None, "critical", "critical", "open", "test", "2026-04-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", None, 1, None, None, None, None, None, None),
            ("k2", None, "high",     "critical", "open", "test", "2026-04-02T00:00:00+00:00", "2026-04-02T00:00:00+00:00", None, 1, None, None, None, None, None, None),
            ("k3", None, "medium",   "canonical","open", "test", "2026-04-03T00:00:00+00:00", "2026-04-03T00:00:00+00:00", None, 1, None, None, None, None, None, None),
            ("k4", None, "warn",     "canonical","open", "test", "2026-04-04T00:00:00+00:00", "2026-04-04T00:00:00+00:00", None, 1, None, None, None, None, None, None),
        ]
        conn.executemany(
            "INSERT INTO incidents "
            "(incident_key, invariant_id, severity, scrutiny_tier, status, "
            "detector, detected_at, last_action_at, closed_at, "
            "consecutive_breaches, observed_state, desired_state, confidence, "
            "mr_iid, ddiff_url, rejection_history) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "incidents_mr4b.db"
    _bare_incidents_schema(db)
    monkeypatch.setenv("AGT_DB_PATH", str(db))
    try:
        from agt_equities import db as _agt_db
        monkeypatch.setattr(_agt_db, "DB_PATH", db, raising=False)
    except ImportError:
        pass
    return db


def test_migration_adds_both_columns(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    stats = run(db_path=fresh_db)
    assert stats["added_error_budget_tier"] == 1
    assert stats["added_budget_consumed_pct"] == 1
    with closing(sqlite3.connect(str(fresh_db))) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()}
    assert "error_budget_tier" in cols
    assert "budget_consumed_pct" in cols


def test_migration_is_idempotent(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    stats1 = run(db_path=fresh_db)
    stats2 = run(db_path=fresh_db)
    assert stats2["added_error_budget_tier"] == 0
    assert stats2["added_budget_consumed_pct"] == 0


def test_backfill_maps_severity_to_tier(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    stats = run(db_path=fresh_db)
    # 2 rows (critical, high) remap away from default; medium/warn stay at 2
    assert stats["backfilled_rows"] == 2
    with closing(sqlite3.connect(str(fresh_db))) as conn:
        mapping = dict(conn.execute(
            "SELECT severity, error_budget_tier FROM incidents ORDER BY id"
        ).fetchall())
    assert mapping["critical"] == 0
    assert mapping["high"] == 1
    assert mapping["medium"] == 2
    assert mapping["warn"] == 2


def test_register_accepts_error_budget_tier_kwarg(fresh_db):
    """register() + the new kwargs writes the requested tier value."""
    from scripts.migrate_incidents_error_budget import run
    run(db_path=fresh_db)
    from agt_equities import incidents_repo

    incidents_repo.register(
        "new_key_1",
        severity="high",
        scrutiny_tier="high",
        detector="test_suite",
        error_budget_tier=1,
        budget_consumed_pct=0.42,
        db_path=fresh_db,
    )
    with closing(sqlite3.connect(str(fresh_db))) as conn:
        row = conn.execute(
            "SELECT error_budget_tier, budget_consumed_pct "
            "FROM incidents WHERE incident_key = ?",
            ("new_key_1",),
        ).fetchone()
    assert row[0] == 1
    assert abs(row[1] - 0.42) < 1e-9


def test_register_defaults_tier_when_kwarg_omitted(fresh_db):
    """Omitting both new kwargs preserves the schema default (2)."""
    from scripts.migrate_incidents_error_budget import run
    run(db_path=fresh_db)
    from agt_equities import incidents_repo

    incidents_repo.register(
        "new_key_2",
        severity="medium",
        scrutiny_tier="high",
        detector="test_suite",
        db_path=fresh_db,
    )
    with closing(sqlite3.connect(str(fresh_db))) as conn:
        row = conn.execute(
            "SELECT error_budget_tier FROM incidents WHERE incident_key = ?",
            ("new_key_2",),
        ).fetchone()
    assert row[0] == 2


def test_count_by_tier_since_counts_only_matching_tier(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    run(db_path=fresh_db)
    from agt_equities.incidents_repo import count_by_tier_since

    # k1=critical→tier 0; k2=high→tier 1; k3=medium→tier 2; k4=warn→tier 2
    # All detected 2026-04-01 to 04-04.
    assert count_by_tier_since(0, "2026-04-01T00:00:00+00:00", db_path=fresh_db) == 1
    assert count_by_tier_since(1, "2026-04-01T00:00:00+00:00", db_path=fresh_db) == 1
    assert count_by_tier_since(2, "2026-04-01T00:00:00+00:00", db_path=fresh_db) == 2


def test_count_by_tier_since_respects_lower_bound(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    run(db_path=fresh_db)
    from agt_equities.incidents_repo import count_by_tier_since

    # Only k4 (2026-04-04) lands after the cutoff
    assert count_by_tier_since(2, "2026-04-04T00:00:00+00:00", db_path=fresh_db) == 1


def test_count_by_tier_since_rejects_invalid_tier(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    run(db_path=fresh_db)
    from agt_equities.incidents_repo import count_by_tier_since
    with pytest.raises(ValueError, match="tier must be 0/1/2"):
        count_by_tier_since(5, "2026-04-01T00:00:00+00:00", db_path=fresh_db)


def test_recent_tier0_tier1_since_returns_both_tiers_only(fresh_db):
    from scripts.migrate_incidents_error_budget import run
    run(db_path=fresh_db)
    from agt_equities.incidents_repo import recent_tier0_tier1_since

    rows = recent_tier0_tier1_since(
        "2026-04-01T00:00:00+00:00", db_path=fresh_db,
    )
    tiers = {r["error_budget_tier"] for r in rows}
    # k1 (tier 0) + k2 (tier 1); k3/k4 (tier 2) excluded
    assert tiers == {0, 1}
    assert len(rows) == 2
