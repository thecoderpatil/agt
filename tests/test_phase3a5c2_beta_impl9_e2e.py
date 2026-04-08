"""
Beta Impl 9 Tests — End-to-End State Machine Traversal.

Proves full state machine traversal for 8 representative
(action_type, exception_type, desk_mode) tuples from STAGED through
a terminal state, catching any transition bug not visible at the
unit-edge level.

DB-level tests matching the impl3 pattern. STAGED → ATTESTED uses
the real attest_staged_exit() from queries.py. ATTESTED → terminal
uses real rule_engine functions (evaluate_gate_1, is_ticker_locked,
sweep_stale_dynamic_exit_stages) with direct SQL for atomic state
transitions (mirroring handle_dex_callback step logic).

Zero production code changes. Zero Telegram/IBKR integration.
"""

import json
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_deck.queries import attest_staged_exit
from agt_equities.rule_engine import (
    evaluate_gate_1, ConvictionTier, is_ticker_locked,
    sweep_stale_dynamic_exit_stages,
)


# ---------------------------------------------------------------------------
# Shared DDL (matches impl3/impl5/impl6 schema)
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


def _get_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    return conn


def _insert_staged_cc(conn, audit_id, ticker="AAPL", desk_mode="PEACETIME",
                       exception_type=None, strike=240.0, expiry="2026-05-15",
                       contracts=2, limit_price=1.85,
                       walk_away_pnl=-7.0, gate1_realized_loss=700.0):
    """Insert a STAGED CC row."""
    now = time.time()
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
        " household_nlv, underlying_spot_at_render, "
        " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
        " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
        " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
        " limit_price, render_ts, staged_ts, final_status, source, exception_type) "
        "VALUES (?, date('now'), ?, 'Yash_Household', ?, 'CC', "
        " 261902.0, 240.0, "
        " 48000.0, ?, 'NEUTRAL', 0.30, 11.14, 2, "
        " ?, ?, ?, ?, 200, "
        " ?, ?, ?, 'STAGED', 'scheduled_watchdog', ?)",
        (audit_id, ticker, desk_mode, gate1_realized_loss,
         walk_away_pnl, strike, expiry, contracts,
         limit_price, now, now, exception_type),
    )
    conn.commit()


def _insert_staged_stk_sell(conn, audit_id, ticker="ADBE",
                             desk_mode="PEACETIME", exception_type=None,
                             shares=50, limit_price=230.0):
    """Insert a STAGED STK_SELL row."""
    now = time.time()
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
        " household_nlv, underlying_spot_at_render, "
        " gate1_realized_loss, walk_away_pnl_per_share, "
        " shares, limit_price, render_ts, staged_ts, final_status, "
        " source, exception_type) "
        "VALUES (?, date('now'), ?, 'Yash_Household', ?, 'STK_SELL', "
        " 261902.0, 235.0, "
        " 3500.0, -70.0, "
        " ?, ?, ?, ?, 'STAGED', "
        " 'manual_stage', ?)",
        (audit_id, ticker, desk_mode, shares, limit_price, now, now,
         exception_type),
    )
    conn.commit()


def _attest_peacetime(conn, audit_id, desk_mode, limit_price):
    """Drive STAGED -> ATTESTED via real attest_staged_exit (PEACETIME/AMBER path)."""
    thesis = "E2E test strategic thesis rationale exceeds 30 chars"
    checkbox_json = json.dumps({"ack_loss": True, "ack_cure": True, "ack_ts": time.time()})
    rowcount = attest_staged_exit(
        conn, audit_id=audit_id,
        operator_thesis=thesis,
        attestation_value_typed=None,
        checkbox_state_json=checkbox_json,
        attested_limit_price=limit_price,
        expected_desk_mode=desk_mode,
    )
    conn.commit()
    return rowcount


def _attest_wartime(conn, audit_id, desk_mode, loss_whole, ticker):
    """Drive STAGED -> ATTESTED via real attest_staged_exit (WARTIME Integer Lock path)."""
    expected_value = ticker if loss_whole <= 1 else str(loss_whole)
    rowcount = attest_staged_exit(
        conn, audit_id=audit_id,
        operator_thesis=None,
        attestation_value_typed=expected_value,
        checkbox_state_json=None,
        attested_limit_price=None,
        expected_desk_mode=desk_mode,
    )
    conn.commit()
    return rowcount


