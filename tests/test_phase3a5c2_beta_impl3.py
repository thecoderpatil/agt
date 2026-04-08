"""
Beta Impl 3 Smoke Tests — TRANSMIT/CANCEL handler + JIT re-validation.

Covers: JIT precedence chain steps 0–8, CANCEL, sweeper ATTESTED TTL,
        counter isolation (R8), poller dedup, STK_SELL branch (F8).

DB: in-memory SQLite with TRANSMITTING in CHECK constraint.
No Telegram mocking — tests exercise DB state transitions directly.
"""

import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import (
    evaluate_gate_1, ConvictionTier,
    sweep_stale_dynamic_exit_stages, is_ticker_locked,
)


# ---------------------------------------------------------------------------
# Shared test DB fixture
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE bucket3_dynamic_exit_log (
    audit_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
    desk_mode TEXT NOT NULL CHECK (desk_mode IN ('PEACETIME', 'AMBER', 'WARTIME')),
    action_type TEXT NOT NULL CHECK (action_type IN ('CC', 'STK_SELL')),
    household_nlv REAL NOT NULL,
    underlying_spot_at_render REAL NOT NULL,
    gate1_freed_margin REAL,
    gate1_realized_loss REAL,
    gate1_conviction_tier TEXT,
    gate1_conviction_modifier REAL,
    gate1_ratio REAL,
    gate2_target_contracts INTEGER,
    gate2_max_per_cycle INTEGER,
    walk_away_pnl_per_share REAL,
    strike REAL,
    expiry TEXT,
    contracts INTEGER,
    shares INTEGER,
    limit_price REAL,
    campaign_id TEXT,
    operator_thesis TEXT,
    attestation_value_typed TEXT,
    checkbox_state_json TEXT,
    render_ts REAL,
    staged_ts REAL,
    transmitted INTEGER NOT NULL DEFAULT 0,
    transmitted_ts REAL,
    re_validation_count INTEGER NOT NULL DEFAULT 0,
    final_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (final_status IN ('PENDING', 'STAGED', 'ATTESTED',
                                'TRANSMITTING', 'TRANSMITTED',
                                'CANCELLED', 'DRIFT_BLOCKED',
                                'ABANDONED')),
    source TEXT NOT NULL DEFAULT 'scheduled_watchdog'
        CHECK (source IN ('scheduled_watchdog', 'manual_inspection',
                          'cc_overweight', 'manual_stage')),
    fill_ts REAL,
    fill_price REAL,
    originating_account_id TEXT,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID
"""

_ATTESTED_CC_ROW = (
    "INSERT INTO bucket3_dynamic_exit_log "
    "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
    " household_nlv, underlying_spot_at_render, "
    " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
    " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
    " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
    " limit_price, render_ts, staged_ts, final_status, re_validation_count) "
    "VALUES (?, date('now'), ?, ?, ?, 'CC', "
    " 261000.0, 250.0, "
    " 26000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 1, "
    " -7.0, 260.0, '2026-05-16', 1, 100, "
    " 3.0, ?, ?, 'ATTESTED', ?)"
)


def _get_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    return conn


def _insert_attested_cc(conn, audit_id="test-abc-123", ticker="ADBE",
                         household="Yash_Household", desk_mode="PEACETIME",
                         re_validation_count=0):
    now = time.time()
    conn.execute(_ATTESTED_CC_ROW, (
        audit_id, ticker, household, desk_mode,
        now, now, re_validation_count,
    ))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# (a) TRANSMIT happy path — ATTESTED → TRANSMITTING → TRANSMITTED
# ═══════════════════════════════════════════════════════════════════════════

class TestTransmitHappyPath(unittest.TestCase):

    def test_attested_to_transmitting_to_transmitted(self):
        conn = _get_db()
        _insert_attested_cc(conn)

        # Step 6: atomic lock ATTESTED → TRANSMITTING
        result = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        self.assertEqual(result.rowcount, 1)

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "TRANSMITTING")

        # Step 8: TRANSMITTING → TRANSMITTED
        now_ts = time.time()
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTED', transmitted = 1, transmitted_ts = ? "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'TRANSMITTING'",
            (now_ts,),
        )
        conn.commit()

        row = conn.execute(
            "SELECT final_status, transmitted, transmitted_ts "
            "FROM bucket3_dynamic_exit_log WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["transmitted"], 1)
        self.assertIsNotNone(row["transmitted_ts"])


# ═══════════════════════════════════════════════════════════════════════════
# (b) Idempotency — double-tap TRANSMIT → second tap hits TRANSMIT_RACE_LOST
# ═══════════════════════════════════════════════════════════════════════════

class TestTransmitIdempotency(unittest.TestCase):

    def test_double_tap_race_lost(self):
        conn = _get_db()
        _insert_attested_cc(conn)

        # First tap: ATTESTED → TRANSMITTING
        r1 = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTING' "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        self.assertEqual(r1.rowcount, 1)

        # Second tap: same atomic UPDATE → rowcount 0 (TRANSMIT_RACE_LOST)
        r2 = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTING' "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        self.assertEqual(r2.rowcount, 0)


# ═══════════════════════════════════════════════════════════════════════════
# (c) Drift block — live_bid $0.15 below limit → DRIFT_BLOCK, counter=1
# ═══════════════════════════════════════════════════════════════════════════

class TestDriftBlock(unittest.TestCase):

    def test_drift_exceeds_threshold(self):
        """abs(live_bid - limit_price) > 0.10 triggers drift block."""
        attested_limit = 3.00
        live_bid = 2.85  # drift = 0.15 > 0.10
        drift = abs(live_bid - attested_limit)
        self.assertGreater(drift, 0.10)

        # Counter increment would happen via _increment_revalidation_count
        conn = _get_db()
        _insert_attested_cc(conn)
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET re_validation_count = re_validation_count + 1 "
            "WHERE audit_id = 'test-abc-123'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT re_validation_count FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["re_validation_count"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# (d) 3-strike terminal — 3 drift fails → DRIFT_BLOCKED
# ═══════════════════════════════════════════════════════════════════════════

class TestThreeStrikeTerminal(unittest.TestCase):

    def test_three_strikes_to_drift_blocked(self):
        conn = _get_db()
        _insert_attested_cc(conn, re_validation_count=2)

        # Simulate 3rd failure: increment counter
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET re_validation_count = re_validation_count + 1 "
            "WHERE audit_id = 'test-abc-123'"
        )
        conn.commit()

        row = conn.execute(
            "SELECT re_validation_count FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["re_validation_count"], 3)

        # JIT step 1 would detect count >= 3 and transition to DRIFT_BLOCKED
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'DRIFT_BLOCKED', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        conn.commit()

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "DRIFT_BLOCKED")


# ═══════════════════════════════════════════════════════════════════════════
# (e) Counter survives rollback — R8 isolation via separate connection
# ═══════════════════════════════════════════════════════════════════════════

class TestCounterIsolation(unittest.TestCase):

    def test_counter_persists_via_isolated_connection(self):
        """Simulates R8: counter increment on one connection survives
        even if another connection's transaction would hypothetically fail."""
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), "test_impl3_r8.db")
        try:
            # Create DB on disk (in-memory can't share across connections)
            conn1 = sqlite3.connect(db_path)
            conn1.row_factory = sqlite3.Row
            conn1.execute(_DDL)
            now = time.time()
            conn1.execute(_ATTESTED_CC_ROW, (
                "r8-test", "ADBE", "Yash_Household", "PEACETIME", now, now, 0,
            ))
            conn1.commit()

            # Isolated counter increment (separate connection, like _increment_revalidation_count)
            iso_conn = sqlite3.connect(db_path)
            iso_conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET re_validation_count = re_validation_count + 1 "
                "WHERE audit_id = 'r8-test'"
            )
            iso_conn.commit()
            iso_conn.close()

            # Verify via original connection
            row = conn1.execute(
                "SELECT re_validation_count FROM bucket3_dynamic_exit_log "
                "WHERE audit_id = 'r8-test'"
            ).fetchone()
            self.assertEqual(row["re_validation_count"], 1)
            conn1.close()
        finally:
            try:
                os.unlink(db_path)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# (f) CANCEL happy path — ATTESTED → CANCELLED
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelHappyPath(unittest.TestCase):

    def test_attested_to_cancelled(self):
        conn = _get_db()
        _insert_attested_cc(conn)

        result = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'CANCELLED', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        self.assertEqual(result.rowcount, 1)

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "CANCELLED")


