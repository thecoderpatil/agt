"""ADR-018 Phase 2 — FLEX_SYNC_BOT_ROUTED_COVERAGE_GAP invariant tests."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


from agt_equities.invariants.checks import check_flex_sync_bot_routed_coverage_gap
from agt_equities.invariants.types import CheckContext


@pytest.fixture
def coverage_db(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "cov.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE master_log_sync (
            sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, finished_at TEXT,
            flex_query_id TEXT, from_date TEXT, to_date TEXT,
            reference_code TEXT,
            sections_processed INTEGER, rows_received INTEGER,
            rows_inserted INTEGER, rows_updated INTEGER,
            status TEXT, error_message TEXT
        );
        CREATE TABLE master_log_trades (
            transaction_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            buy_sell TEXT,
            quantity REAL,
            trade_price REAL,
            date_time TEXT,
            trade_date TEXT
        );
        CREATE TABLE pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            fill_time TEXT,
            payload TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    return conn


def _ctx(live_accounts: frozenset[str] = frozenset({"U22388499"})) -> CheckContext:
    return CheckContext(
        now_utc=datetime(2026, 4, 28, 8, 0, tzinfo=timezone.utc),
        db_path=":memory:",
        paper_mode=True,
        paper_accounts=frozenset(),
        live_accounts=live_accounts,
        expected_daemons=frozenset({"agt_bot", "agt_scheduler"}),
    )


def _seed_success_sync(conn, coverage_date="20260427"):
    conn.execute(
        "INSERT INTO master_log_sync (started_at, finished_at, flex_query_id, "
        "from_date, to_date, status, sections_processed, rows_received) "
        "VALUES ('2026-04-28T07:00:00', '2026-04-28T07:00:30', '1461095', ?, ?, 'success', 46, 5)",
        (coverage_date, coverage_date),
    )
    conn.commit()


def _seed_filled_pending(conn, *, acct="U22388499", symbol="AAPL", side="BUY",
                         qty=100.0, price=150.0, fill_time="2026-04-27T14:30:00+00:00"):
    payload = json.dumps({
        "account_id": acct, "ticker": symbol, "side": side,
        "quantity": qty, "fill_price": price,
    })
    conn.execute(
        "INSERT INTO pending_orders (status, fill_time, payload, created_at) "
        "VALUES ('filled', ?, ?, '2026-04-27T13:00:00+00:00')",
        (fill_time, payload),
    )
    conn.commit()


def _seed_flex_trade(conn, *, tx_id="T1", acct="U22388499", symbol="AAPL",
                     buy_sell="BUY", qty=100.0, price=150.0,
                     date_time="20260427;143000", trade_date="20260427"):
    conn.execute(
        "INSERT INTO master_log_trades (transaction_id, account_id, symbol, "
        "buy_sell, quantity, trade_price, date_time, trade_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tx_id, acct, symbol, buy_sell, qty, price, date_time, trade_date),
    )
    conn.commit()


# ---- Tests -----------------------------------------------------------------


def test_coverage_invariant_no_gap_returns_ok(coverage_db):
    """A == B → no violation."""
    _seed_success_sync(coverage_db)
    _seed_filled_pending(coverage_db)
    _seed_flex_trade(coverage_db)
    vios = check_flex_sync_bot_routed_coverage_gap(coverage_db, _ctx())
    assert vios == []


def test_coverage_invariant_gap_raises_crit(coverage_db):
    """A has extras not in B → tier-0 violation."""
    _seed_success_sync(coverage_db)
    _seed_filled_pending(coverage_db)  # A has this fill
    # B empty (Flex didn't return the trade)
    vios = check_flex_sync_bot_routed_coverage_gap(coverage_db, _ctx())
    assert len(vios) == 1
    assert vios[0].invariant_id == "FLEX_SYNC_BOT_ROUTED_COVERAGE_GAP"
    assert vios[0].severity == "crit"
    assert vios[0].evidence["coverage_date"] == "20260427"
    assert vios[0].evidence["expected_bot_routed_count"] == 1
    assert vios[0].evidence["actual_flex_count"] == 0


def test_coverage_invariant_tuple_key_5sec_bucket(coverage_db):
    """Fills within the same 5s bucket compare equal."""
    _seed_success_sync(coverage_db)
    # Bot logs fill at 14:30:00; Flex logs at 14:30:03 (same 5s bucket).
    _seed_filled_pending(coverage_db, fill_time="2026-04-27T14:30:00+00:00")
    _seed_flex_trade(coverage_db, date_time="20260427;143003")
    vios = check_flex_sync_bot_routed_coverage_gap(coverage_db, _ctx())
    assert vios == []


def test_coverage_invariant_fires_only_for_tracked_accounts(coverage_db):
    """A fill on a non-tracked account is not counted (paper acct)."""
    _seed_success_sync(coverage_db)
    _seed_filled_pending(coverage_db, acct="DUP751003")  # paper
    # B empty
    vios = check_flex_sync_bot_routed_coverage_gap(
        coverage_db, _ctx(live_accounts=frozenset({"U22388499"})),
    )
    assert vios == []


def test_coverage_invariant_no_success_sync_returns_ok(coverage_db):
    """Without a success row in master_log_sync, invariant is inert."""
    _seed_filled_pending(coverage_db)
    vios = check_flex_sync_bot_routed_coverage_gap(coverage_db, _ctx())
    assert vios == []


def test_coverage_invariant_empty_tracked_accounts_returns_ok(coverage_db):
    """No live accounts = invariant inert (CI / dev env)."""
    _seed_success_sync(coverage_db)
    _seed_filled_pending(coverage_db)
    vios = check_flex_sync_bot_routed_coverage_gap(
        coverage_db, _ctx(live_accounts=frozenset()),
    )
    assert vios == []


def test_coverage_invariant_mismatched_qty_is_gap(coverage_db):
    """Bot fill 100 shares, Flex records 99 shares → gap."""
    _seed_success_sync(coverage_db)
    _seed_filled_pending(coverage_db, qty=100.0)
    _seed_flex_trade(coverage_db, qty=99.0)
    vios = check_flex_sync_bot_routed_coverage_gap(coverage_db, _ctx())
    assert len(vios) == 1


def test_coverage_invariant_side_canonicalization_bot_sld(coverage_db):
    """Flex 'BOT' and 'SLD' map to BUY/SELL for comparison."""
    _seed_success_sync(coverage_db)
    _seed_filled_pending(coverage_db, side="BUY")
    _seed_flex_trade(coverage_db, buy_sell="BOT")  # Flex sometimes uses BOT
    vios = check_flex_sync_bot_routed_coverage_gap(coverage_db, _ctx())
    assert vios == []
