"""
Beta Impl 8 Tests — Adaptive Thesis Copy Templates.

Covers: exception-type-aware 422 error copy (FIX 1),
        CC vs STK_SELL R8-default placeholder split (FIX 2).

DB: in-memory SQLite with full schema.
"""

import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ["AGT_DECK_TOKEN"] = "test_token_12345"

from fastapi.testclient import TestClient
from agt_deck.main import app


# ---------------------------------------------------------------------------
# Shared DDL + helpers (matching impl6 pattern)
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


class _NoCloseConnection:
    """Wrapper that prevents close() from destroying an in-memory DB shared
    across TestClient threads."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

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


def _seed_peacetime(conn):
    conn.execute(
        "INSERT INTO mode_history (timestamp, old_mode, new_mode) "
        "VALUES (datetime('now'), 'PEACETIME', 'PEACETIME')"
    )
    conn.commit()


def _insert_staged_cc(conn, audit_id, ticker="META", desk_mode="PEACETIME",
                       exception_type=None):
    """Insert a STAGED CC row with optional exception_type."""
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
        " 26000.0, 700.0, 'NEUTRAL', 0.30, 11.14, 1, "
        " -7.0, 240.0, '2026-05-15', 2, 200, "
        " 1.50, ?, ?, 'STAGED', 'scheduled_watchdog', ?)",
        (audit_id, ticker, desk_mode, now, now, exception_type),
    )
    conn.commit()


def _insert_staged_stk_sell(conn, audit_id, ticker="ADBE", desk_mode="PEACETIME",
                             exception_type=None):
    """Insert a STAGED STK_SELL row with optional exception_type."""
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
        " 500.0, -70.0, "
        " 50, 230.0, ?, ?, 'STAGED', "
        " 'manual_stage', ?)",
        (audit_id, ticker, desk_mode, now, now, exception_type),
    )
    conn.commit()


def _post_short_thesis(client, db, audit_id):
    """POST with a too-short thesis (10 chars) and return the response."""
    wrapped = _NoCloseConnection(db)
    with patch("agt_deck.main.get_rw_conn", lambda: wrapped), \
         patch("agt_deck.main._get_desk_mode", return_value="PEACETIME"):
        return client.post(
            f"/api/cure/dynamic_exit/{audit_id}/attest?t={_TOKEN}",
            data={
                "audit_id": audit_id,
                "render_ts": str(time.time()),
                "ack_loss": "on",
                "ack_cure": "on",
                "operator_thesis": "too short!",  # 10 chars < 30
            },
        )


# ═══════════════════════════════════════════════════════════════════════════
# T1: Default R8 thesis error copy
# ═══════════════════════════════════════════════════════════════════════════

class TestThesisErrorCopyDefaultR8(unittest.TestCase):

    def test_thesis_error_copy_default_r8(self):
        """R8-default (exception_type=NULL) short thesis → 422 with
        'Strategic rationale' in error detail."""
        db = _get_db()
        _seed_peacetime(db)
        _insert_staged_cc(db, audit_id="t1-r8-default", exception_type=None)

        client = TestClient(app)
        resp = _post_short_thesis(client, db, "t1-r8-default")

        self.assertEqual(resp.status_code, 422)
        self.assertIn("Strategic rationale", resp.text)


# ═══════════════════════════════════════════════════════════════════════════
# T2: Thesis deterioration error copy
# ═══════════════════════════════════════════════════════════════════════════

class TestThesisErrorCopyThesisDetermination(unittest.TestCase):

    def test_thesis_error_copy_thesis_deterioration(self):
        """thesis_deterioration short thesis → 422 with
        'Bearish rationale' in error detail."""
        db = _get_db()
        _seed_peacetime(db)
        _insert_staged_cc(db, audit_id="t2-thesis-det", ticker="META",
                          exception_type="thesis_deterioration")

        client = TestClient(app)
        resp = _post_short_thesis(client, db, "t2-thesis-det")

        self.assertEqual(resp.status_code, 422)
        self.assertIn("Bearish rationale", resp.text,
                      "thesis_deterioration must show 'Bearish rationale' error")
        self.assertNotIn("Strategic rationale", resp.text,
                         "Must NOT show default 'Strategic rationale' for thesis_deterioration")


# ═══════════════════════════════════════════════════════════════════════════
# T3: Emergency risk error copy
# ═══════════════════════════════════════════════════════════════════════════

class TestThesisErrorCopyEmergencyRisk(unittest.TestCase):

    def test_thesis_error_copy_emergency_risk(self):
        """emergency_risk_event short thesis → 422 with
        'Risk catalyst' in error detail."""
        db = _get_db()
        _seed_peacetime(db)
        _insert_staged_cc(db, audit_id="t3-emergency", ticker="META",
                          exception_type="emergency_risk_event")

        client = TestClient(app)
        resp = _post_short_thesis(client, db, "t3-emergency")

        self.assertEqual(resp.status_code, 422)
        self.assertIn("Risk catalyst", resp.text,
                      "emergency_risk_event must show 'Risk catalyst' error")
        self.assertNotIn("Strategic rationale", resp.text,
                         "Must NOT show default 'Strategic rationale' for emergency_risk")


# ═══════════════════════════════════════════════════════════════════════════
# T4: Placeholder CC vs STK_SELL for R8-default
# ═══════════════════════════════════════════════════════════════════════════

class TestPlaceholderCcVsStkSellR8Default(unittest.TestCase):

    def test_placeholder_cc_vs_stk_sell_r8_default(self):
        """GET on CC R8-default → 'covered call' in placeholder.
        GET on STK_SELL R8-default → 'share sale' in placeholder.
        CC must NOT contain 'share sale'."""
        db = _get_db()
        _seed_peacetime(db)
        _insert_staged_cc(db, audit_id="t4-cc-r8", ticker="AAPL",
                          exception_type=None)
        _insert_staged_stk_sell(db, audit_id="t4-stk-r8", ticker="ADBE",
                                 exception_type="rule_8_dynamic_exit")

        client = TestClient(app)
        wrapped = _NoCloseConnection(db)

        with patch("agt_deck.main.get_ro_conn", lambda: wrapped):
            resp_cc = client.get(
                f"/api/cure/dynamic_exit/t4-cc-r8/attest?t={_TOKEN}")
            resp_stk = client.get(
                f"/api/cure/dynamic_exit/t4-stk-r8/attest?t={_TOKEN}")

        self.assertEqual(resp_cc.status_code, 200)
        self.assertEqual(resp_stk.status_code, 200)

        # CC R8-default must mention "covered call"
        self.assertIn("covered call", resp_cc.text,
                      "CC R8-default placeholder must mention 'covered call'")

        # STK_SELL R8-default must mention "share sale"
        self.assertIn("share sale", resp_stk.text,
                      "STK_SELL R8-default placeholder must mention 'share sale'")

        # CC must NOT contain STK_SELL copy
        self.assertNotIn("share sale", resp_cc.text,
                         "CC placeholder must NOT contain 'share sale'")


if __name__ == "__main__":
    unittest.main()
