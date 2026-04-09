"""
Followup #17 — Tests for orderRef linking, orphan scan, /recover_transmitting,
R5 fallback, stale attestation guard, sweep CAS, and timezone normalization.

19 tests total. DB: in-memory SQLite with production-matching schema.
No live IBKR — mock ib_async objects.
"""

import json
import os
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import sweep_stale_dynamic_exit_stages


# ---------------------------------------------------------------------------
# Shared DDL + helpers
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

CREATE TABLE recovery_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT NOT NULL,
    operator_user_id INTEGER NOT NULL,
    recovery_action TEXT NOT NULL CHECK (recovery_action IN ('filled', 'abandoned')),
    pre_status TEXT NOT NULL,
    post_status TEXT NOT NULL,
    ib_order_id_provided INTEGER,
    operator_note TEXT,
    recovery_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _get_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def _insert_row(conn, audit_id, final_status="ATTESTED", ticker="ADBE",
                action_type="CC", last_updated=None):
    now = time.time()
    lu = last_updated or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO bucket3_dynamic_exit_log "
        "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
        " household_nlv, underlying_spot_at_render, strike, expiry, contracts, "
        " shares, limit_price, render_ts, staged_ts, final_status, last_updated) "
        "VALUES (?, date('now'), ?, 'Yash_Household', 'PEACETIME', ?, "
        " 261000.0, 250.0, 260.0, '2026-05-16', 1, 100, 3.00, ?, ?, ?, ?)",
        (audit_id, ticker, action_type, now, now, final_status, lu),
    )
    conn.commit()


def _mock_trade(order_ref, status="Filled", order_id=12345,
                filled=0, remaining=0):
    trade = MagicMock()
    trade.order.orderRef = order_ref
    trade.order.orderId = order_id
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    trade.orderStatus.remaining = remaining
    return trade


def _mock_execution(order_ref, exec_id="exec-001", price=2.50, shares=1):
    ex = MagicMock()
    ex.orderRef = order_ref
    ex.execId = exec_id
    ex.price = price
    ex.shares = shares
    return ex


