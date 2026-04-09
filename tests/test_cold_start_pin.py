"""Priority 4: Tests for cold-start wartime leverage pin."""
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _create_mode_history(conn):
    """Create mode_history table matching agt_equities/schema.py."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mode_history (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
            old_mode            TEXT NOT NULL,
            new_mode            TEXT NOT NULL,
            trigger_rule        TEXT,
            trigger_household   TEXT,
            trigger_value       REAL,
            notes               TEXT
        )
    """)
    conn.commit()


class TestColdStartPin(unittest.IsolatedAsyncioTestCase):
    """_pin_mode_on_startup() should pin to WARTIME on high leverage."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _create_mode_history(conn)
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _seed(self, old_mode, new_mode, trigger="manual"):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO mode_history (old_mode, new_mode, trigger_rule) VALUES (?, ?, ?)",
            (old_mode, new_mode, trigger),
        )
        conn.commit()
        conn.close()

    def _count_rows(self):
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM mode_history").fetchone()[0]
        conn.close()
        return count

    def _last_row(self):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM mode_history ORDER BY id DESC LIMIT 1").fetchone()
        result = dict(row) if row else None
        conn.close()
        return result

    @patch("agt_equities.state_builder.build_state")
    @patch("telegram_bot._ibkr_get_spots_batch", new_callable=AsyncMock)
    @patch("telegram_bot._get_db_connection")
    async def test_high_leverage_pins_to_wartime(self, mock_db, mock_spots, mock_build_state):
        mock_db.side_effect = lambda: self._get_conn()
        self._seed("AMBER", "PEACETIME")

        mock_build_state.return_value = SimpleNamespace(
            active_cycles=[
                SimpleNamespace(
                    status="ACTIVE",
                    shares_held=100,
                    household_id="Yash_Household",
                    ticker="AAPL",
                )
            ],
            household_nav={"Yash_Household": 10000.0},
            beta_by_symbol={"AAPL": 1.6},
        )
        mock_spots.return_value = {"AAPL": 100.0}

        mock_ib = AsyncMock()
        mock_ib.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U21971297", tag="NetLiquidation", value="10000"),
        ])

        from telegram_bot import _pin_mode_on_startup
        alert = await _pin_mode_on_startup(mock_ib)

        self.assertIsNotNone(alert)
        self.assertIn("WARTIME", alert)
        self.assertIn("1.50x", alert)

        build_kwargs = mock_build_state.call_args.kwargs
        self.assertEqual(build_kwargs["live_nlv"], {"U21971297": 10000.0})

        row = self._last_row()
        self.assertEqual(row["new_mode"], "WARTIME")
        self.assertEqual(row["trigger_rule"], "cold_start_pin")
        self.assertEqual(row["trigger_household"], "Yash_Household")
        self.assertAlmostEqual(row["trigger_value"], 1.6, places=4)
        self.assertEqual(row["notes"], "Cold-start pin: leverage >= 1.50x")

    @patch("agt_equities.state_builder.build_state")
    @patch("telegram_bot._ibkr_get_spots_batch", new_callable=AsyncMock)
    @patch("telegram_bot._get_db_connection")
    async def test_below_threshold_noop(self, mock_db, mock_spots, mock_build_state):
        mock_db.side_effect = lambda: self._get_conn()
        self._seed("AMBER", "PEACETIME")

        mock_build_state.return_value = SimpleNamespace(
            active_cycles=[
                SimpleNamespace(
                    status="ACTIVE",
                    shares_held=100,
                    household_id="Yash_Household",
                    ticker="AAPL",
                )
            ],
            household_nav={"Yash_Household": 10000.0},
            beta_by_symbol={"AAPL": 1.49},
        )
        mock_spots.return_value = {"AAPL": 100.0}

        mock_ib = AsyncMock()
        mock_ib.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U21971297", tag="NetLiquidation", value="10000"),
        ])

        from telegram_bot import _pin_mode_on_startup
        alert = await _pin_mode_on_startup(mock_ib)

        self.assertIsNone(alert)
        self.assertEqual(self._count_rows(), 1)

    @patch("agt_equities.state_builder.build_state")
    @patch("telegram_bot._ibkr_get_spots_batch", new_callable=AsyncMock)
    @patch("telegram_bot._get_db_connection")
    async def test_already_wartime_noop(self, mock_db, mock_spots, mock_build_state):
        mock_db.side_effect = lambda: self._get_conn()
        self._seed("PEACETIME", "WARTIME", trigger="rule_11")

        mock_ib = AsyncMock()
        mock_ib.accountSummaryAsync = AsyncMock()

        from telegram_bot import _pin_mode_on_startup
        alert = await _pin_mode_on_startup(mock_ib)

        self.assertIsNone(alert)
        self.assertEqual(self._count_rows(), 1)
        mock_ib.accountSummaryAsync.assert_not_awaited()
        mock_build_state.assert_not_called()
        mock_spots.assert_not_awaited()

    @patch("telegram_bot._get_db_connection", side_effect=RuntimeError("DB locked"))
    async def test_db_error_does_not_raise(self, mock_db):
        from telegram_bot import _pin_mode_on_startup
        alert = await _pin_mode_on_startup(AsyncMock())
        self.assertIsNone(alert)


if __name__ == "__main__":
    unittest.main()
