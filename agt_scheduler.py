"""AGT Scheduler daemon — APScheduler process owning IBKR ``clientId=2``.

Decoupling Sprint A Unit A1 — SKELETON ONLY.

Scope of this skeleton (Unit A1):
* APScheduler ``AsyncIOScheduler`` boot with explicit
  ``ThreadPoolExecutor(max_workers=10)`` per blind spot #3 mitigation.
* Owns IB ``clientId=2`` via :class:`agt_equities.ib_conn.IBConnector`.
* ``USE_SCHEDULER_DAEMON`` env flag (default ``False``) gates the actual run
  loop. Per DT Q1a-g, the scheduler is dark for the 4-week cutover window
  unless the operator explicitly flips the flag. With the flag off the file
  imports cleanly and ``main()`` exits 0, allowing tests + smoke imports
  without touching IB or registering jobs.
* Job registration is intentionally empty here. The 13 jobs migrate in Unit
  A5 (atomic cutover per Q1a-g — phased moves are rejected).
* Heartbeat write loop wires up in Unit A2.
* ``flex_sync_eod`` callback and the orphan sweep also land in A2/A3.

This module MUST be importable without side effects when
``USE_SCHEDULER_DAEMON`` is unset/false. Importing must NEVER touch the
production database, schedule jobs, or open an IB connection. Tests rely on
this invariant.

Run modes
---------
* As a script:  ``python -m agt_scheduler`` (or ``python agt_scheduler.py``).
* As an NSSM service: ``nssm install agt_scheduler "<python>" "<repo>\\agt_scheduler.py"``
  with ``USE_SCHEDULER_DAEMON=1`` in the service environment.

Logging
-------
Rotating file handler at ``logs/agt_scheduler.log`` (5 MB × 5 backups), plus
stdout for NSSM capture.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agt_equities.ib_conn import IBConnConfig, IBConnector

if TYPE_CHECKING:  # avoid hard runtime dep until A5
    from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAEMON_NAME = "agt_scheduler"
DEFAULT_CLIENT_ID = 2
SCHEDULER_THREADPOOL_MAX_WORKERS = 10  # blind-spot #3 mitigation
SCHEDULER_TIMEZONE = "America/New_York"

# Match telegram_bot.py BASE_DIR convention.
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logger = logging.getLogger("agt_scheduler")


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def use_scheduler_daemon() -> bool:
    """``USE_SCHEDULER_DAEMON`` env flag (default off).

    Per DT Q1a-g this defaults off for the 4-week cutover window. Operator
    flips it on Windows-side via NSSM service env or a ``set`` in cmd.
    """
    return _env_truthy("USE_SCHEDULER_DAEMON", "0")


def scheduler_client_id() -> int:
    return int(os.environ.get("SCHEDULER_IB_CLIENT_ID", str(DEFAULT_CLIENT_ID)))


# ---------------------------------------------------------------------------
# Logging setup — only fires when actually running, not on import.
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    if logger.handlers:  # idempotent
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FMT)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.setLevel(logging.INFO)
    logger.addHandler(stream)

    try:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / "agt_scheduler.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
    except Exception as exc:
        # Don't let logging-setup failure kill the daemon.
        logger.warning("rotating file handler init failed: %s", exc)


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------

def build_scheduler() -> "AsyncIOScheduler":
    """Construct the AsyncIOScheduler with explicit threadpool sizing.

    The threadpool size is the blind-spot #3 mitigation: APScheduler defaults
    to ``max_workers=1`` which serializes every job and starves intraday
    repeating tasks under flex_sync load.
    """
    from apscheduler.executors.pool import ThreadPoolExecutor as _APThreadPoolExecutor
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import pytz

    executors = {
        "default": _APThreadPoolExecutor(SCHEDULER_THREADPOOL_MAX_WORKERS),
    }
    job_defaults = {
        "coalesce": True,        # don't pile up missed runs
        "max_instances": 1,      # one job instance at a time per id
        "misfire_grace_time": 60,
    }
    return AsyncIOScheduler(
        executors=executors,
        job_defaults=job_defaults,
        timezone=pytz.timezone(SCHEDULER_TIMEZONE),
    )


# ---------------------------------------------------------------------------
# Job registry — intentionally empty until Unit A5.
# ---------------------------------------------------------------------------

def register_jobs(scheduler: "AsyncIOScheduler", ib_connector: IBConnector) -> list[str]:
    """Register all scheduler-owned jobs. Returns list of registered job names.

    A2 adds: heartbeat_writer (60s), orphan_sweep (5 min).
    A5 will add the 13 production jobs migrated from telegram_bot.py
    (atomic cutover, no phased moves per Q1a-g).
    """
    from agt_equities.health import (
        write_heartbeat,
        sweep_orphan_staged_orders,
        DEFAULT_ORPHAN_TTL_HOURS,
    )

    registered: list[str] = []

    client_id = ib_connector.config.client_id

    def _heartbeat_job() -> None:
        write_heartbeat(
            DAEMON_NAME,
            client_id=client_id,
            notes="ok",
        )

    scheduler.add_job(
        _heartbeat_job,
        trigger="interval",
        seconds=60,
        id="heartbeat_writer",
        name="heartbeat_writer",
        replace_existing=True,
    )
    registered.append("heartbeat_writer")

    def _orphan_sweep_job() -> None:
        sweep_orphan_staged_orders(ttl_hours=DEFAULT_ORPHAN_TTL_HOURS)

    scheduler.add_job(
        _orphan_sweep_job,
        trigger="interval",
        minutes=5,
        id="orphan_sweep",
        name="orphan_sweep",
        replace_existing=True,
    )
    registered.append("orphan_sweep")

    # Unit A5 will append: cc_daily, watchdog_daily, universe_monthly,
    #   conviction_weekly, flex_sync_eod, attested_poller (10s),
    #   attested_sweeper (60s), el_snapshot_writer (30s),
    #   staged_alert_flush (15s), beta_cache_refresh (daily 04:00),
    #   beta_startup, corporate_intel_refresh (daily 05:00),
    #   corporate_intel_startup. = 13 jobs total.
    return registered


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def _run() -> int:
    _configure_logging()
    logger.info(
        "agt_scheduler boot: pid=%s clientId=%s threadpool=%d",
        os.getpid(), scheduler_client_id(), SCHEDULER_THREADPOOL_MAX_WORKERS,
    )

    cfg = IBConnConfig(client_id=scheduler_client_id())
    ib_conn = IBConnector(config=cfg)

    scheduler = build_scheduler()
    registered = register_jobs(scheduler, ib_conn)
    logger.info("Registered %d job(s): %s", len(registered), registered)

    scheduler.start()
    logger.info("Scheduler started.")

    stop_event = asyncio.Event()

    def _signal_handler(*_args: object) -> None:
        logger.info("Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler; NSSM sends CTRL+BREAK.
            signal.signal(sig, lambda *_a: _signal_handler())

    try:
        await stop_event.wait()
    finally:
        logger.info("Scheduler stopping…")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("scheduler.shutdown raised")
        try:
            await ib_conn.disconnect()
        except Exception:
            logger.exception("ib_conn.disconnect raised")
        logger.info("agt_scheduler exit clean.")
    return 0


def main() -> int:
    if not use_scheduler_daemon():
        # Cutover-window default: do not run. Allow import smokes.
        # Caller (NSSM service or operator) sets USE_SCHEDULER_DAEMON=1 to enable.
        sys.stderr.write(
            "agt_scheduler: USE_SCHEDULER_DAEMON not set — exiting without action. "
            "Set USE_SCHEDULER_DAEMON=1 to run.\n"
        )
        return 0
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
