"""Sprint 1B: Tests for el_snapshots writer + health strip reader."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from types import SimpleNamespace

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _create_tables(conn):
    """Create el_snapshots with Sprint 1B account_id column."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS el_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            excess_liquidity REAL,
            nlv REAL,
            buying_power REAL,
            source TEXT NOT NULL DEFAULT 'ibkr_live',
            account_id TEXT
        )
    """)
    conn.commit()


class TestElSnapshotWriter(unittest.TestCase):
    """Test the _el_snapshot_writer_job debounce + DB write."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _create_tables(conn)
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _count_rows(self):
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM el_snapshots").fetchone()[0]
        conn.close()
        return count

    @patch("telegram_bot.ACTIVE_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U21971297": "Yash_Household"})
    @patch("telegram_bot._el_last_write", {})
    @patch("telegram_bot._get_db_connection")
    @patch("telegram_bot.ensure_ib_connected")
    def test_writer_inserts_row(self, mock_ib, mock_db):
        import asyncio
        from telegram_bot import _el_snapshot_writer_job

        mock_db.side_effect = lambda: self._get_conn()
        mock_conn = AsyncMock()
        mock_conn.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U21971297", tag="NetLiquidation", value="150000"),
            SimpleNamespace(account="U21971297", tag="ExcessLiquidity", value="45000"),
            SimpleNamespace(account="U21971297", tag="BuyingPower", value="90000"),
        ])
        mock_ib.return_value = mock_conn

        asyncio.get_event_loop().run_until_complete(_el_snapshot_writer_job(None))

        self.assertEqual(self._count_rows(), 1)
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM el_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row["account_id"], "U21971297")
        self.assertEqual(row["nlv"], 150000.0)
        self.assertEqual(row["excess_liquidity"], 45000.0)

    @patch("telegram_bot.AUTHORIZED_USER_ID", 999999)
    @patch("telegram_bot.ACTIVE_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U21971297": "Yash_Household"})
    @patch("telegram_bot.MARGIN_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot._apex_last_alert", {})
    @patch("telegram_bot._el_last_write", {})
    @patch("telegram_bot._get_db_connection")
    @patch("telegram_bot.ensure_ib_connected")
    def test_writer_sends_apex_survival_alert(self, mock_ib, mock_db):
        import asyncio
        from telegram_bot import _el_snapshot_writer_job

        mock_db.side_effect = lambda: self._get_conn()
        mock_conn = AsyncMock()
        mock_conn.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U21971297", tag="NetLiquidation", value="100000"),
            SimpleNamespace(account="U21971297", tag="ExcessLiquidity", value="7000"),
            SimpleNamespace(account="U21971297", tag="BuyingPower", value="14000"),
        ])
        mock_ib.return_value = mock_conn

        bot = AsyncMock()
        context = SimpleNamespace(bot=bot)
        asyncio.get_event_loop().run_until_complete(_el_snapshot_writer_job(context))

        bot.send_message.assert_awaited_once_with(
            chat_id=999999,
            text="[🚨 APEX SURVIVAL: Excess Liquidity < 8%. Executing Tied-Unwinds!]",
        )

    @patch("telegram_bot.AUTHORIZED_USER_ID", 999999)
    @patch("telegram_bot.ACTIVE_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U21971297": "Yash_Household"})
    @patch("telegram_bot.MARGIN_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot._el_last_write", {})
    @patch("telegram_bot._get_db_connection")
    @patch("telegram_bot.ensure_ib_connected")
    def test_writer_debounces_apex_survival_alert_for_15_minutes(self, mock_ib, mock_db):
        import asyncio
        import telegram_bot

        mock_db.side_effect = lambda: self._get_conn()
        mock_conn = AsyncMock()
        mock_conn.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U21971297", tag="NetLiquidation", value="100000"),
            SimpleNamespace(account="U21971297", tag="ExcessLiquidity", value="7000"),
            SimpleNamespace(account="U21971297", tag="BuyingPower", value="14000"),
        ])
        mock_ib.return_value = mock_conn

        telegram_bot._apex_last_alert = {"U21971297": time.time()}
        bot = AsyncMock()
        context = SimpleNamespace(bot=bot)
        asyncio.get_event_loop().run_until_complete(telegram_bot._el_snapshot_writer_job(context))

        bot.send_message.assert_not_awaited()
        self.assertEqual(self._count_rows(), 0)

    @patch("telegram_bot.AUTHORIZED_USER_ID", 999999)
    @patch("telegram_bot.ACTIVE_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U21971297": "Yash_Household"})
    @patch("telegram_bot.MARGIN_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot._el_last_write", {})
    @patch("telegram_bot._get_db_connection")
    @patch("telegram_bot.ensure_ib_connected")
    def test_writer_clears_apex_lock_after_recovery(self, mock_ib, mock_db):
        import asyncio
        import telegram_bot

        mock_db.side_effect = lambda: self._get_conn()
        mock_conn = AsyncMock()
        mock_conn.accountSummaryAsync = AsyncMock(side_effect=[
            [
                SimpleNamespace(account="U21971297", tag="NetLiquidation", value="100000"),
                SimpleNamespace(account="U21971297", tag="ExcessLiquidity", value="20000"),
                SimpleNamespace(account="U21971297", tag="BuyingPower", value="40000"),
            ],
            [
                SimpleNamespace(account="U21971297", tag="NetLiquidation", value="100000"),
                SimpleNamespace(account="U21971297", tag="ExcessLiquidity", value="7000"),
                SimpleNamespace(account="U21971297", tag="BuyingPower", value="14000"),
            ],
        ])
        mock_ib.return_value = mock_conn

        telegram_bot._apex_last_alert = {"U21971297": time.time()}
        bot = AsyncMock()
        context = SimpleNamespace(bot=bot)

        asyncio.get_event_loop().run_until_complete(telegram_bot._el_snapshot_writer_job(context))
        self.assertNotIn("U21971297", telegram_bot._apex_last_alert)

        asyncio.get_event_loop().run_until_complete(telegram_bot._el_snapshot_writer_job(context))
        bot.send_message.assert_awaited_once_with(
            chat_id=999999,
            text="[🚨 APEX SURVIVAL: Excess Liquidity < 8%. Executing Tied-Unwinds!]",
        )

    @patch("telegram_bot.AUTHORIZED_USER_ID", 999999)
    @patch("telegram_bot.ACTIVE_ACCOUNTS", ["U22076329"])
    @patch("telegram_bot.MARGIN_ACCOUNTS", ["U21971297", "U22388499"])
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U22076329": "Yash_Household"})
    @patch("telegram_bot._el_last_write", {})
    @patch("telegram_bot._get_db_connection")
    @patch("telegram_bot.ensure_ib_connected")
    def test_writer_skips_apex_survival_for_cash_accounts(self, mock_ib, mock_db):
        import asyncio
        import telegram_bot

        mock_db.side_effect = lambda: self._get_conn()
        mock_conn = AsyncMock()
        mock_conn.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U22076329", tag="NetLiquidation", value="100000"),
            SimpleNamespace(account="U22076329", tag="ExcessLiquidity", value="2000"),
            SimpleNamespace(account="U22076329", tag="BuyingPower", value="2000"),
        ])
        mock_ib.return_value = mock_conn

        telegram_bot._apex_last_alert = {"U22076329": time.time()}
        bot = AsyncMock()
        context = SimpleNamespace(bot=bot)
        asyncio.get_event_loop().run_until_complete(telegram_bot._el_snapshot_writer_job(context))

        bot.send_message.assert_not_awaited()
        self.assertNotIn("U22076329", telegram_bot._apex_last_alert)
        self.assertEqual(self._count_rows(), 1)

    @patch("telegram_bot.ACTIVE_ACCOUNTS", ["U21971297"])
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U21971297": "Yash_Household"})
    @patch("telegram_bot._get_db_connection")
    @patch("telegram_bot.ensure_ib_connected")
    def test_writer_debounces_30s(self, mock_ib, mock_db):
        import asyncio
        import telegram_bot

        mock_db.side_effect = lambda: self._get_conn()
        mock_conn = AsyncMock()
        mock_conn.accountSummaryAsync = AsyncMock(return_value=[
            SimpleNamespace(account="U21971297", tag="NetLiquidation", value="150000"),
            SimpleNamespace(account="U21971297", tag="ExcessLiquidity", value="45000"),
        ])
        mock_ib.return_value = mock_conn

        # Set last write to now (simulates recent write)
        telegram_bot._el_last_write = {"U21971297": time.time()}

        asyncio.get_event_loop().run_until_complete(telegram_bot._el_snapshot_writer_job(None))

        # Should have 0 rows — debounced
        self.assertEqual(self._count_rows(), 0)

        # Reset debounce
        telegram_bot._el_last_write = {}


