"""
Followup #17 — Empirical verification: TRANSMITTING lock survives hard kill.

Per Gemini Finding 9 / F1 incident rule: any change touching transaction
or lifecycle code requires a scratch-script repro before ship.

This script:
  1. Creates a temp DB with the actual bucket3_dynamic_exit_log schema
  2. Inserts an ATTESTED row with a known audit_id
  3. Forks a child process that:
     - Acquires a connection using closing() + with conn:
     - Executes the CAS lock UPDATE (ATTESTED -> TRANSMITTING)
     - Calls os._exit(9) BEFORE any simulated placeOrder
  4. Parent waits for child, re-opens the DB
  5. Asserts:
     - Row is TRANSMITTING (lock survived hard kill)
     - fill_price is NULL (column ownership: no fill data written)
     - fill_ts is NULL
     - transmitted = 0 (not yet transmitted)
  6. Cleans up temp DB
"""

import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing

# Add project root to path for schema import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


_DDL = """
CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_log (
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
    exception_type TEXT
        CHECK (exception_type IS NULL OR exception_type IN (
            'rule_8_dynamic_exit', 'thesis_deterioration',
            'rule_6_forced_liquidation', 'emergency_risk_event')),
    fill_ts REAL,
    fill_price REAL,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID
"""

AUDIT_ID = "f17-lock-test-001"


def _child_process(db_path: str):
    """Child: acquire CAS lock, then hard-exit before placeOrder."""
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")

        # Canonical pattern: closing() + with conn: for write
        with closing(conn) as c:
            with c:
                result = c.execute(
                    "UPDATE bucket3_dynamic_exit_log "
                    "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "
                    "WHERE audit_id = ? AND final_status = 'ATTESTED'",
                    (AUDIT_ID,),
                )
                assert result.rowcount == 1, f"CAS lock failed: rowcount={result.rowcount}"

        # Simulate crash BEFORE placeOrder — hard exit
        print("  [child] CAS lock acquired, hard-exiting with code 9...")
        os._exit(9)
    except Exception as exc:
        print(f"  [child] ERROR: {exc}")
        os._exit(1)


def main():
    db_path = os.path.join(tempfile.gettempdir(), "f17_lock_test.db")
    print(f"DB path: {db_path}")

    try:
        # Step 1: Create DB with schema
        if os.path.exists(db_path):
            os.unlink(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_DDL)
        conn.commit()
        print("[OK] Schema created")

        # Step 2: Insert ATTESTED row
        now = time.time()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, "
            " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
            " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
            " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
            " limit_price, render_ts, staged_ts, final_status, re_validation_count) "
            "VALUES (?, date('now'), 'ADBE', 'Yash_Household', 'PEACETIME', 'CC', "
            " 261000.0, 250.0, "
            " 26000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 1, "
            " -7.0, 260.0, '2026-05-16', 1, 100, "
            " 3.00, ?, ?, 'ATTESTED', 0)",
            (AUDIT_ID, now, now),
        )
        conn.commit()
        conn.close()
        print("[OK] ATTESTED row inserted")

        # Step 3: Fork child that acquires lock then hard-exits
        print("[..] Spawning child process...")
        p = multiprocessing.Process(target=_child_process, args=(db_path,))
        p.start()
        p.join(timeout=10)

        if p.exitcode != 9:
            print(f"[FAIL] Child exited with code {p.exitcode}, expected 9")
            sys.exit(1)
        print(f"[OK] Child exited with code {p.exitcode} (hard kill)")

        # Step 4: Re-open DB and verify
        conn2 = sqlite3.connect(db_path, timeout=10.0)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(
            "SELECT final_status, transmitted, transmitted_ts, fill_price, fill_ts "
            "FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
            (AUDIT_ID,),
        ).fetchone()
        conn2.close()

        assert row is not None, "Row not found after child exit"

        # Assertion 1: TRANSMITTING lock persisted
        assert row["final_status"] == "TRANSMITTING", \
            f"Expected TRANSMITTING, got {row['final_status']}"
        print("[OK] final_status = TRANSMITTING (lock survived hard kill)")

        # Assertion 2: No fill data written (column ownership)
        assert row["fill_price"] is None, \
            f"Expected fill_price=NULL, got {row['fill_price']}"
        print("[OK] fill_price = NULL (column ownership respected)")

        assert row["fill_ts"] is None, \
            f"Expected fill_ts=NULL, got {row['fill_ts']}"
        print("[OK] fill_ts = NULL (column ownership respected)")

        # Assertion 3: transmitted flag still 0
        assert row["transmitted"] == 0, \
            f"Expected transmitted=0, got {row['transmitted']}"
        print("[OK] transmitted = 0 (not yet transmitted)")

        # Assertion 4: transmitted_ts still NULL
        assert row["transmitted_ts"] is None, \
            f"Expected transmitted_ts=NULL, got {row['transmitted_ts']}"
        print("[OK] transmitted_ts = NULL")

        print("\n=== ALL ASSERTIONS PASSED ===")

    finally:
        # Cleanup
        for suffix in ['', '-wal', '-shm']:
            try:
                os.unlink(db_path + suffix)
            except Exception:
                pass
        print("[OK] Temp DB cleaned up")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows compatibility
    main()
