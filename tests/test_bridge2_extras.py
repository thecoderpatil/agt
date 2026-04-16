"""Bridge-2 extras provider tests — delta, earnings, correlations flow
through to CSP allocator gate checks (Rules 3, 4, 7).

Marker: sprint_a (runs in CI slim container). No ib_async / telegram /
yfinance imports — pure scan_bridge + scan_extras with injected fakes.
"""
from __future__ import annotations

import pytest
from datetime import date
from unittest.mock import MagicMock

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from agt_equities.scan_bridge import (
    ScanCandidate,
    adapt_scanner_candidates,
    make_bridge2_extras_provider,
    make_minimal_extras_provider,
)
from agt_equities.scan_extras import (
    fetch_earnings_map,
    build_correlation_pairs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scanner_row(**overrides):
    base = {
        "ticker": "AAPL",
        "strike": 180.0,
        "expiry": "2026-05-15",
        "premium": 2.40,
        "ann_roi": 35.0,
        "dte": 30,
        "otm_pct": 5.2,
        "capital_required": 18000.0,
        "delta": 0.18,
    }
    base.update(overrides)
    return base


def _make_candidate(**overrides):
    defaults = dict(
        ticker="AAPL", strike=180.0, mid=2.40, expiry="2026-05-15",
        annualized_yield=35.0, dte=30, delta=0.18,
    )
    defaults.update(overrides)
    return ScanCandidate(**defaults)


# ---------------------------------------------------------------------------
# ScanCandidate.delta field
# ---------------------------------------------------------------------------


class TestScanCandidateDelta:
    def test_delta_preserved_from_scanner_row(self):
        out = adapt_scanner_candidates([_scanner_row(delta=0.22)])
        assert len(out) == 1
        assert out[0].delta == pytest.approx(0.22)

    def test_delta_default_zero_when_missing(self):
        row = _scanner_row()
        del row["delta"]
        out = adapt_scanner_candidates([row])
        assert out[0].delta == 0.0

    def test_delta_abs_applied(self):
        out = adapt_scanner_candidates([_scanner_row(delta=-0.15)])
        assert out[0].delta == pytest.approx(0.15)

    def test_delta_none_becomes_zero(self):
        out = adapt_scanner_candidates([_scanner_row(delta=None)])
        assert out[0].delta == 0.0

    def test_delta_bad_string_becomes_zero(self):
        out = adapt_scanner_candidates([_scanner_row(delta="bad")])
        assert out[0].delta == 0.0


# ---------------------------------------------------------------------------
# make_bridge2_extras_provider
# ---------------------------------------------------------------------------


class TestBridge2ExtrasProvider:
    def test_provides_delta_from_candidate(self):
        provider = make_bridge2_extras_provider(
            sector_map={"AAPL": "Technology"},
            earnings_map={"AAPL": 14},
            correlation_pairs={},
        )
        candidate = _make_candidate(delta=0.19)
        extras = provider({}, candidate)
        assert extras["delta"] == pytest.approx(0.19)

    def test_provides_days_to_earnings(self):
        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={"AAPL": 5, "MSFT": 21},
            correlation_pairs={},
        )
        extras = provider({}, _make_candidate(ticker="AAPL"))
        assert extras["days_to_earnings"] == 5

    def test_earnings_none_for_unknown_ticker(self):
        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={"MSFT": 21},
            correlation_pairs={},
        )
        extras = provider({}, _make_candidate(ticker="AAPL"))
        assert extras["days_to_earnings"] is None

    def test_provides_correlation_pairs(self):
        pairs = {("AAPL", "MSFT"): 0.72, ("AAPL", "GOOG"): 0.45}
        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={},
            correlation_pairs=pairs,
        )
        extras = provider({}, _make_candidate())
        assert extras["correlations"][("AAPL", "MSFT")] == pytest.approx(0.72)
        assert extras["correlations"][("AAPL", "GOOG")] == pytest.approx(0.45)

    def test_provides_sector_map(self):
        provider = make_bridge2_extras_provider(
            sector_map={"AAPL": "Technology", "XOM": "Energy"},
            earnings_map={},
            correlation_pairs={},
        )
        extras = provider({}, _make_candidate())
        assert extras["sector_map"]["AAPL"] == "Technology"
        assert extras["sector_map"]["XOM"] == "Energy"

    def test_defensive_copies(self):
        """Mutating the input dicts after construction must not affect provider."""
        sectors = {"AAPL": "Technology"}
        earnings = {"AAPL": 14}
        corr = {("AAPL", "MSFT"): 0.5}
        provider = make_bridge2_extras_provider(sectors, earnings, corr)

        # Mutate inputs
        sectors["AAPL"] = "MUTATED"
        earnings["AAPL"] = 999
        corr[("AAPL", "MSFT")] = 0.99

        extras = provider({}, _make_candidate())
        assert extras["sector_map"]["AAPL"] == "Technology"
        assert extras["days_to_earnings"] == 14
        assert extras["correlations"][("AAPL", "MSFT")] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# fetch_earnings_map (with injected provider)