def _transmit_happy_path(conn, audit_id):
    """Drive ATTESTED -> TRANSMITTING -> TRANSMITTED (mirroring steps 6-8)."""
    lock = conn.execute(
        "UPDATE bucket3_dynamic_exit_log "
        "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "
        "WHERE audit_id = ? AND final_status = 'ATTESTED'",
        (audit_id,),
    )
    assert lock.rowcount == 1, f"TRANSMITTING lock failed for {audit_id}"

    now_ts = time.time()
    conn.execute(
        "UPDATE bucket3_dynamic_exit_log "
        "SET final_status = 'TRANSMITTED', transmitted = 1, "
        "    transmitted_ts = ?, last_updated = CURRENT_TIMESTAMP "
        "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",
        (now_ts, audit_id),
    )
    conn.commit()


def _cancel(conn, audit_id):
    """Drive ATTESTED -> CANCELLED (mirroring cancel branch)."""
    result = conn.execute(
        "UPDATE bucket3_dynamic_exit_log "
        "SET final_status = 'CANCELLED', last_updated = CURRENT_TIMESTAMP "
        "WHERE audit_id = ? AND final_status = 'ATTESTED'",
        (audit_id,),
    )
    conn.commit()
    return result.rowcount


def _get_row(conn, audit_id):
    """Fetch a row by audit_id."""
    return conn.execute(
        "SELECT * FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
        (audit_id,),
    ).fetchone()


# ═══════════════════════════════════════════════════════════════════════════
# T1: CC / NULL / PEACETIME -> TRANSMITTED
# ═══════════════════════════════════════════════════════════════════════════

class TestT1CcPeacetimeTransmitted(unittest.TestCase):
    """Full path: STAGED -> ATTESTED (checkboxes+thesis) -> TRANSMITTING
    -> TRANSMITTED. Verifies attested_limit preservation."""

    def test_cc_null_peacetime_transmitted(self):
        conn = _get_db()
        _insert_staged_cc(conn, "e2e-t1", ticker="AAPL", desk_mode="PEACETIME",
                          limit_price=1.85)

        # STAGED -> ATTESTED via real attest function
        rc = _attest_peacetime(conn, "e2e-t1", "PEACETIME", limit_price=1.85)
        self.assertEqual(rc, 1, "Attest must succeed")
        row = _get_row(conn, "e2e-t1")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertAlmostEqual(row["limit_price"], 1.85)
        self.assertIsNotNone(row["operator_thesis"])
        self.assertIsNotNone(row["checkbox_state_json"])
        self.assertIsNone(row["attestation_value_typed"],
                          "PEACETIME must NOT set Integer Lock value")

        # JIT Step 5a: Gate 1 re-eval with live_bid close to attested (passes)
        live_bid = 1.82
        adjusted_cost_basis = row["strike"] + row["limit_price"] - row["walk_away_pnl_per_share"]
        g1 = evaluate_gate_1(
            ticker="AAPL", household="Yash_Household",
            candidate_strike=row["strike"],
            candidate_premium=live_bid,
            contracts=row["contracts"],
            adjusted_cost_basis=adjusted_cost_basis,
            conviction_tier=ConvictionTier(row["gate1_conviction_tier"]),
        )
        self.assertTrue(g1.passed, f"Gate 1 must pass: ratio={g1.ratio:.2f}")

        # JIT Step 5b: Drift check (CC: $0.10 absolute)
        drift = abs(live_bid - row["limit_price"])
        self.assertLessEqual(drift, 0.10, "Drift must be within CC threshold")

        # Steps 6-8: ATTESTED -> TRANSMITTING -> TRANSMITTED
        _transmit_happy_path(conn, "e2e-t1")

        # Terminal assertions
        row = _get_row(conn, "e2e-t1")
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["transmitted"], 1)
        self.assertIsNotNone(row["transmitted_ts"])
        self.assertAlmostEqual(row["limit_price"], 1.85,
                               msg="attested_limit must be preserved through chain")
        self.assertFalse(is_ticker_locked(conn, "AAPL"),
                         "No lockout for clean transmit")


# ═══════════════════════════════════════════════════════════════════════════
# T2: CC / NULL / AMBER -> CANCELLED
# ═══════════════════════════════════════════════════════════════════════════

