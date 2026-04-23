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
from typing import TYPE_CHECKING, Any

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
# ADR-007 Step 4 — invariant detection on every scheduler heartbeat tick.
#
# The runner itself (``agt_equities.invariants.runner.run_all``) catches per-
# check exceptions and records a ``degraded`` Violation, so a bug in one
# check function cannot abort the batch. The outer try/except here guards
# YAML load, DB connection open, and ``incidents_repo`` IO — belt and
# suspenders, because the scheduler owns ``clientId=2`` and one unguarded
# exception in a 60s loop takes down the whole process.
#
# ADR-007 §9.3 rate limit (5/hr) applies to AUTHORING, not detection;
# register freely here — Step 6 rate-gates downstream.
# ---------------------------------------------------------------------------

# MR !84: both helpers extracted to agt_equities.invariants.tick so the
# bot's JobQueue can run detection while USE_SCHEDULER_DAEMON=0. Re-export
# _evidence_fingerprint so tests that pinned it on this module continue
# to pass without modification. Detector string is stamped by the wrapper
# below so incident forensics can distinguish daemon-owned vs bot-owned
# detections during the Sprint A observation window.
from agt_equities.invariants.tick import (
    _evidence_fingerprint,
    check_invariants_tick as _shared_check_invariants_tick,
)
from agt_equities.runtime_fingerprint import capture_and_log as _capture_runtime_fingerprint


# ---------------------------------------------------------------------------
# Graceful shutdown — top-level handlers registered before the asyncio loop
# steals SIGINT/SIGBREAK. Reduces zombie risk by giving IB a chance to
# disconnect cleanly when NSSM stops the service.
#
# On Windows, NSSM's AppStopMethodConsole sends CTRL+BREAK (SIGBREAK).
# These top-level handlers cover the brief pre-asyncio-loop window;
# once `_run()` enters the asyncio loop, its in-function handler takes
# over and integrates with the scheduler.shutdown(wait=True) path.
# ---------------------------------------------------------------------------
_SHUTDOWN_IN_PROGRESS = False


def _handle_shutdown_signal(signum, frame):  # pragma: no cover
    """Graceful shutdown — disconnect IB, flush heartbeat, exit 0.

    Registered for SIGINT and SIGBREAK (Windows) / SIGTERM (POSIX).
    Reentrant-safe: a second signal during shutdown falls through to
    a hard exit.
    """
    global _SHUTDOWN_IN_PROGRESS
    if _SHUTDOWN_IN_PROGRESS:
        logger.warning("agt_scheduler.shutdown_force signum=%s", signum)
        sys.exit(1)
    _SHUTDOWN_IN_PROGRESS = True
    logger.info("agt_scheduler.shutdown_begin signum=%s", signum)
    try:
        ib = globals().get("ib")
        if ib is not None and hasattr(ib, "disconnect"):
            try:
                ib.disconnect()
                logger.info("agt_scheduler.shutdown_ib_disconnect ok")
            except Exception as e:
                logger.warning("agt_scheduler.shutdown_ib_disconnect_failed err=%s", e)
    finally:
        logger.info("agt_scheduler.shutdown_end")
        sys.exit(0)


def _register_shutdown_handlers() -> None:
    """Register platform-appropriate graceful-shutdown signals.

    Called once at scheduler start, BEFORE the asyncio event loop spins
    up — signals must be registered on the main thread before the loop
    steals them.
    """
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows only
        signal.signal(signal.SIGBREAK, _handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handle_shutdown_signal)
        except (OSError, ValueError):
            logger.info("agt_scheduler.sigterm_register_skipped")


