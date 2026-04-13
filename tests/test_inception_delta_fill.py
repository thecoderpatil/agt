"""Sprint-1.3: inception_delta extraction at fill time via permId join.

Tests verify the full loop: pending_orders.payload (with inception_delta)
→ _lookup_inception_delta_from_payload (permId join) → _apply_fill_atomically
→ fill_log.inception_delta column.

Each test creates an in-memory SQLite DB with the production schema,
optionally inserts a pending_orders row, then exercises the helper
and/or _apply_fill_atomically directly.
"""
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_equities.schema import register_operational_tables, register_master_log_tables


def _init_test_db(db_path):
    """Create and initialize a test DB at the given file path.

    Must call BOTH register_operational_tables (creates pending_orders
    base table + fill_log) AND register_master_log_tables (extends
    pending_orders with ib_perm_id and other R5 lifecycle columns).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_operational_tables(conn)
    register_master_log_tables(conn)
    conn.commit()
    conn.close()


def _insert_pending_order(conn, ib_perm_id, payload_obj):
    """Insert a pending_orders row with given permId and payload."""
    if isinstance(payload_obj, str):
        payload_str = payload_obj  # allow raw string for malformed JSON test
    else:
        payload_str = json.dumps(payload_obj)
    conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at, ib_perm_id) "
        "VALUES (?, 'staged', datetime('now'), ?)",
        (payload_str, ib_perm_id),
    )
    conn.commit()


def _read_fill_log_inception_delta(conn, exec_id):
    """Read inception_delta from fill_log for a given exec_id."""
    row = conn.execute(
        "SELECT inception_delta FROM fill_log WHERE exec_id = ?",
        (exec_id,),
    ).fetchone()
    if row is None:
        return "NO_ROW"
    return row[0]


class TestLookupInceptionDelta(unittest.TestCase):
    """Test _lookup_inception_delta_from_payload in isolation."""

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()
        _init_test_db(self._db_path)
        # Keep a reader connection for assertions
        self.conn = sqlite3.connect(self._db_path)
        self.conn.row_factory = sqlite3.Row

        def _make_conn():
            c = sqlite3.connect(self._db_path)
            c.row_factory = sqlite3.Row
            return c

        self._patcher = patch(
            "telegram_bot._get_db_connection",
            side_effect=_make_conn,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()
        os.unlink(self._db_path)

    def test_7a_happy_path_returns_float(self):
        """permId match with valid inception_delta → returns float."""
        from telegram_bot import _lookup_inception_delta_from_payload
        _insert_pending_order(self.conn, 12345, {
            "inception_delta": 0.27, "ticker": "CRM",
        })
        result = _lookup_inception_delta_from_payload(12345)
        self.assertAlmostEqual(result, 0.27)
        self.assertIsInstance(result, float)

    def test_7b_explicit_none_returns_none(self):
        """Payload with inception_delta: null → returns None."""
        from telegram_bot import _lookup_inception_delta_from_payload
        _insert_pending_order(self.conn, 12346, {
            "inception_delta": None, "ticker": "CRM",
        })
        result = _lookup_inception_delta_from_payload(12346)
        self.assertIsNone(result)

    def test_7c_missing_key_returns_none(self):
        """Pre-sprint-1.2 payload without inception_delta key → None."""
        from telegram_bot import _lookup_inception_delta_from_payload
        _insert_pending_order(self.conn, 12347, {
            "ticker": "CRM", "strike": 110.0,
        })
        result = _lookup_inception_delta_from_payload(12347)
        self.assertIsNone(result)

    def test_7d_no_matching_row_returns_none(self):
        """No pending_orders row for permId → returns None, logs info."""
        from telegram_bot import _lookup_inception_delta_from_payload
        with self.assertLogs("agt_bridge", level="INFO") as cm:
            result = _lookup_inception_delta_from_payload(99999)
        self.assertIsNone(result)
        self.assertTrue(any("no pending_orders match" in m for m in cm.output))

    def test_7e_malformed_json_returns_none(self):
        """Non-JSON payload → returns None, logs warning."""
        from telegram_bot import _lookup_inception_delta_from_payload
        _insert_pending_order(self.conn, 12348, "not valid json {{{")
        with self.assertLogs("agt_bridge", level="WARNING") as cm:
            result = _lookup_inception_delta_from_payload(12348)
        self.assertIsNone(result)
        self.assertTrue(any("payload lookup failed" in m for m in cm.output))

    def test_7f_non_float_value_returns_none(self):
        """inception_delta="not_a_float" → returns None, logs warning."""
        from telegram_bot import _lookup_inception_delta_from_payload
        _insert_pending_order(self.conn, 12349, {
            "inception_delta": "not_a_float", "ticker": "CRM",
        })
        with self.assertLogs("agt_bridge", level="WARNING") as cm:
            result = _lookup_inception_delta_from_payload(12349)
        self.assertIsNone(result)
        self.assertTrue(any("malformed inception_delta" in m for m in cm.output))

    def test_7g_perm_id_none_short_circuits(self):
        """permId=None → returns None without DB query."""
        from telegram_bot import _lookup_inception_delta_from_payload
        result = _lookup_inception_delta_from_payload(None)
        self.assertIsNone(result)

    def test_7g_perm_id_zero_short_circuits(self):
        """permId=0 → returns None without DB query."""
        from telegram_bot import _lookup_inception_delta_from_payload
        result = _lookup_inception_delta_from_payload(0)
        self.assertIsNone(result)


class TestApplyFillAtomicallyWithInceptionDelta(unittest.TestCase):
    """Test _apply_fill_atomically writes inception_delta to fill_log."""

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()
        _init_test_db(self._db_path)
        self.conn = sqlite3.connect(self._db_path)
        self.conn.row_factory = sqlite3.Row

        def _make_conn():
            c = sqlite3.connect(self._db_path)
            c.row_factory = sqlite3.Row
            return c

        self._patcher = patch(
            "telegram_bot._get_db_connection",
            side_effect=_make_conn,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()
        os.unlink(self._db_path)

    def test_fill_log_records_inception_delta_float(self):
        """_apply_fill_atomically with inception_delta=0.27 writes to fill_log."""
        from telegram_bot import _apply_fill_atomically
        result = _apply_fill_atomically(
            "exec-001", "CRM", "SELL_CALL", 1, 2.50, 250.0,
            "U12345", "test_hh",
            inception_delta=0.27,
        )
        self.assertTrue(result)
        val = _read_fill_log_inception_delta(self.conn, "exec-001")
        self.assertAlmostEqual(val, 0.27)

    def test_fill_log_records_none_inception_delta(self):
        """_apply_fill_atomically with inception_delta=None writes NULL."""
        from telegram_bot import _apply_fill_atomically
        result = _apply_fill_atomically(
            "exec-002", "CRM", "SELL_CALL", 1, 2.50, 250.0,
            "U12345", "test_hh",
            inception_delta=None,
        )
        self.assertTrue(result)
        val = _read_fill_log_inception_delta(self.conn, "exec-002")
        self.assertIsNone(val)

    def test_fill_log_default_inception_delta_is_null(self):
        """Existing callers (no inception_delta kwarg) write NULL."""
        from telegram_bot import _apply_fill_atomically
        result = _apply_fill_atomically(
            "exec-003", "MSFT", "SELL_PUT", 1, 3.00, 300.0,
            "U12345", "test_hh",
        )
        self.assertTrue(result)
        val = _read_fill_log_inception_delta(self.conn, "exec-003")
        self.assertIsNone(val)


class TestOnCcFillIntegration(unittest.TestCase):
    """Integration: _on_cc_fill extracts inception_delta from payload
    and threads it through to fill_log."""

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()
        _init_test_db(self._db_path)
        self.conn = sqlite3.connect(self._db_path)
        self.conn.row_factory = sqlite3.Row

        def _make_conn():
            c = sqlite3.connect(self._db_path)
            c.row_factory = sqlite3.Row
            return c

        self._patcher = patch(
            "telegram_bot._get_db_connection",
            side_effect=_make_conn,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()
        os.unlink(self._db_path)

    def _make_trade_fill(self, perm_id, exec_id="exec-100",
                         ticker="CRM", strike=110.0, price=2.50,
                         shares=1, account="U12345"):
        """Build synthetic (trade, fill) tuple matching ib_async shape."""
        contract = SimpleNamespace(
            symbol=ticker, secType="OPT", right="C",
            strike=strike, lastTradeDateOrContractMonth="20260515",
        )
        order = SimpleNamespace(
            action="SELL", account=account,
            permId=perm_id, orderId=999,
        )
        order_status = SimpleNamespace(remaining=0)
        execution = SimpleNamespace(
            execId=exec_id, price=price, shares=shares,
            acctNumber=account,
        )
        fill = SimpleNamespace(execution=execution)
        trade = SimpleNamespace(
            contract=contract, order=order, orderStatus=order_status,
        )
        return trade, fill

    def test_full_loop_with_inception_delta(self):
        """pending_orders payload with inception_delta=0.27 → fill_log gets 0.27."""
        from telegram_bot import _on_cc_fill, ACCOUNT_TO_HOUSEHOLD
        _insert_pending_order(self.conn, 55555, {
            "inception_delta": 0.27, "ticker": "CRM", "strike": 110.0,
        })
        trade, fill = self._make_trade_fill(perm_id=55555, exec_id="exec-200")
        with patch.dict(ACCOUNT_TO_HOUSEHOLD, {"U12345": "test_hh"}):
            _on_cc_fill(trade, fill)
        val = _read_fill_log_inception_delta(self.conn, "exec-200")
        self.assertAlmostEqual(val, 0.27)

    def test_full_loop_no_pending_orders_row(self):
        """No pending_orders match → fill_log still inserted with NULL."""
        from telegram_bot import _on_cc_fill, ACCOUNT_TO_HOUSEHOLD
        trade, fill = self._make_trade_fill(perm_id=88888, exec_id="exec-201")
        with patch.dict(ACCOUNT_TO_HOUSEHOLD, {"U12345": "test_hh"}):
            _on_cc_fill(trade, fill)
        val = _read_fill_log_inception_delta(self.conn, "exec-201")
        self.assertIsNone(val)

    def test_full_loop_perm_id_none(self):
        """trade.order.permId=None → fill_log still inserted with NULL."""
        from telegram_bot import _on_cc_fill, ACCOUNT_TO_HOUSEHOLD
        trade, fill = self._make_trade_fill(perm_id=None, exec_id="exec-202")
        with patch.dict(ACCOUNT_TO_HOUSEHOLD, {"U12345": "test_hh"}):
            _on_cc_fill(trade, fill)
        val = _read_fill_log_inception_delta(self.conn, "exec-202")
        self.assertIsNone(val)


if __name__ == "__main__":
    unittest.main()