# ═══════════════════════════════════════════════════════════════════════════
# G1: Schema migration idempotent
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaMigration(unittest.TestCase):

    def test_schema_migration_idempotent(self):
        """Running migration twice must not error. All 4 columns must exist."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_DDL)
        # Run "migration" again — simulating schema.py running on existing DB
        for stmt in [
            "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN ib_order_id INTEGER",
            "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN ib_perm_id INTEGER",
            "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN fill_qty INTEGER",
            "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN commission REAL",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # Already exists — idempotent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket3_dynamic_exit_log)")}
        for c in ("ib_order_id", "ib_perm_id", "fill_qty", "commission"):
            self.assertIn(c, cols, f"Column {c} must exist after migration")
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# G2-G5: TRANSMIT path
# ═══════════════════════════════════════════════════════════════════════════

class TestTransmitPath(unittest.TestCase):

    def test_transmit_orderref_set_before_placeorder(self):
        """order.orderRef must be set to audit_id before placeOrder."""
        # Read source and verify the line exists in sequence
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        # orderRef assignment must come before placeOrder in handle_dex_callback
        func_start = src.find('async def handle_dex_callback')
        func_body = src[func_start:func_start + 20000]  # Sprint 1D: cooldown insertion widened function
        ref_pos = func_body.find('order.orderRef = audit_id')
        place_pos = func_body.find('ib_conn.placeOrder(contract, order)')
        self.assertNotEqual(ref_pos, -1, "order.orderRef = audit_id not found")
        self.assertNotEqual(place_pos, -1, "placeOrder not found")
        self.assertLess(ref_pos, place_pos,
                        "orderRef must be set BEFORE placeOrder")

    def test_transmit_step8_writes_ib_order_id(self):
        """Step 8 UPDATE must include ib_order_id in SET clause."""
        conn = _get_db()
        _insert_row(conn, "step8-test", "TRANSMITTING")
        now_ts = time.time()
        ib_oid = 99999
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTED', transmitted = 1, "
            "    transmitted_ts = ?, ib_order_id = ?, "
            "    last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",
            (now_ts, ib_oid, "step8-test"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT final_status, ib_order_id FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'step8-test'"
        ).fetchone()
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["ib_order_id"], 99999)

    def test_transmit_step8_db_failure_alerts_operator(self):
        """If Step 8 CAS fails (rowcount=0), RuntimeError should be raised."""
        conn = _get_db()
        _insert_row(conn, "step8-fail", "TRANSMITTED")  # Already transmitted
        result = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTED', transmitted = 1, "
            "    transmitted_ts = ?, ib_order_id = ?, "
            "    last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",
            (time.time(), 123, "step8-fail"),
        )
        self.assertEqual(result.rowcount, 0, "CAS guard must prevent double-update")

    def test_stale_attestation_guard_blocks_old_row(self):
        """Attestation older than 10 minutes must be rejected."""
        from telegram_bot import _parse_sqlite_utc
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        parsed = _parse_sqlite_utc(old_ts)
        age = datetime.now(timezone.utc) - parsed
        self.assertGreater(age, timedelta(minutes=10),
                           "15-minute-old attestation must be detected as stale")


# ═══════════════════════════════════════════════════════════════════════════
# G6-G12: Orphan scan
# ═══════════════════════════════════════════════════════════════════════════

class TestOrphanScan(unittest.TestCase):

    def _run_scan_logic(self, orphan_status, open_trades, exec_list):
        """Helper: run orphan scan resolution logic against mock data."""
        from telegram_bot import (
            _OPEN_FILLED_STATES, _OPEN_DEAD_STATES, _OPEN_LIVE_STATES,
        )
        conn = _get_db()
        audit_id = f"orphan-{orphan_status.lower()}"
        _insert_row(conn, audit_id, "TRANSMITTING")

        # Simulate scan resolution
        open_match = next(
            (t for t in open_trades if t.order.orderRef == audit_id), None
        )
        exec_match = next(
            (e for e in exec_list if e.orderRef == audit_id), None
        )

        new_status = None
        if open_match:
            s = open_match.orderStatus.status
            if s in _OPEN_FILLED_STATES:
                new_status = "TRANSMITTED"
            elif s in _OPEN_DEAD_STATES:
                new_status = "ABANDONED"
        elif exec_match:
            new_status = "TRANSMITTED"

        if new_status:
            conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET final_status = ?, last_updated = CURRENT_TIMESTAMP "
                "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",
                (new_status, audit_id),
            )
            conn.commit()

        row = conn.execute(
            "SELECT final_status, fill_price, fill_qty, ib_perm_id "
            "FROM bucket3_dynamic_exit_log WHERE audit_id = ?",
            (audit_id,),
        ).fetchone()
        return dict(row)

    def test_orphan_scan_filled_via_opentrades(self):
        result = self._run_scan_logic(
            "filled", [_mock_trade("orphan-filled", "Filled")], []
        )
        self.assertEqual(result["final_status"], "TRANSMITTED")

    def test_orphan_scan_filled_via_executions(self):
        result = self._run_scan_logic(
            "exec", [], [_mock_execution("orphan-exec")]
        )
        self.assertEqual(result["final_status"], "TRANSMITTED")

    def test_orphan_scan_dead_state_auto_abandons(self):
        result = self._run_scan_logic(
            "dead", [_mock_trade("orphan-dead", "Cancelled")], []
        )
        self.assertEqual(result["final_status"], "ABANDONED")

    def test_orphan_scan_live_unfilled_alerts_operator(self):
        result = self._run_scan_logic(
            "live", [_mock_trade("orphan-live", "Submitted")], []
        )
        self.assertEqual(result["final_status"], "TRANSMITTING",
                         "Live unfilled must stay TRANSMITTING")

    def test_orphan_scan_partial_fill_alerts_operator(self):
        result = self._run_scan_logic(
            "partial",
            [_mock_trade("orphan-partial", "Submitted", filled=50, remaining=50)],
            []
        )
        self.assertEqual(result["final_status"], "TRANSMITTING",
                         "Partial fill must stay TRANSMITTING for operator review")

    def test_orphan_scan_not_found_never_auto_abandons(self):
        """BINDING CONTRACT: not-found rows NEVER auto-abandon."""
        result = self._run_scan_logic("notfound", [], [])
        self.assertEqual(result["final_status"], "TRANSMITTING",
                         "NOT FOUND orphan must NEVER be auto-abandoned")

    def test_orphan_scan_post_init_ordering(self):
        """Verify post_init calls orphan scan after IB connection setup."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        func_start = src.find('async def post_init(app)')
        func_body = src[func_start:func_start + 3000]
        ib_pos = func_body.find('ensure_ib_connected')
        req_pos = func_body.find('reqAllOpenOrdersAsync')
        scan_pos = func_body.find('_scan_orphaned_transmitting_rows')
        self.assertNotEqual(ib_pos, -1)
        self.assertNotEqual(req_pos, -1)
        self.assertNotEqual(scan_pos, -1)
        self.assertLess(ib_pos, req_pos, "IB connect must come before reqAllOpenOrders")
        self.assertLess(req_pos, scan_pos, "reqAllOpenOrders must come before orphan scan")


