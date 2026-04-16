"""Bot-side heartbeat registration (MR #2).

Thin wiring layer over :mod:`agt_equities.health` for the Telegram-bot
daemon. The scheduler daemon (agt_scheduler.py) already writes its own
heartbeat row every 60s via APScheduler; the bot is now the authoritative
operational daemon for paper trading (USE_SCHEDULER_DAEMON=0 during the
4-week cutover), so it must emit its own ``daemon_heartbeat`` row too.

Per DT Q3 ruling: 90s stale TTL. We write every 30s so two consecutive
misses still fit inside the TTL and a single missed tick is not a false
positive.

Exposed entry points:
    * ``register_bot_heartbeat(job_queue)`` — one-shot JobQueue
      registration called from ``telegram_bot.post_init``.
    * ``heartbeat_tick_seconds`` / ``heartbeat_ttl_seconds`` — constants
      for tests and the Windows watchdog PS1.

MR #2 explicitly does NOT add a second consumer; the Windows watchdog
(``scripts/bot_liveness_watchdog.ps1``) reads the row directly via
SQLite so it works whether the bot or the scheduler wrote it.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from agt_equities.health import (
    DEFAULT_STALE_TTL_S,
    write_heartbeat,
)

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import JobQueue


logger = logging.getLogger("agt_equities.heartbeat")

# Bot daemon name used in the daemon_heartbeat table.
BOT_DAEMON_NAME: str = "agt_bot"

# Write interval. Two missed writes still fit inside the 90s TTL.
heartbeat_tick_seconds: float = 30.0

# Re-export TTL so tests and the PS1 watchdog see a single canonical value.
heartbeat_ttl_seconds: float = float(DEFAULT_STALE_TTL_S)


async def _bot_heartbeat_job(context: Any) -> None:
    """JobQueue callback. Runs in the bot's asyncio loop every 30s.

    Safe no-op on any exception; heartbeat failure must NEVER crash the bot.
    The error surfaces in the log and the stale-TTL check catches it from
    the consumer side.
    """
    try:
        write_heartbeat(
            BOT_DAEMON_NAME,
            pid=os.getpid(),
            notes="ok",
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("bot heartbeat write failed: %s", exc)


def register_bot_heartbeat(job_queue: "JobQueue") -> bool:
    """Register the 30s heartbeat JobQueue tick.

    Returns True on successful registration, False otherwise. Idempotent —
    calling it twice schedules only one job (JobQueue replaces by name).

    The bot calls this from ``post_init`` after ``ensure_ib_connected``
    so the first beat reflects a bot that actually got past IB handshake.
    """
    if job_queue is None:
        logger.warning("register_bot_heartbeat: JobQueue not available")
        return False
    try:
        # Drop any prior job with the same name before re-registering.
        for job in list(job_queue.get_jobs_by_name("bot_heartbeat")):
            try:
                job.schedule_removal()
            except Exception:
                pass
        job_queue.run_repeating(
            callback=_bot_heartbeat_job,
            interval=heartbeat_tick_seconds,
            first=5.0,
            name="bot_heartbeat",
        )
        logger.info(
            "Scheduled: bot_heartbeat every %ds (ttl=%ds)",
            int(heartbeat_tick_seconds),
            int(heartbeat_ttl_seconds),
        )
        return True
    except Exception as exc:
        logger.error("register_bot_heartbeat failed: %s", exc)
        return False


__all__ = (
    "BOT_DAEMON_NAME",
    "heartbeat_tick_seconds",
    "heartbeat_ttl_seconds",
    "register_bot_heartbeat",
    "_bot_heartbeat_job",
)