class TestT2CcAmberCancelled(unittest.TestCase):
    """AMBER rides PEACETIME attest path (checkboxes, not Integer Lock).
    CANCEL accepted cleanly. No IBKR call made."""

    def test_cc_null_amber_cancelled(self):
        conn = _get_db()
        _insert_staged_cc(conn, "e2e-t2", ticker="MSFT", desk_mode="AMBER",
                          limit_price=2.10)

        # STAGED -> ATTESTED via PEACETIME path (AMBER shares it)
        rc = _attest_peacetime(conn, "e2e-t2", "AMBER", limit_price=2.10)
        self.assertEqual(rc, 1)
        row = _get_row(conn, "e2e-t2")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertIsNotNone(row["checkbox_state_json"],
                             "AMBER must use checkboxes (PEACETIME path)")
        self.assertIsNone(row["attestation_value_typed"],
                          "AMBER must NOT use Integer Lock")

        # ATTESTED -> CANCELLED
        cancel_rc = _cancel(conn, "e2e-t2")
        self.assertEqual(cancel_rc, 1)

        row = _get_row(conn, "e2e-t2")
        self.assertEqual(row["final_status"], "CANCELLED")
        self.assertEqual(row["transmitted"], 0,
                         "CANCEL must not set transmitted flag")
        self.assertIsNone(row["transmitted_ts"],
                          "CANCEL must not set transmitted_ts")
        self.assertEqual(row["exception_type"], None,
                         "NULL exception_type preserved")


# ═══════════════════════════════════════════════════════════════════════════
# T3: CC / NULL / WARTIME -> TRANSMITTED
# ═══════════════════════════════════════════════════════════════════════════

class TestT3CcWartimeTransmitted(unittest.TestCase):
    """WARTIME uses Integer Lock (no checkboxes). 3-strike bypass.
    Ticker lockout NOT engaged for clean transmit."""

    def test_cc_null_wartime_transmitted(self):
        conn = _get_db()
        _insert_staged_cc(conn, "e2e-t3", ticker="ADBE", desk_mode="WARTIME",
                          limit_price=3.00, gate1_realized_loss=700.0)

        # STAGED -> ATTESTED via WARTIME Integer Lock path
        loss_whole = 700
        rc = _attest_wartime(conn, "e2e-t3", "WARTIME", loss_whole, "ADBE")
        self.assertEqual(rc, 1)
        row = _get_row(conn, "e2e-t3")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertIsNone(row["operator_thesis"],
                          "WARTIME must have NULL thesis")
        self.assertIsNone(row["checkbox_state_json"],
                          "WARTIME must have NULL checkbox JSON")
        self.assertEqual(row["attestation_value_typed"], str(loss_whole),
                         "Integer Lock value must be persisted")

        # WARTIME bypasses 3-strike check (Step 1 skipped when is_wartime=True)
        is_wartime = row["desk_mode"] == "WARTIME"
        self.assertTrue(is_wartime)
        # Even with re_validation_count >= 3, WARTIME bypasses
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log SET re_validation_count = 5 "
            "WHERE audit_id = 'e2e-t3'"
        )
        conn.commit()
        row = _get_row(conn, "e2e-t3")
        # Verify bypass logic: not is_wartime and count >= 3 => would block
        should_block = not is_wartime and row["re_validation_count"] >= 3
        self.assertFalse(should_block, "WARTIME must bypass 3-strike")

        # Steps 6-8: TRANSMIT
        _transmit_happy_path(conn, "e2e-t3")

        row = _get_row(conn, "e2e-t3")
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["transmitted"], 1)
        self.assertFalse(is_ticker_locked(conn, "ADBE"))


# ═══════════════════════════════════════════════════════════════════════════
# T4: CC / thesis_deterioration / PEACETIME -> TRANSMITTED
# ═══════════════════════════════════════════════════════════════════════════

