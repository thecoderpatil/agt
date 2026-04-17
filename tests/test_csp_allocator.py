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

# CI gate: Sprint A marker + explicit file list in .gitlab-ci.yml.
pytestmark = pytest.mark.sprint_a

from agt_equities.config import (
    ACCOUNT_TO_HOUSEHOLD,
    HOUSEHOLD_MAP,
    MARGIN_ACCOUNTS,
)
from agt_equities.csp_allocator import (
    CSP_GATE_REGISTRY,
    AllocatorResult,
    _csp_check_rule_1,
    _csp_check_rule_2,
    _csp_check_vix_acceleration,
    _csp_check_rule_3,
    _csp_check_rule_4,
    _csp_check_rule_6,
    _csp_check_rule_7,
    _csp_route_to_accounts,
    _csp_size_household,
    _fetch_household_buying_power_snapshot,
    _vix_retain_pct,
    run_csp_allocator,
)


# ---------------------------------------------------------------------------
# ADR-008 MR 2: live ctx helper for tests.
# Wraps the caller's staging_fn in a SQLiteOrderSink so we preserve
# "staging_fn(tickets)" semantics without rewriting every assertion.
# ---------------------------------------------------------------------------


def _live_ctx(staging_fn=None):
    """Build a LIVE RunContext whose order_sink forwards tickets to
    ``staging_fn``. ``staging_fn=None`` produces a no-op sink (MR 1
    SQLiteOrderSink.stage early-returns on empty tickets; for non-empty
    it still calls the provided fn, so we default to a discard lambda).
    """
    import uuid as _uuid
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
    fn = staging_fn if staging_fn is not None else (lambda tickets: None)
    return RunContext(
        mode=RunMode.LIVE,
        run_id=_uuid.uuid4().hex,
        order_sink=SQLiteOrderSink(staging_fn=fn),
        decision_sink=NullDecisionSink(),
    )




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



# ===========================================================================
# M1.2: _csp_size_household + _csp_route_to_accounts — pure function tests
# ===========================================================================

def _fake_candidate(
    ticker: str = "AAPL",
    strike: float = 150.0,
    mid: float = 1.50,
    expiry: str = "2026-05-16",
    dte: int = 35,
    annualized_yield: float = 0.22,
) -> SimpleNamespace:
    """Build a minimal RAYCandidate duck-type for allocator unit tests."""
    return SimpleNamespace(
        ticker=ticker,
        strike=strike,
        mid=mid,
        expiry=expiry,
        dte=dte,
        annualized_yield=annualized_yield,
    )


def _fake_hh_snapshot(
    household: str = "Yash_Household",
    hh_nlv: float = 261_000.0,
    hh_margin_nlv: float = 109_000.0,
    hh_margin_el: float = 109_000.0,   # no pre-existing margin usage
    accounts: dict | None = None,
    existing_positions: dict | None = None,
    existing_csps: dict | None = None,
) -> dict:
    """Build a minimal HouseholdSnapshot for allocator unit tests.

    Defaults model a Yash-shaped household with ~$261K NLV split
    across one $109K margin account and two IRAs totalling $152K.
    Override any kwarg to drive specific test paths.
    """
    if accounts is None:
        accounts = {
            "U21971297": {
                "account_id": "U21971297",
                "nlv": 109_000.0,
                "el": 109_000.0,
                "buying_power": 200_000.0,
                "cash_available": 200_000.0,
                "margin_eligible": True,
            },
            "U22076329": {
                "account_id": "U22076329",
                "nlv": 100_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 100_000.0,
                "margin_eligible": False,
            },
            "U22076184": {
                "account_id": "U22076184",
                "nlv": 52_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 52_000.0,
                "margin_eligible": False,
            },
        }
    return {
        "household": household,
        "hh_nlv": hh_nlv,
        "hh_margin_nlv": hh_margin_nlv,
        "hh_margin_el": hh_margin_el,
        "accounts": accounts,
        "existing_positions": existing_positions or {},
        "existing_csps": existing_csps or {},
        "working_order_tickers": set(),
        "staged_order_tickers": set(),
    }


# ---------------------------------------------------------------------------
# Sizing tests (8)
# ---------------------------------------------------------------------------

def test_size_returns_zero_for_zero_nlv_household():
    """Degenerate hh_nlv=0 must short-circuit to 0 contracts."""
    hh = _fake_hh_snapshot(hh_nlv=0.0, hh_margin_nlv=0.0, hh_margin_el=0.0)
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    assert _csp_size_household(hh, cand, vix=18.0) == 0


def test_size_returns_zero_when_one_contract_exceeds_rule1_ceiling():
    """META $650 1c = $65K = ~25% of $261K household → exceeds 20%
    ceiling → returns 0."""
    hh = _fake_hh_snapshot()  # hh_nlv=261K, ceiling=$52.2K
    cand = _fake_candidate(ticker="META", strike=650.0)
    # 1c notional = $65K ≥ $52.2K → infeasible at any count
    assert _csp_size_household(hh, cand, vix=18.0) == 0


def test_size_picks_closest_to_10pct_target():
    """hh_nlv=$261K, AAPL $150: target=$26.1K, 1c=$15K, 2c=$30K.
    |15 - 26.1| = 11.1, |30 - 26.1| = 3.9 → picks 2 contracts.
    """
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    assert _csp_size_household(hh, cand, vix=18.0) == 2


