"""Finding #4 — R7 earnings cache dual fix tests.

3 tests:
  1. evaluate_all forwards conn to R7 (Bug B fix)
  2. yfinance extraction handles datetime.date (Bug A fix, yfinance 1.2.0)
  3. yfinance extraction handles datetime.datetime (backward compat)
"""

import os
import sqlite3
import sys
import unittest
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Minimal Cycle stub for PortfolioState
# ---------------------------------------------------------------------------

@dataclass
class _StubCycle:
    ticker: str
    household_id: str
    status: str = "ACTIVE"
    shares_held: int = 100
    cost_basis: float = 300.0
    current_cc_contracts: int = 0
    account_id: str = "U21971297"


# ---------------------------------------------------------------------------
# DDL for test DB
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE bucket3_earnings_overrides (
    ticker TEXT PRIMARY KEY,
    override_value TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'manual_override',
    reason TEXT
) WITHOUT ROWID;
"""


class TestEvaluateAllForwardsConnToR7(unittest.TestCase):
    """Bug B: evaluate_all must pass conn= to evaluate_rule_7."""

    def test_r7_sees_override_when_conn_provided(self):
        """With a valid override in DB + conn forwarded, R7 returns GREEN."""
        from agt_equities.rule_engine import (
            PortfolioState, evaluate_all,
        )

        # Build test DB with one override
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(_DDL)
        expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db.execute(
            "INSERT INTO bucket3_earnings_overrides "
            "(ticker, override_value, expires_at, reason) "
            "VALUES ('ADBE', '2026-06-06', ?, 'test')",
            (expires,),
        )
        db.commit()

        # Build minimal PortfolioState with one ADBE cycle
        ps = PortfolioState(
            household_nlv={"Yash_Household": 261000.0},
            household_el={"Yash_Household": 0.50},
            active_cycles=[_StubCycle(ticker="ADBE", household_id="Yash_Household")],
            spots={"ADBE": 230.0},
            betas={"ADBE": 1.2},
            industries={"ADBE": "Software"},
            sector_overrides={},
            vix=18.0,
            report_date="20260409",
        )

        evals = evaluate_all(ps, "Yash_Household", conn=db)

        # Find the R7 evaluation for ADBE
        r7_evals = [e for e in evals if e.rule_id == "rule_7" and e.ticker == "ADBE"]
        self.assertTrue(len(r7_evals) >= 1, "Expected at least one R7 eval for ADBE")
        r7 = r7_evals[0]
        self.assertEqual(r7.status, "GREEN",
                         f"R7 should be GREEN with valid override, got {r7.status}: {r7.message}")
        db.close()


class TestYFinanceExtractionDateTypes(unittest.TestCase):
    """Bug A: provider must handle both datetime.date and datetime.datetime."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_handles_datetime_date(self):
        """yfinance 1.2.0 returns datetime.date — must extract correctly."""
        from agt_equities.providers.yfinance_corporate_intelligence import (
            YFinanceCorporateIntelligenceProvider,
        )

        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker.calendar = {
            "Earnings Date": [date(2026, 6, 11)],
        }

        provider = YFinanceCorporateIntelligenceProvider(
            cache_dir=self._tmpdir, max_age_hours=0.0,
        )

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_corporate_calendar("ADBE")

        self.assertIsNotNone(result, "Provider should return a DTO, not None")
        self.assertEqual(result.next_earnings, date(2026, 6, 11))

        # Also verify cache file was written correctly
        cache_file = Path(self._tmpdir) / "ADBE_calendar.json"
        self.assertTrue(cache_file.exists())
        data = json.loads(cache_file.read_text())
        self.assertEqual(data["next_earnings"], "2026-06-11")

    def test_handles_datetime_datetime_backward_compat(self):
        """Older yfinance returned datetime.datetime — must still work."""
        from agt_equities.providers.yfinance_corporate_intelligence import (
            YFinanceCorporateIntelligenceProvider,
        )

        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker.calendar = {
            "Earnings Date": [datetime(2026, 6, 11, 16, 0)],
        }

        provider = YFinanceCorporateIntelligenceProvider(
            cache_dir=self._tmpdir, max_age_hours=0.0,
        )

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_corporate_calendar("ADBE")

        self.assertIsNotNone(result, "Provider should return a DTO, not None")
        self.assertEqual(result.next_earnings, date(2026, 6, 11))


if __name__ == "__main__":
    unittest.main()