# ---------------------------------------------------------------------------


class TestFetchEarningsMap:
    def test_happy_path(self):
        mock_provider = MagicMock()
        cal = MagicMock()
        cal.next_earnings = date(2026, 5, 1)
        mock_provider.get_corporate_calendar.return_value = cal

        result = fetch_earnings_map(
            ["AAPL", "MSFT"],
            provider=mock_provider,
            today=date(2026, 4, 16),
        )
        assert result["AAPL"] == 15
        assert result["MSFT"] == 15

    def test_none_earnings(self):
        mock_provider = MagicMock()
        cal = MagicMock()
        cal.next_earnings = None
        mock_provider.get_corporate_calendar.return_value = cal

        result = fetch_earnings_map(
            ["AAPL"],
            provider=mock_provider,
            today=date(2026, 4, 16),
        )
        assert result["AAPL"] is None

    def test_provider_returns_none(self):
        mock_provider = MagicMock()
        mock_provider.get_corporate_calendar.return_value = None

        result = fetch_earnings_map(
            ["AAPL"],
            provider=mock_provider,
            today=date(2026, 4, 16),
        )
        assert result["AAPL"] is None

    def test_provider_exception_returns_none(self):
        mock_provider = MagicMock()
        mock_provider.get_corporate_calendar.side_effect = RuntimeError("boom")

        result = fetch_earnings_map(
            ["AAPL"],
            provider=mock_provider,
            today=date(2026, 4, 16),
        )
        assert result["AAPL"] is None

    def test_negative_days_for_past_earnings(self):
        mock_provider = MagicMock()
        cal = MagicMock()
        cal.next_earnings = date(2026, 4, 10)  # 6 days ago
        mock_provider.get_corporate_calendar.return_value = cal

        result = fetch_earnings_map(
            ["AAPL"],
            provider=mock_provider,
            today=date(2026, 4, 16),
        )
        assert result["AAPL"] == -6

    def test_ticker_uppercased(self):
        mock_provider = MagicMock()
        cal = MagicMock()
        cal.next_earnings = date(2026, 5, 1)
        mock_provider.get_corporate_calendar.return_value = cal

        result = fetch_earnings_map(
            ["aapl"],
            provider=mock_provider,
            today=date(2026, 4, 16),
        )
        assert "AAPL" in result


# ---------------------------------------------------------------------------
# build_correlation_pairs (with injected download_fn)
# ---------------------------------------------------------------------------


class TestBuildCorrelationPairs:
    def _make_download_fn(self, data: dict):
        """Return a fake download_fn that returns pre-built DataFrames."""
        import pandas as pd
        import numpy as np

        def _download(symbols, period):
            result = {}
            np.random.seed(42)
            dates = pd.date_range("2025-10-01", periods=130, freq="B")
            for sym in symbols:
                if sym in data:
                    result[sym] = pd.DataFrame(
                        {"Close": data[sym]},
                        index=dates[:len(data[sym])],
                    )
            return result
        return _download

    def test_computes_pairs(self):
        import numpy as np
        np.random.seed(42)
        n = 130
        # AAPL and MSFT highly correlated
        base = np.cumsum(np.random.randn(n)) + 100
        aapl = base + np.random.randn(n) * 0.1
        msft = base + np.random.randn(n) * 0.1
        goog = np.cumsum(np.random.randn(n)) + 200  # uncorrelated

        dl = self._make_download_fn({
            "AAPL": aapl, "MSFT": msft, "GOOG": goog,
        })
        result = build_correlation_pairs(
            ["AAPL"], ["MSFT", "GOOG"], download_fn=dl,
        )
        assert ("AAPL", "MSFT") in result
        assert result[("AAPL", "MSFT")] > 0.8  # highly correlated
        assert ("AAPL", "GOOG") in result

    def test_empty_candidates_returns_empty(self):
        result = build_correlation_pairs([], ["MSFT"])
        assert result == {}

    def test_empty_holdings_returns_empty(self):
        result = build_correlation_pairs(["AAPL"], [])
        assert result == {}

    def test_download_failure_returns_empty(self):
        def _broken(symbols, period):
            return {}
        result = build_correlation_pairs(
            ["AAPL"], ["MSFT"], download_fn=_broken,
        )
        assert result == {}


# ---------------------------------------------------------------------------
# Integration: bridge-2 provider → Rule 7 delta gate
# ---------------------------------------------------------------------------


