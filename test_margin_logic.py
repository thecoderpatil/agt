"""
AGT Equities — Margin Logic Math Prover (Per-Account Isolation)
================================================================
Mocks IBKR accountSummary responses and asserts that:
  1. MARGIN_ACCOUNTS and ACCOUNT_NAMES are correctly defined
  2. _query_margin_stats returns per-account NLV, EL, and EL%
  3. Roth/Trad IRA are completely excluded from margin calculations
  4. Aggregates match the sum of individual accounts
  5. Zero-margin edge case returns zeros without crashing

Usage:
    python test_margin_logic.py
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# Mock IBKR AccountValue dataclass (matches ib_async.AccountValue shape)
# ---------------------------------------------------------------------------
@dataclass
class MockAccountValue:
    account: str
    tag: str
    value: str
    currency: str = "USD"
    modelCode: str = ""


# ---------------------------------------------------------------------------
# Test fixture: a household with margin + cash accounts
# ---------------------------------------------------------------------------
MOCK_SUMMARY = [
    # ── U21971297: Personal Brokerage (MARGIN) — $100k NLV, $25k EL ──
    MockAccountValue(account="U21971297", tag="NetLiquidation", value="100000.00"),
    MockAccountValue(account="U21971297", tag="ExcessLiquidity", value="25000.00"),
    MockAccountValue(account="U21971297", tag="GrossPositionValue", value="80000.00"),
    MockAccountValue(account="U21971297", tag="AccountType", value="INDIVIDUAL"),

    # ── U22076329: Roth IRA (CASH — must be EXCLUDED) — $50k NLV ──
    MockAccountValue(account="U22076329", tag="NetLiquidation", value="50000.00"),
    MockAccountValue(account="U22076329", tag="ExcessLiquidity", value="50000.00"),
    MockAccountValue(account="U22076329", tag="AccountType", value="IRA"),

    # ── U22076184: Traditional IRA (CASH — must be EXCLUDED) — $10k NLV ──
    MockAccountValue(account="U22076184", tag="NetLiquidation", value="10000.00"),
    MockAccountValue(account="U22076184", tag="ExcessLiquidity", value="10000.00"),
    MockAccountValue(account="U22076184", tag="AccountType", value="IRA"),

    # ── U22388499: Brother Brokerage (MARGIN) — $40k NLV, $15k EL ──
    MockAccountValue(account="U22388499", tag="NetLiquidation", value="40000.00"),
    MockAccountValue(account="U22388499", tag="ExcessLiquidity", value="15000.00"),
    MockAccountValue(account="U22388499", tag="GrossPositionValue", value="35000.00"),
    MockAccountValue(account="U22388499", tag="AccountType", value="INDIVIDUAL"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_constants():
    """Verify MARGIN_ACCOUNTS set and ACCOUNT_NAMES mapping."""
    from telegram_bot import MARGIN_ACCOUNTS, ACCOUNT_NAMES

    print()
    print("=" * 60)
    print("  TEST 1: Constants (MARGIN_ACCOUNTS + ACCOUNT_NAMES)")
    print("=" * 60)

    assert "U21971297" in MARGIN_ACCOUNTS
    assert "U22388499" in MARGIN_ACCOUNTS
    assert "U22076329" not in MARGIN_ACCOUNTS
    assert "U22076184" not in MARGIN_ACCOUNTS
    assert len(MARGIN_ACCOUNTS) == 2

    assert ACCOUNT_NAMES["U21971297"] == "Personal Brokerage"
    assert ACCOUNT_NAMES["U22388499"] == "Brother Brokerage"

    print(f"  MARGIN_ACCOUNTS = {MARGIN_ACCOUNTS}")
    print(f"  ACCOUNT_NAMES   = {ACCOUNT_NAMES}")
    print("  [PASS] Set membership correct")
    print("  [PASS] Human-readable names correct")


async def test_per_account_isolation():
    """
    The core test. _query_margin_stats must return per-account data:
      U21971297 (Personal):  nlv=$100k, el=$25k, el_pct=25.0%
      U22388499 (Brother):   nlv=$40k,  el=$15k, el_pct=37.5%

    Aggregates:
      agg_margin_nlv = $140k
      agg_margin_el  = $40k
      agg_el_pct     = 28.57%
      all_book_nlv   = $200k (includes Roth + Trad IRA)

    Roth ($50k NLV, $50k EL) and Trad IRA ($10k NLV, $10k EL) must
    NOT appear in any margin field.
    """
    mock_ib = AsyncMock()
    mock_ib.accountSummaryAsync = AsyncMock(return_value=MOCK_SUMMARY)

    with patch("telegram_bot.ensure_ib_connected", return_value=mock_ib):
        from telegram_bot import _query_margin_stats
        result = await _query_margin_stats()

    accts = result["accounts"]

    print()
    print("=" * 60)
    print("  TEST 2: Per-Account Margin Isolation")
    print("=" * 60)

    # ── Personal Brokerage ──
    personal = accts["U21971297"]
    print(f"  Personal Brokerage:")
    print(f"    nlv:    ${personal['nlv']:>12,.2f}  (expected: $100,000.00)")
    print(f"    el:     ${personal['el']:>12,.2f}  (expected:  $25,000.00)")
    print(f"    el_pct:  {personal['el_pct']:>11.2f}%  (expected:     25.00%)")

    assert personal["nlv"] == 100_000.00, f"got {personal['nlv']}"
    assert personal["el"] == 25_000.00, f"got {personal['el']}"
    assert personal["el_pct"] == 25.0, f"got {personal['el_pct']}"
    assert personal["name"] == "Personal Brokerage"
    print("  [PASS] Personal Brokerage: nlv, el, el_pct, name")

    # ── Brother Brokerage ──
    brother = accts["U22388499"]
    print(f"  Brother Brokerage:")
    print(f"    nlv:    ${brother['nlv']:>12,.2f}  (expected:  $40,000.00)")
    print(f"    el:     ${brother['el']:>12,.2f}  (expected:  $15,000.00)")
    print(f"    el_pct:  {brother['el_pct']:>11.2f}%  (expected:     37.50%)")

    assert brother["nlv"] == 40_000.00, f"got {brother['nlv']}"
    assert brother["el"] == 15_000.00, f"got {brother['el']}"
    assert brother["el_pct"] == 37.5, f"got {brother['el_pct']}"
    assert brother["name"] == "Brother Brokerage"
    print("  [PASS] Brother Brokerage: nlv, el, el_pct, name")

    # ── Aggregates ──
    print(f"  Aggregates:")
    print(f"    agg_margin_nlv: ${result['agg_margin_nlv']:>12,.2f}  (expected: $140,000.00)")
    print(f"    agg_margin_el:  ${result['agg_margin_el']:>12,.2f}  (expected:  $40,000.00)")
    print(f"    agg_el_pct:      {result['agg_el_pct']:>11.2f}%  (expected:     28.57%)")
    print(f"    all_book_nlv:   ${result['all_book_nlv']:>12,.2f}  (expected: $200,000.00)")

    assert result["agg_margin_nlv"] == 140_000.00
    assert result["agg_margin_el"] == 40_000.00
    assert result["agg_el_pct"] == 28.57
    assert result["all_book_nlv"] == 200_000.00
    assert result["error"] is None
    print("  [PASS] Aggregates correct (Roth $50k + Trad $10k EXCLUDED)")


async def test_zero_margin_edge_case():
    """If only cash accounts exist, all margin fields = 0, no crash."""
    mock_summary = [
        MockAccountValue(account="U22076329", tag="NetLiquidation", value="50000.00"),
        MockAccountValue(account="U22076329", tag="ExcessLiquidity", value="50000.00"),
    ]

    mock_ib = AsyncMock()
    mock_ib.accountSummaryAsync = AsyncMock(return_value=mock_summary)

    with patch("telegram_bot.ensure_ib_connected", return_value=mock_ib):
        from telegram_bot import _query_margin_stats
        result = await _query_margin_stats()

    accts = result["accounts"]

    print()
    print("=" * 60)
    print("  TEST 3: Zero Margin Edge Case")
    print("=" * 60)

    assert accts["U21971297"]["nlv"] == 0.0
    assert accts["U21971297"]["el"] == 0.0
    assert accts["U21971297"]["el_pct"] == 0.0
    assert accts["U22388499"]["nlv"] == 0.0
    assert accts["U22388499"]["el"] == 0.0
    assert accts["U22388499"]["el_pct"] == 0.0
    assert result["agg_margin_nlv"] == 0.0
    assert result["agg_margin_el"] == 0.0
    assert result["agg_el_pct"] == 0.0
    print("  [PASS] All per-account fields = $0")
    print("  [PASS] Aggregates = $0")
    print("  [PASS] No division-by-zero crash")


async def test_non_numeric_tags_ignored():
    """AccountType='INDIVIDUAL' must not crash float() parsing."""
    mock_summary = [
        MockAccountValue(account="U21971297", tag="AccountType", value="INDIVIDUAL"),
        MockAccountValue(account="U21971297", tag="NetLiquidation", value="100000.00"),
        MockAccountValue(account="U21971297", tag="ExcessLiquidity", value="25000.00"),
    ]

    mock_ib = AsyncMock()
    mock_ib.accountSummaryAsync = AsyncMock(return_value=mock_summary)

    with patch("telegram_bot.ensure_ib_connected", return_value=mock_ib):
        from telegram_bot import _query_margin_stats
        result = await _query_margin_stats()

    print()
    print("=" * 60)
    print("  TEST 4: Non-Numeric Tags (AccountType='INDIVIDUAL')")
    print("=" * 60)

    assert result["accounts"]["U21971297"]["nlv"] == 100_000.00
    assert result["error"] is None
    print("  [PASS] AccountType tag skipped without crash")
    print("  [PASS] Numeric tags parsed correctly")


async def main():
    print()
    print("*" * 60)
    print("  AGT EQUITIES — MARGIN LOGIC MATH PROVER v2")
    print("  (Per-Account Isolation)")
    print("*" * 60)

    await test_constants()
    await test_per_account_isolation()
    await test_zero_margin_edge_case()
    await test_non_numeric_tags_ignored()

    print()
    print("=" * 60)
    print("  ALL TESTS PASSED")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
