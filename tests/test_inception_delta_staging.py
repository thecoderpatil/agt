"""Sprint-1.2 / 2026-04-15 unified CC engine: inception_delta plumbing from
chain DTO to pending_orders payload.

Tests verify that _walk_cc_chain (unified basis-anchored walker) includes
'inception_delta' in its return dict, and that the value propagates into
pending_orders.payload via the {**ticket, **result} merge pattern.

Prior to 2026-04-15 these tests covered _walk_mode1_chain + _walk_harvest_chain
separately; the unified walker collapses both paths so one set of coverage is
sufficient.
"""
import json
import os
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_equities.schema import register_operational_tables


def _make_chain_data(strike=105.0, bid=3.80, ask=4.20, delta=0.22):
    """Build a chain row list matching get_chain_for_expiry output shape.

    Defaults picked so that with paper_basis=100, spot=104, dte=32 the
    annualized yield (~43%) sits inside the 30-130% band — i.e. the walker
    returns a hit rather than a stand-down.
    """
    return [{
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "last": 4.00,
        "volume": 500,
        "openInterest": 3000,
        "impliedVol": 0.35,
        "delta": delta,
    }]


class TestUnifiedWalkerInceptionDelta(unittest.IsolatedAsyncioTestCase):
    """Verify _walk_cc_chain return dict includes inception_delta."""

    async def test_walker_returns_inception_delta_float(self):
        """CC walker result dict must contain 'inception_delta' as float."""
        from telegram_bot import _walk_cc_chain

        chain_data = _make_chain_data(strike=105.0, delta=0.22)

        with patch("telegram_bot._ibkr_get_expirations", new_callable=AsyncMock) as mock_exp, \
             patch("telegram_bot._ibkr_get_chain", new_callable=AsyncMock) as mock_chain:
            mock_exp.return_value = ["2026-05-15"]
            mock_chain.return_value = chain_data

            result = await _walk_cc_chain("AAPL", 104.0, 100.0, (7, 45))

        self.assertIsNotNone(result)
        # Must be a hit (not a stand-down below_floor dict)
        self.assertFalse(result.get("below_floor", False))
        self.assertIn("inception_delta", result)
        self.assertAlmostEqual(result["inception_delta"], 0.22)
        self.assertIsInstance(result["inception_delta"], float)

    async def test_walker_inception_delta_none_when_chain_delta_none(self):
        """inception_delta must be None when chain DTO carries None delta."""
        from telegram_bot import _walk_cc_chain

        chain_data = _make_chain_data(strike=105.0, delta=None)

        with patch("telegram_bot._ibkr_get_expirations", new_callable=AsyncMock) as mock_exp, \
             patch("telegram_bot._ibkr_get_chain", new_callable=AsyncMock) as mock_chain:
            mock_exp.return_value = ["2026-05-15"]
            mock_chain.return_value = chain_data

            result = await _walk_cc_chain("AAPL", 104.0, 100.0, (7, 45))

        self.assertIsNotNone(result)
        self.assertFalse(result.get("below_floor", False))
        self.assertIn("inception_delta", result)
        self.assertIsNone(result["inception_delta"])

    async def test_walker_does_not_raise_on_all_none_deltas(self):
        """Staging must proceed even if every row has None delta."""
        from telegram_bot import _walk_cc_chain

        chain_data = _make_chain_data(strike=105.0, delta=None)

        with patch("telegram_bot._ibkr_get_expirations", new_callable=AsyncMock) as mock_exp, \
             patch("telegram_bot._ibkr_get_chain", new_callable=AsyncMock) as mock_chain:
            mock_exp.return_value = ["2026-05-15"]
            mock_chain.return_value = chain_data

            result = await _walk_cc_chain("AAPL", 104.0, 100.0, (7, 45))

        self.assertIsNotNone(result)
        self.assertFalse(result.get("below_floor", False))
        self.assertIn("strike", result)
        self.assertIsNone(result["inception_delta"])


class TestPayloadPropagation(unittest.TestCase):
    """Verify inception_delta flows through {**ticket, **result} merge
    into pending_orders.payload JSON."""

    def test_ticket_merge_propagates_inception_delta(self):
        """The merge pattern {**ticket, **result} must carry inception_delta."""
        ticket = {
            "account_id": "U12345",
            "household": "test_hh",
            "ticker": "AAPL",
            "action": "SELL",
            "sec_type": "OPT",
            "right": "C",
            "strike": 105.0,
            "expiry": "2026-05-15",
            "quantity": 1,
            "limit_price": 4.00,
            "annualized_yield": 43.5,
            "mode": "MODE_2_HARVEST",
            "status": "staged",
        }
        result = {
            "branch": "BASIS_ANCHOR",
            "ticker": "AAPL",
            "expiry": "2026-05-15",
            "dte": 32,
            "strike": 105.0,
            "bid": 4.00,
            "annualized": 43.5,
            "otm_pct": 4.76,
            "walk_away_pnl": 9.00,
            "dte_range": "7-45",
            "inception_delta": 0.22,
        }
        merged = {**ticket, **result}

        self.assertIn("inception_delta", merged)
        self.assertAlmostEqual(merged["inception_delta"], 0.22)

        payload_json = json.dumps(merged)
        parsed = json.loads(payload_json)
        self.assertAlmostEqual(parsed["inception_delta"], 0.22)

    def test_ticket_merge_propagates_none_inception_delta(self):
        """None inception_delta must serialize as JSON null."""
        result = {"inception_delta": None, "strike": 105.0}
        ticket = {"ticker": "AAPL", "status": "staged"}
        merged = {**ticket, **result}

        payload_json = json.dumps(merged)
        parsed = json.loads(payload_json)
        self.assertIn("inception_delta", parsed)
        self.assertIsNone(parsed["inception_delta"])


if __name__ == "__main__":
    unittest.main()