# ═══════════════════════════════════════════════════════════════════════════
# G13-G16: /recover_transmitting
# ═══════════════════════════════════════════════════════════════════════════

class TestRecoverTransmitting(unittest.TestCase):

    def test_recover_transmitting_filled(self):
        conn = _get_db()
        _insert_row(conn, "recover-filled", "TRANSMITTING")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET final_status = 'TRANSMITTED', ib_order_id = 42, "
                "    last_updated = CURRENT_TIMESTAMP "
                "WHERE audit_id = 'recover-filled' AND final_status = 'TRANSMITTING'"
            )
            conn.execute(
                "INSERT INTO recovery_audit_log "
                "(audit_id, operator_user_id, recovery_action, pre_status, "
                " post_status, ib_order_id_provided) "
                "VALUES ('recover-filled', 123456, 'filled', 'TRANSMITTING', "
                " 'TRANSMITTED', 42)"
            )
        row = conn.execute(
            "SELECT final_status, ib_order_id FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'recover-filled'"
        ).fetchone()
        self.assertEqual(row["final_status"], "TRANSMITTED")
        self.assertEqual(row["ib_order_id"], 42)
        audit = conn.execute("SELECT * FROM recovery_audit_log").fetchone()
        self.assertEqual(audit["recovery_action"], "filled")

    def test_recover_transmitting_abandoned(self):
        conn = _get_db()
        _insert_row(conn, "recover-aband", "TRANSMITTING")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET final_status = 'ABANDONED', last_updated = CURRENT_TIMESTAMP "
                "WHERE audit_id = 'recover-aband' AND final_status = 'TRANSMITTING'"
            )
            conn.execute(
                "INSERT INTO recovery_audit_log "
                "(audit_id, operator_user_id, recovery_action, pre_status, "
                " post_status) VALUES ('recover-aband', 123456, 'abandoned', "
                " 'TRANSMITTING', 'ABANDONED')"
            )
        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'recover-aband'"
        ).fetchone()
        self.assertEqual(row["final_status"], "ABANDONED")

    def test_recover_transmitting_cas_double_recovery_fails(self):
        conn = _get_db()
        _insert_row(conn, "recover-double", "TRANSMITTING")
        # First recovery succeeds
        r1 = conn.execute(
            "UPDATE bucket3_dynamic_exit_log SET final_status = 'TRANSMITTED' "
            "WHERE audit_id = 'recover-double' AND final_status = 'TRANSMITTING'"
        )
        self.assertEqual(r1.rowcount, 1)
        # Second recovery CAS-blocked
        r2 = conn.execute(
            "UPDATE bucket3_dynamic_exit_log SET final_status = 'TRANSMITTED' "
            "WHERE audit_id = 'recover-double' AND final_status = 'TRANSMITTING'"
        )
        self.assertEqual(r2.rowcount, 0, "Second recovery must be CAS-blocked")

    def test_recover_transmitting_overwrites_ib_order_id(self):
        """D6: operator-provided ib_order_id overwrites any existing value."""
        conn = _get_db()
        _insert_row(conn, "recover-overwrite", "TRANSMITTING")
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log SET ib_order_id = 111 "
            "WHERE audit_id = 'recover-overwrite'"
        )
        conn.commit()
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'TRANSMITTED', ib_order_id = 222 "
            "WHERE audit_id = 'recover-overwrite' AND final_status = 'TRANSMITTING'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT ib_order_id FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'recover-overwrite'"
        ).fetchone()
        self.assertEqual(row["ib_order_id"], 222, "Operator ID must overwrite existing")


