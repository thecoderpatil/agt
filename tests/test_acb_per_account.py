"""
tests/test_acb_per_account.py

ADR-006: Per-account ACB precision + same-day delta reconciliation.
Covers walker.Cycle per-account methods and _load_premium_ledger_snapshot
per-account extension.

Test matrix:
  1. Cycle._premium_by_account attributes premium correctly across accounts
  2. Cycle.adjusted_basis_for_account returns correct per-account ACB
  3. Cycle.adjusted_basis_for_account returns None when account holds 0 shares
  4. _load_premium_ledger_snapshot(hh, tk, account_id) walker path per-account
  5. _load_premium_ledger_snapshot(hh, tk, account_id) legacy path fail-closed
  6. _load_premium_ledger_snapshot(hh, tk) legacy signature regression
  7. V2 router STATE_1 uses per-account basis (integration)
  8. V2 router STATE_3 uses per-account basis (integration)
  9. get_active_cycles_with_intraday_delta merges fill_log correctly
 10. get_active_cycles_with_intraday_delta idempotent with no delta
 11. V2 router writes bot_believed_adjusted_basis + basis_truth_level
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agt_equities.walker import (
    TradeEvent, walk_cycles, _new_cycle,
)


# ---------------------------------------------------------------------------
# Test helpers — build synthetic events for walker
# ---------------------------------------------------------------------------

def make_event(
    *,
    source="FLEX_TRADE",
    account_id,
    household_id,
    ticker,
    date_time,
    asset_category,
    buy_sell,
    quantity,
    trade_price,
    net_cash,
    right=None,
    strike=None,
    open_close=None,
    transaction_type="ExchTrade",
    transaction_id=None,
):
    return TradeEvent(
        source=source,
        account_id=account_id,
        household_id=household_id,
        ticker=ticker,
        trade_date=date_time[:8],
        date_time=date_time,
        ib_order_id=None,
        transaction_id=transaction_id or f"tid_{date_time}_{account_id}",
        asset_category=asset_category,
        right=right,
        strike=strike,
        expiry=None,
        buy_sell=buy_sell,
        open_close=open_close,
        quantity=quantity,
        trade_price=trade_price,
        net_cash=net_cash,
        fifo_pnl_realized=0.0,
        transaction_type=transaction_type,
        notes="",
        currency="USD",
        raw={},
    )


# ---------------------------------------------------------------------------
# 1-3. Walker per-account premium attribution
# ---------------------------------------------------------------------------

def test_1_premium_attributed_per_account():
    """CSPs opened from two different accounts credit their own premium."""
    # Yash Individual opens CSP, receives $100 premium
    ev1 = make_event(
        account_id="U21971297", household_id="Yash_Household", ticker="AAPL",
        date_time="20260101;100000", asset_category="OPT",
        right="P", strike=150.0, buy_sell="SELL", open_close="O",
        quantity=1, trade_price=1.0, net_cash=100.0,
    )
    # Yash Roth opens CSP on same ticker, receives $200 premium
    ev2 = make_event(
        account_id="U22076329", household_id="Yash_Household", ticker="AAPL",
        date_time="20260101;110000", asset_category="OPT",
        right="P", strike=150.0, buy_sell="SELL", open_close="O",
        quantity=1, trade_price=2.0, net_cash=200.0,
    )
    cycles = walk_cycles([ev1, ev2])
    assert len(cycles) == 1
    c = cycles[0]
    assert c.premium_for_account("U21971297") == 100.0
    assert c.premium_for_account("U22076329") == 200.0
    assert c.premium_total == 300.0  # household aggregate still correct


def test_2_adjusted_basis_per_account_different_bases():
    """Two accounts hold same ticker at different basis; adjusted basis differs.

    Cycle is constructed directly to avoid walker's cycle-bootstrap rules
    (STK_BUY_DIRECT cannot open a new cycle). Same unit-test pattern as
    test_3 — exercises Cycle.adjusted_basis_for_account() math directly.
    """
    cycle = _new_cycle("Yash_Household", "AAPL", 1, "20260101;100000")

    # Yash Ind: 100 shares @ $150 cost basis, CC premium $500
    cycle._paper_basis_by_account["U21971297"] = (15000.0, 100.0)
    cycle._premium_by_account["U21971297"] = 500.0

    # Yash Roth: 100 shares @ $140 cost basis, CC premium $300
    cycle._paper_basis_by_account["U22076329"] = (14000.0, 100.0)
    cycle._premium_by_account["U22076329"] = 300.0

    # Mirror the household aggregates the walker would produce
    cycle.shares_held = 200.0
    cycle.premium_total = 800.0

    # Per-account paper basis
    assert cycle.paper_basis_for_account("U21971297") == 150.0
    assert cycle.paper_basis_for_account("U22076329") == 140.0
    # Per-account adjusted basis: paper - (premium / shares)
    # Ind:  150 - (500/100) = 145.0
    # Roth: 140 - (300/100) = 137.0
    assert cycle.adjusted_basis_for_account("U21971297") == 145.0
    assert cycle.adjusted_basis_for_account("U22076329") == 137.0
    # Household aggregate paper_basis is a weighted average:
    # (15000 + 14000) / 200 = 145.0
    # adjusted_basis: 145.0 - 800/200 = 141.0
    assert cycle.paper_basis == 145.0
    assert cycle.adjusted_basis == 141.0
    # CRITICAL: Ind's per-account ACB (145) != household ACB (141).
    # This is exactly the precision the V2 router needs for Act 60.
    assert cycle.adjusted_basis_for_account("U21971297") != cycle.adjusted_basis


def test_3_adjusted_basis_for_account_returns_none_when_zero_shares():
    """Premium attributed to an account with no shares returns None ACB."""
    # Yash Ind: only CC premium, no shares (hypothetical edge case —
    # shouldn't happen in practice because CC requires underlying,
    # but test the guard)
    cycle = _new_cycle("Yash_Household", "AAPL", 1, "20260101;100000")
    cycle._premium_by_account["U21971297"] = 500.0
    # No paper basis entry → shares = 0
    assert cycle.adjusted_basis_for_account("U21971297") is None


# ---------------------------------------------------------------------------
# 4-6. _load_premium_ledger_snapshot per-account extension
# ---------------------------------------------------------------------------

def _make_mock_cycle(
    household_id="Yash_Household", ticker="AAPL",
    per_account_basis=None, per_account_premium=None,
    shares_held=200, premium_total=800.0,
):
    """Build a real Cycle via _new_cycle, not a mock."""
    cycle = _new_cycle(household_id, ticker, 1, "20260101;100000")
    if per_account_basis:
        for acct, (cost, shares) in per_account_basis.items():
            cycle._paper_basis_by_account[acct] = (cost, shares)
    if per_account_premium:
        cycle._premium_by_account.update(per_account_premium)
    cycle.shares_held = shares_held
    cycle.premium_total = premium_total
    return cycle


def test_4_load_snapshot_per_account_walker_path():
    """With READ_FROM_MASTER_LOG=True and account_id, returns per-account values."""
    import telegram_bot
    mock_cycle = _make_mock_cycle(
        per_account_basis={"U21971297": (15000.0, 100), "U22076329": (14000.0, 100)},
        per_account_premium={"U21971297": 500.0, "U22076329": 300.0},
    )

    with patch.object(telegram_bot, "READ_FROM_MASTER_LOG", True):
        with patch("agt_equities.trade_repo.get_active_cycles_with_intraday_delta", return_value=[mock_cycle]):
            result = telegram_bot._load_premium_ledger_snapshot(
                "Yash_Household", "AAPL", account_id="U21971297"
            )

    assert result is not None
    assert result["account_id"] == "U21971297"
    assert result["initial_basis"] == 150.0  # 15000/100
    assert result["adjusted_basis"] == 145.0  # 150 - (500/100)
    assert result["shares_owned"] == 100
    assert result["basis_truth_level"] == "WALKER_WITH_INTRADAY_DELTA"


def test_5_load_snapshot_per_account_legacy_fail_closed():
    """With READ_FROM_MASTER_LOG=False and account_id, returns None."""
    import telegram_bot
    with patch.object(telegram_bot, "READ_FROM_MASTER_LOG", False):
        result = telegram_bot._load_premium_ledger_snapshot(
            "Yash_Household", "AAPL", account_id="U21971297"
        )
    assert result is None


def test_6_load_snapshot_legacy_signature_regression():
    """With account_id=None, household-aggregated legacy behavior is unchanged."""
    import telegram_bot
    mock_cycle = _make_mock_cycle(
        per_account_basis={"U21971297": (15000.0, 100), "U22076329": (14000.0, 100)},
        per_account_premium={"U21971297": 500.0, "U22076329": 300.0},
    )

    with patch.object(telegram_bot, "READ_FROM_MASTER_LOG", True):
        with patch("agt_equities.trade_repo.get_active_cycles_with_intraday_delta", return_value=[mock_cycle]):
            result = telegram_bot._load_premium_ledger_snapshot(
                "Yash_Household", "AAPL"  # account_id omitted
            )

    assert result is not None
    assert "account_id" not in result
    # Household-aggregated paper basis = cycle.paper_basis (property)
    # = (15000 + 14000) / 200 = 145.0
    assert result["initial_basis"] == 145.0


# ---------------------------------------------------------------------------
# 7-8. V2 router integration — per-account classification
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="Integration test — requires V2 router harness scaffolding; add in follow-up sprint")
def test_7_v2_state_1_uses_per_account_basis():
    """V2 STATE_1 ASSIGN velocity uses account-specific initial_basis."""
    raise NotImplementedError("V2 router harness scaffolding — follow-up sprint")


@pytest.mark.xfail(strict=True, reason="Integration test — requires V2 router harness scaffolding; add in follow-up sprint")
def test_8_v2_state_3_uses_per_account_basis():
    """V2 STATE_3 DEFEND uses account-specific adjusted_basis."""
    raise NotImplementedError("V2 router harness scaffolding — follow-up sprint")


# ---------------------------------------------------------------------------
# 9-10. Same-day delta reconciliation
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="Integration test — requires fill_log + master_log_trades fixture setup; add in follow-up sprint")
def test_9_intraday_delta_merges_fill_log():
    """get_active_cycles_with_intraday_delta applies fill_log entries."""
    raise NotImplementedError("fill_log + master_log_trades fixture setup — follow-up sprint")


@pytest.mark.xfail(strict=True, reason="Integration test — requires fill_log fixture setup; add in follow-up sprint")
def test_10_intraday_delta_idempotent_baseline():
    """With no delta rows, result matches get_active_cycles exactly."""
    raise NotImplementedError("fill_log fixture setup — follow-up sprint")


# ---------------------------------------------------------------------------
# 11. Audit column wiring (deferred per Followup #10)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="Deferred to Followup #10 — cc_decision_log V2 router audit wiring")
def test_11_audit_columns_written():
    """V2 router writes bot_believed_adjusted_basis + basis_truth_level."""
    raise NotImplementedError("cc_decision_log V2 router audit wiring — Followup #10")
