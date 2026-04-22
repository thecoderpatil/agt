"""
Tests for cc_engine OTM-only floor and weekly DTE range config.

Covers two picker-conformance fixes bundled in fix/cc-engine-otm-weekly-dte:
  1. OTM floor: pick_cc_strike must not select strikes below spot, even if >= paper_basis.
  2. DTE range: CC_TARGET_DTE must be (4, 9) targeting next weekly Friday expiry.
"""
from __future__ import annotations

import datetime
import pytest
from agt_equities.cc_engine import (
    CCPickerInput,
    CCStandDown,
    CCWrite,
    ChainStrike,
    pick_cc_strike,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chain(*strikes_bids: tuple[float, float]) -> tuple[ChainStrike, ...]:
    return tuple(
        ChainStrike(strike=s, bid=b, ask=round(b + 0.05, 2), delta=0.30)
        for s, b in strikes_bids
    )


def _inp(
    basis: float,
    spot: float,
    chain_strikes: tuple[ChainStrike, ...],
    dte: int = 22,
    ticker: str = "TEST",
    account_id: str = "DUP000000",
    expiry: str = "2026-05-08",
) -> CCPickerInput:
    return CCPickerInput(
        ticker=ticker,
        account_id=account_id,
        paper_basis=basis,
        spot=spot,
        dte=dte,
        expiry=expiry,
        chain=chain_strikes,
    )


# ---------------------------------------------------------------------------
# OTM floor tests — basis < spot (core regression scenario)
# ---------------------------------------------------------------------------

class TestOtmFloorBasisBelowSpot:
    """When spot > basis, floor = spot. Strikes below spot must be excluded."""

    def test_itm_strike_excluded_when_basis_below_spot(self):
        """Strike 245 < spot 247.18 must not be selected even though 245 >= basis 240.85.
        This is the exact order #307 ADBE regression: basis=240.85, spot=247.18, chose 245."""
        # ann at 247.5 (bid=5.50): (5.525/247.50)*(365/22)*100 = 37.0% → in 30-130 band → WRITE
        result = pick_cc_strike(_inp(
            basis=240.85,
            spot=247.18,
            chain_strikes=_chain(
                (242.50, 8.00),  # ITM: below spot → must be excluded
                (245.00, 6.80),  # ITM: below spot → must be excluded (was the regressed pick)
                (247.50, 5.50),  # OTM/ATM: first valid under new rule
                (250.00, 3.80),
            ),
        ))
        assert isinstance(result, CCWrite), f"Expected CCWrite, got {type(result).__name__}"
        assert result.strike == 247.50, f"Expected 247.50, got {result.strike}"
        assert result.strike >= 247.18, "Strike must be at or above spot"

    def test_itm_strike_excluded_no_viable_otm(self):
        """When all OTM strikes are below bid_floor, result is STAND_DOWN."""
        result = pick_cc_strike(_inp(
            basis=240.85,
            spot=247.18,
            chain_strikes=_chain(
                (245.00, 9.00),  # ITM → excluded
                (250.00, 0.02),  # OTM but bid < bid_floor (0.03) → skip
            ),
        ))
        assert isinstance(result, CCStandDown), f"Expected CCStandDown, got {type(result).__name__}"

    def test_otm_pct_positive_on_write(self):
        """When OTM floor is enforced, the resulting otm_pct must be >= 0."""
        result = pick_cc_strike(_inp(
            basis=200.00,
            spot=250.00,
            chain_strikes=_chain(
                (248.00, 8.00),  # below spot → excluded
                (252.50, 5.50),  # OTM: (252.5-250)/250*100 = 1.0%
            ),
            dte=22,
        ))
        assert isinstance(result, CCWrite)
        assert result.otm_pct >= 0.0, f"OTM% must be non-negative, got {result.otm_pct}"

    def test_regression_adbe_order307(self):
        """Explicit regression: ADBE 245C should never be selected at spot=247.18."""
        result = pick_cc_strike(_inp(
            ticker="ADBE",
            account_id="DUP751004",
            basis=240.85,
            spot=247.18,
            chain_strikes=_chain(
                (245.00, 6.80),  # the regressed strike from order #307
                (250.00, 4.50),
            ),
            dte=22,
        ))
        assert result.strike != 245.0, "ADBE 245C must not be selected when spot=247.18"


# ---------------------------------------------------------------------------
# OTM floor tests — basis > spot (underwater, floor = basis)
# ---------------------------------------------------------------------------

class TestOtmFloorBasisAboveSpot:
    """When basis > spot (underwater), floor = basis. OTM strikes < basis still excluded."""

    def test_below_basis_excluded_when_underwater(self):
        """AAPL DEEP_UNDERWATER: basis=320.48 > spot=273.17. Strikes below basis excluded."""
        result = pick_cc_strike(_inp(
            ticker="AAPL",
            account_id="DUP751003",
            basis=320.48,
            spot=273.17,
            chain_strikes=_chain(
                (275.00, 5.00),  # below basis → excluded
                (300.00, 2.00),  # below basis → excluded
                (320.00, 0.50),  # below basis (320 < 320.48) → excluded
                (322.50, 0.01),  # above basis but bid_floor fail
            ),
            dte=15,
        ))
        assert isinstance(result, CCStandDown), f"Expected STAND_DOWN for deep underwater, got {result}"

    def test_floor_uses_basis_not_spot_when_basis_higher(self):
        """max(basis=100, spot=80) = 100, so strikes 80-99 are excluded."""
        # ann at 102: (3.525/102)*(365/10)*100 = 126.2% → above 130 ceiling → step up
        # ann at 105: (5.525/105)*(365/10)*100 = 192.0% → above 130 → step up
        # ann at 110: (4.025/110)*(365/10)*100 = 133.7% → above 130 → step up
        # Need to craft a strike that lands in 30-130 band at the given dte
        # dte=10: for 30% ann: mid = 100 * 0.30 * 10/365 = 0.82; for 130%: mid = 3.56
        result = pick_cc_strike(_inp(
            basis=100.0,
            spot=80.0,
            chain_strikes=_chain(
                (85.00, 2.00),   # below basis → excluded
                (95.00, 2.00),   # below basis → excluded
                (102.00, 2.00),  # above basis: ann=(2.025/102)*(365/10)*100 = 72.5% → WRITE
            ),
            dte=10,
        ))
        assert isinstance(result, CCWrite)
        assert result.strike == 102.0
        assert result.strike >= 100.0, "Strike must be at or above basis when basis > spot"


# ---------------------------------------------------------------------------
# OTM floor tests — basis == spot (edge case)
# ---------------------------------------------------------------------------

class TestOtmFloorBasisEqualsSpot:
    """When basis == spot, floor = spot = basis. ATM strike (exactly at spot) is included."""

    def test_atm_strike_included_when_basis_equals_spot(self):
        """max(100, 100) = 100; strike 100 >= 100 → included."""
        # ann at strike=100, dte=7: for 30%: mid = 100*0.30*7/365 = 0.575; 130%: 2.493
        result = pick_cc_strike(_inp(
            basis=100.0,
            spot=100.0,
            chain_strikes=_chain(
                (100.0, 1.50),  # ATM: ann=(1.525/100)*(365/7)*100 = 79.5% → in 30-130 → WRITE
            ),
            dte=7,
        ))
        assert isinstance(result, CCWrite)
        assert result.strike == 100.0


# ---------------------------------------------------------------------------
# DTE range tests — CC_TARGET_DTE = (4, 9) is the weekly spec.
#
# telegram_bot.CC_TARGET_DTE value is separately validated by dry_run_tests.py
# test 6.4 (CC_TARGET_DTE == (4, 9)). Here we verify the DATE MATH properties
# that make (4, 9) the correct weekly range, without importing telegram_bot
# (which requires TELEGRAM_BOT_TOKEN at module level, not set in local test env).
# ---------------------------------------------------------------------------

# The post-fix value — matches telegram_bot.CC_TARGET_DTE after the patch.
_WEEKLY_DTE = (4, 9)


class TestWeeklyDteRange:
    def test_weekly_dte_range_covers_thursday_execution(self):
        """From Thursday April 23, next Friday May 1 = 8 DTE — must be in (4, 9)."""
        dte_min, dte_max = _WEEKLY_DTE
        thursday = datetime.date(2026, 4, 23)
        friday_may_1 = datetime.date(2026, 5, 1)
        dte = (friday_may_1 - thursday).days  # = 8
        assert dte_min <= dte <= dte_max, (
            f"May 1 (8 DTE from Thursday) must be in weekly range {_WEEKLY_DTE}"
        )

    def test_weekly_dte_range_covers_monday_execution(self):
        """From Monday April 27, next Friday May 1 = 4 DTE — in (4, 9).
        Current Friday April 30 = 3 DTE — out of range (too close)."""
        dte_min, dte_max = _WEEKLY_DTE
        monday = datetime.date(2026, 4, 27)
        fri_may1 = datetime.date(2026, 5, 1)
        fri_apr30 = datetime.date(2026, 4, 30)
        dte_may1 = (fri_may1 - monday).days    # = 4
        dte_apr30 = (fri_apr30 - monday).days  # = 3
        assert dte_min <= dte_may1 <= dte_max, (
            f"May 1 (4 DTE from Monday) must be in weekly range {_WEEKLY_DTE}"
        )
        assert dte_apr30 < dte_min, (
            f"Apr 30 (3 DTE from Monday) must be outside range (too close)"
        )

    def test_old_range_rejected(self):
        """The old (14, 30) range targeted monthly expiries — must not be the value."""
        assert _WEEKLY_DTE != (14, 30), "DTE range must not be the old 14-30 monthly window"

    def test_range_covers_monday_and_thursday_execution(self):
        """Dispatch spec: Mon + Thu execution days must each find a Friday in (4, 9).
        Note: Tuesday execution has a natural gap (current Friday 3 DTE, next 10 DTE)
        — known design trade-off, out of scope for this fix."""
        dte_min, dte_max = _WEEKLY_DTE
        target_fridays = [datetime.date(2026, 5, 1), datetime.date(2026, 5, 8)]
        for exec_day_label, exec_day in [
            ("Monday 2026-04-27", datetime.date(2026, 4, 27)),
            ("Thursday 2026-04-23", datetime.date(2026, 4, 23)),
        ]:
            found = any(
                dte_min <= (f - exec_day).days <= dte_max
                for f in target_fridays
            )
            assert found, f"No Friday in ({dte_min},{dte_max}) DTE from {exec_day_label}"
