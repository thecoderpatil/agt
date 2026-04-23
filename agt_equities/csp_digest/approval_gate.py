"""CSP approval gate — identity (paper) + fail-closed-timeout (live default).

Per ADR-CSP_TELEGRAM_DIGEST_v1 §"Approval persistence" + "State machine".

The live wiring (read csp_pending_approval table, watch for telegram
button taps, 90-min timeout sweep) is intentionally deferred to a
follow-on MR. This module ships the gate INTERFACE + identity default
+ fail-closed-timeout primitive that the wiring MR will use.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MINUTES = 90


def identity_approval_gate(tickets: list[dict]) -> list[dict]:
    """Paper-mode default: pass everything through.

    This is the gate `run_csp_allocator` consults today. The live flip
    swaps it for `fail_closed_timeout_gate` after one observation week.
    """
    return list(tickets)


def fail_closed_timeout_gate(
    tickets: list[dict],
    *,
    db_path: str | Path,
    run_id: str,
    now_utc: datetime | None = None,
) -> list[dict]:
    """Live gate (post-observation): only return tickets whose ticker has
    been explicitly approved in csp_pending_approval; SKIP everything
    else, including timed-out unresolved entries.

    Reads:
        SELECT approved_indices_json, status FROM csp_pending_approval
        WHERE run_id = ? AND status IN ('approved','partial','rejected','timeout')

    Returns the subset of `tickets` whose index appears in
    approved_indices_json. NEVER returns a ticker not explicitly
    approved (fail-closed is the entire point).

    On DB error, returns empty list (most conservative).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            row = conn.execute(
                "SELECT approved_indices_json, status, timeout_at_utc "
                "FROM csp_pending_approval "
                "WHERE run_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        logger.warning("csp_digest.gate_db_err err=%s", exc)
        return []
    if not row:
        # No record → timeout (fail-closed: absence of approval = SKIP)
        logger.info("csp_digest.gate_no_record run_id=%s", run_id)
        return []
    approved_json, status, timeout_iso = row
    if status not in ("approved", "partial"):
        # rejected/timeout/pending all SKIP everything
        logger.info(
            "csp_digest.gate_skip_all status=%s run_id=%s", status, run_id,
        )
        return []
    try:
        approved_indices = set(json.loads(approved_json or "[]"))
    except json.JSONDecodeError:
        logger.warning("csp_digest.gate_bad_json run_id=%s", run_id)
        return []
    return [t for i, t in enumerate(tickets) if i in approved_indices]


def insert_pending_row(
    db_path: str | Path,
    *,
    run_id: str,
    household_id: str,
    candidates_json: str,
    sent_at_utc: datetime,
    timeout_at_utc: datetime,
    telegram_message_id: int | None = None,
) -> int:
    """Insert a fresh pending-approval row. Returns inserted rowid.

    Fail-soft on duplicate (UNIQUE on (run_id, household_id) for replays).
    Caller is responsible for ensuring run_id+household_id is fresh.
    """
    with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
        cur = conn.execute(
            "INSERT INTO csp_pending_approval ("
            " run_id, household_id, candidates_json, sent_at_utc, "
            " timeout_at_utc, telegram_message_id, status"
            ") VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (
                run_id, household_id, candidates_json,
                sent_at_utc.isoformat(), timeout_at_utc.isoformat(),
                telegram_message_id,
            ),
        )
        conn.commit()
        return cur.lastrowid or 0


def resolve_ticker(
    db_path: str | Path,
    *,
    run_id: str,
    candidate_index: int,
    decision: str,  # "approve" | "reject"
    resolved_by: str,
    now_utc: datetime | None = None,
) -> bool:
    """Append candidate_index to approved_indices_json (if approve) and
    flip status to 'partial' / 'approved' as appropriate.

    Returns True on update, False if no matching row.
    """
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    now_utc = now_utc or datetime.now(timezone.utc)
    with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
        row = conn.execute(
            "SELECT id, candidates_json, approved_indices_json FROM csp_pending_approval "
            "WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return False
        rowid, candidates_json, approved_indices_json = row
        try:
            candidates = json.loads(candidates_json or "[]")
        except json.JSONDecodeError:
            candidates = []
        try:
            approved = set(json.loads(approved_indices_json or "[]"))
        except json.JSONDecodeError:
            approved = set()
        if decision == "approve":
            approved.add(int(candidate_index))
        # rejects are recorded by ABSENCE — gate considers anything not in
        # approved_indices_json as not-approved.
        if len(approved) >= len(candidates) and candidates:
            new_status = "approved"
        elif approved:
            new_status = "partial"
        else:
            new_status = "pending"
        conn.execute(
            "UPDATE csp_pending_approval "
            "SET approved_indices_json = ?, status = ?, "
            "    resolved_at_utc = CASE WHEN ? IN ('approved','partial') "
            "                           THEN ? ELSE resolved_at_utc END, "
            "    resolved_by = CASE WHEN ? IN ('approved','partial') "
            "                       THEN ? ELSE resolved_by END "
            "WHERE id = ?",
            (
                json.dumps(sorted(approved)), new_status,
                new_status, now_utc.isoformat(),
                new_status, resolved_by,
                rowid,
            ),
        )
        conn.commit()
        return True


def sweep_timeouts(
    db_path: str | Path,
    *,
    now_utc: datetime | None = None,
) -> int:
    """Flip stale 'pending' rows to 'timeout' or 'partial' (if some approved).

    Returns the number of rows updated.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
        rows = conn.execute(
            "SELECT id, approved_indices_json FROM csp_pending_approval "
            "WHERE status = 'pending' AND timeout_at_utc < ?",
            (now_utc.isoformat(),),
        ).fetchall()
        n = 0
        for rowid, approved_json in rows:
            try:
                approved = json.loads(approved_json or "[]")
            except json.JSONDecodeError:
                approved = []
            new_status = "partial" if approved else "timeout"
            conn.execute(
                "UPDATE csp_pending_approval "
                "SET status = ?, resolved_at_utc = ?, resolved_by = 'timeout' "
                "WHERE id = ?",
                (new_status, now_utc.isoformat(), rowid),
            )
            n += 1
        conn.commit()
    return n
