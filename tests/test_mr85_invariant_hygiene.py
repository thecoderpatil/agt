"""MR !85: invariant hygiene follow-ups.

Covers:
  1. NO_LOCAL_DRIFT             -- stable_key='NO_LOCAL_DRIFT' singleton
  2. NO_STRANDED_STAGED_ORDERS  -- stable_key per pending_order_id (prophylactic)
  3. NO_STUCK_PROCESSING_ORDER  -- stable_key per pending_order_id (prophylactic)
  4. NO_ZOMBIE_BOT_PROCESS      -- stable_key on both degraded and zombie paths

NO_SILENT_BREAKER_TRIP's rewrite (haiku-watchdog -> AGT_Bot_Liveness_Watchdog)
is exercised by the updated tests in tests/test_invariants.py.

Goal: lock the ``Violation.stable_key`` contract for each of these checks
so a future refactor that drops the attribute silently reverts to the
evidence-fingerprint path -- and the INSERT-per-tick regression we saw
with NO_STALE_RED_ALERT pre-MR-!84 recurs.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from agt_equities.invariants.checks import (
    check_no_local_drift,
    check_no_stranded_staged_orders,
    check_no_stuck_processing_order,
    check_no_zombie_bot_process,
)
from agt_equities.invariants.types import CheckContext

pytestmark = pytest.mark.sprint_a


NOW = datetime(2026, 4, 17, 22, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> CheckContext:
    return CheckContext(
        now_utc=NOW,
        db_path=":memory:",
        paper_mode=True,
        live_accounts=frozenset({"U21971297", "U22076329"}),
        paper_accounts=frozenset({"DUP751003", "DUP751004", "DUP751005"}),
        expected_daemons=frozenset({"agt_bot"}),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE pending_orders (
            id INTEGER PRIMARY KEY,
            payload TEXT, status TEXT, created_at TEXT,
            ib_order_id INTEGER, ib_perm_id INTEGER
        );
        """
    )
    return c


# --- NO_STRANDED_STAGED_ORDERS ------------------------------------------------
def test_stranded_staged_stable_key_per_order(conn, ctx):
    old = (NOW - timedelta(hours=4)).isoformat()
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at) "
        "VALUES (1, ?, 'staged', ?)",
        (json.dumps({"ticker": "AAPL", "account_id": "DUP751003", "mode": "CSP"}), old),
    )
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at) "
        "VALUES (2, ?, 'staged', ?)",
        (json.dumps({"ticker": "TSLA", "account_id": "DUP751003", "mode": "CC"}), old),
    )
    vios = check_no_stranded_staged_orders(conn, ctx)
    keys = {v.stable_key for v in vios}
    assert keys == {
        "NO_STRANDED_STAGED_ORDERS:1",
        "NO_STRANDED_STAGED_ORDERS:2",
    }
    # Evidence still carries age_hours for operator readability
    for v in vios:
        assert "age_hours" in v.evidence


def test_stranded_staged_no_key_when_fresh(conn, ctx):
    """Fresh 'staged' orders (under TTL) do not even fire -- no stable_key to test."""
    fresh = NOW.isoformat()
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at) "
        "VALUES (3, ?, 'staged', ?)",
        (json.dumps({"ticker": "AAPL"}), fresh),
    )
    assert check_no_stranded_staged_orders(conn, ctx) == []


# --- NO_STUCK_PROCESSING_ORDER ------------------------------------------------
def test_stuck_processing_stable_key_per_order(conn, ctx):
    old = (NOW - timedelta(hours=5)).isoformat()
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at, ib_order_id) "
        "VALUES (7, ?, 'processing', ?, 42)",
        (json.dumps({"ticker": "AAPL", "account_id": "DUP751003"}), old),
    )
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at, ib_order_id) "
        "VALUES (8, ?, 'processing', ?, 43)",
        (json.dumps({"ticker": "TSLA", "account_id": "DUP751003"}), old),
    )
    vios = check_no_stuck_processing_order(conn, ctx)
    keys = {v.stable_key for v in vios}
    assert keys == {
        "NO_STUCK_PROCESSING_ORDER:7",
        "NO_STUCK_PROCESSING_ORDER:8",
    }
    for v in vios:
        assert "age_hours" in v.evidence
        assert v.evidence["ib_order_id"] in (42, 43)


