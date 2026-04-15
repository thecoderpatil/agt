"""
AGT Equities — cross-daemon alert bus.

Sprint A unit A5b infrastructure. The two-daemon WAL bus needs a way for
agt_scheduler-owned jobs (which have no Telegram bot token) to surface
asynchronous user-facing events into the bot process for delivery.

Pattern:
  - Producer (typically a scheduler job) calls enqueue_alert(...) with a
    kind, JSON-serializable payload, and severity.
  - Consumer (the bot's poll loop, future A5c) calls drain_pending_alerts()
    to atomically flip pending → in_flight rows and receive them.
  - After delivery, consumer calls mark_alert_sent(id) or
    mark_alert_failed(id, error). Failed rows return to pending until
    attempts >= MAX_ATTEMPTS, after which they stay 'failed' and require
    operator triage.

Schema lives in agt_equities/schema.py register_operational_tables() —
table cross_daemon_alerts. Status state machine:
  pending -> in_flight -> sent (terminal)
  pending -> in_flight -> pending (retryable failure, attempts < MAX)
  pending -> in_flight -> failed (terminal, operator action)

All writes use db.tx_immediate(conn) for the BEGIN IMMEDIATE pattern.
db_path kwarg threading follows FU-A-04 — tests inject :memory: or a
tmp_path DB without monkeypatching agt_equities.db.DB_PATH.
"""

from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from agt_equities.db import get_db_connection, tx_immediate

MAX_ATTEMPTS = 3

VALID_SEVERITIES = ("info", "warn", "crit")


def _now() -> float:
    return time.time()


def enqueue_alert(
    kind: str,
    payload: dict[str, Any],
    *,
    severity: str = "info",
    db_path: str | Path | None = None,
) -> int:
    """Insert a new pending alert. Returns the new row id.

    Args:
        kind: short uppercase event tag (e.g. 'STAGED_DIGEST',
            'ATTESTED_KEYBOARD', 'FLEX_SYNC_DIGEST', 'APEX_MARGIN_WARN').
        payload: JSON-serializable dict the consumer will render.
        severity: 'info' | 'warn' | 'crit'.
        db_path: optional explicit DB path for tests.

    Raises:
        ValueError on invalid severity or non-serializable payload.
    """
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {VALID_SEVERITIES}, got {severity!r}")
    if not kind or not kind.strip():
        raise ValueError("kind must be a non-empty string")
    try:
        payload_json = json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload not JSON-serializable: {exc}") from exc

    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                """
                INSERT INTO cross_daemon_alerts
                    (created_ts, kind, severity, payload_json, status, attempts)
                VALUES (?, ?, ?, ?, 'pending', 0)
                """,
                (_now(), kind.strip(), severity, payload_json),
            )
            row_id = cur.lastrowid
    if row_id is None:
        raise RuntimeError("enqueue_alert: lastrowid was None after INSERT")
    return int(row_id)


