"""agt_equities.csp_approval_gate — ADR-010 Phase 1 state machine.

Telegram-based approval gate for the CSP allocator:
1. Persists candidate batches to ``csp_pending_approval`` (survives restarts).
2. Sends a bare digest with per-candidate approve/skip inline buttons + Submit.
3. Polls the DB row every 5 s until resolved or 30-min timeout.

Threading model:
    ``run_csp_allocator`` is synchronous. MR 6b.2 wraps the scheduler call in
    ``asyncio.to_thread`` so this module's ``time.sleep`` polling loop does not
    block the PTB event loop. Until MR 6b.2 lands this gate is built but not
    wired to any composition root.

Fail-open contract (matches ``run_csp_allocator`` existing pattern):
    DB insert failure  -> return full candidate list (identity, logged).
    Send failure       -> row times out after 30 min -> return [].
    Row disappears     -> return [] (fail-closed, logged as error).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from agt_equities.db import get_db_connection

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"
_APPROVAL_TIMEOUT_MINUTES = 30
_POLL_INTERVAL_SECONDS = 5


# ---------------------------------------------------------------------------
# DB helpers (called from schema._register_csp_approval_tables on every boot)
# ---------------------------------------------------------------------------

def _ensure_table(conn) -> None:
    """Create csp_pending_approval + indexes. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS csp_pending_approval (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id                TEXT NOT NULL,
            household_id          TEXT NOT NULL DEFAULT '',
            candidates_json       TEXT NOT NULL,
            sent_at_utc           TEXT NOT NULL,
            timeout_at_utc        TEXT NOT NULL,
            telegram_message_id   INTEGER,
            status                TEXT NOT NULL DEFAULT 'pending'
                                  CHECK(status IN
                                    ('pending','approved','rejected','timeout','error')),
            approved_indices_json TEXT,
            resolved_at_utc       TEXT,
            resolved_by           TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_csp_pending_approval_status
        ON csp_pending_approval(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_csp_pending_approval_telegram_msg
        ON csp_pending_approval(telegram_message_id)
    """)
    conn.commit()


def _insert_pending_row(
    conn,
    run_id: str,
    candidates_json: str,
    sent_at: datetime,
    timeout_at: datetime,
) -> int:
    """Insert a new pending row. Returns the new row id."""
    cur = conn.execute(
        """
        INSERT INTO csp_pending_approval
            (run_id, candidates_json, sent_at_utc, timeout_at_utc, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        (
            run_id,
            candidates_json,
            sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            timeout_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _update_telegram_msg_id(conn, row_id: int, msg_id: int) -> None:
    conn.execute(
        "UPDATE csp_pending_approval SET telegram_message_id=? WHERE id=?",
        (msg_id, row_id),
    )
    conn.commit()


def _poll_row_status(conn, row_id: int) -> dict | None:
    """Return {id, status, approved_indices_json} for row_id, or None."""
    row = conn.execute(
        "SELECT id, status, approved_indices_json "
        "FROM csp_pending_approval WHERE id=?",
        (row_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1], "approved_indices_json": row[2]}


def _timeout_row(conn, row_id: int) -> None:
    conn.execute(
        """
        UPDATE csp_pending_approval
        SET status='timeout',
            resolved_at_utc=?,
            resolved_by='timeout'
        WHERE id=? AND status='pending'
        """,
        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), row_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Telegram send helpers (no PTB dependency — raw requests, same as telegram_utils)
# ---------------------------------------------------------------------------

def _send_approval_digest(
    text: str,
    keyboard: list[list[dict]],
    chat_id: str,
) -> int | None:
    """sendMessage with inline keyboard. Returns Telegram message_id or None."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        logger.warning("csp_approval_gate: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID not set")
        return None
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        resp = requests.post(
            f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage",
            json=payload,
            timeout=10.0,
        )
        body = resp.json()
        if body.get("ok"):
            return body["result"]["message_id"]
        logger.warning("csp_approval_gate: sendMessage failed: %s", body)
        return None
    except Exception as exc:
        logger.warning("csp_approval_gate: sendMessage exception: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------

def _build_digest_text(candidates: list, row_id: int) -> str:
    """Phase 1 bare digest — no LLM ranking, one bullet per candidate."""
    lines = [f"<b>CSP Approval Digest</b> (row #{row_id})", ""]
    for i, c in enumerate(candidates):
        ticker = getattr(c, "ticker", "?")
        strike = getattr(c, "strike", 0.0) or 0.0
        expiry = getattr(c, "expiry", "?")
        mid = getattr(c, "mid", 0.0) or 0.0
        ann_yield = getattr(c, "annualized_yield", 0.0) or 0.0
        hh = getattr(c, "household_id", "[?]")
        lines.append(
            f"{i + 1}. <b>{ticker}</b> ${float(strike):.0f}P {expiry} "
            f"${float(mid):.2f} ({float(ann_yield):.1%}/yr) [{hh}]"
        )
    lines += ["", "Tap \u2705 to approve, \u23ed to skip, then Submit."]
    return "\n".join(lines)


def _build_keyboard(candidates: list, row_id: int) -> list[list[dict]]:
    """Per-candidate approve/skip buttons + Submit row."""
    rows: list[list[dict]] = []
    for i, c in enumerate(candidates):
        ticker = getattr(c, "ticker", f"#{i}")
        rows.append([
            {"text": f"\u2705 {ticker}", "callback_data": f"csp_approve:{row_id}:{i}"},
            {"text": f"\u23ed {ticker}", "callback_data": f"csp_skip:{row_id}:{i}"},
        ])
    rows.append([{"text": "\u2705 Submit", "callback_data": f"csp_submit:{row_id}"}])
    return rows


# ---------------------------------------------------------------------------
# Main gate function
# ---------------------------------------------------------------------------

def telegram_approval_gate(
    candidates: list,
    *,
    db_path: "str | None" = None,
    timeout_minutes: int = _APPROVAL_TIMEOUT_MINUTES,
) -> list:
    """Hold candidate batch for Yash's approval via Telegram.

    Returns the approved subset. Empty list = fail-closed (timeout / reject).
    MUST be called from a worker thread (asyncio.to_thread) — see module docstring.
    """
    if not candidates:
        return []

    run_id = uuid.uuid4().hex
    now_utc = datetime.now(timezone.utc)
    timeout_at = now_utc + timedelta(minutes=timeout_minutes)
    chat_id = os.environ.get("TELEGRAM_USER_ID", "")

    candidates_serial = json.dumps([
        {
            "ticker": getattr(c, "ticker", "?"),
            "strike": float(getattr(c, "strike", 0.0) or 0.0),
            "expiry": str(getattr(c, "expiry", "") or ""),
            "mid": float(getattr(c, "mid", 0.0) or 0.0),
            "annualized_yield": float(getattr(c, "annualized_yield", 0.0) or 0.0),
        }
        for c in candidates
    ])

    try:
        with get_db_connection(db_path) as conn:
            row_id = _insert_pending_row(conn, run_id, candidates_serial, now_utc, timeout_at)
    except Exception:
        logger.exception("csp_approval_gate: DB insert failed — identity fallback")
        return list(candidates)

    text = _build_digest_text(candidates, row_id)
    keyboard = _build_keyboard(candidates, row_id)
    msg_id = _send_approval_digest(text, keyboard, chat_id)

    if msg_id is not None:
        try:
            with get_db_connection(db_path) as conn:
                _update_telegram_msg_id(conn, row_id, msg_id)
        except Exception as exc:
            logger.warning("csp_approval_gate: could not store message_id: %s", exc)
    else:
        logger.warning(
            "csp_approval_gate: Telegram send failed for row %d — will timeout", row_id
        )

    # ── Polling loop ──
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)
        now_utc = datetime.now(timezone.utc)
        try:
            with get_db_connection(db_path) as conn:
                row = _poll_row_status(conn, row_id)
        except Exception as exc:
            logger.warning("csp_approval_gate: poll DB error: %s", exc)
            row = None

        if row is None:
            logger.error("csp_approval_gate: row %d vanished — fail-closed", row_id)
            return []

        if row["status"] != "pending":
            break

        if now_utc >= timeout_at:
            try:
                with get_db_connection(db_path) as conn:
                    _timeout_row(conn, row_id)
            except Exception as exc:
                logger.warning("csp_approval_gate: timeout update failed: %s", exc)
            logger.info(
                "csp_approval_gate: row %d timed out after %d min",
                row_id, timeout_minutes,
            )
            return []

    # ── Resolve ──
    status = row["status"]
    if status != "approved":
        logger.info(
            "csp_approval_gate: row %d resolved as '%s' — returning []", row_id, status
        )
        return []

    raw = row.get("approved_indices_json") or "[]"
    try:
        indices: list[int] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.error("csp_approval_gate: malformed approved_indices_json for row %d", row_id)
        return []

    n = len(candidates)
    approved = []
    for idx in indices:
        if isinstance(idx, int) and 0 <= idx < n:
            approved.append(candidates[idx])
        else:
            logger.warning(
                "csp_approval_gate: dropping out-of-bounds index %r (n=%d)", idx, n
            )
    logger.info(
        "csp_approval_gate: row %d — approved %d/%d candidates",
        row_id, len(approved), n,
    )
    return approved
