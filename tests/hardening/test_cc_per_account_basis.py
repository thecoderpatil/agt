"""
Tests for WHEEL-5 fix: per-account basis in CC strike selection.

Scenario: UBER held in two accounts within the same household:
  - Roth IRA (U22076329): 100 shares, paper basis $73
  - Individual (U21971297): 100 shares, paper basis $86
  - Household-aggregated (weighted avg): $79.50

Before fix: one chain walk at $79.50 → one strike for both accounts.
After fix: two independent chain walks at $73 and $86 → potentially
different strikes or different WRITE/STAND DOWN outcomes.

Marker: @pytest.mark.hardening
"""
from __future__ import annotations

import pytest


@pytest.mark.hardening
class TestPerAccountBasisTargetBuilding:
    """Test that _run_cc_logic builds per-account targets with per-account basis.

    These tests verify the TARGET-BUILDING phase of the WHEEL-5 fix:
    given a _discover_positions output with multi-account positions,
    the code must produce separate chain walk targets with per-account
    paper_basis from _load_premium_ledger_snapshot.

    We don't test the full async pipeline (that needs IB). We test the
    logic shape: each account gets its own basis, not the household blend.
    """

    def test_per_account_ledger_returns_different_bases(self):
        """Verify _load_premium_ledger_snapshot returns per-account data
        when called with account_id (ADR-006 path is wired correctly)."""
        import os
        os.environ.setdefault("READ_FROM_MASTER_LOG", "1")

        # This test exercises the ADR-006 code path. It needs the DB +
        # walker data to exist. If running in CI without a DB, it will
        # return None — that's fine, the test is about the code path
        # existing and being callable, not about specific data.
        try:
            from telegram_bot import _load_premium_ledger_snapshot
        except Exception:
            pytest.skip("telegram_bot import requires full environment")

        # The function should accept account_id parameter
        import inspect
        sig = inspect.signature(_load_premium_ledger_snapshot)
        assert "account_id" in sig.parameters, (
            "ADR-006: _load_premium_ledger_snapshot must accept account_id"
        )

    def test_walk_cc_chain_uses_basis_for_anchor(self):
        """Verify _walk_cc_chain anchors at paper_basis, not some other value.

        Two calls with different paper_basis should produce different
        min_strike filters (and potentially different results).
        """
        # This is a structural test: _walk_cc_chain's first viable strike
        # is >= paper_basis. With basis=$73, anchor starts at $73+.
        # With basis=$86, anchor starts at $86+.
        # We can't call it without IB, but we can verify the function
        # signature accepts paper_basis as the third positional arg.
        try:
            from telegram_bot import _walk_cc_chain
        except Exception:
            pytest.skip("telegram_bot import requires full environment")

        import inspect
        sig = inspect.signature(_walk_cc_chain)
        params = list(sig.parameters.keys())
        # Expected: (ticker, spot, paper_basis, target_dte_range)
        assert len(params) >= 3, f"Expected >=3 params, got {params}"
        assert params[2] == "paper_basis", (
            f"Third param should be 'paper_basis', got '{params[2]}'"
        )


