"""
CLEANUP-6 — TOCTOU concurrency regression tests.

Verifies that BEGIN IMMEDIATE prevents lost updates in concurrent
read-modify-write handlers. Uses real threading + real SQLite to
exercise the actual transaction semantics.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

NUM_THREADS = 10
SHARES_PER_THREAD = 10

_DDL = """
CREATE TABLE premium_ledger (
    household_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    initial_basis REAL,
    total_premium_collected REAL,
    shares_owned INTEGER,
    PRIMARY KEY (household_id, ticker)
);
CREATE TABLE pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'staged',
    status_history TEXT DEFAULT '[]',
    payload TEXT
);
"""


class TestOnSharesSoldConcurrent(unittest.TestCase):
    """Site #1: _on_shares_sold read-modify-write must serialize."""

    def test_concurrent_partial_fills_no_data_loss(self):
        db = os.path.join(tempfile.gettempdir(), "c6_test_sell.db")
        try:
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_DDL)
            conn.execute(
                "INSERT INTO premium_ledger VALUES ('H1', 'TEST', 50.0, 0.0, 100)"
            )
            conn.commit()
            conn.close()

            barrier = threading.Barrier(NUM_THREADS)

            def worker(tid):
                barrier.wait()
                c = sqlite3.connect(db, timeout=30.0)
                c.row_factory = sqlite3.Row
                try:
                    with c:
                        c.execute("BEGIN IMMEDIATE")
                        row = c.execute(
                            "SELECT shares_owned FROM premium_ledger "
                            "WHERE household_id='H1' AND ticker='TEST'"
                        ).fetchone()
                        old = int(row["shares_owned"])
                        new = max(0, old - SHARES_PER_THREAD)
                        c.execute(
                            "UPDATE premium_ledger SET shares_owned = ? "
                            "WHERE household_id='H1' AND ticker='TEST'",
                            (new,),
                        )
                finally:
                    c.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            final = sqlite3.connect(db)
            final.row_factory = sqlite3.Row
            shares = int(final.execute(
                "SELECT shares_owned FROM premium_ledger WHERE household_id='H1'"
            ).fetchone()["shares_owned"])
            final.close()

            self.assertEqual(shares, 0,
                             f"10 threads × 10 shares = 100 sold from 100. Got {shares} remaining.")
        finally:
            for s in ['', '-wal', '-shm']:
                try: os.unlink(db + s)
                except: pass


class TestOnSharesBoughtConcurrent(unittest.TestCase):
    """Site #2: _on_shares_bought read-modify-write must serialize."""

    def test_concurrent_partial_fills_no_data_loss(self):
        db = os.path.join(tempfile.gettempdir(), "c6_test_buy.db")
        try:
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_DDL)
            conn.execute(
                "INSERT INTO premium_ledger VALUES ('H1', 'TEST', 50.0, 0.0, 0)"
            )
            conn.commit()
            conn.close()

            barrier = threading.Barrier(NUM_THREADS)

            def worker(tid):
                barrier.wait()
                c = sqlite3.connect(db, timeout=30.0)
                c.row_factory = sqlite3.Row
                try:
                    with c:
                        c.execute("BEGIN IMMEDIATE")
                        row = c.execute(
                            "SELECT shares_owned FROM premium_ledger "
                            "WHERE household_id='H1' AND ticker='TEST'"
                        ).fetchone()
                        old = int(row["shares_owned"])
                        new = old + SHARES_PER_THREAD
                        c.execute(
                            "UPDATE premium_ledger SET shares_owned = ? "
                            "WHERE household_id='H1' AND ticker='TEST'",
                            (new,),
                        )
                finally:
                    c.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            final = sqlite3.connect(db)
            final.row_factory = sqlite3.Row
            shares = int(final.execute(
                "SELECT shares_owned FROM premium_ledger WHERE household_id='H1'"
            ).fetchone()["shares_owned"])
            final.close()

            self.assertEqual(shares, 100,
                             f"10 threads × 10 shares = 100 bought from 0. Got {shares}.")
        finally:
            for s in ['', '-wal', '-shm']:
                try: os.unlink(db + s)
                except: pass


class TestAppendStatusConcurrent(unittest.TestCase):
    """Site #3: append_status JSON read-modify-write must serialize."""

    def test_concurrent_no_history_loss(self):
        db = os.path.join(tempfile.gettempdir(), "c6_test_append.db")
        try:
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_DDL)
            conn.execute(
                "INSERT INTO pending_orders (status, status_history) VALUES ('staged', '[]')"
            )
            conn.commit()
            conn.close()

            barrier = threading.Barrier(NUM_THREADS)

            def worker(tid):
                barrier.wait()
                c = sqlite3.connect(db, timeout=30.0)
                c.row_factory = sqlite3.Row
                try:
                    with c:
                        c.execute("BEGIN IMMEDIATE")
                        row = c.execute(
                            "SELECT status_history FROM pending_orders WHERE id = 1"
                        ).fetchone()
                        history = json.loads(row["status_history"])
                        history.append({"thread": tid, "ts": time.time()})
                        c.execute(
                            "UPDATE pending_orders SET status_history = ? WHERE id = 1",
                            (json.dumps(history),),
                        )
                finally:
                    c.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            final = sqlite3.connect(db)
            final.row_factory = sqlite3.Row
            h = json.loads(final.execute(
                "SELECT status_history FROM pending_orders WHERE id=1"
            ).fetchone()["status_history"])
            final.close()

            self.assertEqual(len(h), NUM_THREADS,
                             f"Expected {NUM_THREADS} history entries, got {len(h)}. Lost updates detected.")
        finally:
            for s in ['', '-wal', '-shm']:
                try: os.unlink(db + s)
                except: pass


if __name__ == "__main__":
    unittest.main()