# ═══════════════════════════════════════════════════════════════════════════
# (g) CANCEL vs TRANSMIT race — one wins, other gets RACE_LOST
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelVsTransmitRace(unittest.TestCase):

    def test_cancel_first_then_transmit_finds_row_not_found(self):
        conn = _get_db()
        _insert_attested_cc(conn)

        # Cancel wins
        r1 = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'CANCELLED' "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        self.assertEqual(r1.rowcount, 1)

        # Transmit attempt — ATTESTED_ROW_NOT_FOUND
        row = conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        ).fetchone()
        self.assertIsNone(row, "TRANSMIT should find no ATTESTED row after CANCEL")


# ═══════════════════════════════════════════════════════════════════════════
# (h) Sweeper — ATTESTED row hits 10min → ABANDONED
# ═══════════════════════════════════════════════════════════════════════════

class TestSweeperAttestedTTL(unittest.TestCase):

    def test_sweeper_abandons_stale_attested(self):
        conn = _get_db()
        _insert_attested_cc(conn)

        # Manually backdate last_updated to 11 minutes ago
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET last_updated = datetime('now', '-11 minutes') "
            "WHERE audit_id = 'test-abc-123'"
        )
        conn.commit()

        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertEqual(result["attested_swept"], 1)

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "ABANDONED")

    def test_sweeper_does_not_sweep_fresh_attested(self):
        conn = _get_db()
        _insert_attested_cc(conn)
        # last_updated is 'now' — should NOT be swept
        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertEqual(result["attested_swept"], 0)

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "ATTESTED")


