"""
tests/test_market_data_dtos.py — DTO construction and property tests.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.market_data_dtos import (
    OptionContractDTO, VolatilityMetricsDTO,
    CorporateCalendarDTO, CorporateActionType,
    ConvictionMetricsDTO,
)


class TestOptionContractDTO(unittest.TestCase):

    def _make_dto(self, **kw):
        defaults = dict(
            symbol="ADBE", strike=240.0, right="C", expiry=date(2026, 4, 24),
            dte=17, delta=0.30, bid=8.90, ask=9.35, mid=9.125,
            spot_price_used=239.84, extrinsic_value=9.12,
            pricing_drift_ms=0, is_extrinsic_stale=False,
            spot_source="model_greeks",
        )
        defaults.update(kw)
        return OptionContractDTO(**defaults)

    def test_extrinsic_clamped_at_zero(self):
        """Deep ITM option where mid < intrinsic → extrinsic = 0."""
        ext = OptionContractDTO.calculate_extrinsic(mid=5.0, spot=250.0, strike=240.0, right="C")
        # intrinsic = 250 - 240 = 10. mid=5 < 10 → extrinsic = max(0, 5-10) = 0
        self.assertEqual(ext, 0.0)

    def test_spread_pct_property(self):
        dto = self._make_dto(bid=8.90, ask=9.35, mid=9.125)
        expected = (9.35 - 8.90) / 9.125
        self.assertAlmostEqual(dto.spread_pct, expected, places=4)

    def test_spread_pct_zero_mid_returns_999(self):
        dto = self._make_dto(bid=0.0, ask=0.0, mid=0.0)
        self.assertEqual(dto.spread_pct, 999.0)

    def test_calculate_extrinsic_call(self):
        # ATM call: spot=240, strike=240, mid=9.125 → intrinsic=0, extrinsic=9.125
        ext = OptionContractDTO.calculate_extrinsic(9.125, 240.0, 240.0, "C")
        self.assertAlmostEqual(ext, 9.125, places=3)

    def test_calculate_extrinsic_put(self):
        # ITM put: spot=235, strike=240, mid=7.0 → intrinsic=5, extrinsic=2
        ext = OptionContractDTO.calculate_extrinsic(7.0, 235.0, 240.0, "P")
        self.assertAlmostEqual(ext, 2.0, places=3)

    def test_frozen_immutability(self):
        dto = self._make_dto()
        with self.assertRaises(AttributeError):
            dto.strike = 999.0


class TestVolatilityMetricsDTO(unittest.TestCase):

    def test_iv_rank_none_handling(self):
        dto = VolatilityMetricsDTO(
            symbol="ADBE", iv_30=0.43, rv_30=0.35, iv_rank=None,
            vrp=0.08, sample_date=date.today(), iv_rank_sample_days=0,
        )
        self.assertIsNone(dto.iv_rank)
        self.assertEqual(dto.iv_rank_sample_days, 0)

    def test_vrp_computation(self):
        dto = VolatilityMetricsDTO(
            symbol="ADBE", iv_30=0.43, rv_30=0.35, iv_rank=0.75,
            vrp=0.08, sample_date=date.today(), iv_rank_sample_days=252,
        )
        self.assertAlmostEqual(dto.vrp, dto.iv_30 - dto.rv_30, places=4)


class TestCorporateCalendarDTO(unittest.TestCase):

    def test_action_enum(self):
        dto = CorporateCalendarDTO(
            symbol="ADBE", next_earnings=date(2026, 6, 12),
            ex_dividend_date=None, dividend_amount=0.0,
            pending_corporate_action=CorporateActionType.NONE,
            data_source="yfinance_temporary",
            cached_at=datetime.now(timezone.utc), cache_age_hours=0.5,
        )
        self.assertEqual(dto.pending_corporate_action, CorporateActionType.NONE)
        self.assertEqual(dto.pending_corporate_action.value, "none")

    def test_all_action_types(self):
        for at in CorporateActionType:
            self.assertIsInstance(at.value, str)


class TestConvictionMetricsDTO(unittest.TestCase):

    def test_construction(self):
        dto = ConvictionMetricsDTO(
            symbol="ADBE", eps_positive=True,
            revenue_above_sector_median=True,
            has_analyst_downgrade=False, operating_margin=0.35,
            data_source="yfinance_temporary",
            cached_at=datetime.now(timezone.utc), cache_age_hours=1.0,
        )
        self.assertTrue(dto.eps_positive)
        self.assertFalse(dto.has_analyst_downgrade)
        self.assertAlmostEqual(dto.operating_margin, 0.35)


if __name__ == '__main__':
    unittest.main()