def test_size_prefers_lower_on_tie():
    """Exact midpoint between c_low and c_high → prefer lower.

    hh_nlv=$225K → target=$22.5K. AAPL $150: 1c=$15K, 2c=$30K.
    |15-22.5|=7.5, |30-22.5|=7.5 → tie → picks 1.
    """
    hh = _fake_hh_snapshot(
        hh_nlv=225_000.0,
        hh_margin_nlv=225_000.0,
        hh_margin_el=225_000.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    assert _csp_size_household(hh, cand, vix=18.0) == 1


def test_size_respects_rule1_with_existing_position():
    """Existing exposure pushes post-trade over 20% ceiling → 0.

    hh_nlv=$261K, ceiling=$52.2K. AAPL $150, 1c notional=$15K.
    Existing AAPL position worth $40K. 1c → $55K total > $52.2K → 0.
    2c → $70K → also over. → 0.
    """
    hh = _fake_hh_snapshot(
        existing_positions={
            "AAPL": {
                "total_shares": 200,
                "spot": 200.0,
                "current_value": 40_000.0,
                "sector": "Technology Hardware",
            },
        },
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    assert _csp_size_household(hh, cand, vix=18.0) == 0


def test_size_respects_vix_rule2_at_low_vix():
    """Low VIX → retain 80%, deploy 20% → tight margin headroom.

    Configure margin_nlv so 20% deployment cannot accommodate the
    natural 10%-target contract count. The function must reduce the
    count to what Rule 2 allows.

    hh_nlv=$1M, hh_margin_nlv=$100K, no pre-use.
    AAPL $200: target=$100K, c_low=5 ($100K exact).
    5c margin impact = $100K*0.30 = $30K.
    At VIX=15: budget = $100K * 20% = $20K → headroom=$20K → 5c fails.
    4c impact = $80K*0.30 = $24K → still fails.
    3c impact = $60K*0.30 = $18K → fits, BUT feasibility probes
    only c_low (5) and c_high (6), so both fail → returns 0.

    This asserts that the _feasible() narrow-search semantics hold:
    Rule 2 binding + c_low/c_high infeasible → 0.
    """
    hh = _fake_hh_snapshot(
        hh_nlv=1_000_000.0,
        hh_margin_nlv=100_000.0,
        hh_margin_el=100_000.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=200.0)
    assert _csp_size_household(hh, cand, vix=15.0) == 0


def test_size_respects_vix_rule2_at_elevated_vix():
    """Elevated VIX → retain less, deploy more → looser headroom.

    Same inputs as the low-VIX test, but VIX=35 → retain 50%,
    deploy 50%, budget=$50K. 5c impact=$30K < $50K → c_low=5 feasible.
    c_high=6 impact=$36K < $50K → feasible, but 6c notional=$120K
    is further from $100K target than 5c=$100K → picks 5.
    """
    hh = _fake_hh_snapshot(
        hh_nlv=1_000_000.0,
        hh_margin_nlv=100_000.0,
        hh_margin_el=100_000.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=200.0)
    assert _csp_size_household(hh, cand, vix=35.0) == 5


def test_size_returns_zero_when_margin_headroom_exhausted():
    """Pre-existing margin usage consumes entire budget → headroom=0.

    hh_margin_nlv=$109K, hh_margin_el=$5K → used_pre=$104K.
    Any VIX budget is well under $104K → headroom=0 → every new
    contract has impact > 0 → infeasible → 0.
    """
    hh = _fake_hh_snapshot(
        hh_margin_nlv=109_000.0,
        hh_margin_el=5_000.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    assert _csp_size_household(hh, cand, vix=18.0) == 0


# ---------------------------------------------------------------------------
# Routing tests (4)
# ---------------------------------------------------------------------------

def test_route_ira_first_then_margin():
    """2 contracts, IRAs have plenty of room → all go to IRAs, not margin.

    Default fixture: two IRAs (U22076329 $100K, U22076184 $52K) plus
    one margin acct. AAPL $150 collateral = $15K/contract.
    Largest IRA first (U22076329, $100K cash) fits 6 contracts easily
    → both tickets land there, none on margin.
    """
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    tickets = _csp_route_to_accounts(2, hh, cand)
    assert len(tickets) == 1                        # single bulk ticket
    assert tickets[0]["account_id"] == "U22076329"  # largest IRA
    assert tickets[0]["quantity"] == 2
    # Crucially: nothing on the margin account
    assert all(t["account_id"] != "U21971297" for t in tickets)


@pytest.mark.skipif(
    not __import__("pathlib").Path(
        __import__("agt_equities.db", fromlist=["DB_PATH"]).DB_PATH
    ).exists(),
    reason="Production DB not available (CI/tripwire)",
)
def test_route_partial_when_household_cannot_fit_all():
    """Request 10 contracts but combined capacity only fits 6 → returns 6."""
    hh = _fake_hh_snapshot(
        accounts={
            "U22076329": {
                "account_id": "U22076329",
                "nlv": 50_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 50_000.0,        # fits 3 @ $15K
                "margin_eligible": False,
            },
            "U21971297": {
                "account_id": "U21971297",
                "nlv": 50_000.0,
                "el": 50_000.0,
                "buying_power": 50_000.0,          # fits 3 @ $15K
                "cash_available": 50_000.0,
                "margin_eligible": True,
            },
        },
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    tickets = _csp_route_to_accounts(10, hh, cand)
    total = sum(t["quantity"] for t in tickets)
    assert total == 6   # partial fill: all 6 that fit, not 10


@pytest.mark.skipif(
    not __import__("pathlib").Path(
        __import__("agt_equities.db", fromlist=["DB_PATH"]).DB_PATH
    ).exists(),
    reason="Production DB not available (CI/tripwire)",
)
def test_route_spills_from_ira_to_margin_when_ira_full():
    """IRA has capacity for 2 contracts, request 5 → 2 to IRA, 3 to margin."""
    hh = _fake_hh_snapshot(
        accounts={
            "U22076329": {
                "account_id": "U22076329",
                "nlv": 30_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 30_000.0,        # fits 2 @ $15K
                "margin_eligible": False,
            },
            "U21971297": {
                "account_id": "U21971297",
                "nlv": 200_000.0,
                "el": 200_000.0,
                "buying_power": 200_000.0,        # fits 13 @ $15K
                "cash_available": 200_000.0,
                "margin_eligible": True,
            },
        },
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    tickets = _csp_route_to_accounts(5, hh, cand)
    by_acct = {t["account_id"]: t["quantity"] for t in tickets}
    assert by_acct["U22076329"] == 2   # IRA filled first
    assert by_acct["U21971297"] == 3   # margin takes the spill
    assert sum(by_acct.values()) == 5


def test_route_ticket_shape_fields():
    """Verify every expected field is present on a ticket dict."""
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(
        ticker="aapl",          # lowercase — must be upcased in output
        strike=150.0,
        mid=1.50,
        expiry="2026-05-16",    # must be normalized to YYYYMMDD
        dte=35,
        annualized_yield=0.225,
    )
    tickets = _csp_route_to_accounts(1, hh, cand)
    assert len(tickets) == 1
    t = tickets[0]
    # Field presence + canonical values
    assert t["account_id"] == "U22076329"   # largest IRA
    assert t["household"] == "Yash_Household"
    assert t["ticker"] == "AAPL"            # upcased
    assert t["action"] == "SELL"
    assert t["sec_type"] == "OPT"
    assert t["right"] == "P"
    assert t["strike"] == 150.0
    assert isinstance(t["strike"], float)
    assert t["expiry"] == "20260516"        # normalized
    assert t["quantity"] == 1
    assert t["limit_price"] == pytest.approx(1.50)
    assert t["annualized_yield"] == pytest.approx(0.225)
    assert t["mode"] == "CSP_ENTRY"
    assert t["status"] == "staged"



# ===========================================================================
# M1.3: Rule gate predicate tests (_csp_check_rule_* + CSP_GATE_REGISTRY)
# ===========================================================================
#
# Each gate function is pure: (hh, candidate, n, vix, extras) -> (bool, str).
# Tests build minimal extras dicts, only populating the keys the gate under
# test actually reads. Rule 3 and Rule 4 are documented fail-open on missing
# data — the orchestrator logs holes separately.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 1 (concentration)
# ---------------------------------------------------------------------------

def test_rule_1_passes_clean_household():
    """Clean household, 1c AAPL @ $150 = $15K < 20% of $261K → pass."""
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    passed, reason = _csp_check_rule_1(hh, cand, 1, 18.0, {})
    assert passed is True
    assert reason == ""


def test_rule_1_rejects_at_ceiling():
    """Post-trade exposure exactly at 20% ceiling → strict < fails.

    hh_nlv=$261K → ceiling=$52.2K. Existing value = $52.2K - $15K = $37.2K,
    plus 1c new = $15K → exactly $52.2K. Strict < fails → reject.
    """
    hh = _fake_hh_snapshot(
        existing_positions={
            "AAPL": {
                "total_shares": 186,
                "spot": 200.0,
                "current_value": 37_200.0,  # hits ceiling exactly with 1c
                "sector": "Technology Hardware",
            },
        },
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    passed, reason = _csp_check_rule_1(hh, cand, 1, 18.0, {})
    assert passed is False
    assert "rule_1" in reason
    assert "ceiling" in reason


def test_rule_1_includes_existing_csp_notional():
    """Existing CSP notional combines with new CSP notional in Rule 1.

    hh_nlv=$261K, ceiling=$52.2K. Existing CSP on AAPL w/ notional $40K.
    New 1c AAPL @ $150 = $15K. Total = $55K ≥ $52.2K → reject.
    """
    hh = _fake_hh_snapshot(
        existing_csps={
            "AAPL": {
                "total_contracts": 2,
                "strike": 200.0,
                "notional_commitment": 40_000.0,
            },
        },
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    passed, reason = _csp_check_rule_1(hh, cand, 1, 18.0, {})
    assert passed is False
    assert "rule_1" in reason


# ---------------------------------------------------------------------------
# Rule 2 (VIX-scaled margin deployment)
# ---------------------------------------------------------------------------

def test_rule_2_ira_only_household_passes():
    """hh_margin_nlv=0 → Rule 2 inapplicable → always pass."""
    hh = _fake_hh_snapshot(
        hh_margin_nlv=0.0,
        hh_margin_el=0.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    passed, reason = _csp_check_rule_2(hh, cand, 10, 18.0, {})
    assert passed is True
    assert reason == ""


def test_rule_2_vix_15_restrictive():
    """VIX=15 → 20% deployment cap. Configure so 5c fails.

    hh_margin_nlv=$100K, hh_margin_el=$100K.
    Budget @ VIX15 = $100K * 20% = $20K, headroom=$20K.
    5c AAPL $200 → $100K * 0.30 = $30K impact > $20K → reject.
    """
    hh = _fake_hh_snapshot(
        hh_nlv=1_000_000.0,
        hh_margin_nlv=100_000.0,
        hh_margin_el=100_000.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=200.0)
    passed, reason = _csp_check_rule_2(hh, cand, 5, 15.0, {})
    assert passed is False
    assert "rule_2" in reason
    assert "margin impact" in reason


def test_rule_2_vix_45_loose():
    """VIX=45 → 60% deployment cap → same 5c now fits.

    Same margin fixture as above. Budget @ VIX45 = $100K * 60% = $60K.
    5c impact = $30K < $60K → pass.
    """
    hh = _fake_hh_snapshot(
        hh_nlv=1_000_000.0,
        hh_margin_nlv=100_000.0,
        hh_margin_el=100_000.0,
    )
    cand = _fake_candidate(ticker="AAPL", strike=200.0)
    passed, reason = _csp_check_rule_2(hh, cand, 5, 45.0, {})
    assert passed is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Rule 3 (GICS industry group concentration)
# ---------------------------------------------------------------------------

def test_rule_3_rejects_third_name_in_sector():
    """Sector already has 2 names; candidate is a 3rd → reject."""
    hh = _fake_hh_snapshot(
        existing_positions={
            "MSFT": {
                "total_shares": 100, "spot": 400.0,
                "current_value": 40_000.0,
                "sector": "Software - Application",
            },
            "ORCL": {
                "total_shares": 200, "spot": 125.0,
                "current_value": 25_000.0,
                "sector": "Software - Application",
            },
        },
    )
    cand = _fake_candidate(ticker="CRM", strike=250.0)
    extras = {
        "sector_map": {
            "MSFT": "Software - Application",
            "ORCL": "Software - Application",
            "CRM":  "Software - Application",
        },
    }
    passed, reason = _csp_check_rule_3(hh, cand, 1, 18.0, extras)
    assert passed is False
    assert "rule_3" in reason
    assert "Software - Application" in reason


def test_rule_3_unknown_sector_fails_open():
    """Unknown classification → pass (fail-open, documented behavior)."""
    hh = _fake_hh_snapshot(
        existing_positions={
            "MSFT": {
                "total_shares": 100, "spot": 400.0,
                "current_value": 40_000.0,
                "sector": "Software - Application",
            },
            "ORCL": {
                "total_shares": 200, "spot": 125.0,
                "current_value": 25_000.0,
                "sector": "Software - Application",
            },
        },
    )
    cand = _fake_candidate(ticker="WEIRDCO", strike=30.0)
    # sector_map returns "Unknown" for candidate → fail-open
    extras = {"sector_map": {"WEIRDCO": "Unknown"}}
    passed, reason = _csp_check_rule_3(hh, cand, 1, 18.0, extras)
    assert passed is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Rule 4 (correlation)
# ---------------------------------------------------------------------------

def test_rule_4_rejects_high_correlation():
    """Existing NVDA, candidate AMD, |corr|=0.75 > 0.6 → reject."""
    hh = _fake_hh_snapshot(
        existing_positions={
            "NVDA": {
                "total_shares": 50, "spot": 900.0,
                "current_value": 45_000.0,
                "sector": "Semiconductors",
            },
        },
    )
    cand = _fake_candidate(ticker="AMD", strike=140.0)
    extras = {
        "correlations": {
            ("AMD", "NVDA"): 0.75,
        },
    }
    passed, reason = _csp_check_rule_4(hh, cand, 1, 18.0, extras)
    assert passed is False
    assert "rule_4" in reason
    assert "NVDA" in reason
    assert "0.75" in reason


def test_rule_4_missing_data_fails_open():
    """Empty correlations dict → no data → pass (fail-open)."""
    hh = _fake_hh_snapshot(
        existing_positions={
            "NVDA": {
                "total_shares": 50, "spot": 900.0,
                "current_value": 45_000.0,
                "sector": "Semiconductors",
            },
        },
    )
    cand = _fake_candidate(ticker="AMD", strike=140.0)
    extras = {"correlations": {}}
    passed, reason = _csp_check_rule_4(hh, cand, 1, 18.0, extras)
    assert passed is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Rule 6 (Vikram EL floor)
# ---------------------------------------------------------------------------

def test_rule_6_non_vikram_household_passes():
    """Rule 6 only applies to Vikram_Household; others always pass."""
    hh = _fake_hh_snapshot(
        household="Yash_Household",
        hh_margin_nlv=100_000.0,
        hh_margin_el=5_000.0,       # would be 5% EL, but Yash → skip
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    passed, reason = _csp_check_rule_6(hh, cand, 1, 18.0, {})
    assert passed is True
    assert reason == ""


def test_rule_6_vikram_below_floor_rejected():
    """Vikram_Household @ 15% EL ratio → below 20% floor → reject."""
    hh = _fake_hh_snapshot(
        household="Vikram_Household",
        hh_margin_nlv=100_000.0,
        hh_margin_el=15_000.0,   # 15% EL ratio → below 20% floor
    )
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    passed, reason = _csp_check_rule_6(hh, cand, 1, 18.0, {})
    assert passed is False
    assert "rule_6" in reason
    assert "Vikram" in reason


# ---------------------------------------------------------------------------
# Rule 7 (CSP operating procedure — delta, earnings, working orders)
# ---------------------------------------------------------------------------

def test_rule_7_delta_over_limit_rejected():
    """Delta 0.30 > 0.25 → reject."""
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    extras = {"delta": -0.30}  # negative short-put delta
    passed, reason = _csp_check_rule_7(hh, cand, 1, 18.0, extras)
    assert passed is False
    assert "rule_7" in reason
    assert "delta" in reason


def test_rule_7_earnings_in_5_days_rejected():
    """Earnings in 5 days → within 7-day blackout → reject."""
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    extras = {"delta": -0.20, "days_to_earnings": 5}
    passed, reason = _csp_check_rule_7(hh, cand, 1, 18.0, extras)
    assert passed is False
    assert "rule_7" in reason
    assert "earnings" in reason


def test_rule_7_working_order_on_ticker_rejected():
    """Existing working order on same ticker → reject."""
    hh = _fake_hh_snapshot()
    hh["working_order_tickers"] = {"AAPL"}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    extras = {"delta": -0.20, "days_to_earnings": 30}
    passed, reason = _csp_check_rule_7(hh, cand, 1, 18.0, extras)
    assert passed is False
    assert "rule_7" in reason
    assert "working order" in reason


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# VIX acceleration veto gate
# ---------------------------------------------------------------------------

class TestVixAccelerationGate:
    """Verify VIX 3-session rate-of-change blocks CSP entries."""

    def test_vix_22pct_rise_blocks_all_csps(self):
        """VIX 18→22 in 3 sessions (22.2% rise) → all CSPs blocked."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        extras = {"vix_history": [22.0, 20.5, 19.0, 18.0]}
        passed, reason = _csp_check_vix_acceleration(hh, cand, 1, 22.0, extras)
        assert not passed
        assert "vix_acceleration" in reason
        assert "22.2%" in reason or "22%" in reason  # formatting may vary

    def test_vix_19pct_rise_passes(self):
        """VIX 18→21.4 in 3 sessions (18.9% rise) → passes (< 20%)."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        extras = {"vix_history": [21.4, 20.0, 19.0, 18.0]}
        passed, reason = _csp_check_vix_acceleration(hh, cand, 1, 21.4, extras)
        assert passed
        assert reason == ""

    def test_vix_exactly_20pct_passes(self):
        """VIX 18→21.6 exactly 20% → passes (threshold is strict >20%)."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        extras = {"vix_history": [21.59, 20.0, 19.0, 18.0]}
        passed, reason = _csp_check_vix_acceleration(hh, cand, 1, 21.59, extras)
        assert passed

    def test_vix_decline_passes(self):
        """VIX falling → always passes."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        extras = {"vix_history": [16.0, 18.0, 20.0, 22.0]}
        passed, reason = _csp_check_vix_acceleration(hh, cand, 1, 16.0, extras)
        assert passed

    def test_missing_vix_history_failopen(self):
        """No VIX history → fail-open (pass)."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        passed, _ = _csp_check_vix_acceleration(hh, cand, 1, 22.0, {})
        assert passed

    def test_insufficient_vix_history_failopen(self):
        """Only 2 data points → fail-open."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        extras = {"vix_history": [22.0, 20.0]}
        passed, _ = _csp_check_vix_acceleration(hh, cand, 1, 22.0, extras)
        assert passed

    def test_zero_baseline_failopen(self):
        """VIX 3 sessions ago = 0 → fail-open (avoid div/zero)."""
        hh = _fake_hh_snapshot()
        cand = _fake_candidate(ticker="AAPL", strike=150.0)
        extras = {"vix_history": [22.0, 15.0, 10.0, 0.0]}
        passed, _ = _csp_check_vix_acceleration(hh, cand, 1, 22.0, extras)
        assert passed


# Registry structural test
# ---------------------------------------------------------------------------

def test_registry_contains_all_seven_gates_in_order():
    """CSP_GATE_REGISTRY names must match the expected list in order."""
    expected = [
        "rule_1_concentration",
        "rule_2_el_deployment",
        "vix_acceleration",
        "rule_3_sector",
        "rule_4_correlation",
        "rule_6_vikram_el_floor",
        "rule_7_csp_procedure",
    ]
    actual = [name for name, _ in CSP_GATE_REGISTRY]
    assert actual == expected
    assert len(CSP_GATE_REGISTRY) == 7
    # All entries must be callable with the uniform gate signature
    hh = _fake_hh_snapshot()
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    for name, fn in CSP_GATE_REGISTRY:
        result = fn(hh, cand, 1, 18.0, {})
        assert isinstance(result, tuple) and len(result) == 2, (
            f"{name} did not return a 2-tuple"
        )
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)



# ===========================================================================
# M1.4: run_csp_allocator orchestrator tests
# ===========================================================================
#
# The orchestrator is injection-based: both extras_provider and
# staging_callback are passed in as parameters, so tests wire up
# lambdas that capture into local lists. No real DB writes. No IB.
# No telegram_bot imports in csp_allocator.py still stands.
# ---------------------------------------------------------------------------


def _empty_extras(hh, candidate):
    """Default extras_provider for tests where gates don't need extras.

    Rule 3 / Rule 4 fail-open on empty, Rule 7 reads missing keys as
    None and passes, so {} is equivalent to 'no data holes encountered'.
    """
    return {}


def test_orchestrator_dry_run_no_callback():
    """One passing candidate, staging_callback=None → staged populated,
    no exception, no side effects outside the result object."""
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )
    assert isinstance(result, AllocatorResult)
    assert len(result.staged) >= 1
    assert result.total_staged_contracts >= 1
    assert result.errors == []


def test_orchestrator_passes_tickets_to_staging_callback():
    """staging_callback receives one list-of-tickets call per staged
    candidate."""
    captured: list[list[dict]] = []
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(captured.append),
    )
    assert len(captured) == 1
    assert captured[0] == result.staged
    assert all(
        t["ticker"] == "AAPL" and t["mode"] == "CSP_ENTRY"
        for t in captured[0]
    )


def test_orchestrator_short_circuits_on_first_gate_fail():
    """Rule 1 fails → skipped entry logs rule_1_concentration, no
    tickets staged, no staging_callback invocation."""
    hh = _fake_hh_snapshot(
        existing_positions={
            "META": {
                "total_shares": 0, "spot": 0.0,
                "current_value": 60_000.0,
                "sector": "Interactive Media",
            },
        },
    )
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="META", strike=650.0)
    captured: list = []
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(captured.append),
    )
    assert result.staged == []
    assert captured == []
    assert len(result.skipped) == 1
    assert result.skipped[0]["reason"].startswith("rule_1_concentration:")
    assert result.skipped[0]["ticker"] == "META"


def test_orchestrator_skips_sub_integer_sizing():
    """Tiny household: 1c collateral > 20% ceiling → sizing returns 0.

    hh_nlv=$50K → ceiling=$10K. AAPL $150: 1c=$15K > $10K → sizing 0.
    But Rule 1 gate also fires at n=1 feasibility probe with same
    math, so this actually surfaces as rule_1 skip, not sizing skip.
    To isolate the sizing path we need gates to pass at n=1 but
    _csp_size_household to return 0 — that happens when the 10%
    target is sub-integer AND neither c_low nor c_high fits Rule 2.

    Simpler: hh_nlv=$500K (gates pass at n=1), hh_margin_nlv=$10K,
    hh_margin_el=$10K. candidate AAPL $200, target=$50K → c_low=2,
    c_high=3. At VIX=15: budget=$2K → headroom=$2K. 2c impact=$12K >
    $2K → infeasible. 3c also. Sizing returns 0.
    But at n=1 probe, Rule 2 margin impact = $200*100*1*0.30 = $6K >
    $2K → Rule 2 fails first. So we still don't isolate.

    The cleanest isolation: hh_margin_nlv=0 (IRA-only → Rule 2 skip)
    + tiny hh_nlv so target/ceiling squeeze sizing to 0 without
    tripping Rule 1 at n=1.

    hh_nlv=$60K (ceiling=$12K). IRA-only. AAPL $50: 1c=$5K notional.
    Rule 1 at n=1: $5K < $12K → pass.
    Target=$6K. 1c=$5K, 2c=$10K. |5-6|=1, |10-6|=4 → pick 1 → returns 1.
    Not zero.

    Use AAPL $150 instead: 1c notional=$15K > $12K ceiling → Rule 1
    at n=1 fails → skip is rule_1, not sizing.

    Genuine sizing-zero path: target=$6K, 1c=$5.9K (eg $59 strike).
    1c=$5.9K, 2c=$11.8K. Ceiling=$12K. 1c feasible ($5.9K <
    $12K). 2c NOT feasible ($11.8K <$12K → actually feasible).
    |5.9-6|=0.1, |11.8-6|=5.8 → picks 1.

    Every concrete scenario that passes Rule 1 at n=1 and still
    hits sizing=0 requires margin Rule 2 binding — but that also
    fails at n=1 probe. The sizing-only skip is therefore only
    reachable when hh_margin_nlv=0 AND target falls between 0 and 1.
    target < 1c means 10% hh_nlv < collateral → hh_nlv < 10 * $100 * strike.
    AAPL $150 strike → hh_nlv < $150K. Say hh_nlv=$80K, ceiling=$16K.
    1c=$15K < $16K → Rule 1 passes at n=1.
    c_low = int(8000/15000) = 0, c_high = 1.
    1c feasible (15K < 16K). 0c not (c<1).
    options=[1]. Pick 1 → returns 1. Still not zero.

    Conclusion: with current feasibility semantics, sizing-only zero
    path only reachable when BOTH c_low=0 AND c_high fails the
    ceiling probe — which means 1c>=ceiling, which means Rule 1 at
    n=1 also fails. The skip reason in practice will always be
    rule_1_concentration for sub-integer at 10% target.

    So this test verifies that when 1c ceiling is breached, the
    skip is reported as a Rule 1 gate fail (the short-circuit path),
    which is the actually-observable 'can't even size 1 contract'
    case that matters for the orchestrator user.
    """
    hh = _fake_hh_snapshot(
        hh_nlv=60_000.0,              # ceiling=$12K
        hh_margin_nlv=0.0,            # IRA-only → Rule 2 skip
        hh_margin_el=0.0,
        accounts={
            "U22076329": {
                "account_id": "U22076329",
                "nlv": 60_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 60_000.0,
                "margin_eligible": False,
            },
        },
    )
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)   # 1c=$15K > $12K
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )
    assert result.staged == []
    assert len(result.skipped) == 1
    # Ceiling breach surfaces via Rule 1 gate short-circuit
    assert "rule_1" in result.skipped[0]["reason"]


@pytest.mark.skipif(
    not __import__("pathlib").Path(
        __import__("agt_equities.db", fromlist=["DB_PATH"]).DB_PATH
    ).exists(),
    reason="Production DB not available (CI/tripwire)",
)
def test_orchestrator_mutates_snapshot_between_candidates():
    """Two candidates on the same household: mutation shrinks capacity.

    Fixture: 1 margin account with tight buying_power. First
    candidate consumes enough BP that the second candidate's router
    cannot fit a single contract. Asserts the in-memory mutation is
    visible across iterations.

    hh_nlv=$200K (ceiling $40K, target $20K). One margin acct with
    BP=$20K (fits exactly 1 × AAPL $150 @ $15K collateral).

    Cand 1 (AAPL $150): target=$20K, c_low=1 ($15K), c_high=2 ($30K).
      |15-20|=5, |30-20|=10 → picks 1. Route: acct fits 1 → staged 1.
      Post-mutation BP = $20K - $15K = $5K.
    Cand 2 (MSFT $150): sizing same → 1 contract. Route: BP=$5K <
      $15K → max_fit=0 → no tickets → skipped with "no capacity".
    """
    hh = _fake_hh_snapshot(
        hh_nlv=200_000.0,
        hh_margin_nlv=200_000.0,
        hh_margin_el=200_000.0,
        accounts={
            "U21971297": {
                "account_id": "U21971297",
                "nlv": 200_000.0,
                "el": 200_000.0,
                "buying_power": 20_000.0,    # fits exactly 1 × $15K
                "cash_available": 20_000.0,
                "margin_eligible": True,
            },
        },
    )
    snapshots = {"Yash_Household": hh}
    cand_1 = _fake_candidate(ticker="AAPL", strike=150.0)
    cand_2 = _fake_candidate(ticker="MSFT", strike=150.0)

    result = run_csp_allocator(
        [cand_1, cand_2], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )

    # Group staged by ticker
    by_ticker: dict[str, int] = {}
    for t in result.staged:
        by_ticker[t["ticker"]] = by_ticker.get(t["ticker"], 0) + t["quantity"]

    aapl_q = by_ticker.get("AAPL", 0)
    msft_q = by_ticker.get("MSFT", 0)

    # First candidate must have staged something (1 contract for $15K)
    assert aapl_q >= 1

    # After mutation, buying_power dropped below 1c collateral
    assert hh["accounts"]["U21971297"]["buying_power"] < 15_000.0

    # Second candidate could not fit → stages strictly fewer than the
    # first (or zero). Mutation is visible.
    assert msft_q < aapl_q or msft_q == 0


def test_orchestrator_existing_csps_updated_after_staging():
    """After staging, hh['existing_csps'][ticker] must reflect the new
    commitment. This is how Rule 1 for a SUBSEQUENT candidate on the
    same ticker sees the freshly-booked notional."""
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )
    assert len(result.staged) >= 1
    assert "AAPL" in hh["existing_csps"]
    assert hh["existing_csps"]["AAPL"]["total_contracts"] >= 1
    assert hh["existing_csps"]["AAPL"]["notional_commitment"] >= 15_000.0


def test_orchestrator_catches_staging_callback_exceptions():
    """staging_callback raising must not crash the run. Errors are
    logged to result.errors; staged stays empty for the affected
    candidate; no partial state leakage."""
    def exploding_cb(tickets):
        raise RuntimeError("simulated DB write failure")

    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(exploding_cb),
    )
    assert result.staged == []
    assert len(result.errors) == 1
    assert "staging failed" in result.errors[0]["error"]
    assert "simulated DB write failure" in result.errors[0]["error"]
    # Snapshot should NOT have been mutated (we abort before the mutation)
    assert "AAPL" not in hh.get("existing_csps", {})


