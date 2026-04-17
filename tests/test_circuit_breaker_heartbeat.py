"""Tests for scripts/circuit_breaker.py::check_incident_detector_heartbeat.

ADR-007 Step 5 rename + behavioural check. The heartbeat function
queries ``agt_equities.db.get_ro_connection()`` for the newest
``last_action_at`` across ``incidents`` rows and returns a non-halting
verdict plus a ``stale`` flag when the newest row is older than
``INCIDENT_HEARTBEAT_MAX_AGE_HOURS`` (default 8h).

Covers:
    - Fresh row (<1h) -> ok=True, stale=False.
    - Stale row (>8h) -> ok=True, stale=True, age_hours surfaced,
      reason mentions "scheduler may be dead".
    - Empty table -> ok=True, has_incidents=False.
    - Unparseable timestamp -> ok=True, warning surfaced (no raise).
    - DB read failure -> ok=True, warning surfaced (no raise).
    - check_directive_freshness back-compat shim still returns a dict
      whose keys mirror the new function's output.
    - run_all_checks routes the result under the "incident_detector"
      key and does NOT halt on stale.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# DDL -- minimal incidents table the heartbeat query hits.
# ---------------------------------------------------------------------------

_INCIDENTS_DDL = """
CREATE TABLE incidents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key         TEXT NOT NULL,
    invariant_id         TEXT,
    severity             TEXT NOT NULL,
    scrutiny_tier        TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    detector             TEXT NOT NULL,
    detected_at          TEXT NOT NULL,
    closed_at            TEXT,
    last_action_at       TEXT,
    consecutive_breaches INTEGER NOT NULL DEFAULT 1,
    observed_state       TEXT,
    desired_state        TEXT,
    confidence           REAL,
    mr_iid               INTEGER,
    ddiff_url            TEXT,
    rejection_history    TEXT
)
"""


def _mk_db(tmp_path: Path, rows: list[tuple[str, str]] | None = None) -> str:
    """Build a temp sqlite with an incidents table + optional rows.

    rows entries are (incident_key, last_action_at) — detected_at falls
    back to last_action_at for brevity.
    """
    db_file = tmp_path / "cb_heartbeat.db"
    conn = sqlite3.connect(db_file)
    conn.execute(_INCIDENTS_DDL)
    if rows:
        for key, last in rows:
            conn.execute(
                "INSERT INTO incidents("
                "incident_key, severity, scrutiny_tier, detector, "
                "detected_at, last_action_at) "
                "VALUES (?, 'warn', 'medium', 'invariant_check', ?, ?)",
                (key, last, last),
            )
    conn.commit()
    conn.close()
    return str(db_file)


# ---------------------------------------------------------------------------
# Loader -- import scripts/circuit_breaker.py fresh each test to avoid
# cross-test module state (os.chdir at import time).
# ---------------------------------------------------------------------------

def _load_breaker():
    project_root = Path(__file__).resolve().parent.parent
    scripts_dir = project_root / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        if "circuit_breaker" in sys.modules:
            del sys.modules["circuit_breaker"]
        return importlib.import_module("circuit_breaker")
    finally:
        if str(scripts_dir) in sys.path:
            sys.path.remove(str(scripts_dir))


@pytest.fixture
def cb(monkeypatch):
    """circuit_breaker module with agt_equities.db.DB_PATH unset.

    Individual tests patch ``agt_equities.db.DB_PATH`` to their temp
    file before calling check_incident_detector_heartbeat().
    """
    return _load_breaker()


@pytest.fixture
def patch_agt_db(monkeypatch):
    """Return a helper that swaps agt_equities.db.DB_PATH at call time."""
    from agt_equities import db as agt_db

    def _apply(path: str) -> None:
        monkeypatch.setattr(agt_db, "DB_PATH", path)

    return _apply


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------

def test_threshold_constant_is_eight_hours(cb) -> None:
    assert cb.INCIDENT_HEARTBEAT_MAX_AGE_HOURS == 8


# ---------------------------------------------------------------------------
# Fresh row
# ---------------------------------------------------------------------------

def test_fresh_row_returns_ok_not_stale(cb, patch_agt_db, tmp_path) -> None:
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    db = _mk_db(tmp_path, rows=[("FRESH", fresh)])
    patch_agt_db(db)

    result = cb.check_incident_detector_heartbeat()
    assert result["ok"] is True
    assert result["has_incidents"] is True
    assert result["stale"] is False
    assert result["age_hours"] >= 0.0
    assert result["age_hours"] < 1.0


# ---------------------------------------------------------------------------
# Stale row
# ---------------------------------------------------------------------------

def test_stale_row_surfaces_age_and_reason(cb, patch_agt_db, tmp_path) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    db = _mk_db(tmp_path, rows=[("STALE", stale)])
    patch_agt_db(db)

    result = cb.check_incident_detector_heartbeat()
    # Never hard-halts; surfaces stale for breaker aggregator.
    assert result["ok"] is True
    assert result["has_incidents"] is True
    assert result["stale"] is True
    assert result["age_hours"] >= 24.0
    assert "scheduler may be dead" in result["reason"]


def test_stale_threshold_boundary_is_exclusive(cb, patch_agt_db, tmp_path) -> None:
    # Just inside 8h -> not stale.
    inside = (datetime.now(timezone.utc) - timedelta(hours=7, minutes=55)).isoformat()
    db = _mk_db(tmp_path, rows=[("INSIDE", inside)])
    patch_agt_db(db)

    result = cb.check_incident_detector_heartbeat()
    assert result["stale"] is False


# ---------------------------------------------------------------------------
# Empty table
# ---------------------------------------------------------------------------

def test_empty_incidents_table_returns_has_incidents_false(
    cb, patch_agt_db, tmp_path,
) -> None:
    db = _mk_db(tmp_path, rows=None)
    patch_agt_db(db)

    result = cb.check_incident_detector_heartbeat()
    assert result["ok"] is True
    assert result["has_incidents"] is False
    assert "No incidents on record" in result["reason"]


# ---------------------------------------------------------------------------
# Unparseable timestamp
# ---------------------------------------------------------------------------

def test_unparseable_timestamp_soft_warns(cb, patch_agt_db, tmp_path) -> None:
    db = _mk_db(tmp_path, rows=[("BAD_TS", "not-an-iso-timestamp")])
    patch_agt_db(db)

    result = cb.check_incident_detector_heartbeat()
    assert result["ok"] is True
    assert result["has_incidents"] is True
    assert "warning" in result


# ---------------------------------------------------------------------------
# DB read failure
# ---------------------------------------------------------------------------

def test_missing_db_soft_warns(cb, patch_agt_db, tmp_path) -> None:
    # Point at a file that does not exist -- RO URI connect will fail.
    missing = str(tmp_path / "does_not_exist.db")
    patch_agt_db(missing)

    result = cb.check_incident_detector_heartbeat()
    assert result["ok"] is True  # never halts
    assert "warning" in result
    assert "failed" in result["warning"].lower()


# ---------------------------------------------------------------------------
# Back-compat shim
# ---------------------------------------------------------------------------

def test_check_directive_freshness_delegates(cb, patch_agt_db, tmp_path) -> None:
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db = _mk_db(tmp_path, rows=[("SHIM_FRESH", fresh)])
    patch_agt_db(db)

    new_result = cb.check_incident_detector_heartbeat()
    shim_result = cb.check_directive_freshness()

    # Shim must return the same shape / same keys.
    assert set(new_result.keys()) == set(shim_result.keys())
    assert shim_result["ok"] is True
    assert shim_result.get("stale") is False


# ---------------------------------------------------------------------------
# run_all_checks wiring
# ---------------------------------------------------------------------------

def test_run_all_checks_exposes_incident_detector_key(
    cb, patch_agt_db, tmp_path, monkeypatch,
) -> None:
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db = _mk_db(tmp_path, rows=[("ALL_OK", fresh)])
    patch_agt_db(db)

    # Stub the other checks so aggregation does not touch live systems.
    monkeypatch.setattr(cb, "check_daily_order_limit",  lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_daily_notional",     lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_consecutive_errors", lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_nlv_drop",           lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_vix",                lambda: {"ok": True})

    verdict = cb.run_all_checks()
    assert "incident_detector" in verdict["checks"]
    assert "directive" not in verdict["checks"]
    assert verdict["halted"] is False
    # Fresh heartbeat on stubbed everything else -> no violations.
    assert verdict["ok"] is True


def test_run_all_checks_does_not_halt_on_stale_detector(
    cb, patch_agt_db, tmp_path, monkeypatch,
) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    db = _mk_db(tmp_path, rows=[("STALE_RUN", stale)])
    patch_agt_db(db)

    monkeypatch.setattr(cb, "check_daily_order_limit",  lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_daily_notional",     lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_consecutive_errors", lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_nlv_drop",           lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_vix",                lambda: {"ok": True})

    verdict = cb.run_all_checks()
    # Detector staleness is a warning, never a halt.
    assert verdict["halted"] is False
    # And the stale flag surfaces in the warnings aggregation.
    warnings_checks = {w["check"] for w in verdict["warnings"]}
    assert "incident_detector" in warnings_checks
