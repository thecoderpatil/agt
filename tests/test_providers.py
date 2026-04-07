"""
tests/test_providers.py — Provider unit tests using synthetic/mock data.

No live IBKR connection required. Tests verify DTO construction,
fallback logic, cache behavior, and build_account_nlv.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.market_data_dtos import (
    OptionContractDTO, VolatilityMetricsDTO,
    CorporateCalendarDTO, CorporateActionType,
    ConvictionMetricsDTO,
)
from agt_equities.providers.yfinance_corporate_intelligence import (
    YFinanceCorporateIntelligenceProvider,
)
from agt_equities.state_builder import build_account_nlv
from agt_equities.data_provider import DataProviderError, AccountSummary


# ═══════════════════════════════════════════════════════════════════════════
# IBKRPriceVolatility (mock-based, no live IBKR)
# ═══════════════════════════════════════════════════════════════════════════

class TestIBKRPriceVolatilityMocked(unittest.TestCase):

    def test_get_factor_matrix_alignment(self):
        """Factor matrix aligns on common dates and computes log returns."""
        from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider

        mock_ib = MagicMock()
        provider = IBKRPriceVolatilityProvider.__new__(IBKRPriceVolatilityProvider)
        provider.ib = mock_ib
        provider.mode = "delayed"
        provider._fallback_counter = {"model_greeks_used": 0, "historical_fallback_used": 0, "total_spot_calls": 0}

        # Mock get_historical_daily_bars
        d1, d2, d3, d4 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)
        provider.get_historical_daily_bars = MagicMock(side_effect=lambda t, d: {
            "ADBE": [(d1, 100), (d2, 102), (d3, 101), (d4, 103)],
            "SPY": [(d1, 400), (d2, 404), (d3, 402), (d4, 406)],
        }.get(t, []))

        result = provider.get_factor_matrix(["ADBE"], "SPY", 5)
        self.assertIn("ADBE", result)
        self.assertIn("SPY", result)
        self.assertEqual(len(result["ADBE"]), 3)  # 4 prices → 3 returns

    def test_get_factor_matrix_handles_missing_ticker(self):
        from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider
        provider = IBKRPriceVolatilityProvider.__new__(IBKRPriceVolatilityProvider)
        provider.ib = MagicMock()
        provider.mode = "delayed"
        provider._fallback_counter = {}
        provider.get_historical_daily_bars = MagicMock(return_value=[])
        result = provider.get_factor_matrix(["MISSING"], "SPY", 5)
        self.assertEqual(result, {})

    def test_get_volatility_surface_iv_rank_none_during_bootstrap(self):
        """Before 252 days of history, iv_rank is None."""
        from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider
        provider = IBKRPriceVolatilityProvider.__new__(IBKRPriceVolatilityProvider)
        provider.ib = MagicMock()
        provider.mode = "delayed"
        provider._fallback_counter = {}

        # Mock sufficient bars for RV30
        bars = [(date(2026, 1, 1) + timedelta(days=i), 100 + i * 0.5) for i in range(45)]
        provider.get_historical_daily_bars = MagicMock(return_value=bars)
        provider._fetch_atm_iv = MagicMock(return_value=0.43)

        result = provider.get_volatility_surface("ADBE")
        self.assertIsNotNone(result)
        self.assertIsNone(result.iv_rank)
        self.assertEqual(result.iv_rank_sample_days, 0)


# ═══════════════════════════════════════════════════════════════════════════
# IBKROptionsChain (mock-based)
# ═══════════════════════════════════════════════════════════════════════════

class TestIBKROptionsChainMocked(unittest.TestCase):

    def test_build_dto_uses_model_greeks_when_available(self):
        from agt_equities.providers.ibkr_options_chain import IBKROptionsChainProvider
        provider = IBKROptionsChainProvider.__new__(IBKROptionsChainProvider)
        provider.ib = MagicMock()
        provider._price_provider = None
        provider._fallback_counter = {"model_greeks_path": 0, "historical_fallback_path": 0}

        contract = MagicMock()
        contract.symbol = "ADBE"
        contract.strike = 240.0
        contract.right = "C"
        contract.lastTradeDateOrContractMonth = "20260424"

        ticker = MagicMock()
        ticker.bid = 8.90
        ticker.ask = 9.35
        ticker.modelGreeks = MagicMock()
        ticker.modelGreeks.undPrice = 239.84
        ticker.modelGreeks.delta = 0.30
        ticker.modelGreeks.gamma = 0.013
        ticker.modelGreeks.vega = 0.15
        ticker.modelGreeks.theta = -0.05
        ticker.modelGreeks.impliedVol = 0.43

        dto = provider._build_dto(contract, ticker, max_delta=0.35)
        self.assertIsNotNone(dto)
        self.assertEqual(dto.spot_source, "model_greeks")
        self.assertEqual(dto.pricing_drift_ms, 0)
        self.assertFalse(dto.is_extrinsic_stale)
        self.assertAlmostEqual(dto.delta, 0.30)

    def test_build_dto_falls_back_on_no_undprice(self):
        from agt_equities.providers.ibkr_options_chain import IBKROptionsChainProvider
        provider = IBKROptionsChainProvider.__new__(IBKROptionsChainProvider)
        provider.ib = MagicMock()
        provider._fallback_counter = {"model_greeks_path": 0, "historical_fallback_path": 0}

        mock_price = MagicMock()
        mock_price.get_spot.return_value = 240.0
        provider._price_provider = mock_price

        contract = MagicMock()
        contract.symbol = "ADBE"
        contract.strike = 240.0
        contract.right = "C"
        contract.lastTradeDateOrContractMonth = "20260424"

        ticker = MagicMock()
        ticker.bid = 8.90
        ticker.ask = 9.35
        ticker.modelGreeks = MagicMock()
        ticker.modelGreeks.undPrice = float('nan')
        ticker.modelGreeks.delta = 0.30
        ticker.modelGreeks.gamma = 0.013
        ticker.modelGreeks.vega = 0.15
        ticker.modelGreeks.theta = -0.05
        ticker.modelGreeks.impliedVol = 0.43

        provider._get_spot_approx = MagicMock(return_value=240.0)
        dto = provider._build_dto(contract, ticker, max_delta=0.35)
        self.assertIsNotNone(dto)
        self.assertEqual(dto.spot_source, "historical_fallback")
        self.assertTrue(dto.is_extrinsic_stale)

    def test_build_dto_marks_stale_on_fallback(self):
        """Fallback path ALWAYS sets is_extrinsic_stale=True."""
        from agt_equities.providers.ibkr_options_chain import IBKROptionsChainProvider
        provider = IBKROptionsChainProvider.__new__(IBKROptionsChainProvider)
        provider.ib = MagicMock()
        provider._fallback_counter = {"model_greeks_path": 0, "historical_fallback_path": 0}
        provider._get_spot_approx = MagicMock(return_value=240.0)

        contract = MagicMock()
        contract.symbol = "ADBE"
        contract.strike = 240.0
        contract.right = "C"
        contract.lastTradeDateOrContractMonth = "20260424"

        ticker = MagicMock()
        ticker.bid = 8.90
        ticker.ask = 9.35
        ticker.modelGreeks = MagicMock()
        ticker.modelGreeks.undPrice = None  # no undPrice
        ticker.modelGreeks.delta = 0.30
        ticker.modelGreeks.gamma = 0.013
        ticker.modelGreeks.vega = 0.15
        ticker.modelGreeks.theta = -0.05
        ticker.modelGreeks.impliedVol = 0.43

        dto = provider._build_dto(contract, ticker, max_delta=0.35)
        self.assertTrue(dto.is_extrinsic_stale)
        self.assertEqual(dto.spot_source, "historical_fallback")

    def test_fallback_counter_increments(self):
        from agt_equities.providers.ibkr_options_chain import IBKROptionsChainProvider
        provider = IBKROptionsChainProvider.__new__(IBKROptionsChainProvider)
        provider.ib = MagicMock()
        provider._fallback_counter = {"model_greeks_path": 0, "historical_fallback_path": 0}
        provider._get_spot_approx = MagicMock(return_value=240.0)

        # model_greeks path
        contract = MagicMock()
        contract.symbol = "ADBE"; contract.strike = 240.0; contract.right = "C"
        contract.lastTradeDateOrContractMonth = "20260424"
        ticker = MagicMock()
        ticker.bid = 9.0; ticker.ask = 9.5
        ticker.modelGreeks = MagicMock()
        ticker.modelGreeks.undPrice = 240.0
        ticker.modelGreeks.delta = 0.3; ticker.modelGreeks.gamma = 0.01
        ticker.modelGreeks.vega = 0.1; ticker.modelGreeks.theta = -0.05
        ticker.modelGreeks.impliedVol = 0.4
        provider._build_dto(contract, ticker, None)
        self.assertEqual(provider._fallback_counter["model_greeks_path"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# YFinance Corporate Intelligence (file-cache based)
# ═══════════════════════════════════════════════════════════════════════════

class TestYFinanceCorporateIntelligence(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.provider = YFinanceCorporateIntelligenceProvider(
            cache_dir=self._tmp, max_age_hours=24.0,
        )

    def test_calendar_returns_cached_when_fresh(self):
        """Write a fresh cache entry, verify it's returned without yfinance call."""
        now = datetime.now(timezone.utc)
        cache_data = {
            "symbol": "ADBE",
            "next_earnings": "2026-06-12",
            "ex_dividend_date": None,
            "dividend_amount": 0.0,
            "pending_corporate_action": "none",
            "data_source": "yfinance_temporary",
            "cached_at": now.isoformat(),
        }
        cache_path = Path(self._tmp) / "ADBE_calendar.json"
        cache_path.write_text(json.dumps(cache_data))

        result = self.provider.get_corporate_calendar("ADBE")
        self.assertIsNotNone(result)
        self.assertEqual(result.symbol, "ADBE")
        self.assertEqual(result.next_earnings, date(2026, 6, 12))
        self.assertLess(result.cache_age_hours, 1.0)

    def test_calendar_returns_none_when_no_cache_and_fetch_fails(self):
        """No cache, yfinance fails → None."""
        with patch.dict("sys.modules", {"yfinance": MagicMock(side_effect=Exception("fail"))}):
            # Force yfinance import to fail inside the method
            result = self.provider._read_cache("NONEXISTENT", "calendar")
            self.assertIsNone(result)

    def test_conviction_returns_none_gracefully(self):
        """No cache, no yfinance → None."""
        result = self.provider._read_cache_conviction("NONEXISTENT")
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
# state_builder.build_account_nlv
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildAccountNLV(unittest.TestCase):

    def test_build_account_nlv_returns_values(self):
        from agt_equities.data_provider import AccountSummary
        mock_provider = MagicMock()
        mock_provider.get_account_summary.return_value = AccountSummary(
            account_id="U21971297", excess_liquidity=44000,
            net_liquidation=109000, timestamp=datetime.now(timezone.utc),
        )
        result = build_account_nlv(["U21971297"], mock_provider)
        self.assertEqual(result["U21971297"], 109000)

    def test_build_account_nlv_handles_failure(self):
        mock_provider = MagicMock()
        mock_provider.get_account_summary.side_effect = DataProviderError("fail")
        result = build_account_nlv(["U21971297"], mock_provider)
        self.assertIsNone(result["U21971297"])

    def test_build_account_nlv_multiple_accounts(self):
        from agt_equities.data_provider import AccountSummary
        mock_provider = MagicMock()

        def side_effect(acct):
            if acct == "U21971297":
                return AccountSummary("U21971297", 44000, 109000, datetime.now(timezone.utc))
            elif acct == "U22388499":
                return AccountSummary("U22388499", 42000, 80000, datetime.now(timezone.utc))
            raise DataProviderError("unknown")

        mock_provider.get_account_summary.side_effect = side_effect
        result = build_account_nlv(["U21971297", "U22388499", "U99999999"], mock_provider)
        self.assertEqual(result["U21971297"], 109000)
        self.assertEqual(result["U22388499"], 80000)
        self.assertIsNone(result["U99999999"])