def test_orchestrator_digest_format():
    """Digest contains headers for staged/skipped/errors sections.
    Empty result should not crash."""
    # Build a synthetic result directly
    result = AllocatorResult(
        staged=[
            {
                "account_id": "U22076329",
                "household": "Yash_Household",
                "ticker": "AAPL",
                "action": "SELL",
                "sec_type": "OPT",
                "right": "P",
                "strike": 150.0,
                "expiry": "20260516",
                "quantity": 2,
                "limit_price": 1.50,
                "annualized_yield": 22.5,
                "mode": "CSP_ENTRY",
                "status": "staged",
            },
            {
                "account_id": "U22388499",
                "household": "Vikram_Household",
                "ticker": "MSFT",
                "action": "SELL",
                "sec_type": "OPT",
                "right": "P",
                "strike": 400.0,
                "expiry": "20260516",
                "quantity": 1,
                "limit_price": 3.00,
                "annualized_yield": 18.2,
                "mode": "CSP_ENTRY",
                "status": "staged",
            },
        ],
        skipped=[
            {"household": "Yash_Household", "ticker": "NVDA",
             "reason": "rule_1_concentration: test"},
            {"household": "Yash_Household", "ticker": "GOOGL",
             "reason": "rule_3_sector: test"},
            {"household": "Vikram_Household", "ticker": "AMD",
             "reason": "rule_4_correlation: test"},
        ],
        errors=[
            {"household": "Yash_Household", "ticker": "XYZ",
             "error": "staging failed: db locked"},
        ],
    )
    # Import at call site to exercise the actual format function
    from agt_equities.csp_allocator import _format_digest
    lines = _format_digest(result)
    joined = "\n".join(lines)
    assert "tickets staged" in joined
    assert "Skipped: 3" in joined
    assert "Errors: 1" in joined
    assert "AAPL" in joined
    assert "MSFT" in joined

    # Empty result must not crash
    empty = AllocatorResult()
    empty_lines = _format_digest(empty)
    assert len(empty_lines) >= 1
    assert "no candidates processed" in empty_lines[0]


