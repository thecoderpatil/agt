"""ADR-020 Phase A piece 3 — terminal-state sweeper.

Runs daily at 16:30 ET via APScheduler. Identifies non-terminal
pending_orders rows beyond conservative age thresholds and transitions
them to the correct terminal state with structured status_history.

Conservative-by-default: multiple signals must agree before a sweep.
Legitimate in-flight orders are NOT swept.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Conservative thresholds — tuned for paper mode where IB callbacks
# are unreliable. Live mode rarely trips these.
STUCK_PENDING_AGE_HOURS = 24
STUCK_SENT_NO_PERM_ID_AGE_HOURS = 48
STUCK_SENT_WITH_PERM_ID_AGE_HOURS = 96
EXPIRED_OPTION_GRACE_HOURS = 24

TERMINAL_STATES = frozenset({
    "filled", "cancelled", "expired", "rejected", "superseded", "error",
    "partially_filled",
})


@dataclass(frozen=True)
class SweepResult:
    swept_count: int
    by_classification: dict[str, int] = field(default_factory=dict)
    skipped_in_flight: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


def _classify_stuck_order(
    *,
    order_row: dict,
    now_utc: datetime,
) -> Optional[tuple[str, str, dict[str, Any]]]:
    """Return (terminal_state, reason, evidence) or None.

    Pure — no DB access. Caller must pre-populate '_has_status_history',
    'expiry', and 'right' from payload JSON before passing the row.
    """
    status = order_row.get("status")
    if status in TERMINAL_STATES:
        return None

    staged_at_str = order_row.get("staged_at_utc") or order_row.get("created_at")
    if not staged_at_str:
        return None
    try:
        staged_at = datetime.fromisoformat(str(staged_at_str).replace("Z", "+00:00"))
        if staged_at.tzinfo is None:
            staged_at = staged_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    age_hours = (now_utc - staged_at).total_seconds() / 3600.0

    expiry_str = order_row.get("expiry") or ""
    is_option = order_row.get("right") in ("P", "C") or bool(expiry_str)
    expiry_passed = False
    if is_option and expiry_str:
        try:
            exp = str(expiry_str)
            if len(exp) == 8 and exp.isdigit():
                exp = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
            expiry_dt = datetime.strptime(exp, "%Y-%m-%d").replace(
                hour=20, minute=0, tzinfo=timezone.utc
            )
            expiry_passed = now_utc > expiry_dt + timedelta(hours=EXPIRED_OPTION_GRACE_HOURS)
        except (ValueError, TypeError):
            pass

    ib_perm_id = order_row.get("ib_perm_id") or 0
    has_history = bool(order_row.get("_has_status_history"))

    # Rule 1: option with expiry passed, no IB callback -> expired.
    if is_option and expiry_passed:
        return (
            "expired",
            "expiry_passed_no_callback",
            {"age_hours": round(age_hours, 1), "expiry": expiry_str, "ib_perm_id": ib_perm_id},
        )
    # Rule 2: pending, no history -> cancelled (never_sent_to_ib).
    if status == "pending" and not has_history and age_hours >= STUCK_PENDING_AGE_HOURS:
        return (
            "cancelled",
            "never_sent_to_ib",
            {"age_hours": round(age_hours, 1), "ib_perm_id": 0},
        )
    # Rule 3: sent, no ib_perm_id -> cancelled (no_ib_perm_id).
    if status == "sent" and ib_perm_id == 0 and age_hours >= STUCK_SENT_NO_PERM_ID_AGE_HOURS:
        return (
            "cancelled",
            "no_ib_perm_id",
            {"age_hours": round(age_hours, 1)},
        )
    # Rule 4: sent, has ib_perm_id, no callback -> cancelled (no_ib_callback).
    if status == "sent" and ib_perm_id > 0 and age_hours >= STUCK_SENT_WITH_PERM_ID_AGE_HOURS:
        return (
            "cancelled",
            "no_ib_callback",
            {"age_hours": round(age_hours, 1), "ib_perm_id": ib_perm_id},
        )
    return None


def sweep_terminal_states(
    *,
    db_path: str | Path | None = None,
    now_utc: Optional[datetime] = None,
) -> SweepResult:
    """Run one sweep pass.

    Args:
        db_path: path override for testing.
        now_utc: clock override for deterministic testing.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    from agt_equities.db import get_db_connection, tx_immediate
    conn = get_db_connection(db_path=db_path)

    result_classifications: dict[str, int] = {}
    error_count = 0
    errors: list[str] = []
    swept_count = 0
    skipped = 0

    try:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        cursor = conn.execute(
            f"""
            SELECT po.*,
                CASE WHEN po.status_history IS NOT NULL
                          AND po.status_history != '[]'
                          AND po.status_history != ''
                     THEN 1 ELSE 0 END AS _has_status_history
            FROM pending_orders po
            WHERE po.status NOT IN ({placeholders})
            """,
            tuple(TERMINAL_STATES),
        )
        rows = [dict(r) for r in cursor.fetchall()]

        for row in rows:
            try:
                # expiry and right live in payload JSON, not direct columns.
                payload_raw = row.get("payload") or "{}"
                try:
                    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else {}
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                row["expiry"] = payload.get("expiry") or row.get("expiry") or ""
                row["right"] = payload.get("right") or row.get("right") or ""

                classification = _classify_stuck_order(order_row=row, now_utc=now_utc)
                if classification is None:
                    skipped += 1
                    continue
                terminal_state, reason, evidence = classification
                with tx_immediate(conn):
                    _apply_sweep(
                        conn,
                        order_id=row["id"],
                        from_status=row["status"],
                        to_status=terminal_state,
                        reason=reason,
                        evidence=evidence,
                        now_utc=now_utc,
                    )
                swept_count += 1
                result_classifications[reason] = result_classifications.get(reason, 0) + 1
            except Exception as exc:
                error_count += 1
                errors.append(f"id={row.get('id')}: {exc}")
                logger.exception("Sweep failed for order id=%s: %s", row.get("id"), exc)
    finally:
        conn.close()

    if swept_count > 0 or error_count > 0:
        try:
            from agt_equities.alerts import enqueue_alert
            enqueue_alert(
                "STUCK_ORDER_SWEEP",
                {
                    "swept_count": swept_count,
                    "by_classification": result_classifications,
                    "error_count": error_count,
                    "skipped_in_flight": skipped,
                },
                severity="info" if error_count == 0 else "warning",
            )
        except Exception as alert_exc:
            logger.warning("STUCK_ORDER_SWEEP alert enqueue failed: %s", alert_exc)

    return SweepResult(
        swept_count=swept_count,
        by_classification=result_classifications,
        skipped_in_flight=skipped,
        error_count=error_count,
        errors=errors,
    )


def _apply_sweep(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    from_status: str,
    to_status: str,
    reason: str,
    evidence: dict,
    now_utc: datetime,
) -> None:
    """Apply a single sweep transition. Caller controls commit."""
    row = conn.execute(
        "SELECT status_history FROM pending_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if row is None:
        logger.warning("_apply_sweep: order id=%d not found", order_id)
        return

    try:
        history = json.loads(row[0] or "[]")
    except (json.JSONDecodeError, TypeError):
        history = []

    history.append({
        "status": to_status,
        "at": now_utc.isoformat(),
        "by": "terminal_state_sweeper",
        "payload": {"from_status": from_status, "reason": reason, **evidence},
    })
    conn.execute(
        "UPDATE pending_orders SET status = ?, status_history = ?, last_ib_status = ? WHERE id = ?",
        (to_status, json.dumps(history), to_status, order_id),
    )
    logger.info(
        "Swept order id=%d: %s -> %s (reason=%s, age=%sh)",
        order_id, from_status, to_status, reason, evidence.get("age_hours"),
    )
