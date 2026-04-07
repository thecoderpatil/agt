"""
tests/test_phase3a5a.py — Phase 3A.5a tests.

Covers: R4 correlation evaluator, R5 sell gate, R6 refinement,
data provider scaffold, state_builder correlation math.
All tests use FakeProvider — zero IBKR dependency.
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import (
    PortfolioState, RuleEvaluation, CorrelationData, AccountELSnapshot,
    SellException, SellGateResult, CORRELATION_EXCLUDED_TICKERS,
    RULE_10_EXCLUDED_FROM_SECTOR,
    evaluate_rule_3, evaluate_rule_4, evaluate_rule_5, evaluate_rule_5_sell_gate,
    evaluate_rule_6, evaluate_all,
)
from agt_equities.data_provider import (
    MarketDataProvider, Bar, AccountSummary, DataProviderError,
    get_provider, set_provider, reset_provider,
)
from agt_equities.state_builder import (
    compute_pearson_correlation, build_correlation_matrix,
)
from tests.fixtures.fake_provider import (
    FakeProvider, make_bars, make_correlated_bars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_cycle(household='Yash_Household', ticker='TEST', shares=100,
                basis=100.0, status='ACTIVE'):
    c = MagicMock()
    c.household_id = household
    c.ticker = ticker
    c.shares_held = shares
    c.paper_basis = basis
    c.status = status
    return c


def _make_ps(**overrides) -> PortfolioState:
    defaults = dict(
        household_nlv={'Yash_Household': 200000, 'Vikram_Household': 80000},
        household_el={'Yash_Household': None, 'Vikram_Household': None},
        active_cycles=[],
        spots={},
        betas={},
        industries={},
        sector_overrides={},
        vix=None,
        report_date='20260407',
        correlations={},
        account_el={},
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
# Rule 4: Pairwise Correlation
# ═══════════════════════════════════════════════════════════════════════════

class TestRule4Correlation(unittest.TestCase):

    def test_correlation_below_threshold_green(self):
        corrs = {("ADBE", "QCOM"): CorrelationData(0.40, 180, False, "test")}
        ps = _make_ps(
            active_cycles=[_mock_cycle(ticker='ADBE'), _mock_cycle(ticker='QCOM')],
            correlations=corrs,
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        statuses = [r.status for r in results]
        self.assertTrue(all(s == "GREEN" for s in statuses))

    def test_correlation_above_threshold_red(self):
        corrs = {("ADBE", "CRM"): CorrelationData(0.75, 180, False, "test")}
        ps = _make_ps(
            active_cycles=[_mock_cycle(ticker='ADBE'), _mock_cycle(ticker='CRM')],
            correlations=corrs,
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        pair_result = [r for r in results if r.raw_value is not None][0]
        self.assertEqual(pair_result.status, "RED")
        self.assertAlmostEqual(pair_result.raw_value, 0.75, places=2)

    def test_correlation_in_warning_band_amber(self):
        corrs = {("ADBE", "CRM"): CorrelationData(0.58, 180, False, "test")}
        ps = _make_ps(
            active_cycles=[_mock_cycle(ticker='ADBE'), _mock_cycle(ticker='CRM')],
            correlations=corrs,
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        pair_result = [r for r in results if r.raw_value is not None][0]
        self.assertEqual(pair_result.status, "AMBER")

    def test_short_history_low_confidence_flag(self):
        corrs = {("ADBE", "CRM"): CorrelationData(0.45, 90, True, "test")}
        ps = _make_ps(
            active_cycles=[_mock_cycle(ticker='ADBE'), _mock_cycle(ticker='CRM')],
            correlations=corrs,
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        pair_result = [r for r in results if r.raw_value is not None][0]
        self.assertTrue(pair_result.detail["low_confidence"])
        self.assertIn("LOW CONFIDENCE", pair_result.message)

    def test_provider_failure_amber(self):
        """Missing correlation data for a pair → AMBER overall."""
        ps = _make_ps(
            active_cycles=[_mock_cycle(ticker='ADBE'), _mock_cycle(ticker='CRM')],
            correlations={},  # no data
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        statuses = [r.status for r in results]
        self.assertIn("AMBER", statuses)

    def test_excludes_rule_10_instruments(self):
        """SPX, SLS, GTLB should be excluded from correlation pairs."""
        corrs = {("ADBE", "CRM"): CorrelationData(0.40, 180, False, "test")}
        ps = _make_ps(
            active_cycles=[
                _mock_cycle(ticker='ADBE'),
                _mock_cycle(ticker='CRM'),
                _mock_cycle(ticker='SLS'),   # excluded
                _mock_cycle(ticker='GTLB'),  # excluded
            ],
            correlations=corrs,
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        # SLS and GTLB should not appear in any pair detail
        all_tickers = set()
        for r in results:
            if r.detail.get("ticker_a"):
                all_tickers.add(r.detail["ticker_a"])
                all_tickers.add(r.detail["ticker_b"])
        self.assertNotIn("SLS", all_tickers)
        self.assertNotIn("GTLB", all_tickers)

    def test_single_position_vacuous_green(self):
        ps = _make_ps(active_cycles=[_mock_cycle(ticker='ADBE')])
        results = evaluate_rule_4(ps, 'Yash_Household')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "GREEN")
        self.assertIn("vacuously", results[0].message)

    def test_zero_positions_vacuous_green(self):
        ps = _make_ps(active_cycles=[])
        results = evaluate_rule_4(ps, 'Yash_Household')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "GREEN")

    def test_pearson_math_against_known_vector(self):
        """Hand-computed: perfectly correlated returns → correlation = 1.0."""
        bars_a = [
            Bar(date(2025, 10, 1), 100.0),
            Bar(date(2025, 10, 2), 102.0),
            Bar(date(2025, 10, 3), 101.0),
            Bar(date(2025, 10, 4), 103.0),
        ]
        bars_b = [
            Bar(date(2025, 10, 1), 50.0),
            Bar(date(2025, 10, 2), 51.0),
            Bar(date(2025, 10, 3), 50.5),
            Bar(date(2025, 10, 4), 51.5),
        ]
        corr, sample = compute_pearson_correlation(bars_a, bars_b)
        self.assertEqual(sample, 3)
        self.assertAlmostEqual(corr, 1.0, places=4)

    def test_three_pairs_evaluated(self):
        """ADBE, CRM, QCOM → 3 pairs."""
        corrs = {
            ("ADBE", "CRM"): CorrelationData(0.70, 180, False, "test"),
            ("ADBE", "QCOM"): CorrelationData(0.45, 180, False, "test"),
            ("CRM", "QCOM"): CorrelationData(0.50, 180, False, "test"),
        }
        ps = _make_ps(
            active_cycles=[
                _mock_cycle(ticker='ADBE'),
                _mock_cycle(ticker='CRM'),
                _mock_cycle(ticker='QCOM'),
            ],
            correlations=corrs,
        )
        results = evaluate_rule_4(ps, 'Yash_Household')
        valued = [r for r in results if r.raw_value is not None]
        self.assertEqual(len(valued), 3)
        # ADBE-CRM should be RED (0.70 > 0.60)
        adbe_crm = [r for r in valued if "ADBE-CRM" in r.message][0]
        self.assertEqual(adbe_crm.status, "RED")


# ═══════════════════════════════════════════════════════════════════════════
# Rule 5: Capital Velocity / Sell Gate
# ═══════════════════════════════════════════════════════════════════════════

class TestRule5SellGate(unittest.TestCase):

    def test_sell_above_basis_no_exception_allowed(self):
        result = evaluate_rule_5_sell_gate("ADBE", "Yash", 450.0, 445.0)
        self.assertEqual(result.status, "ALLOWED")

    def test_sell_below_basis_no_exception_blocked(self):
        result = evaluate_rule_5_sell_gate("ADBE", "Yash", 360.0, 445.0)
        self.assertEqual(result.status, "BLOCKED")

    def test_sell_below_basis_rule_8_with_token_allowed(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 360.0, 445.0,
            exception_flag=SellException.RULE_8_DYNAMIC_EXIT,
            rule_8_gate_pass=True,
        )
        self.assertEqual(result.status, "ALLOWED")

    def test_sell_below_basis_rule_8_no_token_blocked(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 360.0, 445.0,
            exception_flag=SellException.RULE_8_DYNAMIC_EXIT,
            rule_8_gate_pass=False,
        )
        self.assertEqual(result.status, "BLOCKED")

    def test_sell_below_basis_thesis_deterioration_requires_cio_token(self):
        # Missing CIO token
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 360.0, 445.0,
            exception_flag=SellException.THESIS_DETERIORATION,
            cio_token=False, logged_rationale="structural decline",
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("CIO consultation token", result.required_evidence)

    def test_sell_below_basis_thesis_with_all_evidence_allowed(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 360.0, 445.0,
            exception_flag=SellException.THESIS_DETERIORATION,
            cio_token=True, logged_rationale="structural decline in revenue",
        )
        self.assertEqual(result.status, "ALLOWED")

    def test_sell_below_basis_rule_6_requires_el_below_10pct(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Vikram", 360.0, 445.0,
            exception_flag=SellException.RULE_6_FORCED_LIQUIDATION,
            vikram_el_below_10=False,
        )
        self.assertEqual(result.status, "BLOCKED")

    def test_sell_below_basis_rule_6_with_el_below_10_allowed(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Vikram", 360.0, 445.0,
            exception_flag=SellException.RULE_6_FORCED_LIQUIDATION,
            vikram_el_below_10=True,
        )
        self.assertEqual(result.status, "ALLOWED")

    def test_sell_below_basis_emergency_with_rationale_allowed(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 360.0, 445.0,
            exception_flag=SellException.EMERGENCY_RISK_EVENT,
            logged_rationale="confirmed fraud disclosure",
        )
        self.assertEqual(result.status, "ALLOWED")

    def test_sell_below_basis_emergency_no_rationale_blocked(self):
        result = evaluate_rule_5_sell_gate(
            "ADBE", "Yash", 360.0, 445.0,
            exception_flag=SellException.EMERGENCY_RISK_EVENT,
        )
        self.assertEqual(result.status, "BLOCKED")

    def test_rule_5_status_always_green(self):
        """Portfolio-level status grid slot is always GREEN."""
        ps = _make_ps()
        result = evaluate_rule_5(ps, 'Yash_Household')
        self.assertEqual(result.status, "GREEN")
        self.assertEqual(result.rule_id, "rule_5")


# ═══════════════════════════════════════════════════════════════════════════
# Rule 6: Vikram EL Floor (4-tier refinement)
# ═══════════════════════════════════════════════════════════════════════════

class TestRule6VikramEL(unittest.TestCase):

    def _make_vikram_ps(self, el, nlv):
        return _make_ps(
            household_el={'Vikram_Household': el},
            household_nlv={'Vikram_Household': nlv},
        )

    def test_vikram_el_above_25pct_green(self):
        ps = self._make_vikram_ps(el=30000, nlv=80000)  # 37.5%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "GREEN")

    def test_vikram_el_20_to_25_amber(self):
        ps = self._make_vikram_ps(el=18000, nlv=80000)  # 22.5%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "AMBER")

    def test_vikram_el_10_to_20_red(self):
        ps = self._make_vikram_ps(el=12000, nlv=80000)  # 15%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")

    def test_vikram_el_below_10_critical(self):
        ps = self._make_vikram_ps(el=6000, nlv=80000)  # 7.5%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")
        self.assertEqual(result.detail.get("severity"), "CRITICAL")

    def test_provider_failure_amber(self):
        """EL unavailable → AMBER (not GREEN, not RED)."""
        ps = _make_ps(household_el={'Vikram_Household': None},
                      household_nlv={'Vikram_Household': 80000})
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "AMBER")

    def test_anomalous_nlv_red(self):
        ps = _make_ps(household_el={'Vikram_Household': 5000},
                      household_nlv={'Vikram_Household': 0})
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")

    def test_non_vikram_household_green(self):
        ps = _make_ps()
        result = evaluate_rule_6(ps, 'Yash_Household')
        self.assertEqual(result.status, "GREEN")

    # --- Decision 4 boundary test matrix ---

    def test_boundary_ratio_0_199(self):
        """19.9% → RED (below 20% floor)."""
        ps = self._make_vikram_ps(el=15920, nlv=80000)  # 19.9%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")

    def test_boundary_ratio_0_200(self):
        """20.0% → AMBER (at floor, approaching zone)."""
        ps = self._make_vikram_ps(el=16000, nlv=80000)  # 20.0%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "AMBER")

    def test_boundary_ratio_0_201(self):
        """20.1% → AMBER (slightly above floor)."""
        ps = self._make_vikram_ps(el=16080, nlv=80000)  # 20.1%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "AMBER")

    def test_boundary_ratio_0_099(self):
        """9.9% → RED + CRITICAL."""
        ps = self._make_vikram_ps(el=7920, nlv=80000)  # 9.9%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")
        self.assertEqual(result.detail.get("severity"), "CRITICAL")

    def test_boundary_ratio_0_100(self):
        """10.0% → RED (at lower boundary, not CRITICAL)."""
        ps = self._make_vikram_ps(el=8000, nlv=80000)  # 10.0%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")
        self.assertNotEqual(result.detail.get("severity"), "CRITICAL")

    def test_boundary_ratio_0_101(self):
        """10.1% → RED (above CRITICAL threshold)."""
        ps = self._make_vikram_ps(el=8080, nlv=80000)  # 10.1%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "RED")
        self.assertNotEqual(result.detail.get("severity"), "CRITICAL")

    def test_boundary_ratio_0_249(self):
        """24.9% → AMBER."""
        ps = self._make_vikram_ps(el=19920, nlv=80000)  # 24.9%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "AMBER")

    def test_boundary_ratio_0_250(self):
        """25.0% → GREEN."""
        ps = self._make_vikram_ps(el=20000, nlv=80000)  # 25.0%
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "GREEN")

    def test_account_el_snapshot_preferred(self):
        """account_el data takes precedence over household_el."""
        snapshot = AccountELSnapshot(
            excess_liquidity=30000, net_liquidation=80000,
            timestamp="2026-04-07T10:00:00", stale=False,
        )
        ps = _make_ps(
            household_el={'Vikram_Household': 5000},  # would be RED
            household_nlv={'Vikram_Household': 80000},
            account_el={"U22388499": snapshot},  # overrides → GREEN
        )
        result = evaluate_rule_6(ps, 'Vikram_Household')
        self.assertEqual(result.status, "GREEN")


# ═══════════════════════════════════════════════════════════════════════════
# Data Provider + State Builder
# ═══════════════════════════════════════════════════════════════════════════

class TestFakeProvider(unittest.TestCase):

    def test_fake_provider_returns_deterministic_bars(self):
        bars = make_bars(100.0, [0.01, -0.01, 0.02])
        self.assertEqual(len(bars), 4)
        self.assertAlmostEqual(bars[0].close, 100.0)
        self.assertAlmostEqual(bars[1].close, 101.0, places=2)

    def test_fake_provider_failure_simulation(self):
        fp = FakeProvider(fail_symbols={"FAIL"})
        with self.assertRaises(DataProviderError):
            fp.get_historical_daily_bars("FAIL", 180)

    def test_provider_singleton_caching(self):
        fp = FakeProvider()
        reset_provider()
        set_provider(fp)
        self.assertIs(get_provider(), fp)
        self.assertIs(get_provider(), fp)  # same instance
        reset_provider()

    def test_market_data_mode_env_flag_respected(self):
        """Verify PROVIDER_TYPE env var is read by get_provider()."""
        reset_provider()
        # Don't actually connect — just verify the flag path exists
        # Setting a non-existent type should raise
        os.environ["PROVIDER_TYPE"] = "nonexistent"
        try:
            with self.assertRaises(ValueError):
                get_provider()
        finally:
            os.environ.pop("PROVIDER_TYPE", None)
            reset_provider()


class TestCorrelationMath(unittest.TestCase):

    def test_perfect_positive_correlation(self):
        """Two series with identical return patterns → corr ≈ 1.0."""
        bars_a, bars_b = make_correlated_bars(n=180, correlation=1.0)
        corr, sample = compute_pearson_correlation(bars_a, bars_b)
        self.assertGreater(corr, 0.95)

    def test_low_correlation(self):
        """Two series with low mixing → corr near target."""
        bars_a, bars_b = make_correlated_bars(n=180, correlation=0.2)
        corr, sample = compute_pearson_correlation(bars_a, bars_b)
        self.assertLess(corr, 0.5)  # approximately low

    def test_build_correlation_matrix_excludes_rule_10(self):
        """build_correlation_matrix skips CORRELATION_EXCLUDED_TICKERS."""
        bars_a = make_bars(100.0, [0.01] * 10)
        bars_b = make_bars(50.0, [0.01] * 10)
        fp = FakeProvider(bars={"ADBE": bars_a, "SLS": bars_b})
        result = build_correlation_matrix(["ADBE", "SLS"], fp)
        self.assertEqual(len(result), 0)  # SLS excluded, only 1 eligible


# ═══════════════════════════════════════════════════════════════════════════
# Rule 3: Sector Concentration — Rule 10 Exclusions
# ═══════════════════════════════════════════════════════════════════════════

class TestRule3Rule10Exclusions(unittest.TestCase):

    def test_rule_3_excludes_legacy_picks_from_sector_count(self):
        """SLS in same industry as 2 Wheel names. SLS excluded → GREEN."""
        cycles = [
            _mock_cycle(ticker='ADBE'),
            _mock_cycle(ticker='CRM'),
            _mock_cycle(ticker='SLS'),  # legacy pick, same industry
        ]
        ps = _make_ps(
            active_cycles=cycles,
            industries={'ADBE': 'Software - Application',
                        'CRM': 'Software - Application',
                        'SLS': 'Software - Application'},
        )
        results = evaluate_rule_3(ps, 'Yash_Household')
        sw_app = [r for r in results if 'Software - Application' in r.message]
        self.assertEqual(len(sw_app), 1)
        self.assertEqual(sw_app[0].raw_value, 2)  # ADBE + CRM only
        self.assertEqual(sw_app[0].status, "GREEN")

    def test_rule_3_excludes_negligible_holdings(self):
        """TRAW.CVR (is_negligible) excluded from sector counts."""
        traw = _mock_cycle(ticker='TRAW.CVR')
        traw.is_negligible = True
        cycles = [
            _mock_cycle(ticker='ADBE'),
            _mock_cycle(ticker='CRM'),
            traw,
        ]
        ps = _make_ps(
            active_cycles=cycles,
            industries={'ADBE': 'Software - Application',
                        'CRM': 'Software - Application',
                        'TRAW.CVR': 'Software - Application'},
        )
        results = evaluate_rule_3(ps, 'Yash_Household')
        sw_app = [r for r in results if 'Software - Application' in r.message]
        self.assertEqual(sw_app[0].raw_value, 2)  # TRAW.CVR excluded
        self.assertEqual(sw_app[0].status, "GREEN")

    def test_rule_3_excludes_spx_box_spreads(self):
        """SPX excluded from all sector counting."""
        cycles = [
            _mock_cycle(ticker='ADBE'),
            _mock_cycle(ticker='SPX'),
        ]
        ps = _make_ps(
            active_cycles=cycles,
            industries={'ADBE': 'Software - Application',
                        'SPX': 'Software - Application'},
        )
        results = evaluate_rule_3(ps, 'Yash_Household')
        sw_app = [r for r in results if 'Software - Application' in r.message]
        self.assertEqual(sw_app[0].raw_value, 1)  # SPX excluded
        self.assertEqual(sw_app[0].status, "GREEN")

    def test_rule_3_legacy_picks_dont_save_real_breach(self):
        """3 real Wheel names in same industry + SLS → RED (3 real > 2 limit)."""
        cycles = [
            _mock_cycle(ticker='ADBE'),
            _mock_cycle(ticker='CRM'),
            _mock_cycle(ticker='UBER'),
            _mock_cycle(ticker='SLS'),  # excluded, doesn't save the breach
        ]
        ps = _make_ps(
            active_cycles=cycles,
            industries={'ADBE': 'Software - Application',
                        'CRM': 'Software - Application',
                        'UBER': 'Software - Application',
                        'SLS': 'Software - Application'},
        )
        results = evaluate_rule_3(ps, 'Yash_Household')
        sw_app = [r for r in results if 'Software - Application' in r.message]
        self.assertEqual(sw_app[0].raw_value, 3)  # ADBE + CRM + UBER
        self.assertEqual(sw_app[0].status, "RED")


# ═══════════════════════════════════════════════════════════════════════════
# Day 1 Baseline (synthetic)
# ═══════════════════════════════════════════════════════════════════════════

class TestDay1Baseline(unittest.TestCase):

    def test_day1_synthetic_peacetime(self):
        """Synthetic data matching handoff state → PEACETIME.

        Handoff: Vikram EL >40%, all glide-pathed REDs → AMBER via mode engine.
        At raw evaluation level with Day 1 baselines == current values → GREEN.
        """
        from agt_equities.mode_engine import compute_mode

        cycles = [
            _mock_cycle('Yash_Household', 'ADBE', 4400, 445.0),
            _mock_cycle('Yash_Household', 'CRM', 200, 262.0),
            _mock_cycle('Yash_Household', 'QCOM', 1000, 158.0),
            _mock_cycle('Vikram_Household', 'ADBE', 200, 325.0),
        ]
        # Vikram EL > 40% → GREEN on R6
        vikram_el_snap = AccountELSnapshot(
            excess_liquidity=35000, net_liquidation=80000,
            timestamp="2026-04-07T10:00:00", stale=False,
        )
        # Provide correlations that would be RED (ADBE-CRM)
        # but at Day 1 with glide paths → softened
        corrs = {
            ("ADBE", "CRM"): CorrelationData(0.72, 180, False, "synthetic"),
            ("ADBE", "QCOM"): CorrelationData(0.55, 180, False, "synthetic"),
            ("CRM", "QCOM"): CorrelationData(0.48, 180, False, "synthetic"),
        }
        ps = _make_ps(
            household_nlv={'Yash_Household': 261902, 'Vikram_Household': 80787},
            household_el={'Yash_Household': None, 'Vikram_Household': 35000},
            active_cycles=cycles,
            spots={'ADBE': 360.0, 'CRM': 240.0, 'QCOM': 155.0},
            betas={'ADBE': 1.1, 'CRM': 1.0, 'QCOM': 1.2},
            industries={'ADBE': 'Software - Application', 'CRM': 'Software - Application',
                        'QCOM': 'Semiconductors'},
            sector_overrides={},
            vix=22.0,
            correlations=corrs,
            account_el={"U22388499": vikram_el_snap},
        )

        results = evaluate_all(ps, 'Yash_Household')
        # R4 will have RED for ADBE-CRM — that's expected raw truth
        # Mode engine + glide paths softens post-hoc
        # R5 should be GREEN (placeholder)
        r5 = [r for r in results if r.rule_id == "rule_5"][0]
        self.assertEqual(r5.status, "GREEN")

        results_vik = evaluate_all(ps, 'Vikram_Household')
        r6 = [r for r in results_vik if r.rule_id == "rule_6"][0]
        self.assertEqual(r6.status, "GREEN")  # >40% EL → GREEN


# ═══════════════════════════════════════════════════════════════════════════
# Glide Path Tolerance Band (Phase 3A.5a triage)
# ═══════════════════════════════════════════════════════════════════════════

class TestGlidePathTolerance(unittest.TestCase):
    """Tests for the worsened-check tolerance band in evaluate_glide_path."""

    def _gp(self, rule_id, baseline, target, start='2026-04-07', end='2026-12-31'):
        from agt_equities.mode_engine import GlidePath
        return GlidePath(
            household_id='Test', rule_id=rule_id, ticker=None,
            baseline_value=baseline, target_value=target,
            start_date=start, target_date=end,
            pause_conditions=None, accelerator_clause=None,
        )

    def test_tolerance_R2_within_band(self):
        """R2: actual 0.5397, baseline 0.542. Drift -0.0023 < tolerance 0.01."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_2', 0.542, 0.70)
        status, _, _ = evaluate_glide_path(gp, 0.5397, '2026-04-07')
        self.assertNotEqual(status, "RED", "Should NOT be worsened within tolerance")

    def test_tolerance_R2_breaches_band(self):
        """R2: actual 0.531, baseline 0.542. Drift -0.011 > tolerance 0.01."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_2', 0.542, 0.70)
        status, _, _ = evaluate_glide_path(gp, 0.531, '2026-04-07')
        self.assertEqual(status, "RED")

    def test_tolerance_R11_within_band(self):
        """R11: actual 2.1738, baseline 2.170. Drift +0.0038 < tolerance 0.02."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        status, _, _ = evaluate_glide_path(gp, 2.1738, '2026-04-07')
        self.assertNotEqual(status, "RED", "Should NOT be worsened within tolerance")

    def test_tolerance_R11_breaches_band(self):
        """R11: actual 2.195, baseline 2.170. Drift +0.025 > tolerance 0.02."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        status, _, _ = evaluate_glide_path(gp, 2.195, '2026-04-07')
        self.assertEqual(status, "RED")

    def test_tolerance_R4_within_band(self):
        """R4: actual 0.705, baseline 0.6915. Drift +0.0135 < tolerance 0.02."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_4', 0.6915, 0.55)
        status, _, _ = evaluate_glide_path(gp, 0.705, '2026-04-07')
        self.assertNotEqual(status, "RED", "Should NOT be worsened within tolerance")

    def test_tolerance_default_for_unknown_rule(self):
        """Unknown rule_id uses default tolerance 0.01. Drift -0.015 > 0.01."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_99', 0.5, 0.7)
        status, _, _ = evaluate_glide_path(gp, 0.485, '2026-04-07')
        self.assertEqual(status, "RED")

    def test_exact_baseline_not_worsened(self):
        """actual == baseline exactly -> NOT worsened."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_2', 0.542, 0.70)
        status, _, _ = evaluate_glide_path(gp, 0.542, '2026-04-07')
        self.assertNotEqual(status, "RED")

    def test_exact_tolerance_edge_not_worsened(self):
        """actual at exactly (baseline - tolerance) -> NOT worsened (inclusive)."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_2', 0.542, 0.70)
        # At exactly baseline - tolerance = 0.542 - 0.01 = 0.532
        status, _, _ = evaluate_glide_path(gp, 0.532, '2026-04-07')
        self.assertNotEqual(status, "RED", "Exactly at tolerance edge should NOT be worsened")
        # Just past the edge
        status2, _, _ = evaluate_glide_path(gp, 0.5319, '2026-04-07')
        self.assertEqual(status2, "RED", "Past tolerance edge should be worsened")


class TestGlidePathAmberTolerance(unittest.TestCase):
    """Tests for symmetric tolerance on the AMBER (behind) check."""

    def _gp(self, rule_id, baseline, target, start='2026-04-07', end='2026-12-31'):
        from agt_equities.mode_engine import GlidePath
        return GlidePath(
            household_id='Test', rule_id=rule_id, ticker=None,
            baseline_value=baseline, target_value=target,
            start_date=start, target_date=end,
            pause_conditions=None, accelerator_clause=None,
        )

    def test_amber_tolerance_within_band(self):
        """R11 at Day 0: actual 2.1738, expected 2.170, drift +0.0038 < tol 0.02 -> GREEN."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        status, _, _ = evaluate_glide_path(gp, 2.1738, '2026-04-07')
        self.assertEqual(status, "GREEN")

    def test_amber_tolerance_breaches_band(self):
        """R11 at Day 0: actual 2.195 > baseline+tol=2.190 -> WORSENED -> RED.
        (WORSENED takes precedence over BEHIND per semantic contract.)"""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        status, _, _ = evaluate_glide_path(gp, 2.195, '2026-04-07')
        self.assertEqual(status, "RED")

    def test_amber_tolerance_R2_upward(self):
        """R2 at Day 0: actual 0.5397, expected 0.542, drift -0.0023 < tol 0.01 -> GREEN."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_2', 0.542, 0.70)
        status, _, _ = evaluate_glide_path(gp, 0.5397, '2026-04-07')
        self.assertEqual(status, "GREEN")

    def test_amber_mid_glide_behind(self):
        """R11 at week 6 of 12: expected=1.835, actual=1.90, behind 0.065 > tol 0.02 -> AMBER."""
        from agt_equities.mode_engine import evaluate_glide_path
        # 12 weeks = 84 days from 2026-04-07 -> 2026-06-30
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        # Week 6 = 42 days from start
        mid_date = '2026-05-19'
        status, expected, delta = evaluate_glide_path(gp, 1.90, mid_date)
        self.assertEqual(status, "AMBER")

    def test_amber_mid_glide_on_track(self):
        """R11 at week 6: expected=1.835, actual=1.850, behind 0.015 < tol 0.02 -> GREEN."""
        from agt_equities.mode_engine import evaluate_glide_path
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        mid_date = '2026-05-19'
        status, expected, delta = evaluate_glide_path(gp, 1.850, mid_date)
        self.assertEqual(status, "GREEN")

    def test_precedence_worsened_over_behind(self):
        """A value that triggers WORSENED should be RED, not AMBER."""
        from agt_equities.mode_engine import evaluate_glide_path
        # R11 baseline 2.17, tolerance 0.02. actual=2.20 > baseline+tol=2.19 -> WORSENED
        gp = self._gp('rule_11', 2.170, 1.50, end='2026-06-30')
        status, _, _ = evaluate_glide_path(gp, 2.20, '2026-04-07')
        self.assertEqual(status, "RED")


if __name__ == '__main__':
    unittest.main()