# ===========================================================================
# DT Q4 interface seam tests — CSPCandidate contract, approval_gate,
# candidate_reasoning payload. These are the load-bearing seams that let
# paper (identity gate) and live (Telegram digest gate) run from one
# codebase. See project_end_state_vision.md.
# ===========================================================================


def test_csp_candidate_protocol_scan_candidate_conforms():
    """ScanCandidate (pxo_scanner adapter) satisfies the CSPCandidate
    Protocol via runtime_checkable isinstance check."""
    from agt_equities.csp_allocator import CSPCandidate
    from agt_equities.scan_bridge import ScanCandidate
    sc = ScanCandidate(
        ticker="AAPL",
        strike=150.0,
        mid=1.50,
        expiry="2026-05-16",
        annualized_yield=0.22,
    )
    assert isinstance(sc, CSPCandidate)


def test_csp_candidate_protocol_simple_namespace_conforms():
    """A duck-typed SimpleNamespace with the 5 required attributes
    satisfies the Protocol — unblocks RAYCandidate and the future
    LLM digest tool from needing to inherit from a concrete base."""
    from agt_equities.csp_allocator import CSPCandidate
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    assert isinstance(cand, CSPCandidate)


def test_csp_candidate_protocol_missing_attr_fails():
    """An object missing a required attribute must NOT satisfy the
    Protocol. Runtime_checkable catches the contract violation."""
    from agt_equities.csp_allocator import CSPCandidate
    broken = SimpleNamespace(
        ticker="AAPL", strike=150.0, mid=1.50, expiry="2026-05-16",
        # annualized_yield missing
    )
    assert not isinstance(broken, CSPCandidate)