def _check_invariants_tick() -> None:
    """Thin wrapper over ``invariants.tick.check_invariants_tick``.

    Stamps ``detector='agt_scheduler.heartbeat'`` so incident forensics
    distinguish daemon-owned detections from bot-owned (which use
    ``detector='telegram_bot.invariants_tick'``).
    """
    _shared_check_invariants_tick(detector="agt_scheduler.heartbeat")


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
        # ADR-007 Step 4: run safety invariants once per 60s tick and
        # register any Violations as incidents. Isolated from the
        # heartbeat write above so an invariant-path bug cannot
        # block the liveness signal. Live capital.
        try:
            _check_invariants_tick()
        except Exception:
            logger.exception("invariant heartbeat tick failed")

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

    # -- A5e -- beta_cache_refresh (daily 04:00 ET) + startup ----------------
    # Pure data refresh -- calls agt_equities.beta_cache.refresh_beta_cache
    # on all active tickers.  No IB dependency, no Telegram output.
    # Errors swallowed + logged per scheduler-job resilience pattern.

    def _beta_cache_refresh_job() -> None:
        try:
            from agt_equities.beta_cache import refresh_beta_cache
            from agt_equities import trade_repo

            tickers: list[str] = []
            try:
                cycles = trade_repo.get_active_cycles()
                tickers = list(
                    {c.ticker for c in cycles if c.status == "ACTIVE"}
                )
            except Exception:
                pass
            if tickers:
                refresh_beta_cache(tickers)
                logger.info(
                    "beta_cache_refresh: refreshed %d tickers", len(tickers),
                )
        except Exception as exc:
            logger.warning("beta_cache_refresh_job failed: %s", exc)

    scheduler.add_job(
        _beta_cache_refresh_job,
        trigger="cron",
        hour=4,
        minute=0,
        id="beta_cache_refresh",
        name="beta_cache_refresh",
        replace_existing=True,
    )
    registered.append("beta_cache_refresh")

    from datetime import datetime, timedelta

    scheduler.add_job(
        _beta_cache_refresh_job,
        trigger="date",
        run_date=datetime.now(tz=scheduler.timezone) + timedelta(seconds=10),
        id="beta_startup",
        name="beta_startup",
        replace_existing=True,
    )
    registered.append("beta_startup")

    # -- A5e -- corporate_intel_refresh (daily 05:00 ET) + startup -----------
    # yfinance corporate calendar refresh on active tickers.
    # No IB dependency, no Telegram output.

    def _corporate_intel_refresh_job() -> None:
        try:
            from agt_equities.providers.yfinance_corporate_intelligence import (
                YFinanceCorporateIntelligenceProvider,
            )
            from agt_equities import trade_repo

            tickers: list[str] = []
            try:
                cycles = trade_repo.get_active_cycles()
                tickers = list(
                    {c.ticker for c in cycles if c.status == "ACTIVE"}
                )
            except Exception:
                pass
            if tickers:
                provider = YFinanceCorporateIntelligenceProvider()
                for tk in tickers:
                    try:
                        provider.get_corporate_calendar(tk)
                    except Exception as tk_exc:
                        logger.warning(
                            "corporate_intel refresh failed for %s: %s",
                            tk,
                            tk_exc,
                        )
                logger.info(
                    "corporate_intel: refreshed %d tickers", len(tickers),
                )
        except Exception as exc:
            logger.warning("corporate_intel_refresh_job failed: %s", exc)

    scheduler.add_job(
        _corporate_intel_refresh_job,
        trigger="cron",
        hour=5,
        minute=0,
        id="corporate_intel_refresh",
        name="corporate_intel_refresh",
        replace_existing=True,
    )
    registered.append("corporate_intel_refresh")

    scheduler.add_job(
        _corporate_intel_refresh_job,
        trigger="date",
        run_date=datetime.now(tz=scheduler.timezone) + timedelta(seconds=15),
        id="corporate_intel_startup",
        name="corporate_intel_startup",
        replace_existing=True,
    )
    registered.append("corporate_intel_startup")

    # -- A5e -- flex_sync_eod (Mon-Fri 17:00 ET) ----------------------------
    # IBKR Flex Web Service sync into master_log_* tables.  No IB API
    # dependency (uses HTTPS Flex endpoint).  On success, enqueue
    # FLEX_SYNC_DIGEST alert for bot-side Telegram delivery.  On failure,
    # enqueue FLEX_SYNC_FAILURE alert (crit severity).
    #
    # DT Q2: flex_sync itself uses a single atomic transaction internally.
    # The scheduler job is a thin wrapper that calls run_sync() and surfaces
    # the result onto the cross_daemon_alerts bus.

    def _flex_sync_eod_job() -> None:
        try:
            from agt_equities.flex_sync import run_sync, SyncMode
            result = run_sync(SyncMode.INCREMENTAL)
        except Exception as exc:
            logger.exception("flex_sync_eod: run_sync raised: %s", exc)
            try:
                from agt_equities.alerts import enqueue_alert
                enqueue_alert(
                    "FLEX_SYNC_FAILURE",
                    {"error": str(exc)[:500]},
                    severity="crit",
                )
            except Exception as alert_exc:
                logger.error(
                    "flex_sync_eod: alert enqueue failed: %s", alert_exc,
                )
            return

        try:
            from agt_equities.alerts import enqueue_alert
            payload = {
                "sync_id": getattr(result, "sync_id", None),
                "mode": "INCREMENTAL",
                "sections_processed": getattr(result, "sections_processed", 0),
                "rows_received": getattr(result, "rows_received", 0),
                "rows_inserted": getattr(result, "rows_inserted", 0),
            }
            sev = "info"
            if getattr(result, "error_message", None):
                payload["error"] = str(result.error_message)[:500]
                sev = "warn"
            enqueue_alert("FLEX_SYNC_DIGEST", payload, severity=sev)
        except Exception as alert_exc:
            # Sync already committed; alert-bus failure is best-effort.
            logger.error("flex_sync_eod: alert enqueue failed: %s", alert_exc)

    scheduler.add_job(
        _flex_sync_eod_job,
        trigger="cron",
        day_of_week="mon-fri",
        hour=17,
        minute=0,
        id="flex_sync_eod",
        name="flex_sync_eod",
        replace_existing=True,
    )
    registered.append("flex_sync_eod")

    # -- A5e -- universe_monthly (1st of month, 06:00 ET) ---------------------
    # Refreshes ticker_universe table from Wikipedia + yfinance.  No IB
    # dependency.  Enqueues UNIVERSE_REFRESH alert for bot-side delivery.
    # Long-running (~minutes due to yfinance enrichment) — runs in the
    # APScheduler threadpool, which is why max_workers=10 matters.

    def _universe_monthly_job() -> None:
        try:
            from agt_equities.universe_refresh import refresh_ticker_universe
            result = refresh_ticker_universe()
        except Exception as exc:
            logger.exception("universe_monthly: refresh raised: %s", exc)
            try:
                from agt_equities.alerts import enqueue_alert
                enqueue_alert(
                    "UNIVERSE_REFRESH",
                    {"error": str(exc)[:500]},
                    severity="crit",
                )
            except Exception as alert_exc:
                logger.error(
                    "universe_monthly: alert enqueue failed: %s", alert_exc,
                )
            return

        try:
            from agt_equities.alerts import enqueue_alert
            payload = {
                "added": result.get("added", 0),
                "updated": result.get("updated", 0),
                "total": result.get("total", 0),
            }
            sev = "info"
            err = result.get("error")
            if err:
                payload["error"] = str(err)[:500]
                sev = "warn"
            enqueue_alert("UNIVERSE_REFRESH", payload, severity=sev)
        except Exception as alert_exc:
            logger.error("universe_monthly: alert enqueue failed: %s", alert_exc)

    scheduler.add_job(
        _universe_monthly_job,
        trigger="cron",
        day=1,
        hour=6,
        minute=0,
        id="universe_monthly",
        name="universe_monthly",
        replace_existing=True,
    )
    registered.append("universe_monthly")

    # Unit A5 remaining: cc_daily, watchdog_daily, conviction_weekly,
    #   attested_poller (10s).
    #   el_snapshot_writer shipped in A5d.d.
    #   beta_cache_refresh + corporate_intel_refresh + flex_sync_eod
    #   + universe_monthly shipped in A5e.

    # ── A5e: conviction_weekly (Sunday 20:00 ET) ──────────────────────────
    async def _conviction_weekly_job() -> None:
        try:
            ib = await ib_connector.ensure_connected()
        except Exception as exc:
            logger.warning("conviction_weekly: IB not available: %s", exc)
            try:
                from agt_equities.alerts import enqueue_alert
                enqueue_alert(
                    "CONVICTION_REFRESH",
                    {"error": f"IB connect failed: {exc}"[:500]},
                    severity="warn",
                )
            except Exception as alert_exc:
                logger.error("conviction_weekly: alert enqueue failed: %s", alert_exc)
            return
        try:
            positions = await ib.reqPositionsAsync()
        except Exception as exc:
            logger.warning("conviction_weekly: reqPositionsAsync failed: %s", exc)
            try:
                from agt_equities.alerts import enqueue_alert
                enqueue_alert(
                    "CONVICTION_REFRESH",
                    {"error": f"reqPositions failed: {exc}"[:500]},
                    severity="warn",
                )
            except Exception as alert_exc:
                logger.error("conviction_weekly: alert enqueue failed: %s", alert_exc)
            return
        try:
            from agt_equities.conviction import (
                refresh_conviction_data,
                EXCLUDED_TICKERS,
            )
            held = set()
            for pos in positions:
                if pos.position != 0 and pos.contract.secType == "STK":
                    tkr = pos.contract.symbol.upper()
                    if tkr not in EXCLUDED_TICKERS:
                        held.add(tkr)
            result = refresh_conviction_data(held)
        except Exception as exc:
            logger.exception("conviction_weekly: refresh raised: %s", exc)
            try:
                from agt_equities.alerts import enqueue_alert
                enqueue_alert(
                    "CONVICTION_REFRESH",
                    {"error": str(exc)[:500]},
                    severity="crit",
                )
            except Exception as alert_exc:
                logger.error("conviction_weekly: alert enqueue failed: %s", alert_exc)
            return
        try:
            from agt_equities.alerts import enqueue_alert
            payload = {
                "updated": result.get("updated", 0),
                "failed": result.get("failed", 0),
                "total": result.get("total", 0),
            }
            sev = "info"
            if result.get("failed", 0) > 0:
                sev = "warn"
            if result.get("error"):
                payload["error"] = str(result["error"])[:500]
                sev = "warn"
            enqueue_alert("CONVICTION_REFRESH", payload, severity=sev)
        except Exception as alert_exc:
            logger.error("conviction_weekly: alert enqueue failed: %s", alert_exc)

    scheduler.add_job(
        _conviction_weekly_job,
        trigger="cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        id="conviction_weekly",
        name="conviction_weekly",
        replace_existing=True,
    )
    registered.append("conviction_weekly")

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

    try:
        _state_dir = os.environ.get("AGT_STATE_DIR", "C:/AGT_Runtime/state")
        _capture_runtime_fingerprint(
            service_name="agt_scheduler",
            dotenv_paths=[
                Path(_state_dir) / ".env",
                Path("C:/AGT_Telegram_Bridge/.env"),
            ],
            nssm_services=["agt-telegram-bot", "agt-scheduler"],
            logger=logger,
        )
    except Exception as e:
        logger.warning("runtime_fingerprint wiring soft-fail: %s", e)

    # MR !90: evict any zombie agt_scheduler.py holding IBKR clientId=2
    # before opening a new IB connection. NSSM's restart of the outer
    # venv launcher can leave the inner grandchild alive; that zombie would
    # fail this scheduler's IBKR connect with a clientId collision. See
    # agt_equities/zombie_evict.py for the Windows semantics note.
    from agt_equities.zombie_evict import evict_zombie_daemons
    _zr = evict_zombie_daemons(
        cmdline_marker="agt_scheduler.py",
        self_pid=os.getpid(),
        logger=logger,
    )
    if _zr.zombies_survived_sigkill:
        logger.error(
            "Zombie eviction incomplete: survivors=%s; refusing to boot",
            _zr.zombies_survived_sigkill,
        )
        return 7

    cfg = IBConnConfig(client_id=scheduler_client_id())
    ib_conn = IBConnector(config=cfg)

    scheduler = build_scheduler()
    registered = register_jobs(scheduler, ib_conn)
    logger.info("Registered %d job(s): %s", len(registered), registered)

    scheduler.start()
    logger.info("Scheduler started.")

    stop_event = asyncio.Event()

    def _signal_handler(*_args: object) -> None:
        logger.info("agt_scheduler.shutdown_begin source=asyncio_handler")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
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
            # A5e: wait=True so in-flight jobs can finish cleanly before
            # the loop tears down. Matches DT Q1a-g clean-shutdown spec.
            scheduler.shutdown(wait=True)
        except Exception:
            logger.exception("scheduler.shutdown raised")
        try:
            await ib_conn.disconnect()
        except Exception:
            logger.exception("ib_conn.disconnect raised")
        logger.info("agt_scheduler.shutdown_end exit=clean")
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
    # ADR-007 Addendum §2.1 — halt if DB path is not canonical.
    from agt_equities.invariants.bootstrap import assert_canonical_db_path
    from agt_equities import db as agt_db
    assert_canonical_db_path(resolved_path=agt_db.DB_PATH)
    # Register signal handlers BEFORE asyncio.run so SIGINT/SIGBREAK fire
    # cleanly during the brief pre-loop window. The asyncio in-function
    # _signal_handler in _run() takes over once the loop is established.
    _register_shutdown_handlers()
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
