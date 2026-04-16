"""Sprint 1A: Tests for unified _pre_trade_gates() helper."""
import asyncio
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    """Run async coroutine synchronously for test assertions."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_order(qty=1, lmt_price=5.0):
    return SimpleNamespace(totalQuantity=qty, lmtPrice=lmt_price)


def _mock_contract(sec_type="OPT", strike=100.0):
    return SimpleNamespace(secType=sec_type, strike=strike)


class TestModeGate(unittest.TestCase):
    """Gate 1: WARTIME blocks non-DEX sites."""

    @patch("telegram_bot._get_current_desk_mode", return_value="WARTIME")
    def test_wartime_blocks_orders_match_mid(self, _mock):
        from telegram_bot import _pre_trade_gates
        ok, reason = _run(_pre_trade_gates(
            _mock_order(), _mock_contract(),
            {"site": "orders_match_mid", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("WARTIME", reason)

    @patch("telegram_bot._get_current_desk_mode", return_value="WARTIME")
    def test_wartime_allows_dex(self, _mock):
        from telegram_bot import _pre_trade_gates
        ok, reason = _run(_pre_trade_gates(
            _mock_order(), _mock_contract(strike=100.0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_peacetime_allows_all_sites(self, _mock):
        from telegram_bot import _pre_trade_gates
        for site in ("dex", "orders_match_mid", "legacy_approve"):
            ok, reason = _run(_pre_trade_gates(
                _mock_order(), _mock_contract(strike=100.0),
                {"site": site, "audit_id": None, "household": "Yash_Household"},
            ))
            self.assertTrue(ok, f"PEACETIME should allow {site}, got: {reason}")

    @patch("telegram_bot._get_current_desk_mode", return_value="AMBER")
    def test_amber_allows_all_sites(self, _mock):
        from telegram_bot import _pre_trade_gates
        for site in ("dex", "orders_match_mid", "legacy_approve"):
            ok, reason = _run(_pre_trade_gates(
                _mock_order(), _mock_contract(strike=100.0),
                {"site": site, "audit_id": None, "household": "Yash_Household"},
            ))
            self.assertTrue(ok, f"AMBER should allow {site}, got: {reason}")


class TestNotionalGate(unittest.TestCase):
    """Gate 2: notional calculation (ceiling removed 2026-04-16)."""

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_opt_above_ceiling(self, _mock):
        from telegram_bot import _pre_trade_gates
        # 2 contracts * $250 strike * 100 = $50,000
        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=2, lmt_price=3.50),
            _mock_contract(sec_type="OPT", strike=250.0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("50,000", reason)
        self.assertIn("25,000", reason)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_opt_below_ceiling(self, _mock):
        from telegram_bot import _pre_trade_gates
        # 1 contract * $100 strike * 100 = $10,000
        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=1, lmt_price=2.00),
            _mock_contract(sec_type="OPT", strike=100.0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertTrue(ok)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_stk_above_ceiling(self, _mock):
        from telegram_bot import _pre_trade_gates
        # 100 shares * $300 = $30,000
        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=100, lmt_price=300.0),
            _mock_contract(sec_type="STK", strike=0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("30,000", reason)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_missing_price_fails_closed(self, _mock):
        from telegram_bot import _pre_trade_gates
        # STK with no lmtPrice
        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=100, lmt_price=0),
            _mock_contract(sec_type="STK", strike=0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("fail-closed", reason)


class TestF20NullGuard(unittest.TestCase):
    """Gate 4: F20 NULL originating_account_id guard."""

    def setUp(self):
        import tempfile, os
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY,
                originating_account_id TEXT,
                final_status TEXT DEFAULT 'ATTESTED',
                last_updated TEXT
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        import os
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    @patch("telegram_bot._get_db_connection")
    def test_f20_blocks_null_originating_account(self, mock_db, _mock_mode):
        from telegram_bot import _pre_trade_gates
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log (audit_id, originating_account_id, final_status) "
            "VALUES ('test-audit-123', NULL, 'ATTESTED')"
        )
        conn.commit()
        conn.close()
        # Gate will call _get_db_connection() which returns a fresh connection
        mock_db.side_effect = lambda: self._get_conn()

        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=1, lmt_price=2.00),
            _mock_contract(sec_type="OPT", strike=100.0),
            {"site": "dex", "audit_id": "test-audit-123", "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("F20 NULL guard", reason)

        # Verify side effect: row flipped to CANCELLED (use fresh conn)
        verify_conn = self._get_conn()
        row = verify_conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log WHERE audit_id = 'test-audit-123'"
        ).fetchone()
        verify_conn.close()
        self.assertEqual(row["final_status"], "CANCELLED")

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_f20_not_applied_for_non_dex(self, _mock_mode):
        from telegram_bot import _pre_trade_gates
        # audit_id=None → F20 gate skipped entirely
        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=1, lmt_price=2.00),
            _mock_contract(sec_type="OPT", strike=100.0),
            {"site": "legacy_approve", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertTrue(ok)


class TestGateExceptionFailsClosed(unittest.TestCase):
    """Outer try/except: any exception → block."""

    @patch("telegram_bot._get_current_desk_mode", side_effect=RuntimeError("DB exploded"))
    def test_gate_exception_fails_closed(self, _mock):
        from telegram_bot import _pre_trade_gates
        ok, reason = _run(_pre_trade_gates(
            _mock_order(), _mock_contract(),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertFalse(ok)
        self.assertIn("gate error", reason)


class TestHappyPath(unittest.TestCase):
    """All gates pass."""

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    def test_happy_path_all_gates_pass(self, _mock):
        from telegram_bot import _pre_trade_gates
        # ADBE CC qty=1 strike=$245 → notional $24,500 → passes
        ok, reason = _run(_pre_trade_gates(
            _mock_order(qty=1, lmt_price=3.50),
            _mock_contract(sec_type="OPT", strike=245.0),
            {"site": "dex", "audit_id": None, "household": "Yash_Household"},
        ))
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
