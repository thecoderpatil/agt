"""MR !108 — csp_allocator dedup guard (direct pending_orders query).

Verifies that _fetch_household_buying_power_snapshot builds staged_order_tickers
from a direct pending_orders read, not from the test-fixture-only
has_staged_order flag that _discover_positions never emits in production.

Recon: reports/csp_dedup_recon_20260418.md
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.sprint_a

# ---------------------------------------------------------------------------
# Test constants — isolated account IDs avoid live-config dependency
# ---------------------------------------------------------------------------
_YASH_ACCT = "TEST_YASH_1"
_VIKRAM_ACCT = "TEST_VIKRAM_1"

_FAKE_HH_MAP = {
    "Yash_Household": [_YASH_ACCT],
    "Vikram_Household": [_VIKRAM_ACCT],
}
_FAKE_ACCT_TO_HH = {
    _YASH_ACCT: "Yash_Household",
    _VIKRAM_ACCT: "Vikram_Household",
}
_FAKE_MARGIN = frozenset({_YASH_ACCT})

_SUMMARY_DATA: dict[str, dict[str, float]] = {
    _YASH_ACCT: {
        "NetLiquidation": 100_000.0,
        "ExcessLiquidity": 50_000.0,
        "BuyingPower": 50_000.0,
    },
    _VIKRAM_ACCT: {
        "NetLiquidation": 50_000.0,
        "ExcessLiquidity": 25_000.0,
        "BuyingPower": 25_000.0,
    },
}

_PENDING_ORDERS_DDL = """
CREATE TABLE pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT,
    payload TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "agt_desk.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_PENDING_ORDERS_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr("agt_equities.db.DB_PATH", str(db_path), raising=False)
    return db_path


def _patch_config(monkeypatch) -> None:
    """Swap in test household map so tests don't depend on live config."""
    monkeypatch.setattr(
        "agt_equities.csp_allocator.HOUSEHOLD_MAP", _FAKE_HH_MAP, raising=True,
    )
    monkeypatch.setattr(
        "agt_equities.csp_allocator.ACCOUNT_TO_HOUSEHOLD", _FAKE_ACCT_TO_HH,
    )
    monkeypatch.setattr(
        "agt_equities.csp_allocator.MARGIN_ACCOUNTS", _FAKE_MARGIN,
    )


def _make_ib(data: dict[str, dict[str, float]] | None = None) -> Any:
    items = []
    for acct, tags in (data or _SUMMARY_DATA).items():
        for tag, value in tags.items():
            items.append(SimpleNamespace(account=acct, tag=tag, value=str(value)))

    class _FakeIB:
        async def accountSummaryAsync(self):
            return items

    return _FakeIB()


def _run(coro):
    return asyncio.run(coro)


def _insert_order(
    db_path: Path,
    *,
    status: str,
    account_id: str,
    ticker: str,
    right: str = "P",
    action: str = "SELL",
) -> None:
    payload = json.dumps({
        "account_id": account_id,
        "ticker": ticker,
        "right": right,
        "action": action,
    })
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO pending_orders (status, payload) VALUES (?, ?)",
        (status, payload),
    )
    conn.commit()
    conn.close()


def _get_staged(temp_db, monkeypatch, discovered=None) -> set[str]:
    """Convenience: patch config, run snapshot, return Yash staged set."""
    from agt_equities.csp_allocator import _fetch_household_buying_power_snapshot
    _patch_config(monkeypatch)
    result = _run(
        _fetch_household_buying_power_snapshot(_make_ib(), discovered or {})
    )
    return result["Yash_Household"]["staged_order_tickers"]


# ---------------------------------------------------------------------------
# 1. Single sent row populates staged set
# ---------------------------------------------------------------------------

def test_dedup_query_populates_staged_tickers_from_sent_row(
    temp_db, monkeypatch
) -> None:
    """A sent CSP put row on a Yash account → ticker in Yash staged set."""
    _insert_order(temp_db, status="sent", account_id=_YASH_ACCT, ticker="ZS")
    staged = _get_staged(temp_db, monkeypatch)
    assert "ZS" in staged


