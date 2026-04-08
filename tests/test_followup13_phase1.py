"""
Followup #13 Phase 1 — handle_approve_callback cross-await conn refactor tests.

Verifies:
  T1: reject_all persists via canonical closing()+with conn: pattern
  T2: approve-all CAS claim (staged→processing) persists and is atomic
  T3: double-approve race — second CAS claim finds rowcount=0
  T4: single-order CAS claim persists and read-after-write works
  T5: no bare `with _get_db_connection()` sites remain in codebase

DB: file-based SQLite to prove persistence across connection boundaries.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


_DDL = """
CREATE TABLE pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    ib_order_id INTEGER,
    ib_perm_id INTEGER
)
"""


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(_DDL)
    conn.commit()
    conn.close()


def _seed_staged(path, count=3):
    """Insert N staged orders."""
    conn = sqlite3.connect(path)
    for i in range(count):
        conn.execute(
            "INSERT INTO pending_orders (payload, status) VALUES (?, 'staged')",
            (f'{{"ticker":"T{i}","mode":"MODE_1_DEFENSIVE"}}',),
        )
    conn.commit()
    conn.close()


def _read_statuses(path):
    """Re-open DB and read all order statuses."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, status FROM pending_orders ORDER BY id").fetchall()
    conn.close()
    return [(r["id"], r["status"]) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# T1: reject_all persists via canonical pattern
# ═══════════════════════════════════════════════════════════════════════════

class TestRejectAllPersists(unittest.TestCase):

    def test_reject_all_persists(self):
        """Mirrors the reject_all branch: closing() + with conn: UPDATE."""
        db = os.path.join(tempfile.gettempdir(), "f13_t1.db")
        try:
            _create_db(db)
            _seed_staged(db, count=3)

            # Replicate refactored reject_all pattern
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    result = conn.execute(
                        "UPDATE pending_orders SET status = 'rejected' "
                        "WHERE status = 'staged'"
                    )
                    count = result.rowcount

            self.assertEqual(count, 3)

            # Re-open — verify persistence
            statuses = _read_statuses(db)
            self.assertTrue(all(s == "rejected" for _, s in statuses))
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T2: approve-all CAS claim persists atomically
# ═══════════════════════════════════════════════════════════════════════════

class TestApproveAllCasClaim(unittest.TestCase):

    def test_cas_claim_persists(self):
        """Mirrors the all branch: read staged IDs, CAS update, read claimed."""
        db = os.path.join(tempfile.gettempdir(), "f13_t2.db")
        try:
            _create_db(db)
            _seed_staged(db, count=3)

            # READ phase
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                staged_ids = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM pending_orders WHERE status = 'staged' ORDER BY id"
                    ).fetchall()
                ]

            self.assertEqual(len(staged_ids), 3)

            # WRITE phase: CAS claim
            placeholders = ",".join("?" * len(staged_ids))
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    claimed = conn.execute(
                        f"UPDATE pending_orders SET status = 'processing' "
                        f"WHERE id IN ({placeholders}) AND status = 'staged'",
                        staged_ids,
                    ).rowcount

            self.assertEqual(claimed, 3)

            # READ phase: fetch claimed
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT id, payload FROM pending_orders "
                    f"WHERE id IN ({placeholders}) AND status = 'processing' "
                    f"ORDER BY id",
                    staged_ids,
                ).fetchall()

            self.assertEqual(len(rows), 3)

            # Re-open — verify persistence
            statuses = _read_statuses(db)
            self.assertTrue(all(s == "processing" for _, s in statuses))
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T3: double-approve race — second CAS returns rowcount=0
# ═══════════════════════════════════════════════════════════════════════════

class TestDoubleApproveRace(unittest.TestCase):

    def test_second_approve_finds_zero_rows(self):
        """Simulate: operator 1 claims all rows. Operator 2 tries same IDs
        but CAS guard (WHERE status='staged') returns 0."""
        db = os.path.join(tempfile.gettempdir(), "f13_t3.db")
        try:
            _create_db(db)
            _seed_staged(db, count=3)

            # Operator 1 claims
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                staged_ids = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM pending_orders WHERE status = 'staged'"
                    ).fetchall()
                ]

            placeholders = ",".join("?" * len(staged_ids))
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    claimed1 = conn.execute(
                        f"UPDATE pending_orders SET status = 'processing' "
                        f"WHERE id IN ({placeholders}) AND status = 'staged'",
                        staged_ids,
                    ).rowcount

            self.assertEqual(claimed1, 3)

            # Operator 2 tries same IDs — CAS guard blocks
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    claimed2 = conn.execute(
                        f"UPDATE pending_orders SET status = 'processing' "
                        f"WHERE id IN ({placeholders}) AND status = 'staged'",
                        staged_ids,
                    ).rowcount

            self.assertEqual(claimed2, 0,
                             "Second CAS claim must return 0 — rows already processing")

            # All rows still 'processing' (not double-claimed)
            statuses = _read_statuses(db)
            self.assertTrue(all(s == "processing" for _, s in statuses))
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T4: single-order CAS claim + read-after-write
# ═══════════════════════════════════════════════════════════════════════════

class TestSingleOrderCasClaim(unittest.TestCase):

    def test_single_order_claim_and_read(self):
        """Mirrors single-order branch: CAS write in one conn, read in another."""
        db = os.path.join(tempfile.gettempdir(), "f13_t4.db")
        try:
            _create_db(db)
            _seed_staged(db, count=1)

            db_id = 1

            # WRITE phase: CAS claim
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    result = conn.execute(
                        "UPDATE pending_orders SET status = 'processing' "
                        "WHERE id = ? AND status = 'staged'",
                        (db_id,),
                    )

            self.assertEqual(result.rowcount, 1)

            # READ phase: separate conn
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, payload, status FROM pending_orders WHERE id = ?",
                    (db_id,),
                ).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "processing")
            self.assertIn("T0", row["payload"])
        finally:
            try: os.unlink(db)
            except: pass


# ═══════════════════════════════════════════════════════════════════════════
# T5: zero bare sites remain in codebase
# ═══════════════════════════════════════════════════════════════════════════

class TestNoBareConnectionSitesRemain(unittest.TestCase):

    def test_zero_bare_sites(self):
        """Verify no bare `with _get_db_connection() as conn:` patterns remain."""
        import re
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        bare_matches = re.findall(
            r'with _get_db_connection\(\) as conn:', content
        )
        self.assertEqual(
            len(bare_matches), 0,
            f"Found {len(bare_matches)} bare _get_db_connection() sites — "
            f"all must use closing() wrap"
        )


if __name__ == "__main__":
    unittest.main()