# --- NO_ZOMBIE_BOT_PROCESS ----------------------------------------------------
def test_zombie_bot_degraded_has_stable_key(conn, ctx, monkeypatch):
    """When psutil is absent AND tasklist/ps unavailable, the degraded Violation
    must carry stable_key='NO_ZOMBIE_BOT_PROCESS:degraded' so repeat ticks
    under the same host condition collapse onto one incident row.
    """
    import sys

    # Force the ImportError path. psutil is in requirements-runtime, but CI
    # often runs before install. We simulate absence by aliasing to None +
    # blocking the import via sys.modules.
    monkeypatch.setitem(sys.modules, "psutil", None)

    def boom(*a, **kw):
        raise FileNotFoundError("no tasklist/ps on CI")

    monkeypatch.setattr(
        "agt_equities.invariants.checks.subprocess.run", boom
    )
    vios = check_no_zombie_bot_process(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence.get("degraded") is True
    assert vios[0].stable_key == "NO_ZOMBIE_BOT_PROCESS:degraded"


def test_zombie_bot_real_zombie_has_singleton_stable_key(conn, ctx, monkeypatch):
    """Simulated >1 telegram_bot.py via tasklist output -> singleton stable_key.

    Force ImportError for psutil then stub subprocess.run to return a
    synthetic tasklist showing two telegram_bot.py processes.
    """
    import sys

    monkeypatch.setitem(sys.modules, "psutil", None)

    class _Result:
        stdout = "python.exe 1234 telegram_bot.py\npython.exe 5678 telegram_bot.py\n"
        stderr = ""
        returncode = 0

    def fake_run(*a, **kw):
        return _Result()

    monkeypatch.setattr(
        "agt_equities.invariants.checks.subprocess.run", fake_run
    )
    vios = check_no_zombie_bot_process(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["pid_count"] == 2
    assert vios[0].stable_key == "NO_ZOMBIE_BOT_PROCESS"


# --- NO_LOCAL_DRIFT -----------------------------------------------------------
def test_no_local_drift_singleton_stable_key(conn, ctx, monkeypatch, tmp_path):
    """Two drifted files -> ONE Violation with stable_key='NO_LOCAL_DRIFT'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    class _Result:
        stdout = " M agt_equities/foo.py\n M scripts/bar.py\n"
        stderr = ""
        returncode = 0

    def fake_run(*args, **kw):
        return _Result()

    monkeypatch.setenv("AGT_REPO_PATH", str(repo))
    monkeypatch.setattr(
        "agt_equities.invariants.checks.subprocess.run", fake_run
    )
    vios = check_no_local_drift(conn, ctx)
    assert len(vios) == 1
    assert vios[0].stable_key == "NO_LOCAL_DRIFT"
    assert vios[0].evidence["drift_count"] == 2
    # drift_sample still observable but does not affect stable_key -- that's
    # the whole point.
    sample = vios[0].evidence["drift_sample"]
    assert any(s["path"] == "agt_equities/foo.py" for s in sample)


def test_no_local_drift_degraded_has_sibling_stable_key(conn, ctx, monkeypatch, tmp_path):
    """subprocess exception -> degraded Violation with stable_key='NO_LOCAL_DRIFT:degraded'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    def boom(*a, **kw):
        raise OSError("git binary missing")

    monkeypatch.setenv("AGT_REPO_PATH", str(repo))
    monkeypatch.setattr(
        "agt_equities.invariants.checks.subprocess.run", boom
    )
    vios = check_no_local_drift(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence.get("degraded") is True
    assert vios[0].stable_key == "NO_LOCAL_DRIFT:degraded"


def test_no_local_drift_degraded_nonzero_rc_stable_key(conn, ctx, monkeypatch, tmp_path):
    """Non-zero git status rc -> degraded Violation with stable_key='NO_LOCAL_DRIFT:degraded'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    class _Result:
        stdout = ""
        stderr = "fatal: not a git repository"
        returncode = 128

    def fake_run(*a, **kw):
        return _Result()

    monkeypatch.setenv("AGT_REPO_PATH", str(repo))
    monkeypatch.setattr(
        "agt_equities.invariants.checks.subprocess.run", fake_run
    )
    vios = check_no_local_drift(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence.get("degraded") is True
    assert vios[0].stable_key == "NO_LOCAL_DRIFT:degraded"


def test_no_local_drift_exempt_registry_still_honored(conn, ctx, monkeypatch, tmp_path):
    """Exempt files must not count toward drift_count; clean -> []."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    class _Result:
        stdout = " M boot_desk.bat\n M cure_lifecycle.html\n M tests/test_command_prune.py\n"
        stderr = ""
        returncode = 0

    def fake_run(*a, **kw):
        return _Result()

    monkeypatch.setenv("AGT_REPO_PATH", str(repo))
    monkeypatch.setattr(
        "agt_equities.invariants.checks.subprocess.run", fake_run
    )
    assert check_no_local_drift(conn, ctx) == []
