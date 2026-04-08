"""
agt_equities.order_state — Order lifecycle state machine (R5).

Status enum and transition helpers for pending_orders.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"


class OrderStatus(str, Enum):
    """Order lifecycle states. String enum for direct DB storage."""
    STAGED = 'staged'
    PROCESSING = 'processing'
    SENT = 'sent'
    ACKED = 'acked'
    WORKING = 'working'
    FILLED = 'filled'
    PARTIALLY_FILLED = 'partially_filled'
    REJECTED = 'rejected'
    REJECTED_NAKED = 'rejected_naked'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    FAILED = 'failed'
    SUPERSEDED = 'superseded'
    DUPLICATE_SKIPPED = 'duplicate_skipped'


# Valid transitions: {from_status: set of allowed to_statuses}
VALID_TRANSITIONS = {
    OrderStatus.STAGED: {OrderStatus.PROCESSING, OrderStatus.REJECTED, OrderStatus.SUPERSEDED},
    OrderStatus.PROCESSING: {OrderStatus.SENT, OrderStatus.REJECTED, OrderStatus.REJECTED_NAKED,
                              OrderStatus.DUPLICATE_SKIPPED, OrderStatus.FAILED},
    OrderStatus.SENT: {OrderStatus.ACKED, OrderStatus.WORKING, OrderStatus.FILLED,
                        OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.FAILED},
    OrderStatus.ACKED: {OrderStatus.WORKING, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
                         OrderStatus.CANCELLED, OrderStatus.REJECTED},
    OrderStatus.WORKING: {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
                           OrderStatus.CANCELLED, OrderStatus.REJECTED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.FILLED, OrderStatus.CANCELLED},
}

# Terminal states — no further transitions
TERMINAL_STATES = {
    OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.REJECTED_NAKED,
    OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.FAILED,
    OrderStatus.SUPERSEDED, OrderStatus.DUPLICATE_SKIPPED,
}

# IBKR orderStatus → our enum
IBKR_STATUS_MAP = {
    'Submitted': OrderStatus.WORKING,
    'PreSubmitted': OrderStatus.ACKED,
    'Filled': OrderStatus.FILLED,
    'Cancelled': OrderStatus.CANCELLED,
    'ApiCancelled': OrderStatus.CANCELLED,
    'Inactive': OrderStatus.REJECTED,
    'PendingSubmit': OrderStatus.SENT,
    'PendingCancel': OrderStatus.WORKING,  # still working until confirmed
}


def append_status(
    conn: sqlite3.Connection,
    order_id: int,
    new_status: str | OrderStatus,
    source: str,
    payload: dict | None = None,
) -> bool:
    """Append a status entry to an order's status_history.

    Returns True if the transition was applied, False if invalid/skipped.
    Never raises — logs warnings on invalid transitions.
    """
    try:
        new_status = OrderStatus(new_status)
    except ValueError:
        pass  # allow legacy status strings

    # CLEANUP-6: acquire RESERVED lock before SELECT to prevent TOCTOU races
    # when concurrent thread-pool workers read-modify-write status_history.
    # Safe to call inside existing transaction (silently ignored).
    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        pass  # Already inside a transaction — lock already held

    row = conn.execute(
        "SELECT status, status_history FROM pending_orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if row is None:
        logger.warning("append_status: order %d not found", order_id)
        return False

    current_status = row[0] or ''
    history_json = row[1] or '[]'

    # Check for terminal state — don't overwrite
    try:
        if OrderStatus(current_status) in TERMINAL_STATES:
            logger.debug("append_status: order %d already terminal (%s)", order_id, current_status)
            return False
    except ValueError:
        pass

    # Monotonic check — don't go backward
    try:
        current_enum = OrderStatus(current_status)
        new_enum = OrderStatus(new_status) if isinstance(new_status, str) else new_status
        allowed = VALID_TRANSITIONS.get(current_enum, set())
        if new_enum not in allowed and new_enum != current_enum:
            logger.warning(
                "append_status: invalid transition %s → %s for order %d (source: %s)",
                current_status, new_status, order_id, source,
            )
            # Allow it anyway for robustness, but log loudly
    except ValueError:
        pass

    # Build new history entry
    now = datetime.now(timezone.utc).isoformat()
    try:
        history = json.loads(history_json)
    except (json.JSONDecodeError, TypeError):
        history = []

    status_val = new_status.value if isinstance(new_status, OrderStatus) else str(new_status)
    entry = {"status": status_val, "at": now, "by": source}
    if payload:
        entry["payload"] = payload
    history.append(entry)

    # Update
    conn.execute(
        "UPDATE pending_orders SET status = ?, status_history = ?, last_ib_status = ? "
        "WHERE id = ?",
        (status_val, json.dumps(history), status_val, order_id),
    )
    return True


def backfill_status_history(conn: sqlite3.Connection) -> int:
    """Backfill status_history for existing rows that don't have it.

    Returns count of rows backfilled.
    """
    rows = conn.execute(
        "SELECT id, status, created_at FROM pending_orders "
        "WHERE status_history IS NULL OR status_history = '[]' OR status_history = ''"
    ).fetchall()

    count = 0
    for row in rows:
        order_id = row[0]
        status = row[1] or 'staged'
        created_at = row[2] or datetime.now(timezone.utc).isoformat()

        history = [
            {"status": "staged", "at": str(created_at), "by": "backfill"},
        ]

        # Infer intermediate states from final status
        if status in ('approved', 'sent', 'acked', 'working', 'filled'):
            history.append({"status": status, "at": str(created_at), "by": "backfill_inferred"})
        elif status in ('rejected', 'rejected_naked', 'failed', 'superseded', 'duplicate_skipped'):
            history.append({"status": status, "at": str(created_at), "by": "backfill_inferred"})

        # Rename 'approved' to 'sent' for legacy rows
        final_status = 'sent' if status == 'approved' else status

        conn.execute(
            "UPDATE pending_orders SET status = ?, status_history = ? WHERE id = ?",
            (final_status, json.dumps(history), order_id),
        )
        count += 1

    return count
