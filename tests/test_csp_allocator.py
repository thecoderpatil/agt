"""
tests/test_csp_allocator.py

Unit tests for agt_equities.csp_allocator._fetch_household_buying_power_snapshot.

M1.1 scope is data-layer only: the snapshot function wraps
ib_async.accountSummaryAsync and consumes pre-fetched
_discover_positions output to produce a household-indexed
HouseholdSnapshot dict. These tests mock IBKR at the
accountSummaryAsync boundary (no live IB required) and construct
synthetic _discover_positions dicts to drive the aggregation paths.

No sizing, no gate checks, no routing, no staging — those land in
M1.2+ and are out of scope for this file.

Design note: live-mode HOUSEHOLD_MAP is:
    Yash_Household = ["U21971297", "U22076329", "U22076184"]
    Vikram_Household = ["U22388499"]
MARGIN_ACCOUNTS = frozenset({"U21971297", "U22388499"})

Tests read HOUSEHOLD_MAP and MARGIN_ACCOUNTS directly from the config
module rather than hardcoding account IDs, so they adapt to paper
vs live mode at test collection time.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agt_equities.config import (
    ACCOUNT_TO_HOUSEHOLD,
    HOUSEHOLD_MAP,
    MARGIN_ACCOUNTS,
)
from agt_equities.csp_allocator import _fetch_household_buying_power_snapshot


def _run(coro):
    """Sync wrapper for async test bodies — matches project convention."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_mock_summary(data: dict[str, dict[str, float]]) -> list:
    """Build a fake ib_async AccountSummary list from {acct: {tag: value}}.

    ib_async.accountSummaryAsync returns a flat list of items where each
    item has .account, .tag, .value (string) attributes. This helper
    flattens a nested dict into that shape using SimpleNamespace stubs.
    """
    items = []
    for acct, tags in data.items():
        for tag, value in tags.items():
            items.append(
                SimpleNamespace(
                    account=acct,
                    tag=tag,
                    value=str(value),
                )
            )
    return items


class FakeIB:
    """Test double for ib_async.IB — only implements accountSummaryAsync."""

    def __init__(self, summary_data: dict[str, dict[str, float]]):
        self._summary = _make_mock_summary(summary_data)

    async def accountSummaryAsync(self):
        return self._summary


class FailingIB:
    """Test double that raises on accountSummaryAsync."""

    async def accountSummaryAsync(self):
        raise RuntimeError("simulated IB failure")


def _fake_discovered_positions(
    household: str,
    positions: list[dict] | None = None,
) -> dict:
    """Build a fake _discover_positions return dict for the given household.

    positions is a list of per-ticker dicts with keys:
      ticker, total_shares, spot_price, sector, short_puts,
      has_working_order, has_staged_order
    """
    return {
        "households": {
            household: {
                "household_nlv": 0,  # unused by allocator data layer
                "positions": positions or [],
            },
        },
        "all_book_nlv": 0,
    }


def _yash_margin_acct_id() -> str:
    """First margin-eligible account in Yash_Household.

    Live mode: U21971297. Paper mode: whatever paper account maps there.
    Returns None if Yash_Household isn't present (unusual).
    """
    yash_accts = HOUSEHOLD_MAP.get("Yash_Household", [])
    for acct in yash_accts:
        if acct in MARGIN_ACCOUNTS:
            return acct
    raise RuntimeError("test fixture: no margin-eligible Yash account")


def _yash_ira_acct_ids() -> list[str]:
    """All non-margin-eligible accounts in Yash_Household."""
    yash_accts = HOUSEHOLD_MAP.get("Yash_Household", [])
    return [a for a in yash_accts if a not in MARGIN_ACCOUNTS]


def _vikram_acct_id() -> str:
    """Vikram_Household primary account (margin-eligible)."""
    vikram_accts = HOUSEHOLD_MAP.get("Vikram_Household", [])
    for acct in vikram_accts:
        if acct in MARGIN_ACCOUNTS:
            return acct
    raise RuntimeError("test fixture: no margin-eligible Vikram account")


def _all_yash_summary(
    *, margin_nlv: float, margin_el: float, margin_bp: float,
    ira_nlv_each: float,
) -> dict[str, dict[str, float]]:
    """Build an accountSummary dict for all Yash_Household accounts."""
    margin_acct = _yash_margin_acct_id()
    ira_accts = _yash_ira_acct_ids()
    data: dict[str, dict[str, float]] = {
        margin_acct: {
            "NetLiquidation": margin_nlv,
            "ExcessLiquidity": margin_el,
            "BuyingPower": margin_bp,
        },
    }
    for ira in ira_accts:
        data[ira] = {
            "NetLiquidation": ira_nlv_each,
            "ExcessLiquidity": 0.0,
            "BuyingPower": 0.0,
        }
    return data


