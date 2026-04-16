"""
tests/test_pre_trade_gates_v2.py

ADR-005: V2 Router WARTIME whitelist + BAG support + BTC cash-paid notional.
Covers _pre_trade_gates in telegram_bot.py.

Test matrix (8 core + 4 fail-closed regressions):
  1. v2_router + WARTIME               → allowed
  2. legacy_approve + ALL modes         → allowed (ADR-007, was blocked per ADR-005)
  3. BAG under $25k notional           → allowed
  4. BAG over $25k notional            → blocked
  5. OPT BUY high strike low premium   → allowed (cash-paid semantics)
  6. OPT BUY low strike high premium   → blocked when cash > $25k
  7. OPT SELL high strike              → still uses strike-notional (regression)
  8. _HALTED=True                      → blocked regardless of site
  9. OPT BUY negative lmtPrice         → fail-closed
 10. STK zero lmtPrice                 → fail-closed
 11. Unknown sec_type (FUT)            → fail-closed
 12. Zero quantity                     → fail-closed

Async convention: this suite calls _pre_trade_gates via asyncio.run()
inside sync test functions to match the existing 634-test convention.
Do NOT convert to @pytest.mark.asyncio — the project deliberately
avoids the pytest-asyncio dependency (ADR-005 pre-flight ruling).
"""
import asyncio

import pytest
from types import SimpleNamespace

from telegram_bot import _pre_trade_gates


# ---------------------------------------------------------------------------
# Async-to-sync helper.
#
# _pre_trade_gates is an async coroutine but the only await it ever hits
# is inside Gate 4 (F20 guard), which is short-circuited by audit_id=None
# in every test in this file. So asyncio.run() drives the coroutine to
# completion without any real I/O suspension — safe and fast.
#
# Using asyncio.run() per-call (vs one shared loop) is intentional: each
# test gets a fresh loop, no cross-test state bleed, no fixture complexity.
# ---------------------------------------------------------------------------

def _run_gate(order, contract, context):
    """Sync wrapper so test bodies stay single-line asserts."""
    return asyncio.run(_pre_trade_gates(order, contract, context))


# ---------------------------------------------------------------------------
# Mock helpers — SimpleNamespace because MagicMock returns truthy mock
# instances for undefined attributes, which can mask fail-closed bugs
# in the gate. SimpleNamespace only exposes what we explicitly set.
# DO NOT convert to MagicMock for "convenience."
# ---------------------------------------------------------------------------

def make_order(action="SELL", totalQuantity=1, lmtPrice=1.0):
    """Mock ib_async.Order with the attributes _pre_trade_gates reads."""
    return SimpleNamespace(
        action=action,
        totalQuantity=totalQuantity,
        lmtPrice=lmtPrice,
    )


def make_contract(secType="OPT", strike=100.0):
    """Mock ib_async.Contract / Option with the attributes the gate reads."""
    return SimpleNamespace(
        secType=secType,
        strike=strike,
    )


def make_context(site="legacy_approve", audit_id=None, household="Yash_Household"):
    """Standard _pre_trade_gates context dict."""
    return {"site": site, "audit_id": audit_id, "household": household}


# ---------------------------------------------------------------------------
# Fixtures — each test uses exactly one mode fixture.
# ---------------------------------------------------------------------------

@pytest.fixture
def peacetime(monkeypatch):
    """Force mode gate to PEACETIME and _HALTED=False."""
    monkeypatch.setattr("telegram_bot._HALTED", False)
    monkeypatch.setattr("telegram_bot._get_current_desk_mode", lambda: "PEACETIME")


@pytest.fixture
def wartime(monkeypatch):
    """Force mode gate to WARTIME and _HALTED=False."""
    monkeypatch.setattr("telegram_bot._HALTED", False)
    monkeypatch.setattr("telegram_bot._get_current_desk_mode", lambda: "WARTIME")


@pytest.fixture
def halted(monkeypatch):
    """Force _HALTED=True (mode irrelevant — halt supersedes)."""
    monkeypatch.setattr("telegram_bot._HALTED", True)
    monkeypatch.setattr("telegram_bot._get_current_desk_mode", lambda: "PEACETIME")


# ---------------------------------------------------------------------------
# 1. Core ADR-005 coverage matrix
# ---------------------------------------------------------------------------

def test_1_v2_router_wartime_allowed(wartime):
    """v2_router site must be WARTIME-whitelisted (ADR-005 R4)."""
    contract = make_contract(secType="OPT", strike=200.0)
    order = make_order(action="BUY", totalQuantity=5, lmtPrice=0.05)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert allowed, f"v2_router should be WARTIME-allowed; got: {reason}"


@pytest.mark.parametrize("mode", ["PEACETIME", "AMBER", "WARTIME"])
def test_adr007_legacy_approve_allowed_in_all_modes(mode, monkeypatch):
    """ADR-007 (2026-04-13): Mode-based gating suspended for legacy_approve.

    legacy_approve is allowed to transmit in PEACETIME, AMBER, and WARTIME.
    The previous regression canary (test_2_legacy_approve_wartime_blocked)
    protected the original ADR-005 contract that locked legacy_approve out
    of WARTIME. ADR-007 supersedes that contract because the wartime lockout
    created a deadlock: the desk could not generate the income needed to exit
    WARTIME without writing Mode 1 CCs via legacy_approve.

    Re-enabling the wartime gate is logged as the ADR-007 sunset criterion.
    When the desk has been in PEACETIME for 10+ consecutive trading days
    based on natural leverage, re-introduce the gate via a new ADR.
    """
    monkeypatch.setattr("telegram_bot._HALTED", False)
    monkeypatch.setattr("telegram_bot._get_current_desk_mode", lambda: mode)
    contract = make_contract(secType="OPT", strike=100.0)
    order = make_order(action="SELL", totalQuantity=1, lmtPrice=2.0)
    allowed, reason = _run_gate(order, contract, make_context(site="legacy_approve"))
    assert allowed, (
        f"legacy_approve should be allowed in {mode} per ADR-007 — "
        f"got blocked with reason: {reason}"
    )