class TestBridge2Rule7Integration:
    """Verify that bridge-2 extras actually flow through to Rule 7 gate."""

    def test_rule7_rejects_high_delta(self):
        from agt_equities.csp_allocator import _csp_check_rule_7

        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={},
            correlation_pairs={},
        )
        candidate = _make_candidate(delta=0.30)
        hh = {"working_order_tickers": set(), "staged_order_tickers": set()}
        extras = provider(hh, candidate)

        passed, reason = _csp_check_rule_7(hh, candidate, 1, 20.0, extras)
        assert not passed
        assert "delta" in reason

    def test_rule7_passes_low_delta(self):
        from agt_equities.csp_allocator import _csp_check_rule_7

        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={},
            correlation_pairs={},
        )
        candidate = _make_candidate(delta=0.20)
        hh = {"working_order_tickers": set(), "staged_order_tickers": set()}
        extras = provider(hh, candidate)

        passed, _ = _csp_check_rule_7(hh, candidate, 1, 20.0, extras)
        assert passed

    def test_rule7_rejects_near_earnings(self):
        from agt_equities.csp_allocator import _csp_check_rule_7

        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={"AAPL": 3},  # 3 days to earnings
            correlation_pairs={},
        )
        candidate = _make_candidate(delta=0.15)
        hh = {"working_order_tickers": set(), "staged_order_tickers": set()}
        extras = provider(hh, candidate)

        passed, reason = _csp_check_rule_7(hh, candidate, 1, 20.0, extras)
        assert not passed
        assert "earnings" in reason

    def test_rule7_passes_far_earnings(self):
        from agt_equities.csp_allocator import _csp_check_rule_7

        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={"AAPL": 21},  # 21 days out
            correlation_pairs={},
        )
        candidate = _make_candidate(delta=0.15)
        hh = {"working_order_tickers": set(), "staged_order_tickers": set()}
        extras = provider(hh, candidate)

        passed, _ = _csp_check_rule_7(hh, candidate, 1, 20.0, extras)
        assert passed


# ---------------------------------------------------------------------------
# Integration: bridge-2 provider → Rule 4 correlation gate
# ---------------------------------------------------------------------------


class TestBridge2Rule4Integration:
    def test_rule4_rejects_high_correlation(self):
        from agt_equities.csp_allocator import _csp_check_rule_4

        corr_pairs = {("AAPL", "MSFT"): 0.75}
        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={},
            correlation_pairs=corr_pairs,
        )
        candidate = _make_candidate(ticker="AAPL")
        hh = {
            "existing_positions": {"MSFT": {"current_value": 10000}},
            "existing_csps": {},
        }
        extras = provider(hh, candidate)

        passed, reason = _csp_check_rule_4(hh, candidate, 1, 20.0, extras)
        assert not passed
        assert "correlation" in reason
        assert "MSFT" in reason

    def test_rule4_passes_low_correlation(self):
        from agt_equities.csp_allocator import _csp_check_rule_4

        corr_pairs = {("AAPL", "MSFT"): 0.45}
        provider = make_bridge2_extras_provider(
            sector_map={},
            earnings_map={},
            correlation_pairs=corr_pairs,
        )
        candidate = _make_candidate(ticker="AAPL")
        hh = {
            "existing_positions": {"MSFT": {"current_value": 10000}},
            "existing_csps": {},
        }
        extras = provider(hh, candidate)

        passed, _ = _csp_check_rule_4(hh, candidate, 1, 20.0, extras)
        assert passed

    def test_bridge1_rule4_failopen_vs_bridge2_reject(self):
        """Bridge-1 (empty correlations) fail-opens; bridge-2 with real data rejects."""
        from agt_equities.csp_allocator import _csp_check_rule_4

        candidate = _make_candidate(ticker="AAPL")
        hh = {
            "existing_positions": {"MSFT": {"current_value": 10000}},
            "existing_csps": {},
        }

        # Bridge-1: no correlation data → passes (fail-open)
        b1_provider = make_minimal_extras_provider({"AAPL": "Technology"})
        b1_extras = b1_provider(hh, candidate)
        b1_passed, _ = _csp_check_rule_4(hh, candidate, 1, 20.0, b1_extras)
        assert b1_passed

        # Bridge-2: high correlation → rejects
        b2_provider = make_bridge2_extras_provider(
            sector_map={"AAPL": "Technology"},
            earnings_map={},
            correlation_pairs={("AAPL", "MSFT"): 0.85},
        )
        b2_extras = b2_provider(hh, candidate)
        b2_passed, _ = _csp_check_rule_4(hh, candidate, 1, 20.0, b2_extras)
        assert not b2_passed