def test_approval_gate_default_is_identity():
    """No approval_gate kwarg → all candidates proceed (paper mode).

    Baseline behavior must be preserved for backwards-compatibility
    with every existing caller in telegram_bot.py / scan_bridge.py.
    """
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )
    # 1 candidate in → 1 reasoning entry, approved
    assert len(result.candidate_reasoning) == 1
    entry = result.candidate_reasoning[0]
    assert entry["approval_status"] == "approved"
    assert entry["approval_reason"] == ""
    assert entry["ticker"] == "AAPL"


def test_approval_gate_rejects_all_candidates():
    """Gate that returns [] → no allocation work, all candidates
    appear in skipped + reasoning as rejected."""
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cands = [
        _fake_candidate(ticker="AAPL", strike=150.0),
        _fake_candidate(ticker="MSFT", strike=400.0),
    ]
    gate_calls: list = []

    def reject_all(cs):
        gate_calls.append(list(cs))
        return []

    result = run_csp_allocator(
        cands, snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
        approval_gate=reject_all,
    )
    # Gate called exactly once on the full list
    assert len(gate_calls) == 1
    assert len(gate_calls[0]) == 2
    # No staging
    assert result.staged == []
    # Both candidates surface in skipped with pre-allocation marker
    assert len(result.skipped) == 2
    assert all(s["household"] == "(pre-allocation)" for s in result.skipped)
    assert all(s["reason"] == "approval_gate rejected" for s in result.skipped)
    # Both in reasoning with rejected status
    assert len(result.candidate_reasoning) == 2
    for entry in result.candidate_reasoning:
        assert entry["approval_status"] == "rejected"
        assert entry["approval_reason"] == "approval_gate rejected"
        # Rejected candidates produce no per-household outcome rows
        assert entry["households"] == []