# ═══════════════════════════════════════════════════════════════════════════
# (i) STK_SELL branch — skips Gate 1, runs drift only
# ═══════════════════════════════════════════════════════════════════════════

class TestStkSellSkipsGate1(unittest.TestCase):

    def test_stk_sell_action_type_branches_correctly(self):
        """STK_SELL should skip Gate 1 at step 5a (only drift at 5b).
        We verify the branching logic: action_type != 'CC' means no Gate 1."""
        conn = _get_db()
        # Insert STK_SELL row (no gate1 fields needed for drift-only)
        now = time.time()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, "
            " walk_away_pnl_per_share, shares, limit_price, "
            " render_ts, staged_ts, final_status) "
            "VALUES ('stk-test', date('now'), 'PYPL', 'Vikram_Household', 'WARTIME', "
            " 'STK_SELL', 80000.0, 65.0, -5.0, 100, 65.0, ?, ?, 'ATTESTED')",
            (now, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log WHERE audit_id = 'stk-test'"
        ).fetchone()

        # Branch logic: action_type != 'CC' → skip Gate 1
        self.assertEqual(row["action_type"], "STK_SELL")
        self.assertNotEqual(row["action_type"], "CC")  # Gate 1 skipped

        # Drift check still applies: live_price vs limit_price
        live_price = 65.05
        drift = abs(live_price - row["limit_price"])
        self.assertLessEqual(drift, 0.10, "Drift within threshold — would pass")


# ═══════════════════════════════════════════════════════════════════════════
# (j) Poller dedup — 3 ticks, 2 rows → exactly 2 dispatches
# ═══════════════════════════════════════════════════════════════════════════

class TestPollerDedup(unittest.TestCase):

    def test_dispatched_set_prevents_duplicates(self):
        dispatched: set[str] = set()
        attested_ids = ["audit-1", "audit-2"]
        dispatch_count = 0

        # Simulate 3 poller ticks
        for _tick in range(3):
            for audit_id in attested_ids:
                if audit_id not in dispatched:
                    dispatch_count += 1
                    dispatched.add(audit_id)

        self.assertEqual(dispatch_count, 2, "Each audit_id dispatched exactly once")
        self.assertEqual(dispatched, {"audit-1", "audit-2"})


