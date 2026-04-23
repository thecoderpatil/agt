"""Flex-sync freshness watchdog.

Sprint 4 MR B (2026-04-24). Per ADR-FLEX_FRESHNESS_v1 (Proposal C,
fail-open data + fail-closed paging).

External watchdog — zero touches to agt_equities/flex_sync.py (prohibited
outside Decoupling Sprint A scope per CLAUDE.md).

Three silent-failure modes the watchdog catches:
  1. Empty XML response from IBKR — flex_sync writes a success row with
     zero counts; nobody notices until downstream consumers hit stale data.
  2. Bot-side alert consumer down — FLEX_SYNC_DIGEST is produced but the
     drain loop is dead, so no operator notification for sync runs.
  3. Scheduler daemon stopped — no sync at all; master_log_sync stops
     growing.

Redundant detection paths ship in this MR:
  - Scheduler cron at 18:00 ET (this module's run_flex_sync_watchdog)
  - Sentinel file C:\\AGT_Telegram_Bridge\\state\\flex_sync_stale.flag
    (atomic os.replace; consumed by any external watchdog, including a
    future SMTP/SMS backup page on sentinel-stale-for-1h)
  - /flex_status Telegram command (telegram_bot.py) for on-demand query

Fresh sync deletes the sentinel; stale sync keeps it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.alerts import enqueue_alert
from agt_equities.db import get_ro_connection

logger = logging.getLogger(__name__)

DEFAULT_STALE_THRESHOLD_HOURS = 6
SENTINEL_DIR = Path(r"C:\AGT_Telegram_Bridge\state")
SENTINEL_FILE = SENTINEL_DIR / "flex_sync_stale.flag"

# Sprint 6 Mega-MR 3 — zero-row suspicion watchdog (closes Investigation D §F3
# coverage gap). The 18:00 ET freshness watchdog only catches "sync didn't run"
# (>6h since last success). It does NOT catch "sync ran cleanly but received 0
# rows across a multi-day window where we'd expect rows". That class appeared
# on 2026-04-23 sync 17 (0 rows received, successful status). The zero-row
# watchdog runs 30 min after the freshness watchdog (18:30 ET Mon-Fri) and
# examines the rolling window for this pattern.
DEFAULT_ZERO_ROW_WINDOW = 5
DEFAULT_PRIOR_HISTORY_DAYS = 7
DEFAULT_PRIOR_AVG_THRESHOLD = 2.0


def _parse_sync_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def query_latest_sync(*, db_path: str | Path | None = None) -> dict[str, Any]:
    """Read the most-recent master_log_sync row with a meaningful status.

    Returns ``{started_at, status, sync_id}`` on hit, or ``{started_at: None}``
    on miss / DB error. Uses a RO connection — the watchdog must not
    inadvertently write to the sync ledger.
    """
    try:
        with get_ro_connection(db_path=db_path) as conn:
            row = conn.execute(
                "SELECT started_at, status, sync_id FROM master_log_sync "
                "WHERE status IN ('success', 'running') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    except Exception as exc:
        logger.warning("flex_sync_watchdog: master_log_sync query failed: %s", exc)
        return {"started_at": None, "status": None, "sync_id": None, "db_error": str(exc)}
    if not row:
        return {"started_at": None, "status": None, "sync_id": None}
    return {"started_at": row[0], "status": row[1], "sync_id": row[2]}


def _write_sentinel(ts_utc: datetime, age_hours: float) -> bool:
    """Atomic sentinel write; returns True on success."""
    try:
        SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SENTINEL_FILE.with_suffix(".flag.tmp")
        tmp.write_text(
            f"flex_sync_stale_detected_at={ts_utc.isoformat()}\n"
            f"age_hours={age_hours:.1f}\n",
            encoding="utf-8",
        )
        os.replace(tmp, SENTINEL_FILE)
        return True
    except Exception as exc:
        logger.warning("flex_sync_watchdog: sentinel write failed: %s", exc)
        return False


def _delete_sentinel() -> bool:
    """Best-effort sentinel delete; returns True if deleted OR already absent."""
    try:
        if SENTINEL_FILE.exists():
            SENTINEL_FILE.unlink()
        return True
    except Exception as exc:
        logger.warning("flex_sync_watchdog: sentinel delete failed: %s", exc)
        return False


def run_flex_sync_watchdog(
    *,
    now_utc: datetime | None = None,
    threshold_hours: float = DEFAULT_STALE_THRESHOLD_HOURS,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Single watchdog iteration.

    Scheduler cron calls this at 18:00 ET weekdays. Returns a status dict
    for the caller to log. Never raises — every failure mode maps to a
    status field.
    """
    now = now_utc or datetime.now(timezone.utc)
    latest = query_latest_sync(db_path=db_path)
    last_dt = _parse_sync_timestamp(latest.get("started_at"))

    if last_dt is None:
        # No rows at all — treat as stale + flag. This is rare (bootstrap or
        # table truncation) but worth paging on.
        age_hours = -1.0
        _write_sentinel(now, age_hours)
        enqueue_alert(
            "FLEX_SYNC_MISSED",
            {
                "age_hours": "unknown",
                "last_sync_utc": "never",
                "threshold_hours": float(threshold_hours),
                "reason": "no_master_log_sync_rows",
                "db_error": latest.get("db_error"),
            },
            severity="crit",
            db_path=db_path,
        )
        return {
            "status": "alerted",
            "reason": "no_rows",
            "age_hours": age_hours,
            "sentinel_written": True,
        }

    age_hours = (now - last_dt).total_seconds() / 3600.0
    stale = age_hours > threshold_hours

    if stale:
        sentinel_ok = _write_sentinel(now, age_hours)
        enqueue_alert(
            "FLEX_SYNC_MISSED",
            {
                "age_hours": round(age_hours, 1),
                "last_sync_utc": last_dt.isoformat(),
                "threshold_hours": float(threshold_hours),
                "sync_id": latest.get("sync_id"),
            },
            severity="crit",
            db_path=db_path,
        )
        return {
            "status": "alerted",
            "reason": "stale",
            "age_hours": round(age_hours, 1),
            "sentinel_written": sentinel_ok,
            "last_sync_utc": last_dt.isoformat(),
        }

    # Fresh — clear sentinel if present
    sentinel_cleared = _delete_sentinel()
    return {
        "status": "fresh",
        "age_hours": round(age_hours, 1),
        "sentinel_cleared": sentinel_cleared,
        "last_sync_utc": last_dt.isoformat(),
    }