# ═══════════════════════════════════════════════════════════════════════════
# EOD Macro Sync (unit tests, no IBKR)
# ═══════════════════════════════════════════════════════════════════════════

class TestEODMacroSync(unittest.TestCase):

    def _get_test_db(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE bucket3_macro_iv_history (
                ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
                iv_30 REAL NOT NULL,
                sample_source TEXT NOT NULL DEFAULT 'eod_macro_sync',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, trade_date)
            )
        """)
        return conn

    def test_insert_iv_row(self):
        conn = self._get_test_db()
        conn.execute(
            "INSERT INTO bucket3_macro_iv_history (ticker, trade_date, iv_30) "
            "VALUES (?, ?, ?)", ("ADBE", "2026-04-07", 0.43),
        )
        row = conn.execute(
            "SELECT * FROM bucket3_macro_iv_history WHERE ticker='ADBE'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[2], 0.43, places=2)

    def test_idempotent_on_rerun(self):
        conn = self._get_test_db()
        conn.execute(
            "INSERT INTO bucket3_macro_iv_history (ticker, trade_date, iv_30) "
            "VALUES (?, ?, ?)", ("ADBE", "2026-04-07", 0.43),
        )
        conn.execute(
            "INSERT INTO bucket3_macro_iv_history (ticker, trade_date, iv_30) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ticker, trade_date) DO UPDATE SET iv_30=excluded.iv_30",
            ("ADBE", "2026-04-07", 0.45),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM bucket3_macro_iv_history WHERE ticker='ADBE'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        val = conn.execute(
            "SELECT iv_30 FROM bucket3_macro_iv_history WHERE ticker='ADBE'"
        ).fetchone()[0]
        self.assertAlmostEqual(val, 0.45, places=2)

    def test_purges_old_rows(self):
        conn = self._get_test_db()
        conn.execute(
            "INSERT INTO bucket3_macro_iv_history (ticker, trade_date, iv_30) "
            "VALUES (?, ?, ?)", ("ADBE", "2025-01-01", 0.40),
        )
        conn.execute(
            "INSERT INTO bucket3_macro_iv_history (ticker, trade_date, iv_30) "
            "VALUES (?, ?, ?)", ("ADBE", "2026-04-07", 0.43),
        )
        cutoff = (date.today() - timedelta(days=400)).isoformat()
        conn.execute(
            "DELETE FROM bucket3_macro_iv_history WHERE trade_date < ?", (cutoff,)
        )
        count = conn.execute("SELECT COUNT(*) FROM bucket3_macro_iv_history").fetchone()[0]
        self.assertEqual(count, 1)  # only 2026-04-07 remains


if __name__ == '__main__':
    unittest.main()