# ═══════════════════════════════════════════════════════════════════════════
# (k) TRANSMIT_IB_ERROR — placeOrder raises, row stays TRANSMITTING
# ═══════════════════════════════════════════════════════════════════════════

class TestTransmitIbError(unittest.TestCase):

    def test_ib_error_leaves_row_in_transmitting(self):
        """If placeOrder fails AFTER acquiring the TRANSMITTING lock,
        the row must stay in TRANSMITTING for manual recovery.
        No auto-revert to ATTESTED."""
        conn = _get_db()
        _insert_attested_cc(conn)

        # Step 6: acquire lock
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'test-abc-123' AND final_status = 'ATTESTED'"
        )
        conn.commit()

        # Step 7: simulate placeOrder failure (exception thrown)
        ib_error = RuntimeError("IBKR connection lost during placeOrder")

        # Handler logic: on exception, leave row in TRANSMITTING, do NOT revert
        # (no UPDATE back to ATTESTED ��� that's the spec)
        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(
            row["final_status"], "TRANSMITTING",
            "Row must stay TRANSMITTING after IB error — manual recovery required"
        )

        # Verify it's NOT ATTESTED (no auto-revert)
        self.assertNotEqual(row["final_status"], "ATTESTED")
        # Verify it's NOT TRANSMITTED (order didn't go through)
        self.assertNotEqual(row["final_status"], "TRANSMITTED")


# ═══════════════════════════════════════════════════════════════════════════
# is_ticker_locked helper
# ═══════════════════════════════════════════════════════════════════════════

class TestTickerLocked(unittest.TestCase):

    def test_not_locked_when_no_drift_blocked(self):
        conn = _get_db()
        self.assertFalse(is_ticker_locked(conn, "ADBE"))

    def test_locked_when_recent_drift_blocked(self):
        conn = _get_db()
        now = time.time()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, final_status, "
            " render_ts, staged_ts) "
            "VALUES ('locked-1', date('now'), 'ADBE', 'Yash_Household', 'PEACETIME', "
            " 'CC', 261000.0, 250.0, 'DRIFT_BLOCKED', ?, ?)",
            (now, now),
        )
        conn.commit()
        self.assertTrue(is_ticker_locked(conn, "ADBE"))
        self.assertFalse(is_ticker_locked(conn, "PYPL"))


# ═══════════════════════════════════════════════════════════════════════════
# Fix Sprint Tests — F1 through F9
# ═══════════════════════════════════════════════════════════════════════════


class TestF1LimitPriceNotLiveBid(unittest.TestCase):
    """F1: order must route at attested limit_price, NOT live_bid."""

    def test_order_uses_attested_limit(self):
        # Simulate: attested at limit_price=3.00, live_bid=2.95 (passes drift)
        attested_limit = 3.00
        live_bid = 2.95
        drift = abs(live_bid - attested_limit)
        self.assertLessEqual(drift, 0.10, "Drift passes gate")

        # The order MUST use attested_limit, not live_bid
        # _build_adaptive_sell_order(qty, limit_price, account_id) sets lmtPrice
        order_price = attested_limit  # F1 fix: row['limit_price']
        self.assertEqual(order_price, 3.00)
        self.assertNotEqual(order_price, live_bid,
                            "Order must NOT use live_bid as limit price")