# ---------------------------------------------------------------------------
# 1. Empty snapshot on IB failure
# ---------------------------------------------------------------------------

def test_snapshot_empty_on_ib_failure():
    """accountSummaryAsync raises → returns empty dict, never propagates."""
    failing = FailingIB()
    result = _run(
        _fetch_household_buying_power_snapshot(
            failing, _fake_discovered_positions("Yash_Household"),
        )
    )
    assert result == {}


# ---------------------------------------------------------------------------
# 2. Unknown accounts are skipped
# ---------------------------------------------------------------------------

def test_snapshot_skips_accounts_not_in_household_map():
    """accountSummaryAsync returns data for an unknown account ID.
    That account must not contribute to any household's totals.
    """
    summary = {
        "UNKNOWN_ACCT_9999": {
            "NetLiquidation": 500000.0,
            "ExcessLiquidity": 250000.0,
            "BuyingPower": 1000000.0,
        },
    }
    ib = FakeIB(summary)
    result = _run(
        _fetch_household_buying_power_snapshot(
            ib, _fake_discovered_positions("Yash_Household"),
        )
    )
    # Snapshots are built for every household in HOUSEHOLD_MAP regardless
    # of whether any account data was found, so the structure is present.
    # The unknown account must not appear anywhere in the output.
    for hh_name, snap in result.items():
        assert "UNKNOWN_ACCT_9999" not in snap["accounts"]
        # All accounts have zero NLV since no known accounts had data
        assert snap["hh_nlv"] == 0.0


# ---------------------------------------------------------------------------
# 3. Yash household structure (happy path with one long position)
# ---------------------------------------------------------------------------

def test_snapshot_yash_household_structure():
    """Mock Yash + Vikram summary + one AAPL position. Verify:
    - hh_nlv sums all Yash account NLVs
    - hh_margin_nlv counts only the margin-eligible Yash account
    - margin_eligible flags are correct
    - existing_positions carries AAPL with correct current_value
    """
    yash_margin_acct = _yash_margin_acct_id()
    yash_ira_accts = _yash_ira_acct_ids()
    vikram_acct = _vikram_acct_id()

    summary = _all_yash_summary(
        margin_nlv=109000.0,
        margin_el=32000.0,
        margin_bp=54000.0,
        ira_nlv_each=76000.0,  # arbitrary per-IRA NLV
    )
    summary[vikram_acct] = {
        "NetLiquidation": 200000.0,
        "ExcessLiquidity": 50000.0,
        "BuyingPower": 100000.0,
    }
    ib = FakeIB(summary)

    discovered = _fake_discovered_positions(
        "Yash_Household",
        positions=[
            {
                "ticker": "AAPL",
                "total_shares": 200,
                "spot_price": 185.50,
                "sector": "Technology Hardware",
                "short_puts": [],
                "has_working_order": False,
                "has_staged_order": False,
            }
        ],
    )

    result = _run(_fetch_household_buying_power_snapshot(ib, discovered))

    yash = result["Yash_Household"]
    expected_hh_nlv = 109000.0 + 76000.0 * len(yash_ira_accts)
    assert yash["hh_nlv"] == pytest.approx(expected_hh_nlv)
    # hh_margin_nlv only counts the margin-eligible account
    assert yash["hh_margin_nlv"] == pytest.approx(109000.0)
    assert yash["hh_margin_el"] == pytest.approx(32000.0)

    # Margin-eligible flag correctness
    assert yash["accounts"][yash_margin_acct]["margin_eligible"] is True
    for ira in yash_ira_accts:
        assert yash["accounts"][ira]["margin_eligible"] is False

    # AAPL position
    assert "AAPL" in yash["existing_positions"]
    aapl = yash["existing_positions"]["AAPL"]
    assert aapl["total_shares"] == 200
    assert aapl["spot"] == 185.50
    assert aapl["current_value"] == pytest.approx(200 * 185.50)
    assert aapl["sector"] == "Technology Hardware"


# ---------------------------------------------------------------------------
# 4. Vikram household structure
# ---------------------------------------------------------------------------

def test_snapshot_vikram_household_structure():
    """Vikram_Household has a single margin-eligible account.
    - hh_nlv == hh_margin_nlv
    - No IRAs in accounts dict
    """
    vikram_acct = _vikram_acct_id()
    summary = {
        vikram_acct: {
            "NetLiquidation": 200000.0,
            "ExcessLiquidity": 50000.0,
            "BuyingPower": 100000.0,
        },
    }
    ib = FakeIB(summary)
    result = _run(
        _fetch_household_buying_power_snapshot(
            ib, _fake_discovered_positions("Vikram_Household"),
        )
    )

    vikram = result["Vikram_Household"]
    assert vikram["hh_nlv"] == pytest.approx(200000.0)
    assert vikram["hh_margin_nlv"] == pytest.approx(200000.0)
    assert vikram["hh_nlv"] == vikram["hh_margin_nlv"]

    # Every account in Vikram_Household is margin-eligible
    for acct_id, acct in vikram["accounts"].items():
        assert acct["margin_eligible"] is True