class TestHealthStripReader(unittest.TestCase):
    """Test get_health_strip_data() fallback chain."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_reader_falls_back_to_db(self):
        from agt_deck.queries import get_health_strip_data
        # Seed a fresh el_snapshot
        self.conn.execute(
            "INSERT INTO el_snapshots (account_id, household, excess_liquidity, nlv, buying_power) "
            "VALUES ('U21971297', 'Yash_Household', 45000, 150000, 90000)"
        )
        self.conn.commit()

        data = get_health_strip_data(self.conn)
        yash = next(a for a in data["accounts"] if a["account_id"] == "U21971297")
        self.assertEqual(yash["nlv"], 150000.0)
        self.assertEqual(yash["excess_liquidity"], 45000.0)
        self.assertAlmostEqual(yash["el_pct"], 30.0, places=0)

    def test_reader_returns_null_when_empty(self):
        from agt_deck.queries import get_health_strip_data
        data = get_health_strip_data(self.conn)
        for acct in data["accounts"]:
            self.assertIsNone(acct["nlv"])
            self.assertTrue(acct["is_stale"])

    def test_schema_migration_adds_account_id(self):
        """Verify account_id column exists after migration."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(el_snapshots)").fetchall()]
        self.assertIn("account_id", cols)


if __name__ == "__main__":
    unittest.main()