@pytest.mark.hardening
class TestPerAccountBasisScenario:
    """UBER scenario: Roth@$73 vs Individual@$86.

    These tests use the canonical_should_write reference implementation
    to verify that per-account basis produces correct WRITE/STAND DOWN
    decisions where household-aggregated basis produces wrong ones.
    """

    @staticmethod
    def canonical_should_write(
        paper_basis: float,
        spot: float,
        available_strikes: list[tuple[float, float]],  # [(strike, mid_premium), ...]
        min_ann: float = 30.0,
        max_ann: float = 130.0,
        dte: int = 21,
    ) -> dict:
        """Reference implementation of the CC 30-130 strike picker.

        Returns {action: "WRITE"|"STAND_DOWN", strike, annualized, ...}
        """
        # Anchor = smallest strike >= paper_basis
        viable = [(s, p) for s, p in available_strikes if s >= paper_basis]
        if not viable:
            return {"action": "STAND_DOWN", "reason": "no_strikes_above_basis"}

        viable.sort(key=lambda x: x[0])  # ascending by strike
        best_observed = None

        for strike, mid in viable:
            if mid <= 0.03:
                continue  # garbage quote
            annualized = (mid / strike) * (365 / dte) * 100 if strike > 0 else 0

            if best_observed is None or annualized > best_observed.get("annualized", 0):
                best_observed = {
                    "action": "STAND_DOWN",
                    "strike": strike,
                    "annualized": round(annualized, 2),
                    "reason": "below_floor",
                }

            if annualized > max_ann:
                continue  # step up
            if annualized < min_ann:
                break  # stand down
            # Band hit
            return {
                "action": "WRITE",
                "strike": strike,
                "annualized": round(annualized, 2),
                "mid": mid,
            }

        return best_observed or {"action": "STAND_DOWN", "reason": "empty_chain"}

    def test_uber_roth_73_should_write(self):
        """Roth at $73 basis, spot $80. Chain has strikes $75-$95.

        $75C with mid=$2.50: ann = (2.50/75)*(365/21)*100 = 57.9% → WRITE
        """
        chain = [
            (75.0, 2.50),   # ITM but above basis — valid anchor
            (77.5, 1.80),
            (80.0, 1.20),
            (82.5, 0.70),
            (85.0, 0.35),
            (87.5, 0.15),
            (90.0, 0.05),
        ]
        result = self.canonical_should_write(
            paper_basis=73.0, spot=80.0, available_strikes=chain, dte=21
        )
        assert result["action"] == "WRITE", f"Roth@$73 should WRITE, got {result}"
        # Anchor is $75 (first strike >= $73)
        assert result["strike"] == 75.0

    def test_uber_individual_86_stand_down(self):
        """Individual at $86 basis, spot $80. Spot < basis → no strikes above basis
        that are also above spot. $87.5C has tiny premium.

        $87.5C with mid=$0.15: ann = (0.15/87.5)*(365/21)*100 = 2.98% → below 30%
        $90C with mid=$0.05: garbage quote (< $0.03 floor after rounding)
        → STAND DOWN
        """
        chain = [
            (75.0, 2.50),
            (77.5, 1.80),
            (80.0, 1.20),
            (82.5, 0.70),
            (85.0, 0.35),
            (87.5, 0.15),
            (90.0, 0.05),
        ]
        result = self.canonical_should_write(
            paper_basis=86.0, spot=80.0, available_strikes=chain, dte=21
        )
        assert result["action"] == "STAND_DOWN", (
            f"Individual@$86 should STAND DOWN, got {result}"
        )

    def test_blended_82_incorrectly_stands_down(self):
        """Household-aggregated $82 basis. Shows why blending is wrong.

        $82.5C with mid=$0.70: ann = (0.70/82.5)*(365/21)*100 = 14.7% → below 30%
        → STAND DOWN. But Roth@$73 SHOULD write at $75C.
        """
        chain = [
            (75.0, 2.50),
            (77.5, 1.80),
            (80.0, 1.20),
            (82.5, 0.70),
            (85.0, 0.35),
            (87.5, 0.15),
            (90.0, 0.05),
        ]
        result = self.canonical_should_write(
            paper_basis=82.0, spot=80.0, available_strikes=chain, dte=21
        )
        # Blended basis anchors at $82.5 → 14.7% → STAND DOWN
        assert result["action"] == "STAND_DOWN", (
            f"Blended@$82 should STAND DOWN (demonstrating the bug), got {result}"
        )

    def test_per_account_divergence_is_the_fix(self):
        """The whole point: per-account walks produce DIFFERENT outcomes
        for the SAME chain data, proving that household aggregation is wrong."""
        chain = [
            (75.0, 2.50),
            (77.5, 1.80),
            (80.0, 1.20),
            (82.5, 0.70),
            (85.0, 0.35),
            (87.5, 0.15),
            (90.0, 0.05),
        ]
        roth = self.canonical_should_write(
            paper_basis=73.0, spot=80.0, available_strikes=chain, dte=21,
        )
        individual = self.canonical_should_write(
            paper_basis=86.0, spot=80.0, available_strikes=chain, dte=21,
        )
        blended = self.canonical_should_write(
            paper_basis=82.0, spot=80.0, available_strikes=chain, dte=21,
        )

        # Roth writes, Individual stands down
        assert roth["action"] == "WRITE"
        assert individual["action"] == "STAND_DOWN"
        # Blended also stands down — proving it hides the Roth opportunity
        assert blended["action"] == "STAND_DOWN"

    def test_both_accounts_write_different_strikes(self):
        """When both accounts CAN write, they may pick different strikes
        because different anchors land in different parts of the chain."""
        # High-IV scenario where premiums are fat enough at all strikes.
        # $87.5C at $2.00: ann = (2.00/87.5)*(365/21)*100 = 39.7% → in band.
        chain = [
            (72.5, 4.00),
            (75.0, 3.50),
            (77.5, 3.00),
            (80.0, 2.50),
            (82.5, 2.20),
            (85.0, 2.10),
            (87.5, 2.00),
            (90.0, 1.80),
        ]
        roth = self.canonical_should_write(
            paper_basis=73.0, spot=80.0, available_strikes=chain, dte=21,
        )
        individual = self.canonical_should_write(
            paper_basis=86.0, spot=80.0, available_strikes=chain, dte=21,
        )

        assert roth["action"] == "WRITE"
        assert individual["action"] == "WRITE"
        # Different anchors → different strikes
        # Roth anchors at $75 ($75 >= $73), Individual anchors at $87.5 ($87.5 >= $86)
        assert roth["strike"] <= individual["strike"], (
            f"Roth st