# ---------------------------------------------------------------------------
# 5. Existing CSPs aggregated across accounts
# ---------------------------------------------------------------------------

def test_snapshot_existing_csps_aggregated():
    """Yash has 2 short MSFT puts spread across accounts (aggregated
    by _discover_positions before reaching the allocator).
    Verify total_contracts and notional_commitment aggregate correctly.
    """
    summary = _all_yash_summary(
        margin_nlv=109000.0, margin_el=32000.0, margin_bp=54000.0,
        ira_nlv_each=76000.0,
    )
    ib = FakeIB(summary)

    discovered = _fake_discovered_positions(
        "Yash_Household",
        positions=[
            {
                "ticker": "MSFT",
                "total_shares": 0,
                "spot_price": 420.0,
                "sector": "Technology",
                "short_puts": [
                    {"contracts": 1, "strike": 420.0},
                    {"contracts": 1, "strike": 420.0},
                ],
                "has_working_order": False,
                "has_staged_order": False,
            }
        ],
    )

    result = _run(_fetch_household_buying_power_snapshot(ib, discovered))

    yash_csps = result["Yash_Household"]["existing_csps"]
    assert "MSFT" in yash_csps
    assert yash_csps["MSFT"]["total_contracts"] == 2
    # Notional: 2 contracts × $420 × 100 = $84,000
    assert yash_csps["MSFT"]["notional_commitment"] == pytest.approx(84000.0)

    # With MSFT short_puts but zero shares, existing_positions should NOT
    # include MSFT (only long stock positions go there).
    assert "MSFT" not in result["Yash_Household"]["existing_positions"]


# ---------------------------------------------------------------------------
# 6. IRA cash approximation formula
# ---------------------------------------------------------------------------

def test_snapshot_ira_cash_approximation():
    """Verify the IRA cash_available formula:
        acct_pct = acct_nlv / hh_nlv
        hh_unencumbered = max(0, hh_nlv - hh_long_notional - hh_csp_notional)
        acct_cash = acct_pct * hh_unencumbered

    Setup: Yash margin acct $109k NLV, one IRA $152k NLV, one long position
    worth $50k, no CSPs. Total hh_nlv = 109 + 152 = 261k.
    Unencumbered = 261 - 50 - 0 = 211k.
    IRA cash ≈ (152/261) * 211 = 122.87k

    (If live config includes the dormant U22076184 IRA, we set its NLV
    to zero so it doesn't distort the ratio. We compute the expected
    value dynamically using the actual IRA account list.)
    """
    ira_accts = _yash_ira_acct_ids()
    # Give the first IRA $152k, any additional IRAs $0
    ira_nlvs = {ira_accts[0]: 152000.0}
    for extra in ira_accts[1:]:
        ira_nlvs[extra] = 0.0

    yash_margin_acct = _yash_margin_acct_id()
    summary = {
        yash_margin_acct: {
            "NetLiquidation": 109000.0,
            "ExcessLiquidity": 32000.0,
            "BuyingPower": 54000.0,
        },
    }
    for ira, nlv in ira_nlvs.items():
        summary[ira] = {
            "NetLiquidation": nlv,
            "ExcessLiquidity": 0.0,
            "BuyingPower": 0.0,
        }
    ib = FakeIB(summary)

    # One long position: $50k total market value
    discovered = _fake_discovered_positions(
        "Yash_Household",
        positions=[
            {
                "ticker": "GOOG",
                "total_shares": 250,
                "spot_price": 200.0,  # 250 × 200 = $50,000
                "sector": "Communication",
                "short_puts": [],
                "has_working_order": False,
                "has_staged_order": False,
            }
        ],
    )

    result = _run(_fetch_household_buying_power_snapshot(ib, discovered))
    yash = result["Yash_Household"]

    # Expected hh_nlv = 109k + sum(ira_nlvs) = 109 + 152 = 261
    expected_hh_nlv = 109000.0 + sum(ira_nlvs.values())
    assert yash["hh_nlv"] == pytest.approx(expected_hh_nlv)

    # Long notional = 50k, csp notional = 0
    hh_long = 50000.0
    hh_csp = 0.0
    expected_unencumbered = max(0.0, expected_hh_nlv - hh_long - hh_csp)

    # First IRA cash_available: (152/expected_hh_nlv) × expected_unencumbered
    first_ira = ira_accts[0]
    expected_first_ira_cash = (152000.0 / expected_hh_nlv) * expected_unencumbered
    assert yash["accounts"][first_ira]["cash_available"] == pytest.approx(
        expected_first_ira_cash
    )

    # Margin account cash_available is buying_power directly (54k), NOT the
    # IRA formula. This is the key distinction — test it explicitly.
    assert yash["accounts"][yash_margin_acct]["cash_available"] == pytest.approx(54000.0)