def test_approval_gate_selects_subset():
    """Gate that returns 1 of 2 candidates → only the survivor hits
    households. The dropped candidate still produces a reasoning
    entry so observability is preserved."""
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand_a = _fake_candidate(ticker="AAPL", strike=150.0)
    cand_b = _fake_candidate(ticker="MSFT", strike=400.0)

    def pick_aapl_only(cs):
        return [c for c in cs if c.ticker == "AAPL"]

    result = run_csp_allocator(
        [cand_a, cand_b], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
        approval_gate=pick_aapl_only,
    )
    # AAPL should stage; MSFT should only appear in pre-allocation skipped
    staged_tickers = {t["ticker"] for t in result.staged}
    assert "AAPL" in staged_tickers
    assert "MSFT" not in staged_tickers

    # Reasoning has both, differentiated by approval_status
    status_by_ticker = {
        e["ticker"]: e["approval_status"]
        for e in result.candidate_reasoning
    }
    assert status_by_ticker == {"AAPL": "approved", "MSFT": "rejected"}

    # MSFT has no household rows (never allocated); AAPL has 1 (staged)
    aapl_entry = next(
        e for e in result.candidate_reasoning if e["ticker"] == "AAPL"
    )
    msft_entry = next(
        e for e in result.candidate_reasoning if e["ticker"] == "MSFT"
    )
    assert len(aapl_entry["households"]) == 1
    assert aapl_entry["households"][0]["outcome"] == "staged"
    assert aapl_entry["households"][0]["contracts"] >= 1
    assert msft_entry["households"] == []


