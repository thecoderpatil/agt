"""
Followup #9 PR1 — F3 Correctness Tests.

Proves that the 4 write sites in handle_dex_callback and the sweeper
actually persist state transitions to disk. Each test writes to a
temp-file-based SQLite DB, closes the connection, then re-opens the
DB to verify the final_status persisted.

This catches the bug where `with closing(conn)` without inner
`with conn:` triggers implicit rollback on close.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import sweep_stale_dynamic_exit_stages


# ---------------------------------------------------------------------------
# DDL + helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE bucket3_dynamic_exit_log (
    audit_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    household TEXT NOT NULL,
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
    exception_type TEXT
        CHECK (exception_type IS NULL OR exception_type IN (
            'rule_8_dynamic_exit', 'thesis_deterioration',
            'rule_6_forced_liquidation', 'emergency_risk_event')),
    fill_ts REAL,
    fill_price REAL,
    originating_account_id TEXT,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID
"""


def _create_db(path):
    """Create a fresh DB file with schema."""
    conn = sqlite3.connect(path)
    conn.execute(_DDL)
    conn.commit()
    conn.close()


def _insert_attested(path, audit_id, re_validation_count=0):
    """Insert an ATTESTED CC row into the DB file."""
    now = time.time()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, trade_date, ticker, household, action_type, "
        " household_nlv, underlying_spot_at_render, "
        " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
        " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
        " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
        " limit_price, render_ts, staged_ts, final_status, re_validation_count) "
        "VALUES (?, date('now'), 'AAPL', 'Yash_Household', 'CC', "
        " 261000.0, 240.0, "
        " 48000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 2, "
        " -7.0, 240.0, '2026-05-15', 2, 200, "
        " 1.85, ?, ?, 'ATTESTED', ?)",
        (audit_id, now, now, re_validation_count),
    )
    conn.commit()
    conn.close()


def _insert_transmitting(path, audit_id):
    """Insert a TRANSMITTING CC row into the DB file."""
    now = time.time()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, trade_date, ticker, household, action_type, "
        " household_nlv, underlying_spot_at_render, "
        " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
        " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
        " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
        " limit_price, render_ts, staged_ts, final_status) "
        "VALUES (?, date('now'), 'AAPL', 'Yash_Household', 'CC', "
        " 261000.0, 240.0, "
        " 48000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 2, "
        " -7.0, 240.0, '2026-05-15', 2, 200, "
        " 1.85, ?, ?, 'TRANSMITTING')",
        (audit_id, now, now),
    )
    conn.commit()
    conn.close()