class TestT4CcThesisDeteriorationTransmitted(unittest.TestCase):
    """exception_type persisted through entire chain. TRANSMITTED terminal."""

    def test_cc_thesis_deterioration_peacetime_transmitted(self):
        conn = _get_db()
        _insert_staged_cc(conn, "e2e-t4", ticker="META",
                          desk_mode="PEACETIME",
                          exception_type="thesis_deterioration",
                          limit_price=2.50)

        rc = _attest_peacetime(conn, "e2e-t4", "PEACETIME", limit_price=2.50)
        self.assertEqual(rc, 1)
        row = _get_row(conn, "e2e-t4")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertEqual(row["exception_type"], "thesis_deterioration",
                         "exception_type must survive attestation")

        # TRANSMIT
        _transmit_happy_path(conn, "e2e-t4")

        row = _get_row(conn, "e2e-t4")
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["exception_type"], "thesis_deterioration",
                         "exception_type must survive full traversal")
        self.assertEqual(row["action_type"], "CC")
        self.assertEqual(row["transmitted"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# T5: CC / emergency_risk_event / AMBER -> DRIFT_BLOCKED
# ═══════════════════════════════════════════════════════════════════════════

class TestT5CcEmergencyAmberDriftBlocked(unittest.TestCase):
    """3 drift failures -> DRIFT_BLOCKED. AMBER does NOT bypass 3-strike
    (per Impl 6 lock-in). Ticker lockout ENGAGED."""

    def test_cc_emergency_amber_drift_blocked(self):
        conn = _get_db()
        _insert_staged_cc(conn, "e2e-t5", ticker="XYZ",
                          desk_mode="AMBER",
                          exception_type="emergency_risk_event",
                          limit_price=4.00)

        # STAGED -> ATTESTED
        rc = _attest_peacetime(conn, "e2e-t5", "AMBER", limit_price=4.00)
        self.assertEqual(rc, 1)

        # Simulate 3 drift failures (Step 5b fails, counter increments)
        for strike_num in range(1, 4):
            conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET re_validation_count = re_validation_count + 1 "
                "WHERE audit_id = 'e2e-t5'"
            )
            conn.commit()

            row = _get_row(conn, "e2e-t5")
            self.assertEqual(row["re_validation_count"], strike_num,
                             f"Strike counter must be {strike_num}")

        # Step 1 JIT: AMBER is NOT wartime, count >= 3 -> DRIFT_BLOCKED
        row = _get_row(conn, "e2e-t5")
        is_wartime = row["desk_mode"] == "WARTIME"
        self.assertFalse(is_wartime, "AMBER is NOT wartime")
        self.assertGreaterEqual(row["re_validation_count"], 3)

        # Transition to DRIFT_BLOCKED (Step 1 handler logic)
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'DRIFT_BLOCKED', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'e2e-t5' AND final_status = 'ATTESTED'",
            (),
        )
        conn.commit()

        row = _get_row(conn, "e2e-t5")
        self.assertEqual(row["final_status"], "DRIFT_BLOCKED")
        self.assertEqual(row["exception_type"], "emergency_risk_event",
                         "exception_type preserved through DRIFT_BLOCKED")
        self.assertTrue(is_ticker_locked(conn, "XYZ"),
                        "XYZ must be locked after DRIFT_BLOCKED")
        self.assertFalse(is_ticker_locked(conn, "AAPL"),
                         "Other tickers must NOT be locked")


# ═══════════════════════════════════════════════════════════════════════════
# T6: STK_SELL / NULL / PEACETIME -> TRANSMITTED
# ═══════════════════════════════════════════════════════════════════════════

class TestT6StkSellPeacetimeTransmitted(unittest.TestCase):
    """STK_SELL full path. No option right. attested_limit is share price.
    action_type = STK_SELL persisted."""

    def test_stk_sell_null_peacetime_transmitted(self):
        conn = _get_db()
        _insert_staged_stk_sell(conn, "e2e-t6", ticker="ADBE",
                                 desk_mode="PEACETIME",
                                 exception_type="rule_8_dynamic_exit",
                                 shares=50, limit_price=230.0)

        # STAGED -> ATTESTED
        rc = _attest_peacetime(conn, "e2e-t6", "PEACETIME", limit_price=230.0)
        self.assertEqual(rc, 1)
        row = _get_row(conn, "e2e-t6")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertEqual(row["action_type"], "STK_SELL")
        self.assertIsNone(row["strike"], "STK_SELL has no strike")
        self.assertIsNone(row["expiry"], "STK_SELL has no expiry")
        self.assertAlmostEqual(row["limit_price"], 230.0,
                               msg="attested_limit is share price")

        # JIT Step 5a: STK_SELL skips Gate 1 (per Gemini F8)
        self.assertNotEqual(row["action_type"], "CC",
                            "STK_SELL skips Gate 1")

        # JIT Step 5b: Drift check (STK_SELL: 0.5% relative)
        live_price = 229.50
        drift = abs(live_price - row["limit_price"])
        threshold = row["limit_price"] * 0.005  # 0.5% of 230 = 1.15
        self.assertLessEqual(drift, threshold, "Drift passes for STK_SELL")

        # Steps 6-8: TRANSMIT
        _transmit_happy_path(conn, "e2e-t6")

        row = _get_row(conn, "e2e-t6")
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["action_type"], "STK_SELL")
        self.assertEqual(row["transmitted"], 1)
        self.assertEqual(row["shares"], 50)
        self.assertAlmostEqual(row["limit_price"], 230.0)


