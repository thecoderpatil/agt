"""Tests for R5 order state machine."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.order_state import (
    OrderStatus, append_status, backfill_status_history,
    VALID_TRANSITIONS, TERMINAL_STATES, IBKR_STATUS_MAP,
)


class TestOrderStatus(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(tempfile.gettempdir(), 'test_order_state.db')
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ib_order_id INTEGER,
                ib_perm_id INTEGER,
                status_history TEXT,
                fill_price REAL,
                fill_qty INTEGER,
                fill_commission REAL,
                fill_time TEXT,
                last_ib_status TEXT
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def _insert_order(self, status='staged'):
        history = json.dumps([{"status": status, "at": "2026-04-07T00:00:00", "by": "test"}])
        self.conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at, status_history) "
            "VALUES (?, ?, datetime('now'), ?)",
            ('{}', status, history),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_append_status_basic(self):
        oid = self._insert_order('staged')
        ok = append_status(self.conn, oid, 'processing', 'test')
        self.conn.commit()
        self.assertTrue(ok)
        row = self.conn.execute("SELECT status, status_history FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'processing')
        history = json.loads(row['status_history'])
        self.assertGreaterEqual(len(history), 2)  # initial + processing
        self.assertEqual(history[-1]['status'], 'processing')
        self.assertEqual(history[-1]['by'], 'test')

    def test_append_status_chain(self):
        """Full lifecycle: staged → processing → sent → acked → working → filled."""
        oid = self._insert_order('staged')
        for status in ['processing', 'sent', 'acked', 'working', 'filled']:
            append_status(self.conn, oid, status, f'test_{status}')
        self.conn.commit()
        row = self.conn.execute("SELECT status, status_history FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'filled')
        history = json.loads(row['status_history'])
        self.assertGreaterEqual(len(history), 6)  # initial + 5

    def test_terminal_state_blocks_further(self):
        """Once FILLED, no further transitions accepted."""
        oid = self._insert_order('filled')
        # Try to move filled → cancelled
        ok = append_status(self.conn, oid, 'cancelled', 'test')
        self.assertFalse(ok)
        row = self.conn.execute("SELECT status FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'filled')

    def test_monotonic_guard_logs_invalid(self):
        """Invalid transition (sent → staged) is logged but still applied for robustness."""
        oid = self._insert_order('sent')
        # This is an invalid backward transition
        ok = append_status(self.conn, oid, 'staged', 'test')
        self.conn.commit()
        # append_status allows it (for robustness) but logs warning
        # The status is updated
        row = self.conn.execute("SELECT status FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'staged')

    def test_ibkr_status_mapping(self):
        """Verify IBKR status strings map to correct OrderStatus values."""
        self.assertEqual(IBKR_STATUS_MAP['PreSubmitted'], OrderStatus.ACKED)
        self.assertEqual(IBKR_STATUS_MAP['Submitted'], OrderStatus.WORKING)
        self.assertEqual(IBKR_STATUS_MAP['Filled'], OrderStatus.FILLED)
        self.assertEqual(IBKR_STATUS_MAP['Cancelled'], OrderStatus.CANCELLED)
        self.assertEqual(IBKR_STATUS_MAP['Inactive'], OrderStatus.REJECTED)

    def test_backfill_renames_approved(self):
        """Backfill converts 'approved' → 'sent'."""
        self.conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) "
            "VALUES ('{}', 'approved', datetime('now'))"
        )
        self.conn.commit()
        count = backfill_status_history(self.conn)
        self.conn.commit()
        self.assertEqual(count, 1)
        row = self.conn.execute("SELECT status FROM pending_orders").fetchone()
        self.assertEqual(row['status'], 'sent')

    def test_orphan_order_not_found(self):
        """append_status on nonexistent order returns False."""
        ok = append_status(self.conn, 99999, 'filled', 'test')
        self.assertFalse(ok)

    def test_end_to_end_filled_lifecycle(self):
        """stage → processing → sent → acked → working → filled."""
        oid = self._insert_order('staged')
        transitions = [
            ('processing', '/approve'),
            ('sent', 'placeOrder'),
            ('acked', 'orderStatusEvent'),
            ('working', 'orderStatusEvent'),
            ('filled', 'execDetailsEvent'),
        ]
        for status, source in transitions:
            ok = append_status(self.conn, oid, status, source)
            self.assertTrue(ok, f"Transition to {status} failed")
        self.conn.commit()
        row = self.conn.execute("SELECT status, status_history FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'filled')
        history = json.loads(row['status_history'])
        self.assertGreaterEqual(len(history), 6)  # initial + 5 transitions
        statuses = [h['status'] for h in history]
        self.assertEqual(statuses[-5:],
                         ['processing', 'sent', 'acked', 'working', 'filled'])

    def test_reject_lifecycle(self):
        """stage → processing → sent → rejected."""
        oid = self._insert_order('staged')
        for status, source in [('processing', '/approve'), ('sent', 'placeOrder'), ('rejected', 'orderStatusEvent')]:
            append_status(self.conn, oid, status, source)
        self.conn.commit()
        row = self.conn.execute("SELECT status FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'rejected')

    def test_cancel_lifecycle(self):
        """stage → processing → sent → acked → cancelled."""
        oid = self._insert_order('staged')
        for status in ['processing', 'sent', 'acked', 'cancelled']:
            append_status(self.conn, oid, status, 'test')
        self.conn.commit()
        row = self.conn.execute("SELECT status FROM pending_orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row['status'], 'cancelled')

    def test_partial_fill_lifecycle(self):
        """sent → acked → working → partially_filled → filled."""
        oid = self._insert_order('sent')
        for status in ['acked', 'working', 'partially_filled', 'filled']:
            append_status(self.conn, oid, status, 'test')
        self.conn.commit()
        row = self.conn.execute(
            "SELECT status, status_history FROM pending_orders WHERE id=?", (oid,)
        ).fetchone()
        self.assertEqual(row['status'], 'filled')
        history = json.loads(row['status_history'])
        self.assertGreaterEqual(len(history), 5)  # initial + 4


if __name__ == '__main__':
    unittest.main()