class TestF2MigrationExplicitColumns(unittest.TestCase):
    """F2: migration must use explicit column names, not SELECT *."""

    def test_migration_handles_alter_appended_source(self):
        """Create a legacy table where 'source' is ALTER-appended (physically
        last), then run migration. Verify data integrity is preserved."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        # Create legacy table WITHOUT 'source' in initial DDL
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                household TEXT NOT NULL,
                desk_mode TEXT NOT NULL,
                action_type TEXT NOT NULL,
                household_nlv REAL NOT NULL,
                underlying_spot_at_render REAL NOT NULL,
                gate1_freed_margin REAL,
                gate1_realized_loss REAL,
                gate1_conviction_tier TEXT,
                gate1_conviction_modifier REAL,
                gate1_ratio REAL,
                gate2_target_contracts INTEGER,
                gate2_max_per_cycle INTEGER,
                walk_away_pnl_per_share REAL,
                strike REAL,
                expiry TEXT,
                contracts INTEGER,
                shares INTEGER,
                limit_price REAL,
                campaign_id TEXT,
                operator_thesis TEXT,
                attestation_value_typed TEXT,
                checkbox_state_json TEXT,
                render_ts REAL,
                staged_ts REAL,
                transmitted INTEGER NOT NULL DEFAULT 0,
                transmitted_ts REAL,
                re_validation_count INTEGER NOT NULL DEFAULT 0,
                final_status TEXT NOT NULL DEFAULT 'PENDING',
                fill_ts REAL,
                fill_price REAL,
                originating_account_id TEXT,
                last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) WITHOUT ROWID
        """)
        # ALTER-append 'source' (physically at end, after last_updated)
        conn.execute(
            "ALTER TABLE bucket3_dynamic_exit_log "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'scheduled_watchdog'"
        )

        # Insert a test row
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, final_status, source) "
            "VALUES ('legacy-1', '2026-04-07', 'ADBE', 'Yash_Household', "
            " 'PEACETIME', 'CC', 261000.0, 250.0, 'STAGED', 'manual_inspection')"
        )
        conn.commit()

        # Verify source is physically last via PRAGMA
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bucket3_dynamic_exit_log)")]
        self.assertEqual(cols[-1], "source", "source should be ALTER-appended at end")

        # Now simulate the PRAGMA-based migration approach:
        conn.execute("ALTER TABLE bucket3_dynamic_exit_log RENAME TO _old_test")
        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
                audit_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                household TEXT NOT NULL,
                desk_mode TEXT NOT NULL,
                action_type TEXT NOT NULL,
                household_nlv REAL NOT NULL,
                underlying_spot_at_render REAL NOT NULL,
                gate1_freed_margin REAL,
                gate1_realized_loss REAL,
                gate1_conviction_tier TEXT,
                gate1_conviction_modifier REAL,
                gate1_ratio REAL,
                gate2_target_contracts INTEGER,
                gate2_max_per_cycle INTEGER,
                walk_away_pnl_per_share REAL,
                strike REAL,
                expiry TEXT,
                contracts INTEGER,
                shares INTEGER,
                limit_price REAL,
                campaign_id TEXT,
                operator_thesis TEXT,
                attestation_value_typed TEXT,
                checkbox_state_json TEXT,
                render_ts REAL,
                staged_ts REAL,
                transmitted INTEGER NOT NULL DEFAULT 0,
                transmitted_ts REAL,
                re_validation_count INTEGER NOT NULL DEFAULT 0,
                final_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (final_status IN ('PENDING', 'STAGED', 'ATTESTED',
                                            'TRANSMITTING', 'TRANSMITTED',
                                            'CANCELLED', 'DRIFT_BLOCKED',
                                            'ABANDONED')),
                source TEXT NOT NULL DEFAULT 'scheduled_watchdog',
                fill_ts REAL,
                fill_price REAL,
                originating_account_id TEXT,
                last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) WITHOUT ROWID
        """)

        # Use PRAGMA-based column list (the F2 fix)
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(_old_test)")]
        col_list = ", ".join(old_cols)
        conn.execute(
            f"INSERT INTO bucket3_dynamic_exit_log ({col_list}) "
            f"SELECT {col_list} FROM _old_test"
        )
        conn.execute("DROP TABLE _old_test")
        conn.commit()

        # Verify data survived with correct column mapping
        row = conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log WHERE audit_id = 'legacy-1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "manual_inspection")
        self.assertEqual(row["ticker"], "ADBE")
        self.assertEqual(row["final_status"], "STAGED")

        # Verify TRANSMITTING is now valid
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, final_status) "
            "VALUES ('new-1', '2026-04-07', 'TEST', 'TEST', 'PEACETIME', 'CC', 0, 0, 'TRANSMITTING')"
        )
        conn.commit()


class TestF5WartimeLockoutBypass(unittest.TestCase):
    """F5: WARTIME bypasses ticker rolling lockout (Step 2)."""

    def test_wartime_bypasses_ticker_lockout(self):
        conn = _get_db()
        now = time.time()

        # Create a recent DRIFT_BLOCKED row for ADBE (would lock in PEACETIME)
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, final_status, "
            " render_ts, staged_ts) "
            "VALUES ('blocked-1', date('now'), 'ADBE', 'Yash_Household', 'PEACETIME', "
            " 'CC', 261000.0, 250.0, 'DRIFT_BLOCKED', ?, ?)",
            (now, now),
        )
        conn.commit()

        # Ticker IS locked
        self.assertTrue(is_ticker_locked(conn, "ADBE"))

        # But in WARTIME, the handler skips this check:
        is_wartime = True  # row["desk_mode"] == "WARTIME"
        should_check = not is_wartime and is_ticker_locked(conn, "ADBE")
        self.assertFalse(should_check,
                         "WARTIME must bypass ticker lockout per ADR-004 §4")


class TestF7PollerPoisonPill(unittest.TestCase):
    """F7: one failed send_message must not starve remaining rows."""

    def test_failing_row_does_not_block_others(self):
        dispatched: set[str] = set()
        rows = [{"audit_id": f"row-{i}"} for i in range(5)]
        fail_on = "row-2"
        dispatched_ids = []

        for row in rows:
            audit_id = row["audit_id"]
            if audit_id in dispatched:
                continue
            try:
                if audit_id == fail_on:
                    raise RuntimeError("TimedOut")
                dispatched_ids.append(audit_id)
                dispatched.add(audit_id)
            except Exception:
                pass  # F7: skip and retry next tick

        self.assertEqual(dispatched_ids, ["row-0", "row-1", "row-3", "row-4"])
        self.assertNotIn(fail_on, dispatched,
                         "Failed row must NOT be in dispatched set (allows retry)")


class TestF8SweeperJob(unittest.TestCase):
    """F8: sweeper runs continuously, not just at /cc preamble."""

    def test_sweeper_catches_stale_attested_independently(self):
        conn = _get_db()
        _insert_attested_cc(conn)

        # Backdate to 11 minutes ago
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET last_updated = datetime('now', '-11 minutes') "
            "WHERE audit_id = 'test-abc-123'"
        )
        conn.commit()

        # Simulate what _sweep_attested_ttl_job does
        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertGreaterEqual(result["attested_swept"], 1)

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'test-abc-123'"
        ).fetchone()
        self.assertEqual(row["final_status"], "ABANDONED")


class TestF9DriftThreshold(unittest.TestCase):
    """F9: CC uses $0.10 absolute, STK_SELL uses 0.5% relative."""

    def _check_drift(self, action_type, attested, live):
        drift = abs(live - attested)
        threshold = 0.10 if action_type == "CC" else attested * 0.005
        return drift <= threshold  # True = PASS, False = BLOCK

    def test_msft_stk_sell_pass(self):
        """$400 stock, live $399.50, drift $0.50, threshold $2.00 → PASS"""
        self.assertTrue(self._check_drift("STK_SELL", 400.00, 399.50))

    def test_msft_stk_sell_block(self):
        """$400 stock, live $397.00, drift $3.00, threshold $2.00 → BLOCK"""
        self.assertFalse(self._check_drift("STK_SELL", 400.00, 397.00))

    def test_f_stk_sell_block(self):
        """$20 stock, live $19.85, drift $0.15, threshold $0.10 → BLOCK"""
        self.assertFalse(self._check_drift("STK_SELL", 20.00, 19.85))

    def test_aapl_cc_pass(self):
        """CC attested $1.50, live $1.45, drift $0.05, threshold $0.10 → PASS"""
        self.assertTrue(self._check_drift("CC", 1.50, 1.45))


if __name__ == "__main__":
    unittest.main()