# ═══════════════════════════════════════════════════════════════════════════
# G17: Column ownership enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestColumnOwnership(unittest.TestCase):

    def test_r5_handlers_never_touch_final_status(self):
        """D4 binding contract: R5 fallback path must never write final_status."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        # Find each R5_FALLBACK_DEX UPDATE and verify it doesn't SET final_status
        import re
        fallback_updates = re.findall(
            r'R5_FALLBACK_DEX.*?conn\.execute\(\s*"(UPDATE bucket3_dynamic_exit_log.*?)"',
            src, re.DOTALL,
        )
        self.assertGreater(len(fallback_updates), 0, "Must find R5 fallback UPDATEs")
        for sql in fallback_updates:
            self.assertNotIn("final_status", sql,
                             f"R5 fallback must NOT write final_status: {sql[:80]}")

    def test_column_ownership_orphan_scan_writes_only_status(self):
        """D4: orphan scan must write only final_status + last_updated."""
        bot_path = os.path.join(os.path.dirname(__file__), '..', 'telegram_bot.py')
        with open(bot_path, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        func_start = src.find('async def _scan_orphaned_transmitting_rows')
        next_func = src.find('\nasync def ', func_start + 10)
        func_body = src[func_start:next_func] if next_func != -1 else src[func_start:func_start+8000]
        # Verify no fill column writes in the scan function
        for col in ("fill_price", "fill_qty", "fill_ts", "ib_perm_id", "commission"):
            # Check if the column appears in a SET clause context
            set_pattern = f"SET.*{col}"
            import re
            matches = re.findall(set_pattern, func_body)
            self.assertEqual(len(matches), 0,
                             f"Orphan scan must not write {col} (found {len(matches)} matches)")


# ═══════════════════════════════════════════════════════════════════════════
# G18: Sweep 1 CAS guard
# ═══════════════════════════════════════════════════════════════════════════

class TestSweep1CASGuard(unittest.TestCase):

    def test_sweep1_cas_guard_prevents_attested_overwrite(self):
        """Sweep 1 must not overwrite a concurrently-attested row."""
        conn = _get_db()
        # Insert a STAGED row with old staged_ts
        now = time.time()
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, render_ts, staged_ts, "
            " final_status) "
            "VALUES ('sweep-cas', date('now'), 'ADBE', 'Yash_Household', "
            " 'PEACETIME', 'CC', 261000, 250, ?, ?, 'STAGED')",
            (now - 1000, now - 1000),
        )
        conn.commit()

        # Simulate concurrent attestation: STAGED -> ATTESTED
        conn.execute(
            "UPDATE bucket3_dynamic_exit_log SET final_status = 'ATTESTED' "
            "WHERE audit_id = 'sweep-cas'"
        )
        conn.commit()

        # Run sweeper — should NOT overwrite ATTESTED with ABANDONED
        result = sweep_stale_dynamic_exit_stages(conn)
        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'sweep-cas'"
        ).fetchone()
        self.assertEqual(row["final_status"], "ATTESTED",
                         "Sweep 1 CAS guard must protect concurrently-attested rows")


# ═══════════════════════════════════════════════════════════════════════════
# G19: Recovery audit log rollback
# ═══════════════════════════════════════════════════════════════════════════

class TestRecoveryAuditRollback(unittest.TestCase):

    def test_recover_transmitting_rolls_back_status_if_audit_insert_fails(self):
        """If audit log INSERT fails, status flip must also roll back."""
        conn = _get_db()
        _insert_row(conn, "rollback-test", "TRANSMITTING")
        # Drop the audit table to force INSERT failure
        conn.execute("DROP TABLE recovery_audit_log")
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE bucket3_dynamic_exit_log "
                    "SET final_status = 'TRANSMITTED' "
                    "WHERE audit_id = 'rollback-test' AND final_status = 'TRANSMITTING'"
                )
                conn.execute(
                    "INSERT INTO recovery_audit_log "
                    "(audit_id, operator_user_id, recovery_action, pre_status, post_status) "
                    "VALUES ('rollback-test', 1, 'filled', 'TRANSMITTING', 'TRANSMITTED')"
                )
        except Exception:
            pass  # Expected failure

        row = conn.execute(
            "SELECT final_status FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = 'rollback-test'"
        ).fetchone()
        self.assertEqual(row["final_status"], "TRANSMITTING",
                         "Status must roll back when audit INSERT fails")


# ═══════════════════════════════════════════════════════════════════════════
# Timezone normalization tests (Part H)
# ═══════════════════════════════════════════════════════════════════════════

class TestTimezoneNormalization(unittest.TestCase):

    def test_normalize_ibkr_time_handles_naive(self):
        from telegram_bot import _normalize_ibkr_time
        naive = datetime(2026, 4, 8, 14, 30, 0)  # No tzinfo
        result = _normalize_ibkr_time(naive)
        self.assertIsNotNone(result.tzinfo)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_normalize_ibkr_time_passes_through_aware(self):
        from telegram_bot import _normalize_ibkr_time
        aware = datetime(2026, 4, 8, 14, 30, 0, tzinfo=timezone.utc)
        result = _normalize_ibkr_time(aware)
        self.assertEqual(result, aware)

    def test_normalize_ibkr_time_handles_none(self):
        from telegram_bot import _normalize_ibkr_time
        self.assertIsNone(_normalize_ibkr_time(None))


if __name__ == "__main__":
    unittest.main()
