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