# ---------------------------------------------------------------------------
# Sprint 6 Mega-MR 3 — zero-row suspicion watchdog
# ---------------------------------------------------------------------------


def _query_recent_successful_syncs(
    *,
    limit: int,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return the most-recent N master_log_sync rows with status=success.

    Ordered newest-first. Each row exposes sync_id, started_at,
    rows_received. Errors return empty list (caller logs and bails out).
    """
    try:
        with get_ro_connection(db_path=db_path) as conn:
            rows = conn.execute(
                "SELECT sync_id, started_at, rows_received "
                "FROM master_log_sync WHERE status = 'success' "
                "ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except Exception as exc:
        logger.warning(
            "flex_sync_watchdog.zero_row: master_log_sync query failed: %s",
            exc,
        )
        return []
    return [
        {
            "sync_id": r[0],
            "started_at": r[1],
            "rows_received": r[2] or 0,
        }
        for r in rows
    ]


def _count_engine_activity_since(
    *,
    since_utc: datetime,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Count pending_orders + csp_allocator_latest rows created since a time.

    The dispatch asked for `order_state` / `pending_orders` /
    `csp_allocator_latest`. `order_state` does not exist in the current
    schema; `pending_orders` carries the staged-order signal and
    `csp_allocator_latest` carries Sprint 4 digest activity. An engine-
    activity count > 0 during a window of all-zero flex syncs is the
    strong signal the dispatch flagged as "definite suspicious".
    """
    since_iso = since_utc.isoformat()
    pending = 0
    allocator = 0
    try:
        with get_ro_connection(db_path=db_path) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM pending_orders WHERE created_at >= ?",
                (since_iso,),
            ).fetchone()[0]
    except Exception as exc:
        logger.debug("zero_row: pending_orders count failed: %s", exc)
    try:
        with get_ro_connection(db_path=db_path) as conn:
            allocator = conn.execute(
                "SELECT COUNT(*) FROM csp_allocator_latest WHERE created_at >= ?",
                (since_iso,),
            ).fetchone()[0]
    except Exception as exc:
        logger.debug("zero_row: csp_allocator_latest count failed: %s", exc)
    return {"pending_orders": int(pending), "csp_allocator_latest": int(allocator)}


