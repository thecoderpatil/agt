"""Sprint 1B: Tests for get_lifecycle_rows() query."""
import sqlite3
import time
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _create_tables(conn):
    """Minimal bucket3_dynamic_exit_log for lifecycle tests."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_log (
            audit_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            household TEXT NOT NULL,
            action_type TEXT NOT NULL DEFAULT 'CC',
            contracts INTEGER,
            shares INTEGER,
            strike REAL,
            expiry TEXT,
            limit_price REAL,
            final_status TEXT NOT NULL DEFAULT 'STAGED',
            desk_mode TEXT NOT NULL DEFAULT 'PEACETIME',
            staged_ts REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            transmitted_ts REAL,
            fill_price REAL,
            fill_qty INTEGER,
            fill_ts REAL,
            originating_account_id TEXT,
            re_validation_count INTEGER DEFAULT 0,
            exception_type TEXT
        )
    """)
    conn.commit()


def _insert(conn, audit_id, status, ticker="AAPL", staged_ts=None, transmitted_ts=None, **kw):
    now = time.time()
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, ticker, household, action_type, final_status, staged_ts, transmitted_ts, "
        " desk_mode, contracts, strike, limit_price) "
        "VALUES (?, ?, 'Yash_Household', 'CC', ?, ?, ?, 'PEACETIME', 1, 100.0, 2.50)",
        (audit_id, ticker, status, staged_ts or now, transmitted_ts),
    )
    conn.commit()


class TestLifecycleQuery(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_returns_staged_rows(self):
        from agt_deck.queries import get_lifecycle_rows
        _insert(self.conn, "s1", "STAGED")
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_status"], "STAGED")

    def test_returns_attested_rows(self):
        from agt_deck.queries import get_lifecycle_rows
        _insert(self.conn, "a1", "ATTESTED")
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_status"], "ATTESTED")

    def test_returns_transmitting_rows(self):
        from agt_deck.queries import get_lifecycle_rows
        now = time.time()
        _insert(self.conn, "t1", "TRANSMITTING", transmitted_ts=now)
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_status"], "TRANSMITTING")
        self.assertFalse(rows[0]["is_orphan"])

    def test_returns_recent_transmitted_within_5m(self):
        from agt_deck.queries import get_lifecycle_rows
        now = time.time()
        _insert(self.conn, "d1", "TRANSMITTED", transmitted_ts=now - 60)
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_status"], "TRANSMITTED")

    def test_excludes_transmitted_older_than_5m(self):
        from agt_deck.queries import get_lifecycle_rows
        now = time.time()
        _insert(self.conn, "d2", "TRANSMITTED", transmitted_ts=now - 400)
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(len(rows), 0)

    def test_orphans_sorted_first(self):
        from agt_deck.queries import get_lifecycle_rows
        now = time.time()
        _insert(self.conn, "normal", "STAGED", staged_ts=now)
        _insert(self.conn, "orphan", "TRANSMITTING", transmitted_ts=now - 700)
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["is_orphan"])
        self.assertEqual(rows[0]["audit_id"], "orphan")

    def test_empty_queue_returns_empty_list(self):
        from agt_deck.queries import get_lifecycle_rows
        rows = get_lifecycle_rows(self.conn)
        self.assertEqual(rows, [])

    def test_ordering_by_state_priority(self):
        from agt_deck.queries import get_lifecycle_rows
        now = time.time()
        _insert(self.conn, "tx", "TRANSMITTED", transmitted_ts=now - 30)
        _insert(self.conn, "at", "ATTESTED")
        _insert(self.conn, "st", "STAGED")
        _insert(self.conn, "xm", "TRANSMITTING", transmitted_ts=now)
        rows = get_lifecycle_rows(self.conn)
        statuses = [r["final_status"] for r in rows]
        self.assertEqual(statuses, ["STAGED", "ATTESTED", "TRANSMITTING", "TRANSMITTED"])

    def test_handles_db_error_gracefully(self):
        from agt_deck.queries import get_lifecycle_rows
        self.conn.close()
        bad_conn = sqlite3.connect(":memory:")
        # No table created — should return []
        rows = get_lifecycle_rows(bad_conn)
        self.assertEqual(rows, [])
        bad_conn.close()


if __name__ == "__main__":
    unittest.main()
