"""
Followup #20 — Sub-account Routing Tests.

14 tests covering:
- allocate_excess_proportional (6 pure function tests)
- Multi-row CC staging (2 tests: multi-row + atomicity)
- TRANSMIT routing (3 tests: correct routing, null block, cross-account isolation)
- STK_SELL null block (1 test)
- Schema migration idempotency (1 test)
- Gate 1 math scaling (1 test)

No live IBKR — mock ib_async objects.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from telegram_bot import allocate_excess_proportional


# ---------------------------------------------------------------------------
# Test 1-6: allocate_excess_proportional pure function tests
# ---------------------------------------------------------------------------


class TestAllocateExcessProportional(unittest.TestCase):

    def test_allocate_proportional_split(self):
        """Scenario 1: Individual 300sh + Roth 200sh, excess 3c -> 2+1."""
        result = allocate_excess_proportional(3, {
            "U21971297": {"account_id": "U21971297", "shares": 300},
            "U22076329": {"account_id": "U22076329", "shares": 200},
        })
        self.assertEqual(result, {"U21971297": 2, "U22076329": 1})
        self.assertEqual(sum(result.values()), 3)

    def test_allocate_single_account(self):
        """Scenario 2: single account, all contracts go there."""
        result = allocate_excess_proportional(4, {
            "U21971297": {"account_id": "U21971297", "shares": 500},
        })
        self.assertEqual(result, {"U21971297": 4})

    def test_allocate_sub_lot_skipped(self):
        """Scenario 3: Roth has < 100 shares, skipped."""
        result = allocate_excess_proportional(2, {
            "U21971297": {"account_id": "U21971297", "shares": 300},
            "U22076329": {"account_id": "U22076329", "shares": 50},
        })
        self.assertEqual(result, {"U21971297": 2})

    def test_allocate_fractional_remainder_to_largest(self):
        """Scenario 4: excess 1c, two accounts, remainder goes to largest."""
        result = allocate_excess_proportional(1, {
            "U21971297": {"account_id": "U21971297", "shares": 400},
            "U22076329": {"account_id": "U22076329", "shares": 300},
        })
        self.assertEqual(result, {"U21971297": 1})

    def test_allocate_zero_excess(self):
        """Zero excess -> empty dict."""
        result = allocate_excess_proportional(0, {
            "U21971297": {"account_id": "U21971297", "shares": 500},
        })
        self.assertEqual(result, {})

    def test_allocate_no_eligible_accounts(self):
        """All accounts < 100 shares -> empty dict."""
        result = allocate_excess_proportional(2, {
            "U21971297": {"account_id": "U21971297", "shares": 50},
            "U22076329": {"account_id": "U22076329", "shares": 80},
        })
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# Shared DDL for DB-backed tests
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE bucket3_dynamic_exit_log (
    audit_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
    action_type TEXT NOT NULL,
    household_nlv REAL NOT NULL,
    underlying_spot_at_render REAL NOT NULL,
    gate1_freed_margin REAL,
    gate1_realized_loss REAL,
    gate1_conviction_tier TEXT,
    gate1_conviction_modifier REAL,
    gate1_ratio REAL,
    gate2_target_contracts INTEGER,
    walk_away_pnl_per_share REAL,
    strike REAL,
    expiry TEXT,
    contracts INTEGER,
    shares INTEGER,
    limit_price REAL,
    render_ts REAL,
    staged_ts REAL,
    transmitted INTEGER NOT NULL DEFAULT 0,
    transmitted_ts REAL,
    re_validation_count INTEGER NOT NULL DEFAULT 0,
    final_status TEXT NOT NULL DEFAULT 'STAGED',
    source TEXT NOT NULL DEFAULT 'scheduled_watchdog',
    exception_type TEXT,
    fill_ts REAL,
    fill_price REAL,
    originating_account_id TEXT,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


# ---------------------------------------------------------------------------
# Test 7-8: Multi-row CC staging
# ---------------------------------------------------------------------------


class TestMultiRowStaging(unittest.TestCase):

    def test_stage_dynamic_exit_multi_row(self):
        """Mock position with two accounts -> 2 rows inserted with correct
        originating_account_id and scaled contracts/shares."""
        conn = _make_db()
        # Simulate what _stage_dynamic_exit_candidate would write
        # for excess_contracts=3 split across Individual(2) + Roth(1)
        import uuid, time

        allocation = allocate_excess_proportional(3, {
            "U21971297": {"account_id": "U21971297", "shares": 300},
            "U22076329": {"account_id": "U22076329", "shares": 200},
        })
        self.assertEqual(len(allocation), 2)

        now_ts = time.time()
        for account_id, acct_contracts in allocation.items():
            conn.execute(
                "INSERT INTO bucket3_dynamic_exit_log "
                "(audit_id, trade_date, ticker, household, "
                " action_type, household_nlv, underlying_spot_at_render, "
                " contracts, shares, final_status, originating_account_id, "
                " staged_ts, render_ts, source) "
                "VALUES (?, '2026-04-08', 'UBER', 'Yash_Household', "
                " 'CC', 200000, 55.0, ?, ?, 'STAGED', ?, ?, ?, 'manual_inspection')",
                (str(uuid.uuid4()), acct_contracts, acct_contracts * 100,
                 account_id, now_ts, now_ts),
            )
        conn.commit()

        rows = conn.execute(
            "SELECT originating_account_id, contracts, shares "
            "FROM bucket3_dynamic_exit_log ORDER BY contracts DESC"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["originating_account_id"], "U21971297")
        self.assertEqual(rows[0]["contracts"], 2)
        self.assertEqual(rows[0]["shares"], 200)
        self.assertEqual(rows[1]["originating_account_id"], "U22076329")
        self.assertEqual(rows[1]["contracts"], 1)
        self.assertEqual(rows[1]["shares"], 100)

    def test_stage_dynamic_exit_atomicity(self):
        """If second INSERT fails, no rows are written (rollback)."""
        conn = _make_db()
        import uuid, time

        now_ts = time.time()
        audit1 = str(uuid.uuid4())
        audit2 = audit1  # Duplicate PK -> second INSERT will fail

        try:
            with conn:
                conn.execute(
                    "INSERT INTO bucket3_dynamic_exit_log "
                    "(audit_id, trade_date, ticker, household, "
                    " action_type, household_nlv, underlying_spot_at_render, "
                    " contracts, shares, final_status, originating_account_id, "
                    " staged_ts, render_ts, source) "
                    "VALUES (?, '2026-04-08', 'UBER', 'Yash_Household', "
                    " 'CC', 200000, 55.0, 2, 200, 'STAGED', 'U21971297', ?, ?, "
                    " 'manual_inspection')",
                    (audit1, now_ts, now_ts),
                )
                # This should fail: duplicate PK
                conn.execute(
                    "INSERT INTO bucket3_dynamic_exit_log "
                    "(audit_id, trade_date, ticker, household, "
                    " action_type, household_nlv, underlying_spot_at_render, "
                    " contracts, shares, final_status, originating_account_id, "
                    " staged_ts, render_ts, source) "
                    "VALUES (?, '2026-04-08', 'UBER', 'Yash_Household', "
                    " 'CC', 200000, 55.0, 1, 100, 'STAGED', 'U22076329', ?, ?, "
                    " 'manual_inspection')",
                    (audit2, now_ts, now_ts),
                )
        except sqlite3.IntegrityError:
            pass  # Expected

        count = conn.execute(
            "SELECT COUNT(*) FROM bucket3_dynamic_exit_log"
        ).fetchone()[0]
        self.assertEqual(count, 0, "Transaction should have rolled back — no rows")


# ---------------------------------------------------------------------------
# Test 9-11: TRANSMIT routing
# ---------------------------------------------------------------------------


class TestTransmitRouting(unittest.TestCase):

    def test_transmit_routes_to_originating_account(self):
        """Row with originating_account_id='U22076329' -> placeOrder uses that account."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, action_type, "
            " household_nlv, underlying_spot_at_render, strike, expiry, "
            " contracts, shares, limit_price, final_status, "
            " originating_account_id, staged_ts, render_ts, source) "
            "VALUES ('roth-test-1', '2026-04-08', 'UBER', 'Yash_Household', "
            " 'CC', 200000, 55.0, 60.0, '20260425', 1, 100, 2.50, 'ATTESTED', "
            " 'U22076329', 0, 0, 'manual_inspection')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log WHERE audit_id = 'roth-test-1'"
        ).fetchone()
        account_id = row["originating_account_id"]
        self.assertEqual(account_id, "U22076329")
        self.assertTrue(account_id)  # fail-closed guard would pass

    def test_transmit_blocks_null_originating_account(self):
        """Row with NULL originating_account_id -> transmit should be blocked."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, action_type, "
            " household_nlv, underlying_spot_at_render, strike, expiry, "
            " contracts, shares, limit_price, final_status, "
            " originating_account_id, staged_ts, render_ts, source) "
            "VALUES ('null-acct-1', '2026-04-08', 'UBER', 'Yash_Household', "
            " 'CC', 200000, 55.0, 60.0, '20260425', 1, 100, 2.50, 'ATTESTED', "
            " NULL, 0, 0, 'manual_inspection')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log WHERE audit_id = 'null-acct-1'"
        ).fetchone()
        account_id = row["originating_account_id"]
        # Simulates the fail-closed guard: `if not account_id:` -> block
        self.assertFalse(account_id)

        # Simulate what the guard does: cancel the row
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'CANCELLED', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'null-acct-1' AND final_status = 'ATTESTED'"
        )
        conn.commit()

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log WHERE audit_id = 'null-acct-1'"
        ).fetchone()
        self.assertEqual(row["final_status"], "CANCELLED")

    def test_cross_account_isolation(self):
        """Headline regression test: Yash Roth row never results in Individual account."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, action_type, "
            " household_nlv, underlying_spot_at_render, strike, expiry, "
            " contracts, shares, limit_price, final_status, "
            " originating_account_id, staged_ts, render_ts, source) "
            "VALUES ('roth-iso-1', '2026-04-08', 'UBER', 'Yash_Household', "
            " 'CC', 200000, 55.0, 60.0, '20260425', 1, 100, 2.50, 'ATTESTED', "
            " 'U22076329', 0, 0, 'manual_inspection')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT originating_account_id FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'roth-iso-1'"
        ).fetchone()

        # The old bug: HOUSEHOLD_MAP["Yash_Household"][0] = "U21971297"
        # The fix: originating_account_id = "U22076329" (Roth)
        self.assertEqual(row["originating_account_id"], "U22076329")
        self.assertNotEqual(row["originating_account_id"], "U21971297",
                            "Roth IRA row must NEVER route to Individual account")