def check_zero_row_suspicion(
    *,
    now_utc: datetime | None = None,
    window: int = DEFAULT_ZERO_ROW_WINDOW,
    prior_history_days: int = DEFAULT_PRIOR_HISTORY_DAYS,
    prior_avg_threshold: float = DEFAULT_PRIOR_AVG_THRESHOLD,
    min_history_syncs: int = 5,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Zero-row suspicion check — Sprint 6 Mega-MR 3.

    Trigger FLEX_SYNC_EMPTY_SUSPICIOUS alert iff:
      (a) the last ``window`` successful sync rows ALL have
          ``rows_received == 0``, AND
      (b) at least one of:
          - prior ``prior_history_days`` days of successful syncs have
            average rows_received > ``prior_avg_threshold`` (the
            "we normally see rows" signal), OR
          - engine activity (pending_orders OR csp_allocator_latest)
            observed during the window (the "engines saw trades but
            flex didn't capture" signal).

    Grace period: if we have fewer than ``min_history_syncs`` successful
    syncs total, skip — fresh installs / DB resets shouldn't flap.

    Does NOT raise. Never touches ``flex_sync.py``. RO DB connection.
    """
    now = now_utc or datetime.now(timezone.utc)

    recent = _query_recent_successful_syncs(
        limit=window + max(prior_history_days, 1) * 2,
        db_path=db_path,
    )

    # Grace period: not enough history to judge.
    if len(recent) < min_history_syncs:
        return {
            "status": "insufficient_history",
            "rows_available": len(recent),
            "min_required": min_history_syncs,
        }

    window_rows = recent[:window]
    if len(window_rows) < window:
        return {
            "status": "insufficient_window",
            "rows_available": len(window_rows),
            "window_required": window,
        }

    all_zero = all(r["rows_received"] == 0 for r in window_rows)
    if not all_zero:
        nonzero = [r for r in window_rows if r["rows_received"] > 0]
        return {
            "status": "fresh",
            "window": window,
            "window_sync_ids": [r["sync_id"] for r in window_rows],
            "nonzero_in_window": len(nonzero),
        }

    # Window is all-zero. Check the prior-history mean.
    prior_rows = recent[window : window + prior_history_days]
    prior_mean = 0.0
    if prior_rows:
        prior_mean = sum(r["rows_received"] for r in prior_rows) / len(prior_rows)

    prior_suspicious = prior_mean > prior_avg_threshold

    # Engine-activity cross-check: span window back to oldest window sync.
    oldest_in_window = window_rows[-1]
    try:
        since_utc = _parse_sync_timestamp(oldest_in_window["started_at"]) or now
    except Exception:
        since_utc = now
    activity = _count_engine_activity_since(since_utc=since_utc, db_path=db_path)
    activity_suspicious = (
        activity["pending_orders"] > 0 or activity["csp_allocator_latest"] > 0
    )

    if not (prior_suspicious or activity_suspicious):
        # All-zero but no corroborating signal. Log, don't alert.
        return {
            "status": "all_zero_benign",
            "window": window,
            "window_sync_ids": [r["sync_id"] for r in window_rows],
            "prior_mean": round(prior_mean, 2),
            "engine_activity": activity,
        }

    reasons: list[str] = []
    if prior_suspicious:
        reasons.append(
            f"prior_mean={prior_mean:.1f}>threshold={prior_avg_threshold}"
        )
    if activity_suspicious:
        reasons.append(
            f"engine_activity="
            f"pending={activity['pending_orders']},"
            f"allocator={activity['csp_allocator_latest']}"
        )

    enqueue_alert(
        "FLEX_SYNC_EMPTY_SUSPICIOUS",
        {
            "window": int(window),
            "window_sync_ids": [r["sync_id"] for r in window_rows],
            "prior_mean": round(prior_mean, 2),
            "prior_avg_threshold": float(prior_avg_threshold),
            "pending_orders": activity["pending_orders"],
            "csp_allocator_latest": activity["csp_allocator_latest"],
            "reasons": "; ".join(reasons),
        },
        severity="warn",
        db_path=db_path,
    )

    return {
        "status": "alerted",
        "reasons": reasons,
        "window": window,
        "window_sync_ids": [r["sync_id"] for r in window_rows],
        "prior_mean": round(prior_mean, 2),
        "engine_activity": activity,
    }
