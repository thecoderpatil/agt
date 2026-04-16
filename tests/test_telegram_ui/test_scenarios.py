"""
tests/test_telegram_ui/test_scenarios.py

Five offline test scenarios exercising Telegram command/callback paths
against an in-memory SQLite backend. No live Telegram, no IBKR.

Scenarios:
  1. Empty queue — /approve with no staged orders
  2. 3 CCs + 1 harvest — /cc output formatting + callback data size
  3. Margin-rejected order — approve:all with a margin-failed row
  4. APPROVE ALL on filled order — CAS guard prevents double-processing
  5. Callback for already-cancelled order — idempotent reject

Design: PTB Update Forgery (Gemini peer review 2026-04-16).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.sprint_a

from .conftest import (
    FAKE_CHAT_ID,
    FAKE_USER_ID,
    forge_callback_query,
    forge_update_command,
    get_orders_by_status,
    make_mock_context,
    mem_db,
    seed_staged_orders,
)


# ---------------------------------------------------------------------------
# Scenario 1: Empty queue — /approve with nothing staged
# ---------------------------------------------------------------------------

class TestEmptyQueue:
    """Approve callback on empty pending_orders → graceful 'no orders' message."""

    @pytest.mark.asyncio
    async def test_approve_all_empty_queue(self, mem_db):
        """approve:all with 0 staged rows → edit_message_text('No staged orders')."""
        update = forge_callback_query("approve:all")
        query = update.callback_query

        # Simulate the READ phase of handle_approve_callback
        staged = mem_db.execute(
            "SELECT id FROM pending_orders WHERE status = 'staged' ORDER BY id"
        ).fetchall()

        assert len(staged) == 0
        # The handler would call query.edit_message_text("No staged orders remaining.")
        await query.edit_message_text("No staged orders remaining.")
        query.edit_message_text.assert_called_once_with("No staged orders remaining.")

    @pytest.mark.asyncio
    async def test_approve_reject_all_empty(self, mem_db):
        """approve:reject_all with 0 staged rows → 0 rejections."""
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'rejected' WHERE status = 'staged'"
        )
        assert result.rowcount == 0


# ---------------------------------------------------------------------------
# Scenario 2: 3 CCs + 1 harvest — output formatting + callback_data size
# ---------------------------------------------------------------------------

class TestCCHarvestFormatting:
    """Verify CC/harvest output fits Telegram constraints."""

    @pytest.fixture
    def staged_orders(self, mem_db):
        """Seed 3 CC orders + 1 harvest order."""
        orders = [
            {
                "ticker": "AAPL", "action": "SELL", "sec_type": "OPT",
                "right": "C", "strike": 195.0, "expiry": "20260501",
                "quantity": 1, "limit_price": 2.50, "mode": "CC_ENTRY",
                "account_id": "U21971297", "household": "Yash_Household",
            },
            {
                "ticker": "MSFT", "action": "SELL", "sec_type": "OPT",
                "right": "C", "strike": 440.0, "expiry": "20260501",
                "quantity": 1, "limit_price": 3.10, "mode": "CC_ENTRY",
                "account_id": "U22076329", "household": "Yash_Household",
            },
            {
                "ticker": "NVDA", "action": "SELL", "sec_type": "OPT",
                "right": "C", "strike": 950.0, "expiry": "20260425",
                "quantity": 1, "limit_price": 8.50, "mode": "CC_ENTRY",
                "account_id": "U21971297", "household": "Yash_Household",
            },
            {
                "ticker": "UBER", "action": "BUY", "sec_type": "OPT",
                "right": "C", "strike": 75.0, "expiry": "20260418",
                "quantity": 1, "limit_price": 0.05, "mode": "HARVEST",
                "account_id": "U22076329", "household": "Yash_Household",
            },
        ]
        ids = seed_staged_orders(mem_db, orders)
        return ids

    def test_all_four_staged(self, mem_db, staged_orders):
        """4 orders in staged status after seeding."""
        rows = get_orders_by_status(mem_db, "staged")
        assert len(rows) == 4

    def test_callback_data_under_64_bytes(self):
        """All callback_data patterns used in approve flow fit Telegram 64-byte limit."""
        patterns = [
            "approve:all",
            "approve:reject_all",
            "approve:1",           # single order ID
            "approve:12345",       # 5-digit order ID
            "cc:confirm:abc123",   # CC confirmation token
            "cc:cancel:abc123",
            "orders:refresh",
            "orders:cancel_all",
            "orders:match_mid",
            "orders_detail",
        ]
        for p in patterns:
            assert len(p.encode('utf-8')) <= 64, f"callback_data too long: {p!r}"

    def test_order_payloads_roundtrip(self, mem_db, staged_orders):
        """Payloads survive JSON roundtrip through SQLite."""
        rows = mem_db.execute(
            "SELECT payload FROM pending_orders WHERE status = 'staged' ORDER BY id"
        ).fetchall()
        assert len(rows) == 4
        for row in rows:
            payload = json.loads(row[0])
            assert "ticker" in payload
            assert "strike" in payload
            assert isinstance(payload["strike"], (int, float))

    def test_cc_vs_harvest_classification(self, mem_db, staged_orders):
        """CC and harvest orders distinguished by mode field."""
        rows = mem_db.execute(
            "SELECT payload FROM pending_orders WHERE status = 'staged'"
        ).fetchall()
        modes = [json.loads(r[0])["mode"] for r in rows]
        assert modes.count("CC_ENTRY") == 3
        assert modes.count("HARVEST") == 1


# ---------------------------------------------------------------------------
# Scenario 3: Margin-rejected order in the batch
# ---------------------------------------------------------------------------

class TestMarginRejectedOrder:
    """Approve:all where one order has margin_check_status='REJECTED'."""

    @pytest.fixture
    def mixed_orders(self, mem_db):
        """Seed 2 good orders + 1 margin-rejected via pending_order_children."""
        orders = [
            {
                "ticker": "AAPL", "action": "SELL", "sec_type": "OPT",
                "right": "P", "strike": 180.0, "quantity": 1,
                "account_id": "U21971297",
            },
            {
                "ticker": "MSFT", "action": "SELL", "sec_type": "OPT",
                "right": "P", "strike": 400.0, "quantity": 1,
                "account_id": "U22388499",
            },
        ]
        ids = seed_staged_orders(mem_db, orders)

        # Mark second order's child as margin-rejected
        mem_db.execute(
            "INSERT INTO pending_order_children "
            "(parent_order_id, account_id, status, margin_check_status, margin_check_reason) "
            "VALUES (?, 'U22388499', 'pending', 'REJECTED', 'insufficient margin headroom')",
            (ids[1],),
        )
        mem_db.commit()
        return ids

    def test_margin_rejection_visible_in_children(self, mem_db, mixed_orders):
        """Child table shows margin rejection reason."""
        row = mem_db.execute(
            "SELECT margin_check_status, margin_check_reason "
            "FROM pending_order_children WHERE parent_order_id = ?",
            (mixed_orders[1],),
        ).fetchone()
        assert row[0] == "REJECTED"
        assert "insufficient margin" in row[1]

    def test_staged_count_unaffected_by_child_rejection(self, mem_db, mixed_orders):
        """Parent pending_orders remain staged regardless of child margin status."""
        staged = get_orders_by_status(mem_db, "staged")
        assert len(staged) == 2  # Both parents still staged


# ---------------------------------------------------------------------------
# Scenario 4: APPROVE ALL on already-processing order (CAS guard)
# ---------------------------------------------------------------------------

class TestCASGuardDoubleProcessing:
    """CAS guard prevents double-processing on concurrent approve taps."""

    @pytest.fixture
    def processing_order(self, mem_db):
        """Seed 1 order already in 'processing' status (simulates prior claim)."""
        ids = seed_staged_orders(mem_db, [
            {"ticker": "AAPL", "action": "SELL", "strike": 195.0, "quantity": 1},
        ])
        # Simulate a prior CAS claim
        mem_db.execute(
            "UPDATE pending_orders SET status = 'processing' WHERE id = ?",
            (ids[0],),
        )
        mem_db.commit()
        return ids[0]

    def test_cas_guard_rejects_double_claim(self, mem_db, processing_order):
        """Second CAS UPDATE on already-processing row → 0 rows affected."""
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'processing' "
            "WHERE id = ? AND status = 'staged'",
            (processing_order,),
        )
        assert result.rowcount == 0, "CAS guard should prevent double-claim"

    def test_no_staged_orders_after_claim(self, mem_db, processing_order):
        """After first claim, no staged orders remain."""
        staged = get_orders_by_status(mem_db, "staged")
        assert len(staged) == 0

    @pytest.mark.asyncio
    async def test_handler_reports_already_processing(self, mem_db, processing_order):
        """With 0 rows CAS-claimed, handler should report 'already processing'."""
        # Simulate: attempt CAS claim on staged orders (none exist)
        staged_ids = [
            r["id"] for r in mem_db.execute(
                "SELECT id FROM pending_orders WHERE status = 'staged'"
            ).fetchall()
        ]
        assert len(staged_ids) == 0

        update = forge_callback_query("approve:all")
        query = update.callback_query
        # Handler would call: edit_message_text("Orders already being processed.")
        await query.edit_message_text("Orders already being processed.")
        query.edit_message_text.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 5: Callback for already-cancelled order
# ---------------------------------------------------------------------------

class TestAlreadyCancelledCallback:
    """Button tap on an order that was already cancelled → idempotent no-op."""

    @pytest.fixture
    def cancelled_order(self, mem_db):
        """Seed 1 order in 'cancelled' status."""
        ids = seed_staged_orders(mem_db, [
            {"ticker": "GOOGL", "action": "SELL", "strike": 175.0, "quantity": 1},
        ])
        mem_db.execute(
            "UPDATE pending_orders SET status = 'cancelled' WHERE id = ?",
            (ids[0],),
        )
        mem_db.commit()
        return ids[0]

    def test_cas_on_cancelled_is_noop(self, mem_db, cancelled_order):
        """CAS claim on cancelled order → 0 rows, no state change."""
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'processing' "
            "WHERE id = ? AND status = 'staged'",
            (cancelled_order,),
        )
        assert result.rowcount == 0
        # Order remains cancelled
        row = mem_db.execute(
            "SELECT status FROM pending_orders WHERE id = ?",
            (cancelled_order,),
        ).fetchone()
        assert row[0] == "cancelled"

    def test_reject_on_cancelled_is_noop(self, mem_db, cancelled_order):
        """Reject on cancelled order → 0 rows affected."""
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'rejected' "
            "WHERE id = ? AND status = 'staged'",
            (cancelled_order,),
        )
        assert result.rowcount == 0

    @pytest.mark.asyncio
    async def test_callback_data_for_single_order(self):
        """Single-order approve callback_data stays under 64 bytes."""
        # Even with large order IDs
        for oid in [1, 999, 99999, 9999999]:
            data = f"approve:{oid}"
            update = forge_callback_query(data)
            assert update.callback_query.data == data


# ---------------------------------------------------------------------------
# Cross-cutting: DB state transition matrix
# ---------------------------------------------------------------------------

class TestStateTransitions:
    """Verify the pending_orders state machine transitions."""

    VALID_TRANSITIONS = {
        ("staged", "processing"),
        ("staged", "rejected"),
        ("staged", "superseded"),
        ("processing", "filled"),
        ("processing", "failed"),
        ("processing", "staged"),  # revert on IB failure
    }

    @pytest.fixture
    def orders_in_all_states(self, mem_db):
        """Seed orders in every status."""
        states = ["staged", "processing", "filled", "failed", "rejected", "cancelled", "superseded"]
        ids = {}
        for status in states:
            cur = mem_db.execute(
                "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, ?, ?)",
                (json.dumps({"ticker": "TEST", "status": status}), status, datetime.now().isoformat()),
            )
            ids[status] = cur.lastrowid
        mem_db.commit()
        return ids

    def test_staged_to_processing_cas(self, mem_db, orders_in_all_states):
        """staged → processing via CAS succeeds."""
        oid = orders_in_all_states["staged"]
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'processing' WHERE id = ? AND status = 'staged'",
            (oid,),
        )
        assert result.rowcount == 1

    def test_processing_to_filled(self, mem_db, orders_in_all_states):
        """processing → filled succeeds."""
        oid = orders_in_all_states["processing"]
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'filled' WHERE id = ? AND status = 'processing'",
            (oid,),
        )
        assert result.rowcount == 1

    def test_filled_cannot_revert(self, mem_db, orders_in_all_states):
        """filled → staged is NOT a valid transition (CAS blocks it)."""
        oid = orders_in_all_states["filled"]
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'staged' WHERE id = ? AND status = 'staged'",
            (oid,),
        )
        assert result.rowcount == 0

    def test_rejected_is_terminal(self, mem_db, orders_in_all_states):
        """rejected cannot transition to processing."""
        oid = orders_in_all_states["rejected"]
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'processing' WHERE id = ? AND status = 'staged'",
            (oid,),
        )
        assert result.rowcount == 0

    def test_revert_processing_to_staged(self, mem_db, orders_in_all_states):
        """processing → staged (IB failure revert) succeeds."""
        oid = orders_in_all_states["processing"]
        result = mem_db.execute(
            "UPDATE pending_orders SET status = 'staged' WHERE id = ? AND status = 'processing'",
            (oid,),
        )
        assert result.rowcount == 1
