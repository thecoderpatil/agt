"""Daemon heartbeat + orphan sweep utilities (Decoupling Sprint A Unit A2).

Per DT Q3 ruling: each daemon writes a heartbeat row every 60s; consumers
treat any heartbeat older than ``DEFAULT_STALE_TTL_S`` (90s) as stale and
take action (Telegram CRITICAL alert from the bot side, fallback skip from
the scheduler side).

Both daemons share the same SQLite WAL ``agt_desk.db`` as the synchronization
layer per the two-daemon WAL bus design — no IPC, no sockets.
"""

from __future__ import annotations

import logging
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from agt_equities.db import get_db_connection, get_ro_connection


logger = logging.getLogger("agt_equities.health")

# DT Q3 ruling: 90s stale TTL.
DEFAULT_STALE_TTL_S: float = 90.0
# Default sweep TTL: 24h. Anything older than this in pending_orders.status='staged'
# is treated as orphan and superseded.
DEFAULT_ORPHAN_TTL_HOURS: float = 24.0


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Heartbeat write
# ---------------------------------------------------------------------------

def write_heartbeat(
    daemon_name: str,
    *,
    pid: int | None = None,
    client_id: int | None = None,
    notes: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """UPSERT a heartbeat row for ``daemon_name``.

    Idempotent. Safe to call from any thread; per-call connection.
    """
    pid = pid if pid is not None else os.getpid()
    now = _utcnow_iso()
    try:
        with closing(get_db_connection(db_path=db_path)) as conn:
            conn.execute(
                """
                INSERT INTO daemon_heartbeat
                    (daemon_name, last_beat_utc, pid, client_id, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(daemon_name) DO UPDATE SET
                    last_beat_utc = excluded.last_beat_utc,
                    pid           = excluded.pid,
                    client_id     = excluded.client_id,
                    notes         = excluded.notes
                """,
                (daemon_name, now, pid, client_id, notes),
            )
            conn.commit()
    except Exception as exc:
        # Heartbeat failure must not crash the daemon; just log loudly.
        logger.exception("write_heartbeat(%s) failed: %s", daemon_name, exc)


# ---------------------------------------------------------------------------
# Heartbeat read
# ---------------------------------------------------------------------------

def get_heartbeat(
    daemon_name: str,
    *,
    db_path: str | Path | None = None,
) -> dict | None:
    """Return the most recent heartbeat row for ``daemon_name`` or ``None``."""
    try:
        with closing(get_ro_connection(db_path=db_path)) as conn:
            row = conn.execute(
                """
                SELECT daemon_name, last_beat_utc, pid, client_id, notes
                FROM daemon_heartbeat
                WHERE daemon_name = ?
                """,
                (daemon_name,),
            ).fetchone()
    except Exception as exc:
        logger.warning("get_heartbeat(%s) failed: %s", daemon_name, exc)
        return None
    if row is None:
        return None
    return dict(row)


def heartbeat_age_seconds(
    daemon_name: str,
    *,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> float | None:
    """Return age in seconds, or ``None`` if no heartbeat row exists."""
    hb = get_heartbeat(daemon_name, db_path=db_path)
    if hb is None:
        return None
    try:
        last = datetime.fromisoformat(hb["last_beat_utc"])
    except (TypeError, ValueError):
        logger.warning(
            "heartbeat_age_seconds(%s): bad last_beat_utc=%r",
            daemon_name, hb.get("last_beat_utc"),
        )
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now if now is not None else datetime.now(tz=timezone.utc)
    return (now - last).total_seconds()


def is_daemon_stale(
    daemon_name: str,
    *,
    ttl_s: float = DEFAULT_STALE_TTL_S,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Return True if heartbeat is missing OR older than ``ttl_s``."""
    age = heartbeat_age_seconds(daemon_name, now=now, db_path=db_path)
    if age is None:
        return True
    return age > ttl_s


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------

def sweep_orphan_staged_orders(
    *,
    ttl_hours: float = DEFAULT_ORPHAN_TTL_HOURS,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Mark any ``pending_orders.status='staged'`` row older than ``ttl_hours``
    as ``superseded`` and append an audit entry to ``orphan_sweep_log``.

    Returns the number of rows swept. Safe under concurrent fill writes — the
    scheduler holds ``BEGIN IMMEDIATE`` only for the duration of the UPDATE
    + audit write, well under the global ``busy_timeout=15000`` window.
    """
    from agt_equities.db import tx_immediate

    now = now if now is not None else datetime.now(tz=timezone.utc)
    cutoff = now.timestamp() - (ttl_hours * 3600.0)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")

    swept = 0
    try:
        with closing(get_db_connection(db_path=db_path)) as conn:
            with tx_immediate(conn):
                # ``created_at`` is stored as ISO string per
                # ``append_pending_tickets`` — string compare works for ISO8601.
                result = conn.execute(
                    """
                    UPDATE pending_orders
                    SET status = 'superseded'
                    WHERE status = 'staged' AND created_at < ?
                    """,
                    (cutoff_iso,),
                )
                swept = result.rowcount or 0
                conn.execute(
                    """
                    INSERT INTO orphan_sweep_log
                        (run_at_utc, swept_count, ttl_hours, notes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        _utcnow_iso(),
                        swept,
                        ttl_hours,
                        f"cutoff_iso={cutoff_iso}",
                    ),
                )
    except Exception as exc:
        logger.exception("sweep_orphan_staged_orders failed: %s", exc)
        return 0

    if swept > 0:
        logger.warning(
            "orphan sweep: marked %d staged pending_orders as superseded "
            "(ttl=%.1fh, cutoff=%s)",
            swept, ttl_hours, cutoff_iso,
        )
    else:
        logger.info("orphan sweep: nothing to sweep (ttl=%.1fh)", ttl_hours)
    return swept