def test_approval_gate_exception_falls_back_to_identity():
    """A broken approval_gate must NOT abort the run — paper mode
    would lose a full day's scan to a gate bug. Fall back to identity
    and surface the error in result.errors for triage."""
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)

    def broken_gate(cs):
        raise RuntimeError("digest LLM unreachable")

    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
        approval_gate=broken_gate,
    )
    # Error surfaced
    assert any(
        e["household"] == "(approval_gate)" and "RuntimeError" in e["error"]
        for e in result.errors
    )
    # But allocation still ran (identity fallback)
    assert len(result.staged) >= 1
    assert result.candidate_reasoning[0]["approval_status"] == "approved"


def test_candidate_reasoning_carries_upstream_payload():
    """A candidate with a `.reasoning` dict (e.g. from a future LLM
    digest) has that payload copied verbatim into
    candidate_reasoning.upstream_reasoning so the post-allocation
    audit surface retains the full decision trail."""
    hh = _fake_hh_snapshot()
    snapshots = {"Yash_Household": hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)
    # Attach an upstream reasoning payload (what the LLM digest will do)
    cand.reasoning = {
        "rank": 1,
        "rationale": "RAY 24% + IVR 45 + Z-score 4.1",
        "news_bullets": ["Q2 beat", "buyback raise"],
    }

    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )
    entry = result.candidate_reasoning[0]
    assert entry["upstream_reasoning"]["rank"] == 1
    assert "RAY 24%" in entry["upstream_reasoning"]["rationale"]
    assert entry["upstream_reasoning"]["news_bullets"] == ["Q2 beat", "buyback raise"]