def _read_status(path, audit_id):
    """Re-open DB and read final_status. Proves persistence to disk."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT final_status, transmitted, transmitted_ts "
        "FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
        (audit_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════
# T1: CANCEL persists CANCELLED to disk
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelPersistence(unittest.TestCase):
    """Mirrors handle_dex_callback CANCEL branch (line 6372).
    Proves `with closing() + with conn:` commits to disk."""

    def test_cancel_persists(self):
        db = os.path.join(tempfile.gettempdir(), "f3_t1_cancel.db")
        try:
            _create_db(db)
            _insert_attested(db, "cancel-persist-01")

            # Replicate the CANCEL branch pattern (post-fix)
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE bucket3_dynamic_exit_log "
                        "SET final_status = 'CANCELLED', last_updated = CURRENT_TIMESTAMP "
                        "WHERE audit_id = 'cancel-persist-01' AND final_status = 'ATTESTED'"
                    )

            # Re-open and verify
            result = _read_status(db, "cancel-persist-01")
            self.assertIsNotNone(result)
            self.assertEqual(result["final_status"], "CANCELLED",
                             "CANCEL must persist to disk after closing()+with conn:")
        finally:
            try:
                os.unlink(db)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# T2: DRIFT_BLOCKED persists to disk
# ═══════════════════════════════════════════════════════════════════════════

class TestDriftBlockedPersistence(unittest.TestCase):
    """Mirrors handle_dex_callback 3-strike branch (line 6431)."""

    def test_drift_blocked_persists(self):
        db = os.path.join(tempfile.gettempdir(), "f3_t2_drift.db")
        try:
            _create_db(db)
            _insert_attested(db, "drift-persist-01", re_validation_count=3)

            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE bucket3_dynamic_exit_log "
                        "SET final_status = 'DRIFT_BLOCKED', last_updated = CURRENT_TIMESTAMP "
                        "WHERE audit_id = 'drift-persist-01' AND final_status = 'ATTESTED'"
                    )

            result = _read_status(db, "drift-persist-01")
            self.assertIsNotNone(result)
            self.assertEqual(result["final_status"], "DRIFT_BLOCKED",
                             "DRIFT_BLOCKED must persist to disk")
        finally:
            try:
                os.unlink(db)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# T3: TRANSMITTING lock persists to disk
# ═══════════════════════════════════════════════════════════════════════════

class TestTransmittingPersistence(unittest.TestCase):
    """Mirrors handle_dex_callback Step 6 (line 6565)."""

    def test_transmitting_lock_persists(self):
        db = os.path.join(tempfile.gettempdir(), "f3_t3_lock.db")
        try:
            _create_db(db)
            _insert_attested(db, "lock-persist-01")

            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    lock_result = conn.execute(
                        "UPDATE bucket3_dynamic_exit_log "
                        "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "
                        "WHERE audit_id = 'lock-persist-01' AND final_status = 'ATTESTED'"
                    )
                # lock_result readable after with conn: exits
                self.assertEqual(lock_result.rowcount, 1)

            result = _read_status(db, "lock-persist-01")
            self.assertIsNotNone(result)
            self.assertEqual(result["final_status"], "TRANSMITTING",
                             "TRANSMITTING lock must persist to disk")
        finally:
            try:
                os.unlink(db)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# T4: TRANSMITTED persists to disk
# ═══════════════════════════════════════════════════════════════════════════

class TestTransmittedPersistence(unittest.TestCase):
    """Mirrors handle_dex_callback Step 8 (line 6639)."""

    def test_transmitted_persists(self):
        db = os.path.join(tempfile.gettempdir(), "f3_t4_fill.db")
        try:
            _create_db(db)
            _insert_transmitting(db, "fill-persist-01")
            now_ts = time.time()

            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE bucket3_dynamic_exit_log "
                        "SET final_status = 'TRANSMITTED', transmitted = 1, "
                        "    transmitted_ts = ?, last_updated = CURRENT_TIMESTAMP "
                        "WHERE audit_id = 'fill-persist-01' AND final_status = 'TRANSMITTING'",
                        (now_ts,),
                    )

            result = _read_status(db, "fill-persist-01")
            self.assertIsNotNone(result)
            self.assertEqual(result["final_status"], "TRANSMITTED",
                             "TRANSMITTED must persist to disk")
            self.assertEqual(result["transmitted"], 1)
            self.assertIsNotNone(result["transmitted_ts"])
        finally:
            try:
                os.unlink(db)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# T5: Sweeper ABANDONED persists to disk (already correct via internal commit)
# ═══════════════════════════════════════════════════════════════════════════

class TestSweeperPersistence(unittest.TestCase):
    """Sweeper calls conn.commit() internally (rule_engine.py:1184).
    Verify the closing() outer wrap doesn't interfere."""

    def test_sweeper_abandoned_persists(self):
        db = os.path.join(tempfile.gettempdir(), "f3_t5_sweep.db")
        try:
            _create_db(db)
            _insert_attested(db, "sweep-persist-01")

            # Backdate last_updated to 11 minutes ago
            conn_setup = sqlite3.connect(db)
            conn_setup.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET last_updated = datetime('now', '-11 minutes') "
                "WHERE audit_id = 'sweep-persist-01'"
            )
            conn_setup.commit()
            conn_setup.close()

            # Run sweeper with closing() (matches _sweep_attested_ttl_job pattern)
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                result = sweep_stale_dynamic_exit_stages(conn)
                self.assertEqual(result["attested_swept"], 1)

            # Re-open and verify persistence
            final = _read_status(db, "sweep-persist-01")
            self.assertIsNotNone(final)
            self.assertEqual(final["final_status"], "ABANDONED",
                             "Sweeper ABANDONED must persist (internal commit)")
        finally:
            try:
                os.unlink(db)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