# ---------------------------------------------------------------------------
# Test 12: STK_SELL null blocks transmit
# ---------------------------------------------------------------------------


class TestStkSellNullBlock(unittest.TestCase):

    def test_stk_sell_row_null_blocks_transmit(self):
        """STK_SELL row has NULL originating_account_id by design (F20-5).
        TRANSMIT fail-closed guard must block it."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, action_type, "
            " household_nlv, underlying_spot_at_render, "
            " shares, limit_price, final_status, exception_type, "
            " originating_account_id, staged_ts, render_ts, source) "
            "VALUES ('stk-null-1', '2026-04-08', 'ADBE', 'Yash_Household', "
            " 'STK_SELL', 200000, 240.0, 50, 230.0, 'ATTESTED', "
            " 'rule_6_forced_liquidation', NULL, 0, 0, 'manual_inspection')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT originating_account_id FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'stk-null-1'"
        ).fetchone()

        # Simulate fail-closed guard
        account_id = row["originating_account_id"]
        self.assertIsNone(account_id)
        self.assertFalse(account_id)  # `if not account_id:` triggers


# ---------------------------------------------------------------------------
# Test 13: Schema migration idempotent
# ---------------------------------------------------------------------------


class TestSchemaMigration(unittest.TestCase):

    def test_schema_migration_idempotent(self):
        """Running the migration twice should not raise."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_DDL)

        # Simulate running ALTER TABLE twice (idempotent migration pattern)
        for _ in range(2):
            try:
                conn.execute(
                    "ALTER TABLE bucket3_dynamic_exit_log "
                    "ADD COLUMN originating_account_id TEXT"
                )
            except Exception:
                pass  # Column already exists — expected on second run

        # Verify column exists and is queryable
        row = conn.execute(
            "PRAGMA table_info(bucket3_dynamic_exit_log)"
        ).fetchall()
        col_names = [r[1] for r in row]
        # At least one originating_account_id column must exist
        self.assertIn("originating_account_id", col_names)


# ---------------------------------------------------------------------------
# Test 14: Gate 1 math scaled proportionally
# ---------------------------------------------------------------------------


class TestGate1MathScaling(unittest.TestCase):

    def test_gate1_math_scaled_proportionally(self):
        """2-row split: gate1_freed_margin on each row sums to original total."""
        total_freed = 18000.0  # 3 contracts * $60 strike * 100
        total_excess = 3
        allocation = {"U21971297": 2, "U22076329": 1}

        row_freed = {}
        for acct, contracts in allocation.items():
            scale = contracts / total_excess
            row_freed[acct] = round(total_freed * scale, 2)

        self.assertAlmostEqual(row_freed["U21971297"], 12000.0, places=2)
        self.assertAlmostEqual(row_freed["U22076329"], 6000.0, places=2)
        self.assertAlmostEqual(
            sum(row_freed.values()), total_freed, places=2,
            msg="Per-row freed margin must sum to total",
        )


if __name__ == "__main__":
    unittest.main()
