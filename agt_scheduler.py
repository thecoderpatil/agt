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
        # A5c: when the sweep finds orphan staged rows, surface a warn-severity
        # ORPHAN_SWEEP event onto the cross_daemon_alerts bus for bot-side
        # delivery (consumer wires up in A5d). Zero-swept runs stay silent.
        try:
            swept = sweep_orphan_staged_orders(ttl_hours=DEFAULT_ORPHAN_TTL_HOURS)
        except Exception as exc:
            logger.error("orphan_sweep call failed: %s", exc)
            return
        if swept and swept > 0:
            try:
                # Lazy import — keeps agt_scheduler module-import cost minimal
                # and matches the pattern used by _attested_sweep_job.
                from agt_equities.alerts import enqueue_alert
                enqueue_alert(
                    "ORPHAN_SWEEP",
                    {
                        "swept_count": int(swept),
                        "ttl_hours": float(DEFAULT_ORPHAN_TTL_HOURS),
                    },
                    severity="warn",
                )
            except Exception as exc:
                # Alert enqueue is best-effort. Sweep already committed; an
                # alert-bus failure must not crash the scheduler job loop.
                logger.error("orphan_sweep alert enqueue failed: %s", exc)

    scheduler.add_job(
        _orphan_sweep_job,
        trigger="interval",
        minutes=5,
        id="orphan_sweep",
        name="orphan_sweep",
        replace_existing=True,
    )
    registered.append("orphan_sweep")

    # ── A5a — attested_sweeper (60s) ────────────────────────────────────────
    # Continuous sweeper for stale STAGED + ATTESTED rows in
    # bucket3_dynamic_exit_log (R7 — 10min ATTESTED TTL, 15min STAGED TTL).
    # Pure DB op; identical semantics to telegram_bot._sweep_attested_ttl_job
    # but addressed via the shared get_db_connection helper rather than the
    # bot-local _get_db_connection alias. Bot-side registration in
    # telegram_bot.main() is intentionally left in place — the jq.run_repeating
    # call there is harmless when USE_SCHEDULER_DAEMON=0 (default) because
    # the scheduler daemon is not running. Bot-side gating lands in A5d
    # alongside NSSM cutover prep.
    def _attested_sweep_job() -> None:
        # Lazy imports to keep agt_scheduler module-import cost minimal and
        # avoid pulling rule_engine + agt_equities.db into slim CI containers
        # that don't need them at import time.
        from contextlib import closing
        from agt_equities.db import get_db_connection
        from agt_equities.rule_engine import sweep_stale_dynamic_exit_stages
        try:
            with closing(get_db_connection()) as conn:
                result = sweep_stale_dynamic_exit_stages(conn)
                swept = result.get("swept", 0)
                att_swept = result.get("attested_swept", 0)
                if swept > 0 or att_swept > 0:
                    logger.info(
                        "attested_sweeper: staged=%d attested=%d swept",
                        swept, att_swept,
                    )
        except Exception as exc:
            logger.error("attested_sweeper error: %s", exc)

    scheduler.add_job(
        _attested_sweep_job,
        trigger="interval",
        seconds=60,
        id="attested_sweeper",
        name="attested_sweeper",
        replace_existing=True,
    )
    registered.append("attested_sweeper")

    # ------------------------------------------------------------------
    # A5d.d: EL snapshot writer — polls IB accountSummaryAsync every 30s,
    # writes one row per active account to el_snapshots, and emits an
    # APEX_SURVIVAL alert onto cross_daemon_alerts when
    # excess_liquidity / NLV <= 0.08 on a margin-eligible account
    # (15-min per-account debounce; 30s per-account write debounce).
    #
    # Dormant under USE_SCHEDULER_DAEMON=0 (daemon not running); bot-side
    # telegram_bot._el_snapshot_writer_job continues to own this surface
    # until the A5e atomic cutover flips the flag.
    # ------------------------------------------------------------------
    _el_last_write: dict[str, float] = {}
    _apex_last_alert: dict[str, float] = {}
    _EL_WRITE_DEBOUNCE_SECONDS = 30.0
    _APEX_ALERT_DEBOUNCE_SECONDS = 900.0  # 15 min; matches bot behavior
    _APEX_EL_THRESHOLD = 0.08

    async def _el_snapshot_writer_job() -> None:
        """Poll accountSummary, write el_snapshots, emit APEX alerts onto bus.

        All failure modes (IB down, accountSummaryAsync exception, DB write
        error, alert enqueue error) are swallowed + logged — the job must
        not crash the scheduler event loop.
        """
        import time
        from contextlib import closing
        try:
            from agt_equities.config import (
                ACTIVE_ACCOUNTS,
                MARGIN_ACCOUNTS,
                ACCOUNT_TO_HOUSEHOLD,
            )
        except Exception as exc:
            logger.error("el_snapshot_writer: config import failed: %s", exc)
            return
        try:
            ib = await ib_connector.ensure_connected()
        except Exception as exc:
            logger.debug("el_snapshot_writer: IB not available: %s", exc)
            return
        try:
            summary = await ib.accountSummaryAsync()
        except Exception as exc:
            logger.debug(
                "el_snapshot_writer: accountSummaryAsync failed: %s", exc,
            )
            return
        if not summary:
            return

        now = time.time()
        _WANTED = {"NetLiquidation", "ExcessLiquidity", "BuyingPower"}
        acct_data: dict[str, dict[str, float]] = {}
        for item in summary:
            if item.account not in ACTIVE_ACCOUNTS:
                continue
            if item.tag not in _WANTED:
                continue
            acct_data.setdefault(item.account, {})
            try:
                acct_data[item.account][item.tag] = float(item.value)
            except (TypeError, ValueError):
                continue

        for acct_id, data in acct_data.items():
            nlv = float(data.get("NetLiquidation") or 0.0)
            if nlv <= 0:
                continue
            excess_liquidity = float(data.get("ExcessLiquidity") or 0.0)
            hh = ACCOUNT_TO_HOUSEHOLD.get(acct_id, "Unknown")

            if acct_id in MARGIN_ACCOUNTS:
                el_pct = excess_liquidity / nlv if nlv else 0.0
                if el_pct <= _APEX_EL_THRESHOLD:
                    if now - _apex_last_alert.get(acct_id, 0.0) > _APEX_ALERT_DEBOUNCE_SECONDS:
                        try:
                            from agt_equities.alerts import enqueue_alert
                            enqueue_alert(
                                "APEX_SURVIVAL",
                                {
                                    "account_id": acct_id,
                                    "household": hh,
                                    "el_pct": float(el_pct),
                                    "nlv": float(nlv),
                                    "excess_liquidity": float(excess_liquidity),
                                },
                                severity="critical",
                            )
                            _apex_last_alert[acct_id] = now
                        except Exception as alert_exc:
                            logger.error(
                                "APEX_SURVIVAL alert enqueue failed for %s: %s",
                                acct_id, alert_exc,
                            )
                    # Skip DB write while APEX condition active (matches bot).
                    continue
                else:
                    _apex_last_alert.pop(acct_id, None)
            else:
                _apex_last_alert.pop(acct_id, None)

            last = _el_last_write.get(acct_id, 0.0)
            if now - last < _EL_WRITE_DEBOUNCE_SECONDS:
                continue

            try:
                from agt_equities.db import get_db_connection, tx_immediate
                with closing(get_db_connection()) as conn:
                    with tx_immediate(conn):
                        conn.execute(
                            "INSERT INTO el_snapshots "
                            "(account_id, household, excess_liquidity, nlv, "
                            "buying_power, source) "
                            "VALUES (?, ?, ?, ?, ?, 'ibkr_live')",
                            (
                                acct_id, hh, excess_liquidity, nlv,
                                data.get("BuyingPower"),
                            ),
                        )
                _el_last_write[acct_id] = now
            except Exception as db_exc:
                logger.warning(
                    "el_snapshot_writer: DB write failed for %s: %s",
                    acct_id, db_exc,
                )

    scheduler.add_job(
        _el_snapshot_writer_job,
        trigger="interval",
        seconds=30,
        id="el_snapshot_writer",
        name="el_snapshot_writer",
        replace_existing=True,
    )
    registered.append("el_snapshot_writer")

    # Unit A5 remaining: cc_daily, watchdog_daily, universe_monthly,
    #   conviction_weekly, flex_sync_eod, attested_poller (10s),
    #   beta_cache_refresh (daily 04:00), beta_startup,
    #   corporate_intel_refresh (daily 05:00), corporate_intel_startup.
    #   el_snapshot_writer shipped in A5d.d.
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