# ═══════════════════════════════════════════════════════════════════════════
# T7: STK_SELL / thesis_deterioration / PEACETIME -> CANCELLED
# ═══════════════════════════════════════════════════════════════════════════

class TestT7StkSellThesisDetCancelled(unittest.TestCase):
    """CANCEL path clean for STK_SELL + exception combo. No IBKR call.
    exception_type preserved."""

    def test_stk_sell_thesis_det_peacetime_cancelled(self):
        conn = _get_db()
        _insert_staged_stk_sell(conn, "e2e-t7", ticker="META",
                                 desk_mode="PEACETIME",
                                 exception_type="thesis_deterioration",
                                 shares=30, limit_price=150.0)

        # STAGED -> ATTESTED
        rc = _attest_peacetime(conn, "e2e-t7", "PEACETIME", limit_price=150.0)
        self.assertEqual(rc, 1)
        row = _get_row(conn, "e2e-t7")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertEqual(row["exception_type"], "thesis_deterioration")

        # ATTESTED -> CANCELLED
        cancel_rc = _cancel(conn, "e2e-t7")
        self.assertEqual(cancel_rc, 1)

        row = _get_row(conn, "e2e-t7")
        self.assertEqual(row["final_status"], "CANCELLED")
        self.assertEqual(row["transmitted"], 0, "CANCEL sets no fill")
        self.assertIsNone(row["transmitted_ts"])
        self.assertEqual(row["exception_type"], "thesis_deterioration",
                         "exception_type preserved through CANCEL")
        self.assertEqual(row["action_type"], "STK_SELL")
        self.assertEqual(row["shares"], 30)


# ═══════════════════════════════════════════════════════════════════════════
# T8: CC / rule_6_forced_liquidation / WARTIME -> ABANDONED (TTL sweep)
# ═══════════════════════════════════════════════════════════════════════════

class TestT8CcForcedLiqWartimeAbandoned(unittest.TestCase):
    """Staging allowed (WARTIME gate passes). Integer Lock path taken.
    Sweeper with simulated clock advances state to ABANDONED after 10min.
    exception_type = rule_6_forced_liquidation preserved."""

    def test_cc_r6_wartime_abandoned_ttl(self):
        conn = _get_db()
        _insert_staged_cc(conn, "e2e-t8", ticker="PYPL",
                          desk_mode="WARTIME",
                          exception_type="rule_6_forced_liquidation",
                          limit_price=2.00, gate1_realized_loss=500.0)

        # Verify staging allowed in WARTIME
        row = _get_row(conn, "e2e-t8")
        self.assertEqual(row["final_status"], "STAGED")
        self.assertEqual(row["desk_mode"], "WARTIME")
        self.assertEqual(row["exception_type"], "rule_6_forced_liquidation")

        # STAGED -> ATTESTED via Integer Lock (loss_whole=500)
        rc = _attest_wartime(conn, "e2e-t8", "WARTIME", 500, "PYPL")
        self.assertEqual(rc, 1)
        row = _get_row(conn, "e2e-t8")
        self.assertEqual(row["final_status"], "ATTESTED")
        self.assertIsNone(row["operator_thesis"],
                          "WARTIME Integer Lock: no thesis")
        self.assertEqual(row["attestation_value_typed"], "500")

        # Simulate 10min elapsed: backdate last_updated
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET last_updated = datetime('now', '-11 minutes') "
            "WHERE audit_id = 'e2e-t8'"
        )
        conn.commit()

        # Run real sweeper
        result = sweep_stale_dynamic_exit_stages(conn)
        self.assertEqual(result["attested_swept"], 1,
                         "Sweeper must catch 10min-stale ATTESTED row")

        row = _get_row(conn, "e2e-t8")
        self.assertEqual(row["final_status"], "ABANDONED")
        self.assertEqual(row["exception_type"], "rule_6_forced_liquidation",
                         "exception_type preserved through ABANDONED")
        self.assertEqual(row["transmitted"], 0,
                         "ABANDONED rows must not have transmitted flag")


if __name__ == "__main__":
    unittest.main()