def drain_pending_alerts(
    *,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Atomically claim up to `limit` pending alerts as in_flight.

    FIFO by created_ts ASC, id ASC. Returns parsed dicts with keys:
    id, created_ts, kind, severity, payload (deserialized), attempts.

    The status flip pending -> in_flight + attempts++ happens in a single
    BEGIN IMMEDIATE transaction so two concurrent consumers cannot claim
    the same row. Consumers are responsible for calling mark_alert_sent
    or mark_alert_failed for every returned id.
    """
    if limit <= 0:
        return []

    out: list[dict[str, Any]] = []
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            rows = conn.execute(
                """
                SELECT id, created_ts, kind, severity, payload_json, attempts
                  FROM cross_daemon_alerts
                 WHERE status = 'pending'
                 ORDER BY created_ts ASC, id ASC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            if not rows:
                return []
            ids: list[int] = [int(r["id"]) for r in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE cross_daemon_alerts
                   SET status = 'in_flight',
                       attempts = attempts + 1
                 WHERE id IN ({placeholders})
                """,
                ids,
            )
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"])
                except (TypeError, ValueError):
                    payload = {"_raw": r["payload_json"], "_decode_error": True}
                out.append(
                    {
                        "id": int(r["id"]),
                        "created_ts": float(r["created_ts"]),
                        "kind": r["kind"],
                        "severity": r["severity"],
                        "payload": payload,
                        "attempts": int(r["attempts"]) + 1,
                    }
                )
    return out


def mark_alert_sent(alert_id: int, *, db_path: str | Path | None = None) -> None:
    """Terminal success: in_flight -> sent + sent_ts."""
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            conn.execute(
                """
                UPDATE cross_daemon_alerts
                   SET status = 'sent', sent_ts = ?, last_error = NULL
                 WHERE id = ?
                """,
                (_now(), int(alert_id)),
            )


def mark_alert_failed(
    alert_id: int,
    error: str,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Failure handling.

    If attempts < MAX_ATTEMPTS the row returns to 'pending' for retry on
    the next drain. Once attempts >= MAX_ATTEMPTS the row is terminal
    'failed' and requires operator triage.
    """
    err_text = (error or "").strip()[:2000] or "unknown"
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            row = conn.execute(
                "SELECT attempts FROM cross_daemon_alerts WHERE id = ?",
                (int(alert_id),),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"])
            new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            conn.execute(
                """
                UPDATE cross_daemon_alerts
                   SET status = ?, last_error = ?
                 WHERE id = ?
                """,
                (new_status, err_text, int(alert_id)),
            )


def get_alert(alert_id: int, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Read-only fetch for a single alert, primarily for tests + operator
    inspection. Returns None if not found."""
    with closing(get_db_connection(db_path=db_path)) as conn:
        row = conn.execute(
            """
            SELECT id, created_ts, kind, severity, payload_json,
                   status, sent_ts, attempts, last_error
              FROM cross_daemon_alerts
             WHERE id = ?
            """,
            (int(alert_id),),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        payload = {"_raw": row["payload_json"], "_decode_error": True}
    return {
        "id": int(row["id"]),
        "created_ts": float(row["created_ts"]),
        "kind": row["kind"],
        "severity": row["severity"],
        "payload": payload,
        "status": row["status"],
        "sent_ts": float(row["sent_ts"]) if row["sent_ts"] is not None else None,
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"],
    }


def format_alert_text(alert: dict[str, Any]) -> str:
    """A5d: render a drained cross_daemon_alerts row to a Telegram-ready
    string. Pure function — kept here so the bot consumer can import it
    without pulling Telegram-specific deps and so we can unit-test the
    rendering in CI without booting the bot.

    Renders by `kind`. Unknown kinds fall through to a generic format
    so a producer landing in a future MR doesn't silently swallow alerts.
    """
    kind = (alert.get("kind") or "UNKNOWN").strip() or "UNKNOWN"
    severity = ((alert.get("severity") or "info").strip() or "info").upper()
    payload = alert.get("payload")
    if not isinstance(payload, dict):
        payload = {"_raw": payload}

    if kind == "ORPHAN_SWEEP":
        n = payload.get("swept_count", "?")
        ttl = payload.get("ttl_hours", "?")
        return (
            f"[{severity}] orphan_sweep swept {n} staged pending_orders "
            f"(ttl={ttl}h)"
        )

    if kind == "FLEX_SYNC_DIGEST":
        sid = payload.get("sync_id", "?")
        sync_mode = payload.get("mode", "?")
        secs = payload.get("sections_processed", "?")
        rcv = payload.get("rows_received", "?")
        ins = payload.get("rows_inserted", "?")
        return (
            f"[{severity}] flex_sync ok (sync_id={sid} mode={sync_mode}): "
            f"{secs} sections, {rcv} rows received, {ins} upserted"
        )

    if kind == "UNIVERSE_REFRESH":
        added = payload.get("added", "?")
        updated = payload.get("updated", "?")
        total = payload.get("total", "?")
        err = payload.get("error")
        msg = f"[{severity}] Universe refresh: added={added} updated={updated} total={total}"
        if err:
            msg += f" error={err}"
        return msg

    if kind == "FLEX_SYNC_FAILURE":
        err = payload.get("error", "unknown")
        return f"[{severity}] flex_sync FAILED: {err}"

    if kind == "INCEPTION_DELTA_MISS":
        # Sprint B4: fill callback could not resolve inception_delta from
        # the FA-block reader or legacy flat path after 3 retries.
        hh = payload.get("household", "?")
        tk = payload.get("ticker", "?")
        acct = payload.get("acct_id", "?")
        perm = payload.get("perm_id", "?")
        client = payload.get("client_id", "?")
        return (
            f"[{severity}] inception_delta miss: {hh}/{tk} acct={acct} "
            f"permId={perm} clientId={client} "
            f"(fill booked without inception_delta)"
        )

    if kind == "APEX_SURVIVAL":
        # A5d.d: critical leverage-safety alert produced by scheduler-side
        # el_snapshot_writer when excess_liquidity / NLV <= 0.08 on a
        # margin-eligible account. Payload keys: account_id, household,
        # el_pct, nlv, excess_liquidity.
        acct = payload.get("account_id", "?")
        hh = payload.get("household", "?")
        def _money(x):
            try:
                return f"${float(x):,.0f}"
            except (TypeError, ValueError):
                return "?" if x is None else str(x)
        def _pct(x):
            try:
                return f"{float(x):.1%}"
            except (TypeError, ValueError):
                return "?" if x is None else str(x)
        return (
            f"[{severity}] \U0001F6A8 APEX SURVIVAL [{acct}/{hh}]: "
            f"Excess Liquidity {_money(payload.get('excess_liquidity'))} "
            f"({_pct(payload.get('el_pct'))} of NLV {_money(payload.get('nlv'))}). "
            f"Tied-unwinds required."
        )

    # Generic fallback for unknown kinds (forward-compat for A5d.b/c/d
    # producers landing later — they will still surface via Telegram even
    # before this function gets a dedicated branch for their `kind`).
    try:
        payload_str = json.dumps(payload, default=str, sort_keys=True)
    except Exception:
        payload_str = repr(payload)
    return f"[{severity}] {kind}: {payload_str}"


__all__: Iterable[str] = (
    "MAX_ATTEMPTS",
    "enqueue_alert",
    "drain_pending_alerts",
    "mark_alert_sent",
    "mark_alert_failed",
    "get_alert",
    "format_alert_text",
)
