"""Tests for ADR-007 Addendum §2.2 + §2.3.

Verifies:
  - check function returns [] on clean state (path match + fresh heartbeat)
  - violates on path mismatch
  - violates on stale heartbeat
  - violates on empty heartbeat table
  - tick.py out-of-band branch calls send_telegram_message
  - tick.py out-of-band branch does NOT call incidents write surface
    (THE critical regression test -- this is the 42hr failure mode)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from agt_equities.invariants.checks import (
    check_self_healing_write_path_canonical,
)
from agt_equities.invariants.types import CheckContext, Violation
from agt_equities.runtime import PROD_DB_PATH

pytestmark = pytest.mark.sprint_a


def _make_ctx(now=None):
    return CheckContext(
        now_utc=now or datetime.now(tz=timezone.utc),
        db_path=PROD_DB_PATH,
        paper_mode=False,
        live_accounts=frozenset(),
        paper_accounts=frozenset(),
        expected_daemons=frozenset(),
    )


def _seed_heartbeat_db(path, ts):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE daemon_heartbeat (daemon TEXT, last_beat_utc TEXT)"
    )
    conn.execute("INSERT INTO daemon_heartbeat VALUES ('agt_bot', ?)", (ts,))
    conn.commit()
    conn.close()


def test_clean_state_returns_empty(tmp_path, monkeypatch):
    """Path matches canonical + fresh heartbeat -> no violation."""
    seeded = tmp_path / "canonical.db"
    now = datetime(2026, 4, 19, 20, 0, tzinfo=timezone.utc)
    _seed_heartbeat_db(seeded, (now - timedelta(seconds=30)).isoformat())
    monkeypatch.setattr(
        "agt_equities.runtime.PROD_DB_PATH", str(seeded), raising=False
    )
    import agt_equities.db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", seeded, raising=False)
    ctx = _make_ctx(now=now)
    conn = sqlite3.connect(":memory:")
    assert check_self_healing_write_path_canonical(conn, ctx) == []


def test_path_mismatch_violates(tmp_path, monkeypatch):
    """agt_equities.db.DB_PATH != runtime.PROD_DB_PATH -> violation."""
    scratch = tmp_path / "orphan.db"
    scratch.touch()
    import agt_equities.db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", scratch, raising=False)
    ctx = _make_ctx()
    conn = sqlite3.connect(":memory:")
    result = check_self_healing_write_path_canonical(conn, ctx)
    assert len(result) == 1
    assert "write path" in result[0].description.lower()
    assert result[0].severity == "crit"


def test_stale_heartbeat_violates(tmp_path, monkeypatch):
    """Path matches but heartbeat is stale -> violation."""
    seeded = tmp_path / "canonical.db"
    now = datetime(2026, 4, 19, 20, 0, tzinfo=timezone.utc)
    _seed_heartbeat_db(seeded, (now - timedelta(seconds=500)).isoformat())
    monkeypatch.setattr(
        "agt_equities.runtime.PROD_DB_PATH", str(seeded), raising=False
    )
    import agt_equities.db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", seeded, raising=False)
    ctx = _make_ctx(now=now)
    conn = sqlite3.connect(":memory:")
    result = check_self_healing_write_path_canonical(conn, ctx)
    assert len(result) == 1
    assert "stale" in result[0].description.lower()


def test_empty_heartbeat_table_violates(tmp_path, monkeypatch):
    """Path matches but heartbeat table is empty -> violation."""
    seeded = tmp_path / "canonical.db"
    c = sqlite3.connect(str(seeded))
    c.execute("CREATE TABLE daemon_heartbeat (daemon TEXT, last_beat_utc TEXT)")
    c.commit(); c.close()
    monkeypatch.setattr(
        "agt_equities.runtime.PROD_DB_PATH", str(seeded), raising=False
    )
    import agt_equities.db as agt_db
    monkeypatch.setattr(agt_db, "DB_PATH", seeded, raising=False)
    ctx = _make_ctx()
    conn = sqlite3.connect(":memory:")
    result = check_self_healing_write_path_canonical(conn, ctx)
    assert len(result) == 1


def test_tick_outofband_pages_telegram(tmp_path, monkeypatch):
    """When the canary violates, tick.py must call send_telegram_message."""
    from agt_equities.invariants import tick as tick_mod

    fake_violation = Violation(
        invariant_id="SELF_HEALING_WRITE_PATH_CANONICAL",
        description="test violation",
        severity="crit",
        evidence={"foo": "bar"},
    )
    monkeypatch.setattr(
        "agt_equities.invariants.run_all",
        lambda *a, **k: {"SELF_HEALING_WRITE_PATH_CANONICAL": [fake_violation]},
    )
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **k: [{
            "id": "SELF_HEALING_WRITE_PATH_CANONICAL",
            "check_fn": "check_self_healing_write_path_canonical",
            "severity_floor": "crit",
            "scrutiny_tier": "architect_only",
            "fix_by_sprint": "immediate",
            "max_consecutive_violations": 1,
            "description": "test",
        }],
    )
    sent = []
    monkeypatch.setattr(
        "agt_equities.telegram_utils.send_telegram_message",
        lambda msg, parse_mode=None: sent.append(msg),
    )
    tick_mod.check_invariants_tick(detector="test")
    assert any("SELF_HEALING_WRITE_PATH_CANONICAL" in m for m in sent)


def test_tick_outofband_does_NOT_call_incidents_write(
    tmp_path, monkeypatch,
):
    """CRITICAL: when the canary violates, tick.py must NOT call
    incidents_repo.register -- that is the 42hr failure mode.
    """
    from agt_equities.invariants import tick as tick_mod

    fake_violation = Violation(
        invariant_id="SELF_HEALING_WRITE_PATH_CANONICAL",
        description="test violation",
        severity="crit",
        evidence={},
    )
    monkeypatch.setattr(
        "agt_equities.invariants.run_all",
        lambda *a, **k: {"SELF_HEALING_WRITE_PATH_CANONICAL": [fake_violation]},
    )
    monkeypatch.setattr(
        "agt_equities.invariants.load_invariants",
        lambda *a, **k: [{
            "id": "SELF_HEALING_WRITE_PATH_CANONICAL",
            "check_fn": "check_self_healing_write_path_canonical",
            "severity_floor": "crit",
            "scrutiny_tier": "architect_only",
            "fix_by_sprint": "immediate",
            "max_consecutive_violations": 1,
            "description": "test",
        }],
    )
    monkeypatch.setattr(
        "agt_equities.telegram_utils.send_telegram_message",
        lambda *a, **k: None,
    )
    register_calls = []
    monkeypatch.setattr(
        "agt_equities.incidents_repo.register", lambda *a, **k: register_calls.append(1)
    )
    tick_mod.check_invariants_tick(detector="test")
    assert register_calls == [], (
        "incidents_repo.register must NOT be called for "
        "SELF_HEALING_WRITE_PATH_CANONICAL -- see ADR-007 Addendum §2.3"
    )