# ---------------------------------------------------------------------------
# 7. cash_available never negative (defensive)
# ---------------------------------------------------------------------------

def test_snapshot_cash_never_negative():
    """If existing long notional + CSP commitment exceeds household NLV
    (pathological case — shouldn't happen in practice but defensive),
    cash_available must clamp to 0.0 for all accounts, not go negative.
    """
    yash_margin_acct = _yash_margin_acct_id()
    ira_accts = _yash_ira_acct_ids()
    summary = {
        yash_margin_acct: {
            "NetLiquidation": 50000.0,
            "ExcessLiquidity": 10000.0,
            # Negative buying power → cash_available clamped to 0
            "BuyingPower": -5000.0,
        },
    }
    for ira in ira_accts:
        summary[ira] = {
            "NetLiquidation": 30000.0,
            "ExcessLiquidity": 0.0,
            "BuyingPower": 0.0,
        }
    ib = FakeIB(summary)

    # Huge long position swamps household NLV
    discovered = _fake_discovered_positions(
        "Yash_Household",
        positions=[
            {
                "ticker": "TSLA",
                "total_shares": 10000,
                "spot_price": 500.0,  # $5M long notional
                "sector": "Consumer Discretionary",
                "short_puts": [],
                "has_working_order": False,
                "has_staged_order": False,
            }
        ],
    )

    result = _run(_fetch_household_buying_power_snapshot(ib, discovered))
    yash = result["Yash_Household"]

    for acct_id, acct in yash["accounts"].items():
        assert acct["cash_available"] >= 0.0, (
            f"{acct_id} cash_available went negative: {acct['cash_available']}"
        )


# ---------------------------------------------------------------------------
# 8. Zero NLV households handled without crash
# ---------------------------------------------------------------------------

def test_snapshot_zero_nlv_households_handled():
    """A household with zero NLV in every account (new empty account,
    or data-fetch failure on specific tags) must produce a snapshot
    with all zeros and no division-by-zero crash."""
    # Empty summary — NO tags for any known account
    summary: dict[str, dict[str, float]] = {}
    ib = FakeIB(summary)

    result = _run(
        _fetch_household_buying_power_snapshot(
            ib, _fake_discovered_positions("Yash_Household"),
        )
    )

    # Every household in HOUSEHOLD_MAP should be present in output
    for hh_name in HOUSEHOLD_MAP:
        assert hh_name in result
        snap = result[hh_name]
        assert snap["hh_nlv"] == 0.0
        assert snap["hh_margin_nlv"] == 0.0
        assert snap["hh_margin_el"] == 0.0
        for acct in snap["accounts"].values():
            assert acct["nlv"] == 0.0
            assert acct["el"] == 0.0
            assert acct["buying_power"] == 0.0
            assert acct["cash_available"] == 0.0


# ---------------------------------------------------------------------------
# Working / staged order tickers (bonus structural coverage)
# ---------------------------------------------------------------------------

def test_snapshot_working_and_staged_order_tickers():
    """Positions flagged has_working_order / has_staged_order surface in
    the household's working_order_tickers / staged_order_tickers sets.
    Not in the numbered spec but structurally important — these sets
    are consumed by M1.2+ "don't double up" checks."""
    summary = _all_yash_summary(
        margin_nlv=109000.0, margin_el=32000.0, margin_bp=54000.0,
        ira_nlv_each=76000.0,
    )
    ib = FakeIB(summary)
    discovered = _fake_discovered_positions(
        "Yash_Household",
        positions=[
            {
                "ticker": "NVDA",
                "total_shares": 100,
                "spot_price": 900.0,
                "sector": "Semiconductors",
                "short_puts": [],
                "has_working_order": True,
                "has_staged_order": False,
            },
            {
                "ticker": "GOOGL",
                "total_shares": 0,
                "spot_price": 180.0,
                "sector": "Communication",
                "short_puts": [],
                "has_working_order": False,
                "has_staged_order": True,
            },
        ],
    )
    result = _run(_fetch_household_buying_power_snapshot(ib, discovered))
    yash = result["Yash_Household"]
    assert yash["working_order_tickers"] == {"NVDA"}
    assert yash["staged_order_tickers"] == {"GOOGL"}
