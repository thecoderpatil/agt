"""
tests/test_no_live_in_paper_paper_suppress.py

Sprint 9 Item 6 — check_no_live_in_paper returns [] when AGT_BROKER_MODE=paper,
even when live-account pending_orders rows exist. The broker_mode gate in
order_state.py already rejects live submissions; the invariant suppresses itself
to avoid accumulating false positives in paper-mode deployments.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agt_equities.invariants.checks import check_no_live_in_paper
from agt_equities.invariants.types import CheckContext

pytestmark = pytest.mark.sprint_a

_NOW = datetime(2026, 4, 24, 22, 0, 0, tzinfo=timezone.utc)
_LIVE_ACCOUNTS = frozenset({"U21971297", "U22076329"})
_PAPER_ACCOUNTS = frozenset({"DUP751003", "DUP751004", "DUP751005"})


def _make_ctx(paper_mode: bool = True) -> CheckContext:
    return CheckContext(
        now_utc=_NOW,
        db_path=":memory:",
        paper_mode=paper_mode,
        live_accounts=_LIVE_ACCOUNTS,
        paper_accounts=_PAPER_ACCOUNTS,
        expected_daemons=frozenset({"agt_bot"}),
    )


def _seed_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT,
            status TEXT NOT NULL,
            fill_time TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


@pytest.fixture
def db_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    p = tmp_path / "test_no_live.db"
    monkeypatch.setenv("AGT_DB_PATH", str(p))
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    return conn


def _insert_live_row(conn: sqlite3.Connection, account_id: str = "U22076329") -> None:
    conn.execute(
        "INSERT INTO pending_orders (payload, status) VALUES (?, ?)",
        (json.dumps({"account_id": account_id}), "staged"),
    )
    conn.commit()


def test_suppressed_under_paper_broker_mode(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When AGT_BROKER_MODE=paper, check_no_live_in_paper returns [] for live rows."""
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    _insert_live_row(db_conn, account_id="U22076329")

    violations = check_no_live_in_paper(db_conn, _make_ctx())

    assert violations == []


def test_active_without_paper_broker_mode(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without AGT_BROKER_MODE=paper, check_no_live_in_paper fires on live rows."""
    monkeypatch.delenv("AGT_BROKER_MODE", raising=False)
    _insert_live_row(db_conn, account_id="U22076329")

    violations = check_no_live_in_paper(db_conn, _make_ctx())

    assert len(violations) == 1
    assert violations[0].invariant_id == "NO_LIVE_IN_PAPER"
