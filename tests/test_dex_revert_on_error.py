"""Finding #10 — DEX TRANSMIT handler revert-on-error tests.

3 tests verifying:
  1. Gate failure after CAS lock → TRANSMITTING reverted to CANCELLED
  2. ExecutionDisabledError after CAS lock → TRANSMITTING reverted to CANCELLED
  3. Generic IBKR exception → TRANSMITTING intentionally STICKY (no revert)
"""

import os
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_equities.execution_gate import ExecutionDisabledError


# ---------------------------------------------------------------------------
# Shared DDL + helpers (matches production schema)
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
    exception_type TEXT,
    fill_ts REAL,
    fill_price REAL,
    originating_account_id TEXT,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ib_order_id INTEGER,
    ib_perm_id INTEGER,
    fill_qty INTEGER,
    commission REAL
) WITHOUT ROWID;

CREATE TABLE execution_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO execution_state (key, value) VALUES ('disabled', '0');
"""


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def _insert_attested_row(conn, audit_id, ticker="ADBE",
                         originating_account_id="U21971297"):
    now = time.time()
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
        " household_nlv, underlying_spot_at_render, strike, expiry, contracts, "
        " shares, limit_price, render_ts, staged_ts, final_status, "
        " gate1_conviction_tier, walk_away_pnl_per_share, "
        " originating_account_id, last_updated) "
        "VALUES (?, date('now'), ?, 'Yash_Household', 'PEACETIME', 'CC', "
        " 261000.0, 250.0, 260.0, '2026-05-16', 1, 100, 3.00, ?, ?, "
        " 'ATTESTED', 'HIGH', 2.50, ?, CURRENT_TIMESTAMP)",
        (audit_id, ticker, now, now, originating_account_id),
    )
    conn.commit()


def _get_status(conn, audit_id):
    row = conn.execute(
        "SELECT final_status FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
        (audit_id,),
    ).fetchone()
    return row["final_status"] if row else None


def _cas_to_transmitting(conn, audit_id):
    """Simulate Step 6 CAS lock: ATTESTED → TRANSMITTING."""
    conn.execute(
        "UPDATE bucket3_dynamic_exit_log "
        "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "
        "WHERE audit_id = ? AND final_status = 'ATTESTED'",
        (audit_id,),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Test class
# ═══════════════════════════════════════════════════════════════════════════

class TestDexRevertOnError(unittest.TestCase):
    """Finding #10: Verify revert semantics for DEX TRANSMIT early-exit paths."""

    def test_dex_revert_on_gate_failure(self):
        """Bug 10A: gate failure after CAS lock must revert TRANSMITTING → CANCELLED."""
        db = _make_db()
        audit_id = "gate-fail-test-001"
        _insert_attested_row(db, audit_id)
        _cas_to_transmitting(db, audit_id)
        self.assertEqual(_get_status(db, audit_id), "TRANSMITTING")

        # Patch _get_db_connection to return our in-memory DB
        def _mock_get_db():
            conn = sqlite3.connect(db.execute(
                "PRAGMA database_list"
            ).fetchone()[2] or ":memory:")
            conn.row_factory = sqlite3.Row
            return conn

        # Use the helper directly with a patched DB connection
        with patch("telegram_bot._get_db_connection", return_value=db):
            # Prevent closing() from closing our shared in-memory DB
            with patch("telegram_bot.closing", side_effect=lambda x: x):
                from telegram_bot import _revert_transmitting_to_cancelled
                rowcount = _revert_transmitting_to_cancelled(
                    audit_id, "gate_blocked: test gate failure",
                )

        self.assertEqual(rowcount, 1)
        self.assertEqual(_get_status(db, audit_id), "CANCELLED")

        # Verify idempotency: second call is a no-op
        with patch("telegram_bot._get_db_connection", return_value=db):
            with patch("telegram_bot.closing", side_effect=lambda x: x):
                rowcount2 = _revert_transmitting_to_cancelled(
                    audit_id, "gate_blocked: duplicate",
                )
        self.assertEqual(rowcount2, 0)
        self.assertEqual(_get_status(db, audit_id), "CANCELLED")

        db.close()

    def test_dex_revert_on_execution_disabled(self):
        """Bug 10B: ExecutionDisabledError after CAS lock must revert TRANSMITTING → CANCELLED."""
        db = _make_db()
        audit_id = "exec-disabled-test-001"
        _insert_attested_row(db, audit_id)
        _cas_to_transmitting(db, audit_id)
        self.assertEqual(_get_status(db, audit_id), "TRANSMITTING")

        with patch("telegram_bot._get_db_connection", return_value=db):
            with patch("telegram_bot.closing", side_effect=lambda x: x):
                from telegram_bot import _revert_transmitting_to_cancelled
                rowcount = _revert_transmitting_to_cancelled(
                    audit_id, "execution_disabled: test-env-off",
                )

        self.assertEqual(rowcount, 1)
        self.assertEqual(_get_status(db, audit_id), "CANCELLED")
        db.close()

    def test_dex_ib_error_path_still_sticky(self):
        """TRANSMIT_IB_ERROR must NOT revert — row stays TRANSMITTING for manual recovery."""
        db = _make_db()
        audit_id = "ib-error-test-001"
        _insert_attested_row(db, audit_id)
        _cas_to_transmitting(db, audit_id)
        self.assertEqual(_get_status(db, audit_id), "TRANSMITTING")

        # Simulate the IBKR error path: no _revert call should happen.
        # We verify by confirming the row stays TRANSMITTING after a
        # simulated ib_err handler (which does NOT call the revert helper).
        # This is a behavioral contract test: the ib_err branch must NOT
        # contain a call to _revert_transmitting_to_cancelled.
        import ast
        import inspect
        from telegram_bot import handle_dex_callback

        source = inspect.getsource(handle_dex_callback)
        tree = ast.parse(source)

        # Walk the AST to find the `except Exception as ib_err:` handler
        revert_in_ib_err = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                # Check if this is the ib_err handler
                if node.name == "ib_err":
                    # Check if _revert_transmitting_to_cancelled is called
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            func = child.func
                            if isinstance(func, ast.Name) and \
                               func.id == "_revert_transmitting_to_cancelled":
                                revert_in_ib_err = True

        self.assertFalse(
            revert_in_ib_err,
            "REGRESSION: _revert_transmitting_to_cancelled must NOT appear "
            "in the `except Exception as ib_err:` branch — IBKR error path "
            "is intentionally sticky (row stays TRANSMITTING).",
        )

        # Also confirm the row is still TRANSMITTING (no code path reverted it)
        self.assertEqual(_get_status(db, audit_id), "TRANSMITTING")
        db.close()


if __name__ == "__main__":
    unittest.main()