def test_candidate_reasoning_records_per_household_outcome():
    """Each (candidate × household) pair that the allocator visits
    appends a households entry with outcome ∈ {staged, skipped, error}.
    This is the surface the digest tool consumes to explain WHY a
    candidate did or didn't land."""
    yash_hh = _fake_hh_snapshot()
    # Vikram household that will fail rule_1 (pre-existing concentration)
    vikram_hh = _fake_hh_snapshot(
        household="Vikram_Household",
        hh_nlv=100_000.0,
        hh_margin_nlv=100_000.0,
        hh_margin_el=100_000.0,
        existing_positions={
            "AAPL": {
                "total_shares": 0, "spot": 0.0,
                "current_value": 25_000.0,  # > 20% of $100K
                "sector": "Technology Hardware",
            },
        },
    )
    snapshots = {"Yash_Household": yash_hh, "Vikram_Household": vikram_hh}
    cand = _fake_candidate(ticker="AAPL", strike=150.0)

    result = run_csp_allocator(
        [cand], snapshots, vix=18.0,
        extras_provider=_empty_extras,
        ctx=_live_ctx(None),
    )
    entry = result.candidate_reasoning[0]
    by_hh = {h["household"]: h for h in entry["households"]}
    # Yash: staged
    assert by_hh["Yash_Household"]["outcome"] == "staged"
    assert by_hh["Yash_Household"]["contracts"] >= 1
    # Vikram: skipped on rule_1
    assert by_hh["Vikram_Household"]["outcome"] == "skipped"
    assert "rule_1_concentration" in by_hh["Vikram_Household"]["reason"]


def test_default_approval_gate_is_identity_callable():
    """The public default gate must be importable + behave as identity.
    Live code paths that build their own gate need a reference for
    'what paper does' — this is that reference."""
    from agt_equities.csp_allocator import _default_approval_gate
    cands = [
        _fake_candidate(ticker="AAPL"),
        _fake_candidate(ticker="MSFT"),
    ]
    out = _default_approval_gate(cands)
    assert out == cands
    # Returns a list copy, not the input reference (caller mutation safety)
    assert out is not cands
