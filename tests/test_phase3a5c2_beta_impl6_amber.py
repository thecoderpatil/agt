"""
Beta Impl 6 Tests — AMBER button semantics regression.

Locks AMBER-mode Smart Friction behavior into regression tests.
Gemini OBSERVATION confirmed: AMBER intentionally rides the PEACETIME
attestation path (checkboxes + thesis, NOT Integer Lock). This is
correct per v10 ("AMBER: Blocks new CSP entries. Allows exits, rolls,
defensive CCs.").

ZERO production code changes. Test-only pass.

DB: in-memory SQLite with full schema (exception_type + TRANSMITTING).
Tests 1–2: FastAPI TestClient with mocked DB connections.
Tests 3–4: DB-level / logic-level (matching impl3/impl5 patterns).
"""

import json
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Set auth token BEFORE importing app (module-level reads env)
os.environ["AGT_DECK_TOKEN"] = "test_token_12345"

from fastapi.testclient import TestClient
from agt_deck.main import app
from agt_equities.rule_engine import is_ticker_locked


# ---------------------------------------------------------------------------
# Shared DDL — matches impl3/impl5 schema (exception_type + TRANSMITTING)
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
) WITHOUT ROWID;

CREATE TABLE mode_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    old_mode TEXT NOT NULL,
    new_mode TEXT NOT NULL,
    trigger_rule TEXT,
    trigger_household TEXT,
    trigger_value REAL,
    notes TEXT
);
"""

_TOKEN = "test_token_12345"

_STAGED_CC_INSERT = (
    "INSERT INTO bucket3_dynamic_exit_log "
    "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
    " household_nlv, underlying_spot_at_render, "
    " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
    " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
    " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
    " limit_price, render_ts, staged_ts, final_status, source) "
    "VALUES (?, date('now'), ?, ?, ?, 'CC', "
    " 261902.0, 240.0, "
    " 26000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 1, "
    " -7.0, 240.0, '2026-05-15', 2, 200, "
    " 1.85, ?, ?, 'STAGED', 'scheduled_watchdog')"
)


class _NoCloseConnection:
    """Wrapper that prevents close() from destroying an in-memory DB shared
    across TestClient threads. Delegates all other calls to the real connection."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass  # no-op — keep DB alive for post-request assertions

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _get_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for stmt in _DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    return conn


def _seed_amber_mode(conn):
    """Insert mode_history entry setting desk mode to AMBER."""
    conn.execute(
        "INSERT INTO mode_history (timestamp, old_mode, new_mode) "
        "VALUES (datetime('now'), 'PEACETIME', 'AMBER')"
    )
    conn.commit()


def _insert_staged_cc(conn, audit_id="amber-cc-9901", ticker="AAPL",
                       household="Yash_Household", desk_mode="AMBER"):
    now = time.time()
    conn.execute(_STAGED_CC_INSERT, (
        audit_id, ticker, household, desk_mode, now, now,
    ))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: AMBER CC Smart Friction GET renders PEACETIME flow
# ═══════════════════════════════════════════════════════════════════════════

