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


# ---------------------------------------------------------------------------
# Sprint B3 — pending_order_children writer helpers (force-clear cutover)
# ---------------------------------------------------------------------------
#
# Writer-only. Reads against pending_order_children are NOT introduced here
# -- the CSP Allocator (B5) will own the first consumer. These helpers exist
# so that today's 1:1 pending_orders flow ALSO populates the 1:N child table,
# giving B5 / ACB (B4) a hydrated history to migrate against.
#
# Feature flag: AGT_B3_CHILDREN_WRITER. Default ON ('1'). Set to '0' to
# short-circuit the write at the call site (callers check; helpers do not).
#
# Idempotency contract: insert_pending_order_child is idempotent on
# (parent_order_id, account_id) -- a second call with the same pair
# updates IB ids if they were previously NULL and leaves status alone.


import os  # noqa: E402  (deferred so we don't reorder top-of-module imports)


def children_writer_enabled() -> bool:
    """Return True iff the B3 child-row writer is enabled.

    Reads AGT_B3_CHILDREN_WRITER from env on every call so operators can
    toggle without a process restart (the flag is read at fill time, which
    is infrequent relative to env-var churn).
    """
    return os.environ.get("AGT_B3_CHILDREN_WRITER", "1") == "1"


def _child_row_exists(
    conn: sqlite3.Connection,
    parent_order_id: int,
    account_id: str,
) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (parent_order_id, account_id),
    ).fetchone()
    return None if row is None else int(row[0])


def insert_pending_order_child(
    conn: sqlite3.Connection,
    *,
    parent_order_id: int,
    account_id: str,
    status: str = "staged",
    child_ib_order_id: int | None = None,
    child_ib_perm_id: int | None = None,
    margin_check_status: str | None = None,
    margin_check_reason: str | None = None,
) -> int:
    """Insert (or upsert) a pending_order_children row.

    Idempotent on (parent_order_id, account_id):
    * First call inserts the row with provided status + optional IB ids.
    * Subsequent calls update child_ib_order_id / child_ib_perm_id if they
      are currently NULL, and append a seeded status_history entry. Status
      is NOT downgraded on re-call.

    Returns the child row id.

    Never raises on constraint-OK inputs. Caller must hold its own
    transaction (BEGIN IMMEDIATE or tx_immediate) -- this helper does not
    open its own transaction so it composes inside the _place_single_order
    atomic section alongside append_status on the parent.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing_id = _child_row_exists(conn, parent_order_id, account_id)
    if existing_id is not None:
        # Upsert: only fill in IB ids if currently NULL. Do not overwrite
        # a live value -- that would indicate a logic bug we want to surface.
        set_fragments = []
        params: list = []
        if child_ib_order_id:
            set_fragments.append(
                "child_ib_order_id = COALESCE(child_ib_order_id, ?)"
            )
            params.append(int(child_ib_order_id))
        if child_ib_perm_id:
            set_fragments.append(
                "child_ib_perm_id = COALESCE(child_ib_perm_id, ?)"
            )
            params.append(int(child_ib_perm_id))
        if set_fragments:
            set_fragments.append("updated_at = ?")
            params.append(now)
            params.append(existing_id)
            conn.execute(
                "UPDATE pending_order_children SET "
                + ", ".join(set_fragments)
                + " WHERE id = ?",
                tuple(params),
            )
        return existing_id

    initial_history = json.dumps([
        {"status": status, "at": now, "by": "b3_writer"},
    ])
    cur = conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, child_ib_order_id, child_ib_perm_id, "
        " status, status_history, margin_check_status, margin_check_reason, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(parent_order_id),
            str(account_id),
            int(child_ib_order_id) if child_ib_order_id else None,
            int(child_ib_perm_id) if child_ib_perm_id else None,
            str(status),
            initial_history,
            margin_check_status,
            margin_check_reason,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def update_child_ib_ids(
    conn: sqlite3.Connection,
    *,
    parent_order_id: int,
    account_id: str,
    child_ib_order_id: int | None = None,
    child_ib_perm_id: int | None = None,
) -> bool:
    """Populate child_ib_order_id / child_ib_perm_id on an existing child row.

    Used by the openOrderEvent handler: IBKR assigns the real permId/orderId
    asynchronously after placeOrder returns, so we seed the child row with
    whatever we had at placement time then fill in the true ids here.

    Matches by (parent_order_id, account_id). Returns True if a row was
    updated. Does NOT create rows -- if the child row doesn't exist yet,
    the caller (openOrderEvent) is racing the place-order INSERT and should
    simply no-op; the insert will include whatever ids are available.

    COALESCE semantics: existing non-NULL ids are preserved. A transition
    from NULL -> real-id is a one-way door.
    """
    existing_id = _child_row_exists(conn, parent_order_id, account_id)
    if existing_id is None:
        return False
    set_fragments = []
    params: list = []
    if child_ib_order_id:
        set_fragments.append("child_ib_order_id = COALESCE(child_ib_order_id, ?)")
        params.append(int(child_ib_order_id))
    if child_ib_perm_id:
        set_fragments.append("child_ib_perm_id = COALESCE(child_ib_perm_id, ?)")
        params.append(int(child_ib_perm_id))
    if not set_fragments:
        return False
    set_fragments.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(existing_id)
    conn.execute(
        "UPDATE pending_order_children SET "
        + ", ".join(set_fragments)
        + " WHERE id = ?",
        tuple(params),
    )
    return True