def test_3_bag_under_ceiling_allowed(peacetime):
    """BAG with small net debit passes notional ceiling (ADR-005 CC1)."""
    contract = make_contract(secType="BAG", strike=None)
    # Net debit $2.50 * 1 contract * 100 = $250
    order = make_order(action="BUY", totalQuantity=1, lmtPrice=2.50)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert allowed, f"BAG at $250 notional should pass; got: {reason}"


def test_4_bag_high_notional_allowed(peacetime):
    """BAG with large net debit blocked by $25k notional ceiling."""
    contract = make_contract(secType="BAG", strike=None)
    # Net debit $300 * 1 contract * 100 = $30,000 > $25,000
    order = make_order(action="BUY", totalQuantity=1, lmtPrice=300.0)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert not allowed
    assert "Notional" in reason
    assert "ceiling" in reason


def test_5_opt_buy_high_strike_low_premium_allowed(peacetime):
    """BTC on $200 strike, $0.05 premium, 5 contracts → $25 cash, allowed.

    Under old strike-notional math this registered as $100,000 and was
    blocked. ADR-005 CC1 fixes this.
    """
    contract = make_contract(secType="OPT", strike=200.0)
    order = make_order(action="BUY", totalQuantity=5, lmtPrice=0.05)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert allowed, (
        f"BTC high strike + low premium should use cash-paid notional; got: {reason}"
    )


def test_6_opt_buy_high_cash_blocked(peacetime):
    """BUY at $50 strike, $60 premium, 10 contracts → $60k cash, blocked."""
    contract = make_contract(secType="OPT", strike=50.0)
    order = make_order(action="BUY", totalQuantity=10, lmtPrice=60.0)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert not allowed
    assert "Notional" in reason
    assert "60,000" in reason


def test_7_opt_sell_still_strike_notional(peacetime):
    """OPT SELL $100 strike, 3 contracts → $30k obligation, blocked (regression).

    Ensures the cash-paid correction did NOT break the SELL path.
    """
    contract = make_contract(secType="OPT", strike=100.0)
    order = make_order(action="SELL", totalQuantity=3, lmtPrice=1.50)
    allowed, reason = _run_gate(order, contract, make_context(site="legacy_approve"))
    assert not allowed
    assert "Notional" in reason
    # strike-notional: 3 * 100 * 100 = 30,000 > 25,000
    assert "30,000" in reason


def test_8_halted_blocks_v2_router(halted):
    """_HALTED=True supersedes site whitelist."""
    contract = make_contract(secType="OPT", strike=50.0)
    order = make_order(action="BUY", totalQuantity=1, lmtPrice=0.10)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert not allowed
    assert "halted" in reason.lower()


# ---------------------------------------------------------------------------
# 2. Fail-closed regressions — ensure no silent passthroughs
# ---------------------------------------------------------------------------

def test_9_opt_buy_negative_lmtprice_fail_closed(peacetime):
    """OPT BUY with negative lmtPrice must fail-closed (no credit BTCs)."""
    contract = make_contract(secType="OPT", strike=100.0)
    order = make_order(action="BUY", totalQuantity=1, lmtPrice=-1.0)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert not allowed
    assert "negative" in reason.lower() or "fail-closed" in reason.lower()


def test_10_stk_zero_lmtprice_fail_closed(peacetime):
    """STK with zero lmtPrice must fail-closed (no silent passthrough)."""
    contract = make_contract(secType="STK", strike=None)
    order = make_order(action="SELL", totalQuantity=100, lmtPrice=0.0)
    allowed, reason = _run_gate(order, contract, make_context(site="legacy_approve"))
    assert not allowed
    assert "lmtPrice" in reason or "fail-closed" in reason.lower()


def test_11_unknown_sectype_fail_closed(peacetime):
    """Unknown secType (FUT) must fail-closed at Gate 2, not slip through.

    Catches the exact bug class that let BAG slip the old non-wheel filter
    pre-ADR-005: an unhandled elif branch silently returning notional=0.
    """
    contract = make_contract(secType="FUT", strike=4500.0)
    order = make_order(action="BUY", totalQuantity=1, lmtPrice=100.0)
    allowed, reason = _run_gate(order, contract, make_context(site="v2_router"))
    assert not allowed
    assert "FUT" in reason or "unsupported" in reason.lower()


def test_12_zero_quantity_fail_closed(peacetime):
    """Zero qty must fail-closed at Gate 2 before any math runs."""
    contract = make_contract(secType="OPT", strike=100.0)
    order = make_order(action="SELL", totalQuantity=0, lmtPrice=1.0)
    allowed, reason = _run_gate(order, contract, make_context(site="legacy_approve"))
    assert not allowed
    assert "zero" in reason.lower() or "fail-closed" in reason.lower()