# ---------------------------------------------------------------------------
# 2. All five active statuses feed the set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    "staged", "processing", "sent", "transmitting", "partially_filled",
])
def test_dedup_query_populates_from_all_active_statuses(
    status, temp_db, monkeypatch
) -> None:
    _insert_order(temp_db, status=status, account_id=_YASH_ACCT, ticker="SHOP")
    staged = _get_staged(temp_db, monkeypatch)
    assert "SHOP" in staged


# ---------------------------------------------------------------------------
# 3. Terminal statuses are excluded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    "filled", "cancelled", "rejected", "superseded", "failed",
])
def test_dedup_query_ignores_terminal_statuses(
    status, temp_db, monkeypatch
) -> None:
    _insert_order(temp_db, status=status, account_id=_YASH_ACCT, ticker="COHR")
    staged = _get_staged(temp_db, monkeypatch)
    assert "COHR" not in staged


# ---------------------------------------------------------------------------
# 4. Household-account filter
# ---------------------------------------------------------------------------

def test_dedup_query_filters_by_household_accounts(
    temp_db, monkeypatch
) -> None:
    """Vikram's orders do not appear in Yash's staged set (and vice versa)."""
    from agt_equities.csp_allocator import _fetch_household_buying_power_snapshot
    _patch_config(monkeypatch)
    _insert_order(temp_db, status="sent", account_id=_VIKRAM_ACCT, ticker="APP")
    result = _run(_fetch_household_buying_power_snapshot(_make_ib(), {}))
    assert "APP" not in result["Yash_Household"]["staged_order_tickers"]
    assert "APP" in result["Vikram_Household"]["staged_order_tickers"]


# ---------------------------------------------------------------------------
# 5. Non-CSP orders ignored (CC sell, put BTC)
# ---------------------------------------------------------------------------

def test_dedup_query_ignores_non_csp_orders(
    temp_db, monkeypatch
) -> None:
    """CC sells (right='C') and buy-to-close (action='BUY') are excluded."""
    _insert_order(temp_db, status="sent", account_id=_YASH_ACCT, ticker="NVDA",
                  right="C", action="SELL")
    _insert_order(temp_db, status="sent", account_id=_YASH_ACCT, ticker="MSFT",
                  right="P", action="BUY")
    staged = _get_staged(temp_db, monkeypatch)
    assert "NVDA" not in staged
    assert "MSFT" not in staged


# ---------------------------------------------------------------------------
# 6. Malformed payload is skipped without crash
# ---------------------------------------------------------------------------

def test_dedup_query_tolerates_malformed_payload(
    temp_db, monkeypatch
) -> None:
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        "INSERT INTO pending_orders (status, payload) VALUES (?, ?)",
        ("sent", '{"not_json": }'),
    )
    conn.commit()
    conn.close()
    staged = _get_staged(temp_db, monkeypatch)
    assert staged == set()


# ---------------------------------------------------------------------------
# 7. Legacy flag-union and DB query both contribute
# ---------------------------------------------------------------------------

def test_dedup_query_merges_with_legacy_flag_union(
    temp_db, monkeypatch
) -> None:
    """has_staged_order=True on a pos dict AND a DB row both land in staged."""
    from agt_equities.csp_allocator import _fetch_household_buying_power_snapshot
    _patch_config(monkeypatch)
    _insert_order(temp_db, status="sent", account_id=_YASH_ACCT, ticker="ZS")
    discovered = {
        "households": {
            "Yash_Household": {
                "positions": [{
                    "ticker": "SHOP",
                    "has_staged_order": True,
                    "total_shares": 0,
                    "spot_price": 0.0,
                    "short_puts": [],
                }]
            }
        }
    }
    result = _run(_fetch_household_buying_power_snapshot(_make_ib(), discovered))
    staged = result["Yash_Household"]["staged_order_tickers"]
    assert "ZS" in staged    # from direct DB query
    assert "SHOP" in staged  # from legacy has_staged_order flag