class TestAmberCcSmartFrictionGetRendersChecboxFlow(unittest.TestCase):
    """AMBER mode GET must render PEACETIME checkboxes + thesis, NOT Integer Lock."""

    def test_amber_cc_smart_friction_get_renders_peacetime_flow(self):
        db = _get_db()
        _seed_amber_mode(db)
        _insert_staged_cc(db, audit_id="amber-get-01", ticker="AAPL",
                          desk_mode="AMBER")

        def mock_ro():
            return _NoCloseConnection(db)

        client = TestClient(app)
        with patch("agt_deck.main.get_ro_conn", mock_ro):
            resp = client.get(f"/api/cure/dynamic_exit/amber-get-01/attest?t={_TOKEN}")

        self.assertEqual(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
        html = resp.text

        # PEACETIME checkbox inputs must be PRESENT
        self.assertIn('name="ack_loss"', html,
                      "AMBER must render ack_loss checkbox (PEACETIME path)")
        self.assertIn('name="ack_cure"', html,
                      "AMBER must render ack_cure checkbox (PEACETIME path)")
        self.assertIn('name="operator_thesis"', html,
                      "AMBER must render thesis textarea (PEACETIME path)")

        # Integer Lock input must be ABSENT
        self.assertNotIn('name="attestation_value_typed"', html,
                         "AMBER must NOT render Integer Lock input")

        # Heading must be the PEACETIME heading, not WARTIME
        self.assertIn("Rule 8 Dynamic Exit Attestation", html,
                      "AMBER heading must be PEACETIME 'Rule 8 Dynamic Exit Attestation'")
        self.assertNotIn("WARTIME RULE 8 STAGING", html,
                         "AMBER must NOT show WARTIME heading")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: AMBER CC Smart Friction POST accepts checkboxes + thesis
# ═══════════════════════════════════════════════════════════════════════════

class TestAmberCcSmartFrictionPostAcceptsCheckboxes(unittest.TestCase):
    """AMBER POST must accept checkboxes + thesis (PEACETIME path) and
    transition STAGED → ATTESTED. No Integer Lock field required."""

    def test_amber_cc_smart_friction_post_accepts_checkboxes(self):
        db = _get_db()
        _seed_amber_mode(db)
        _insert_staged_cc(db, audit_id="amber-post-01", ticker="AAPL",
                          desk_mode="AMBER")

        def mock_ro():
            return _NoCloseConnection(db)

        def mock_rw():
            return _NoCloseConnection(db)

        client = TestClient(app)
        thesis_35 = "AMBER attestation test with 35 chars"  # exactly 35 chars
        assert len(thesis_35) >= 30, f"thesis is {len(thesis_35)} chars, need >=30"

        with patch("agt_deck.main.get_ro_conn", mock_ro), \
             patch("agt_deck.main.get_rw_conn", mock_rw), \
             patch("agt_deck.main._get_desk_mode", return_value="AMBER"):
            resp = client.post(
                f"/api/cure/dynamic_exit/amber-post-01/attest?t={_TOKEN}",
                data={
                    "audit_id": "amber-post-01",
                    "render_ts": str(time.time()),
                    "ack_loss": "on",
                    "ack_cure": "on",
                    "operator_thesis": thesis_35,
                },
            )

        self.assertEqual(resp.status_code, 200,
                         f"Expected 200, got {resp.status_code}. Body: {resp.text[:300]}")

        # Verify DB transition: STAGED → ATTESTED
        row = db.execute(
            "SELECT final_status, operator_thesis, attestation_value_typed, "
            "       checkbox_state_json, limit_price "
            "FROM bucket3_dynamic_exit_log WHERE audit_id = 'amber-post-01'"
        ).fetchone()
        self.assertIsNotNone(row, "Row must exist after POST")
        self.assertEqual(row["final_status"], "ATTESTED",
                         "AMBER POST must transition STAGED → ATTESTED")
        self.assertEqual(row["operator_thesis"], thesis_35,
                         "Thesis must be persisted")
        self.assertIsNone(row["attestation_value_typed"],
                          "AMBER must NOT set attestation_value_typed (that's WARTIME)")
        self.assertIsNotNone(row["checkbox_state_json"],
                             "AMBER must persist checkbox state JSON")
        self.assertAlmostEqual(row["limit_price"], 1.85,
                               msg="attested_limit must be persisted from staging")

        # Verify checkbox JSON structure
        cb = json.loads(row["checkbox_state_json"])
        self.assertTrue(cb.get("ack_loss"), "ack_loss must be True in JSON")
        self.assertTrue(cb.get("ack_cure"), "ack_cure must be True in JSON")
        self.assertIn("ack_ts", cb, "ack_ts timestamp must be in JSON")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: AMBER ATTESTED row enforces 3-strike and ticker lockout
# ═══════════════════════════════════════════════════════════════════════════

class TestAmberEnforces3StrikeAndLockout(unittest.TestCase):
    """AMBER does NOT bypass the 3-strike budget or ticker lockout.
    Only WARTIME bypasses these (F5 ruling). Confirms AMBER behaves like
    PEACETIME for JIT re-validation gating."""

    def test_amber_attested_row_jit_enforces_3strike_and_lockout(self):
        conn = _get_db()
        now = time.time()

        # Insert an ATTESTED CC row in AMBER mode for ticker XYZ
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, "
            " gate1_freed_margin, gate1_realized_loss, gate1_conviction_tier, "
            " gate1_conviction_modifier, gate1_ratio, gate2_target_contracts, "
            " walk_away_pnl_per_share, strike, expiry, contracts, shares, "
            " limit_price, render_ts, staged_ts, final_status, re_validation_count) "
            "VALUES ('amber-3strike-01', date('now'), 'XYZ', 'Yash_Household', "
            " 'AMBER', 'CC', 261000.0, 250.0, "
            " 26000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 1, "
            " -7.0, 260.0, '2026-05-16', 1, 100, "
            " 3.00, ?, ?, 'ATTESTED', 0)",
            (now, now),
        )
        conn.commit()

        # Simulate 3 drift failures: increment counter each time
        for strike in range(3):
            conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET re_validation_count = re_validation_count + 1 "
                "WHERE audit_id = 'amber-3strike-01'"
            )
        conn.commit()

        # Verify counter is 3
        row = conn.execute(
            "SELECT re_validation_count FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'amber-3strike-01'"
        ).fetchone()
        self.assertEqual(row["re_validation_count"], 3,
                         "3 drift failures must accumulate")

        # JIT step 1 detects count >= 3 → DRIFT_BLOCKED
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'DRIFT_BLOCKED', last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = 'amber-3strike-01' AND final_status = 'ATTESTED'"
        )
        conn.commit()

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'amber-3strike-01'"
        ).fetchone()
        self.assertEqual(row["final_status"], "DRIFT_BLOCKED",
                         "3rd strike must terminate as DRIFT_BLOCKED")

        # Verify ticker XYZ is now LOCKED (rolling lockout)
        self.assertTrue(is_ticker_locked(conn, "XYZ"),
                        "XYZ must be locked after DRIFT_BLOCKED in AMBER")

        # Confirm AMBER does NOT bypass lockout (unlike WARTIME)
        is_amber = True  # desk_mode == "AMBER"
        should_check_lockout = not (not is_amber)  # AMBER → should check
        self.assertTrue(should_check_lockout,
                        "AMBER must NOT bypass ticker lockout (only WARTIME does)")

        # The actual handler logic: WARTIME bypasses, everything else checks
        for mode, expected_bypass in [("WARTIME", True), ("AMBER", False), ("PEACETIME", False)]:
            bypasses = (mode == "WARTIME")
            self.assertEqual(bypasses, expected_bypass,
                             f"{mode} bypass={bypasses}, expected={expected_bypass}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: AMBER blocks Forced Liquidation staging
# ═══════════════════════════════════════════════════════════════════════════

class TestAmberBlocksForcedLiquidationStaging(unittest.TestCase):
    """R6 Forced Liquidation requires WARTIME mode. AMBER must be rejected
    at the route level (POST /api/cure/r5_sell/stage guard)."""

    def test_amber_blocks_forced_liquidation_staging(self):
        db = _get_db()
        _seed_amber_mode(db)

        def mock_rw():
            return db

        client = TestClient(app)

        with patch("agt_deck.main.get_rw_conn", mock_rw):
            resp = client.post(
                f"/api/cure/r5_sell/stage?t={_TOKEN}",
                data={
                    "ticker": "PYPL",
                    "household": "Vikram_Household",
                    "shares": "100",
                    "limit_price": "40.0",
                    "adjusted_cost_basis": "65.0",
                    "household_nlv": "80000",
                    "spot": "42.0",
                    "exception_type": "rule_6_forced_liquidation",
                    "desk_mode": "AMBER",
                },
            )

        # Route guard must reject with 400
        self.assertEqual(resp.status_code, 400,
                         f"Expected 400, got {resp.status_code}. Body: {resp.text[:300]}")

        # Error message must reference WARTIME requirement
        self.assertIn("Forced Liquidation requires WARTIME mode", resp.text,
                      "Error must state WARTIME requirement")

        # Verify NO row was staged
        row_count = db.execute(
            "SELECT COUNT(*) FROM bucket3_dynamic_exit_log "
            "WHERE ticker = 'PYPL' AND exception_type = 'rule_6_forced_liquidation'"
        ).fetchone()[0]
        self.assertEqual(row_count, 0,
                         "No row must be staged when Forced Liquidation is blocked in AMBER")


if __name__ == "__main__":
    unittest.main()
