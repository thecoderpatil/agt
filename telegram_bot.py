



"""

AGT Equities — Telegram ↔ IB Bridge  (Hybrid Architecture)



Two processing paths:

  PATH 1 — Hardcoded Execution Parser (message starts with "ACCOUNT:")

           Parses a structured payload from the Morning Screener and writes

           staged tickets to the AGT desk database with transmit=False.

           Bypasses the LLM entirely.



  PATH 2 — Tool-Calling Quant (all other messages)

           Claude dispatches to hardcoded tool functions (VIX, PnL, news).

           No dynamic code generation or exec().

"""



import asyncio

import html

import json

import logging

import logging.handlers

import math

import os

import re

import secrets

import socket

import sqlite3

import sys

import time

import uuid

from collections import defaultdict

from contextlib import closing

from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from datetime import date, date as _date, datetime as _datetime, time as _time, timedelta, timedelta as _timedelta, timezone as _timezone

from zoneinfo import ZoneInfo as _ZoneInfo

from pathlib import Path



import nest_asyncio

import pytz

nest_asyncio.apply()



import anthropic

import finnhub

import ib_async

import pandas as pd

import yfinance as yf

from agt_equities.execution_gate import assert_execution_enabled, ExecutionDisabledError

from agt_equities.walker import compute_walk_away_pnl as _compute_walk_away_pnl

from agt_equities import roll_engine
from agt_equities import roll_scanner

from agt_equities.runtime import RunContext, RunMode

from agt_equities.sinks import CollectorOrderSink, SQLiteDecisionSink

from agt_equities.ib_order_builder import (

    build_adaptive_option_order,

    build_adaptive_sell_order,

    build_adaptive_roll_combo,

    build_adaptive_stk_order,

)

from agt_equities.urgency_policy import decide_roll_urgency

from agt_equities.cc_engine import (

    CCPickerInput, CCWrite, CCStandDown, ChainStrike, pick_cc_strike,

)

from agt_equities.roll_engine import (

    ConstraintMatrix, MarketSnapshot, OptionQuote, PortfolioContext, Position,

    HoldResult, HarvestResult, RollResult, AssignResult, LiquidateResult, AlertResult,

)

from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from telegram.ext import (

    ApplicationBuilder,

    CallbackQueryHandler,

    CommandHandler,

    ExtBot,

    MessageHandler,

    ContextTypes,

    filters,

)



# ---------------------------------------------------------------------------

# Configuration

# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "agt_desk.db"



_env_path = BASE_DIR / ".env"

load_dotenv(_env_path, override=True)



TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]

AUTHORIZED_USER_ID  = int(os.environ["TELEGRAM_USER_ID"])

ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]

FINNHUB_API_KEY     = os.environ["FINNHUB_API_KEY"]



if not ANTHROPIC_API_KEY.strip():

    raise RuntimeError("ANTHROPIC_API_KEY is empty — check your .env file")



finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)



# ── Sprint 1C: Paper mode infrastructure ──────────────────────────

# Sprint C pre-step: canonical home is now agt_equities/config.py

from agt_equities.config import (  # noqa: E402

    PAPER_MODE, HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD, ACTIVE_ACCOUNTS,

    MARGIN_ACCOUNTS, EXCLUDED_TICKERS,

)



IB_HOST         = "127.0.0.1"

IB_TWS_PORT     = 4002 if PAPER_MODE else 4001    # Paper sim / IB Gateway

IB_TWS_FALLBACK = 7497 if PAPER_MODE else 7496    # Paper sim / TWS direct

IB_CLIENT_ID    = 1



# ── Master Log Refactor v3: reader cutover flag ──

# Set False to rollback all reads to legacy tables.

READ_FROM_MASTER_LOG = True

_MASTER_LOG_CUTOVER_NOTIFIED = False  # one-time dashboard notification



CLAUDE_MODEL_HAIKU   = "claude-haiku-4-5-20251001"

CLAUDE_MODEL_SONNET  = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

CLAUDE_MODEL_OPUS    = "claude-opus-4-6"



# Beta Impl 3: Poller dedup set for ATTESTED row keyboard dispatch (R6).

# Process-local — on restart, all ATTESTED rows re-deliver (idempotent via

# TRANSMITTING atomic lock in handle_dex_callback).

_dispatched_audits: set[str] = set()



# Sprint 1A/1D: /halt killswitch

_HALTED: bool = False



# Sprint 1D: Trust tier for DEX cooldown (T0=10s, T1=5s, T2=0s)

TRUST_TIER = os.environ.get("AGT_TRUST_TIER", "T0")



def _get_cooldown_seconds() -> int:

    return {"T0": 10, "T1": 5, "T2": 0}.get(TRUST_TIER, 10)



# Sprint 1D: cooldown active tracking (audit_id → asyncio.Task)

_cooldown_tasks: dict[str, "asyncio.Task"] = {}



MAX_HISTORY          = 50

MAX_ROUNDS           = 15

MAX_TOKENS_PER_REPLY = 8192

DAILY_TOKEN_BUDGET   = int(os.environ.get("DAILY_TOKEN_BUDGET", "250000"))



# Anthropic API pricing (per million tokens)

MODEL_PRICING = {

    "claude-haiku-4-5-20251001": {"input":  1.00, "output":  5.00},

    "claude-sonnet-4-6":         {"input":  3.00, "output": 15.00},

    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},

}



# ---------------------------------------------------------------------------

# CIO Payload Generator — "Project & Paste" pipeline

# (local math only; no Anthropic API calls from this module)

# ---------------------------------------------------------------------------



# CIO system prompt removed — see Portfolio_Risk_Rulebook_v7.md

# Dynamic Exit candidates staged via _stage_dynamic_exit_candidate() -> bucket3_dynamic_exit_log



LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger("agt_bridge")

logger.setLevel(logging.INFO)

logger.handlers.clear()

logger.propagate = False



# ── Rotating file handler (5 MB × 5 backups) ─────────────────────────────

_log_formatter = logging.Formatter(LOG_FMT)

_stream_handler = logging.StreamHandler()

_stream_handler.setFormatter(_log_formatter)

_stream_handler.setLevel(logging.INFO)



_file_handler = logging.handlers.RotatingFileHandler(

    BASE_DIR / "telegram_ui.log",

    maxBytes=5 * 1024 * 1024,

    backupCount=3,

    encoding="utf-8",

)

_file_handler.setFormatter(_log_formatter)

_file_handler.setLevel(logging.INFO)



logger.addHandler(_stream_handler)

logger.addHandler(_file_handler)



# ---------------------------------------------------------------------------

# Rulebook — loaded from file, appended to system prompt for Sonnet/Opus only.

# Haiku doesn't need it — it only routes tools.

# ---------------------------------------------------------------------------

_RULEBOOK_PATH = BASE_DIR / "rulebook_llm_condensed.md"

_RULEBOOK_TEXT = ""

try:

    _RULEBOOK_TEXT = _RULEBOOK_PATH.read_text(encoding="utf-8")

    logger.info("Loaded Rulebook from %s (%d chars)", _RULEBOOK_PATH.name, len(_RULEBOOK_TEXT))

except FileNotFoundError:

    logger.warning("Rulebook not found at %s — /think and /deep will lack Rulebook context",

                   _RULEBOOK_PATH)

except Exception as _rb_exc:

    logger.warning("Failed to load Rulebook: %s", _rb_exc)



# ---------------------------------------------------------------------------

# Timezone-aware override expiry helpers (CLEANUP-5)

# ---------------------------------------------------------------------------



# Legacy override rows were written with naive _datetime.now() which uses

# the deployment machine's local timezone (US/Eastern). New rows use UTC.

_LEGACY_OVERRIDE_TZ = _ZoneInfo("America/New_York")





def _parse_override_expiry(raw: str) -> _datetime:

    """Parse an override expiry. Handles legacy naive (assume ET) and

    new UTC-aware ISO formats. Returns a UTC-aware datetime."""

    dt = _datetime.fromisoformat(raw)

    if dt.tzinfo is None:

        dt = dt.replace(tzinfo=_LEGACY_OVERRIDE_TZ)

    return dt.astimezone(_timezone.utc)





def _new_override_expiry(*, days: int = 0, hours: int = 0) -> str:

    """Generate a new override expiry as UTC-aware ISO string."""

    return (_datetime.now(_timezone.utc) + _timedelta(days=days, hours=hours)).isoformat()





# ---------------------------------------------------------------------------

# Followup #17: IBKR timestamp normalization (Issue #287 workaround)

# ---------------------------------------------------------------------------



# MUST match IBKR Gateway timezone setting (verified in PF2).

# Gateway defaults to OS locale. Deployment machine = US/Eastern.

_TWS_TZ = _ZoneInfo("America/New_York")





def _normalize_ibkr_time(naive_dt):

    """Workaround for ib_async Issue #287: parseIBDatetime returns naive

    datetime for timezone-less IBKR strings. Treats naive datetimes as

    TWS-configured timezone, returns UTC-aware.



    Without this, calling .astimezone(timezone.utc) on a naive datetime

    in Python uses OS-local timezone, NOT TWS configured timezone,

    silently corrupting fill timestamps by the OS-vs-TWS delta.



    Verified: IBDefaults.timezone has "no impact on orders or data

    processing" so it does NOT solve this bug.

    """

    if naive_dt is None:

        return None

    if naive_dt.tzinfo is not None:

        return naive_dt.astimezone(_timezone.utc)

    return naive_dt.replace(tzinfo=_TWS_TZ).astimezone(_timezone.utc)





def _parse_sqlite_utc(ts: str) -> _datetime:

    """Parse SQLite CURRENT_TIMESTAMP variants into UTC-aware datetime.



    Handles:

      "2026-04-08 13:27:22"       (standard SQLite)

      "2026-04-08 13:27:22.258"   (fractional seconds)

      "2026-04-08T13:27:22Z"      (legacy ISO with Z)

    """

    s = ts.replace(" ", "T").rstrip("Z")

    dt = _datetime.fromisoformat(s)

    return dt.replace(tzinfo=_timezone.utc) if dt.tzinfo is None else dt.astimezone(_timezone.utc)





# ---------------------------------------------------------------------------

# SQLite bridge helpers

# ---------------------------------------------------------------------------



# Sprint A Phase C1: shared connection module from agt_equities/db.py.

# The local name _get_db_connection is preserved as an alias to keep

# all 75 existing call sites in this file unchanged. New code should

# import get_db_connection directly from agt_equities.db.

from agt_equities.db import (

    get_db_connection as _get_db_connection,

    tx_immediate,

    init_pragmas,

)



# Decoupling Sprint A Unit A5e — atomic cutover flag. When

# USE_SCHEDULER_DAEMON=1, the standalone agt_scheduler daemon owns the

# jobs gated below (heartbeat_writer and orphan_sweep already scheduler-

# only; attested_sweeper and el_snapshot_writer have scheduler-side

# counterparts via A5a / A5d.d). Default remains off for the 4-week

# cutover window per DT Q1a-g. cross_daemon_alerts_drain is bot-owned

# under both flag states.

from agt_scheduler import use_scheduler_daemon as _use_scheduler_daemon





# ---------------------------------------------------------------------------

# Phase 3A: Mode engine helpers for Telegram commands

# ---------------------------------------------------------------------------



def _get_current_desk_mode() -> str:

    """Read current desk mode from mode_history. Returns 'PEACETIME' on any error."""

    try:

        from agt_equities.mode_engine import get_current_mode

        with closing(_get_db_connection()) as conn:

            return get_current_mode(conn)

    except Exception:

        return "PEACETIME"





def _check_mode_gate(required_mode_max: str) -> tuple[bool, str]:

    """Check if current mode allows the operation.



    required_mode_max: the highest mode in which the operation is allowed.

    'PEACETIME' = only allowed in peacetime

    'AMBER'     = allowed in peacetime and amber (blocked in wartime)

    'WARTIME'   = always allowed



    Returns (allowed: bool, message: str).

    """

    mode = _get_current_desk_mode()

    mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}

    allowed_rank = mode_rank.get(required_mode_max, 2)

    current_rank = mode_rank.get(mode, 0)



    if current_rank > allowed_rank:

        return False, (

            f"\u26d4 Mode {mode}: this command is blocked.\n"

            f"Current desk mode is {mode}. "

            f"Use /cure to view the Cure Console for next steps."

        )

    return True, ""





async def _push_mode_transition(app, old_mode: str, new_mode: str,

                                  trigger: str = "", notes: str = "") -> None:

    """Push mode transition alert to Telegram."""

    try:

        emoji = {"PEACETIME": "\u2705", "AMBER": "\u26a0\ufe0f", "WARTIME": "\U0001f6a8"}.get(new_mode, "\u2753")

        text = (

            f"{emoji} DESK MODE: {old_mode} \u2192 {new_mode}\n"

            f"{trigger}\n{notes}".strip()

        )

        await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)

    except Exception as exc:

        logger.error("Mode transition push failed: %s", exc)





def init_db() -> None:

    with closing(_get_db_connection()) as conn:

        # Sprint A Phase C1: PRAGMAs must run outside any transaction.

        # PRAGMA journal_mode=WAL specifically requires no active

        # transaction to persist the mode change. init_pragmas()

        # handles journal_mode, synchronous, wal_autocheckpoint.

        # busy_timeout is set per-connection by get_db_connection().

        init_pragmas(conn)



        # DDL registration. CREATE TABLE IF NOT EXISTS auto-commits

        # in SQLite when run outside an explicit transaction.

        # Cleanup Sprint A Purge 5: operational DDL moved to schema.py

        from agt_equities.schema import register_operational_tables

        register_operational_tables(conn)

        # ── Master Log Refactor v3: Bucket 2 + Bucket 3 new tables ──

        from agt_equities.schema import register_master_log_tables

        register_master_log_tables(conn)

        # ── Autonomous paper-trading state tables ──

        from agt_equities.schema import _register_autonomous_tables

        _register_autonomous_tables(conn)



    _cleanup_test_orders()

    _load_todays_usage()





def _cleanup_test_orders():

    """Mark all stale staged orders as superseded on boot."""

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                result = conn.execute(

                    """

                    UPDATE pending_orders

                    SET status = 'superseded'

                    WHERE status = 'staged'

                    """

                )

                if result.rowcount > 0:

                    logger.info("Cleaned %d stale staged orders", result.rowcount)

    except Exception as exc:

        logger.warning("_cleanup_test_orders failed: %s", exc)





def _load_todays_usage():

    """Load today's token count from SQLite into memory."""

    global _budget_date, _tokens_used_today

    try:

        today = str(_date.today())

        with closing(_get_db_connection()) as conn:

            row = conn.execute(

                "SELECT input_tokens + output_tokens as total "

                "FROM api_usage WHERE date = ?",

                (today,),

            ).fetchone()

            _budget_date = today

            _tokens_used_today = int(row["total"]) if row else 0

    except Exception:

        _budget_date = str(_date.today())

        _tokens_used_today = 0





def append_pending_tickets(tickets: list[dict]) -> None:

    if not tickets:

        return



    rows = []

    now = _datetime.now().isoformat()

    for ticket in tickets:

        payload = dict(ticket)

        payload.setdefault("status", "pending")

        created_at = str(payload.get("created_at") or payload.get("timestamp") or now)

        rows.append((

            json.dumps(payload, default=str),

            str(payload.get("status", "pending")),

            created_at,

        ))



    with closing(_get_db_connection()) as conn:

        with tx_immediate(conn):

            conn.executemany(

                """

                INSERT INTO pending_orders (payload, status, created_at)

                VALUES (?, ?, ?)

                """,

                rows,

            )





def _revert_pending_order_claims(order_ids: list[int]) -> int:

    """Revert only the specific rows this approval flow claimed."""

    if not order_ids:

        return 0



    placeholders = ",".join("?" for _ in order_ids)

    with closing(_get_db_connection()) as conn:

        with tx_immediate(conn):

            result = conn.execute(

                f"""

                UPDATE pending_orders

                SET status = 'staged'

                WHERE id IN ({placeholders}) AND status = 'processing'

                """,

                order_ids,

            )

            return result.rowcount





def _log_cc_cycle(entries: list[dict]) -> None:

    """Log CC cycle results to cc_cycle_log — both staged and skipped."""

    if not entries:

        return

    try:

        rows = []

        for e in entries:

            rows.append((

                e.get("ticker"),

                e.get("household"),

                e.get("mode", ""),

                e.get("strike"),

                e.get("expiry"),

                e.get("bid"),

                e.get("annualized"),

                e.get("otm_pct"),

                e.get("dte"),

                e.get("walk_away_pnl"),

                e.get("spot_price") or e.get("spot"),

                e.get("adjusted_basis"),

                e.get("flag", "NORMAL"),

            ))

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                conn.executemany(

                    """

                    INSERT INTO cc_cycle_log

                        (ticker, household, mode, strike, expiry, bid,

                         annualized, otm_pct, dte, walk_away_pnl,

                         spot, adjusted_basis, flag)

                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                    """,

                    rows,

                )

    except Exception as exc:

        logger.warning("_log_cc_cycle failed: %s", exc)





# A4 (Decoupling Sprint A): init_db() moved from module scope into main()

# so importing telegram_bot does not touch the on-disk DB. Daemon callers

# unaffected — main() invokes init_db() before any handler runs.



# Sprint 1C: loud paper mode startup log

if PAPER_MODE:

    logger.warning("=" * 60)

    logger.warning("PAPER MODE ACTIVE — port %d — all orders simulated", IB_TWS_PORT)

    logger.warning("=" * 60)

else:

    logger.info("LIVE MODE — primary port %d, fallback %d", IB_TWS_PORT, IB_TWS_FALLBACK)



# MR !70: paper autopilot. When PAPER_MODE is on, staged orders auto-execute

# without a Telegram /approve gate — paper's job is to exercise bot → IBKR

# end-to-end. Live stays fully gated. Kill-switch: PAPER_AUTO_EXECUTE=0.

PAPER_AUTO_EXECUTE = PAPER_MODE and os.environ.get("PAPER_AUTO_EXECUTE", "1") != "0"

if PAPER_MODE:

    logger.warning(

        "PAPER_AUTO_EXECUTE=%s — paper orders %s without /approve",

        PAPER_AUTO_EXECUTE,

        "will auto-submit" if PAPER_AUTO_EXECUTE else "require manual /approve",

    )





# ---------------------------------------------------------------------------

# Conversation history & rate-limiting

# ---------------------------------------------------------------------------

chat_histories: dict[int, list[dict]] = defaultdict(list)

stop_flags:     dict[int, bool]       = defaultdict(bool)



# ── Interactive Dashboard state (inline keyboard views) ──────────────────

# Keyed by chat_id → {msg_id, tool, views, keyboard, created_at}

dashboard_cache: dict[int, dict] = {}

DASHBOARD_TTL = 3600  # seconds before cache entry expires

cc_confirmation_cache: dict[str, dict] = {}



_budget_date:      str = ""

_tokens_used_today: int = 0



# ── Hunter daily screening cache ──────────────────────────────────────────

# Stores {"date": "YYYY-MM-DD", "technical_pass": [...], "excluded": [...]}

# Reused within the same calendar day to avoid re-pinging market data APIs.

STOP_WORDS = {"stop", "cancel", "halt", "abort", "enough",

              "nevermind", "never mind", "quit", "exit"}





def _check_and_track_tokens(input_tokens: int, output_tokens: int, model: str = "") -> bool:

    """Track API usage in SQLite. Returns True if within daily budget."""

    global _budget_date, _tokens_used_today

    today = str(_date.today())

    total = input_tokens + output_tokens



    if _budget_date != today:

        _budget_date = today

        _tokens_used_today = 0

    _tokens_used_today += total



    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                conn.execute(

                    """

                    INSERT INTO api_usage (date, input_tokens, output_tokens, api_calls)

                    VALUES (?, ?, ?, 1)

                    ON CONFLICT(date) DO UPDATE SET

                        input_tokens = input_tokens + excluded.input_tokens,

                        output_tokens = output_tokens + excluded.output_tokens,

                        api_calls = api_calls + 1

                    """,

                    (today, input_tokens, output_tokens),

                )

                if model and total > 0:

                    conn.execute(

                        """

                        INSERT INTO api_usage_by_model (date, model, input_tokens, output_tokens, api_calls)

                        VALUES (?, ?, ?, ?, 1)

                        ON CONFLICT(date, model) DO UPDATE SET

                            input_tokens = input_tokens + excluded.input_tokens,

                            output_tokens = output_tokens + excluded.output_tokens,

                            api_calls = api_calls + 1

                        """,

                        (today, model, input_tokens, output_tokens),

                    )

    except Exception as exc:

        logger.warning("Failed to persist api_usage: %s", exc)



    return _tokens_used_today <= DAILY_TOKEN_BUDGET





def add_to_history(chat_id: int, role: str, content: str):

    chat_histories[chat_id].append({"role": role, "content": content})

    if len(chat_histories[chat_id]) > MAX_HISTORY:

        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]





def clear_history(chat_id: int):

    chat_histories[chat_id].clear()





# ---------------------------------------------------------------------------

# Anthropic async client — Tool-calling dispatcher

# ---------------------------------------------------------------------------

claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)



SYSTEM_PROMPT = """\

You are a DISPATCHER for AGT Equities, a registered investment advisor.

Do NOT write code. Do NOT fabricate data.



RESPONSE STYLE — BLUF (Bottom Line Up Front):

  Always answer the user's specific question in the VERY FIRST sentence,

  heavily **bolded**. Put tables, math, and supporting data BELOW the

  main takeaway. The user reads on a mobile phone — lead with the answer.



WHEN THE USER ASKS ABOUT THEIR PORTFOLIO, HOLDINGS, MARGIN, OR RETURNS:

  Trigger get_portfolio_snapshot to receive the full JSON state of all

  accounts, then do the math natively to answer their question. This tool

  returns NLV, Excess Liquidity, GrossPositionValue, and every active

  position with Symbol, Quantity, and Average Cost.



  If the portfolio snapshot returns an NLV of $0 for any account,

  calculate the Estimated NLV yourself by summing the market values of

  all long positions and cash, minus short positions for that account.



FOR SPECIFIC PRICES, VIX, OR INDEX LEVELS:

  Use get_market_quote with the appropriate ticker symbol.

  Standard Yahoo Finance tickers: AAPL, ^VIX, ^GSPC, ^DJI, SPY, QQQ.



FOR NEWS:

  Use get_top_news with the ticker symbol.



COVERED CALL LADDER:

  If the user asks for a covered call ladder, covered call chain, or

  covered call strikes for shares they already own, call

  run_cc_ladder(household_id, ticker). Use Yash_Household for the

  Yash multi-account structure and Vikram_Household for Vikram. This tool renders its own

  interactive Telegram ladder with strike buttons, pagination, and

  expiry switching. Do NOT manually narrate the full chain if the tool

  already rendered the dashboard.



  CRITICAL: Clicking a strike in the ladder is NOT trade approval.

  The ladder itself sends a separate confirm/cancel message. Only after

  the user presses CONFIRM should a covered call ticket be staged into

  the AGT desk database with transmit=False.





STAGING CONFIRMED TRADES:

  Trade execution is handled by deterministic commands (/approve,

  /approve), not by conversational tools. Do NOT attempt to stage

  trades directly.



  For structured options orders that are already written in execution

  block format, call parse_and_stage_order immediately so the tickets

  are written to the AGT desk database with transmit=False.



  For structured options orders, use the Morning Screener format:

    ACCOUNT: [name]

    TICKER: [ticker]

    STRATEGY: [strategy]

    LEG 1: [STO/BTO] [qty]x [YYYY-MM-DD] [strike][C/P]



RECONNAISSANCE & REPRICING (Live Order Management):

  You have two tools for monitoring and modifying LIVE working orders

  directly on the exchange. The source of truth is Interactive Brokers.



  get_working_orders — Call this when the user says "check pending trades",

    "status update", "what's working?", or similar. It queries IB for all

    live Submitted/PreSubmitted/PendingSubmit orders and returns dual

    indicative mid-prices (all 15-min delayed data):

      • natural_mid — calculated from each individual option leg's delayed

        bid/ask mid-prices, combined by ratio and action.

      • market_mid  — the exchange-quoted combo-level bid/ask mid.

    ⚠️  This tool renders its own Interactive Dashboard in Telegram with

    inline buttons — you do NOT need to format or narrate the results.

    Just acknowledge briefly, e.g., "Here's your working orders dashboard."

    The dashboard provides three action buttons:

      🔄 Refresh Prices — re-runs the delayed snapshot in-place.

      ⚖️ Match Natural Mid — reprices ALL eligible orders to their

         natural mid and transmits the modifications to the exchange.

      ❌ Cancel All Working — cancels every working order on the exchange.

    The user can interact with these buttons directly without sending

    another chat message.



  update_live_order — Call this when the user says "update AAPL to $3.40",

    "reprice MSFT to 2.00", or "adjust the limit on [ticker] to [price]".

    This modifies the live order on the exchange (transmit=True). It finds

    the matching order by ticker + account_id, updates the limit price,

    and submits the modification immediately. Confirm the old → new price

    in your response.

    ⚠️  This is a LIVE exchange modification — always confirm the ticker,

    account, and new price with the user before calling the tool.

    If the user says "update AAPL to $3.40" without specifying an account

    and AAPL has working orders in multiple accounts, ask which account

    they mean. If there is only one working order for that ticker, proceed.



ACCOUNTS:

  U21971297 = Individual  |  U22076329 = Roth IRA  |  U22388499 = Vikram

  U22076184 dormant — history-only, not in active routing

  Aliases: 'Vikram', 'Brother', 'Brother's Brokerage' all map to U22388499.



RISK ASSESSMENT — SPX BOX SPREADS:

  If you see 4 SPX option legs with matching strikes (e.g., forming a

  synthetic long and synthetic short), recognize this as an SPX Box

  Spread used for artificial margin financing. Do NOT call it an Iron

  Condor. Box spreads carry zero directional price risk. Do not flag

  them as dangerous, and do not calculate them as potential max losses

  in risk assessments.



IB DATA CONVENTIONS:

  Option avgCost is already per-contract total (×100 included).

  To get per-share premium: avgCost / 100.

  DTE: compute from expiry date minus today's date.

  SHOW YOUR MATH before giving any financial result.



OUTPUT RULES:

  Format: HTML (Telegram parse_mode="HTML").

  Use ONLY these tags: <b>, <i>, <code>, <pre>, <blockquote expandable>.

  NEVER use markdown (no **, ##, ~~~, or | table | pipes).

  NEVER use raw < or > in text — they break HTML parsing.

  Use ≤ ≥ for comparisons (not <= >=). Use &amp; if you need a literal ampersand.



  Structure every response as:

  1. BLUF — first line, wrapped in <b>. Answer the question. Period.

  2. DATA — if numbers are needed, use a <pre> block with aligned columns.

  3. DETAIL — if supporting math or methodology is needed, put it in

     <blockquote expandable>...</blockquote> so the user can tap to expand.

  4. ACTION — if the user asked for a recommendation, one sentence at the end.



  Constraints:

  - Max 1500 characters for tool results. Max 2500 for /think and /deep.

  - No section headers. No ### or ##. Use <b>Topic:</b> inline if needed.

  - No "What's Working" sections. No "Summary" sections. No filler.

  - No "Recommended Actions" unless the user explicitly asked for recommendations.

  - One line per position in any portfolio display. Never a full table.

  - Use <code>TICKER</code> for inline ticker references.

  - Keep <pre> blocks short (≤10 lines). Longer data goes in expandable.



  Position format (always use this exact layout in <pre> blocks):

  MSFT  300sh  $481→$373  -22%  ❌ no CC

  ADBE  700sh  $329→$243  -26%  ✅ 4x 260C



  Money: $X,XXX.XX  |  Percent: X.XX%

  SHOW YOUR MATH in an expandable blockquote, never in the main response.

"""



# ---------------------------------------------------------------------------

# IB connection

# ---------------------------------------------------------------------------

ib: ib_async.IB | None = None

_ib_connect_lock = asyncio.Lock()

_reconnect_lock = asyncio.Lock()

_reconnect_task: asyncio.Task | None = None

_shutdown_started = False





def _paper_prefix(text: str) -> str:

    """Prepend [PAPER] to outbound Telegram text when PAPER_MODE active."""

    if PAPER_MODE and not text.startswith("[PAPER]"):

        return f"[PAPER] {text}"

    return text





def _mode_prefix(text: str) -> str:

    """Prepend mode badge to outbound Telegram text when WARTIME/AMBER."""

    try:

        with closing(_get_db_connection()) as conn:

            from agt_equities.mode_engine import get_current_mode

            mode = get_current_mode(conn)

    except Exception:

        return text

    if mode == "WARTIME" and "[WARTIME]" not in text:

        return f"[\U0001f6a8 WARTIME] {text}"

    if mode == "AMBER" and "[AMBER]" not in text:

        return f"[\u26a0\ufe0f AMBER] {text}"

    return text





def _format_outbound(text: str) -> str:

    """Apply all outbound Telegram text formatting: paper prefix + mode prefix."""

    return _paper_prefix(_mode_prefix(text))





class AGTFormattedBot(ExtBot):

    """ExtBot subclass that applies _format_outbound to all outbound text.



    Replaces the monkey-patch approach (Sprint 1C/1D) which broke on

    PTB 22.7 due to TelegramObject._frozen attribute lockdown.

    Coverage: send_message + edit_message_text. reply_text gap remains

    (Followup #14).

    """



    async def send_message(self, chat_id, text, *args, **kwargs):

        text = _format_outbound(text)

        return await super().send_message(chat_id, text, *args, **kwargs)



    async def edit_message_text(self, text, *args, **kwargs):

        text = _format_outbound(text)

        return await super().edit_message_text(text, *args, **kwargs)





async def _alert_telegram(text: str) -> None:

    """Send a one-shot Telegram alert to the operator.



    Used by infrastructure handlers (_auto_reconnect, errorEvent, shutdown)

    that run outside PTB's Application context and therefore cannot use

    context.bot.  Callers must wrap in try/except if alert failure should

    not abort the caller.

    """

    text = _format_outbound(text)

    from telegram import Bot

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    await bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)





async def _graceful_shutdown(application) -> None:

    """PTB post_shutdown callback. Fires after Application.stop() completes.



    Releases IBKR connection and cancels any pending reconnect task.



    NOTE: This does NOT fire on Windows X-button close. Operator must use

    Ctrl+C only. See PRE_PAPER_CHECKLIST.md.



    Guarded against reentry (F23-patch-1): PTB can invoke post_shutdown

    multiple times during cascading teardown. The flag prevents 7x runs.

    """

    global ib, _reconnect_task, _shutdown_started



    if _shutdown_started:

        logger.debug("post_shutdown: already running, skipping reentry")

        return

    _shutdown_started = True



    logger.info("post_shutdown: graceful shutdown initiated")



    # 0. Detach BOTH event handlers BEFORE disconnect to prevent cascade.

    #    ib.disconnect() tears down the socket → ib_async surfaces 1100/1101/1102

    #    → our handlers fire → asyncio.create_task against dying loop. (F23-patch-1)

    if ib is not None:

        try:

            ib.disconnectedEvent -= _schedule_reconnect

        except Exception:

            pass

        try:

            ib.errorEvent -= _on_ib_error

        except Exception:

            pass



    # 1. Cancel auto-reconnect task if pending

    if _reconnect_task is not None and not _reconnect_task.done():

        try:

            _reconnect_task.cancel()

            try:

                await asyncio.wait_for(_reconnect_task, timeout=2.0)

            except (asyncio.CancelledError, asyncio.TimeoutError):

                pass

            logger.info("post_shutdown: _reconnect_task cancelled")

        except Exception as exc:

            logger.warning("post_shutdown: _reconnect_task cancel failed: %s", exc)

    _reconnect_task = None



    # 2. Disconnect IBKR cleanly

    if ib is not None:

        try:

            if ib.isConnected():

                ib.disconnect()  # synchronous socket close (ib_async 2.1.0 has no disconnectAsync)

                logger.info("post_shutdown: IBKR disconnected cleanly")

            else:

                logger.info("post_shutdown: IBKR already disconnected")

        except Exception as exc:

            logger.error("post_shutdown: IBKR disconnect failed: %s", exc)

    ib = None



    # 3. SQLite — all connections are per-call via _get_db_connection() with

    #    closing() context manager. No persistent connection to close.



    logger.info("post_shutdown: complete — all resources released")





# ---------------------------------------------------------------------------

# IBKR errorEvent handler — 1100/1101/1102 connectivity differentiation

# ---------------------------------------------------------------------------





def _on_ib_error(reqId: int, errorCode: int, errorString: str, contract) -> None:

    """IBKR error event handler. Differentiates 1100/1101/1102 connectivity events.



    1100 = Connectivity lost (handled by disconnectedEvent -> _schedule_reconnect)

    1101 = Connectivity restored, DATA LOST -> re-fetch open orders + executions,

           re-run orphan scan to reconcile bucket3_dynamic_exit_log

    1102 = Connectivity restored, DATA MAINTAINED -> log + alert only, no action



    Other error codes are logged at DEBUG and ignored here (other handlers

    elsewhere in the codebase deal with order-specific errors).



    Threading: errorEvent dispatches on the asyncio main loop via Connection

    (asyncio.Protocol). Verified against ib_async 2.1.0 source — see

    reports/f23_shutdown_survey_addendum_20260408.md

    """

    if errorCode == 1100:

        logger.warning("IBKR 1100: connectivity lost — disconnect handler will fire")

        return



    if errorCode == 1102:

        logger.info("IBKR 1102: connectivity restored, data maintained")

        # Safe: errorEvent dispatches on the asyncio main loop via Connection

        # (asyncio.Protocol). Verified against ib_async 2.1.0 source — see

        # reports/f23_shutdown_survey_addendum_20260408.md

        asyncio.create_task(_alert_1102())

        return



    if errorCode == 1101:

        logger.critical("IBKR 1101: connectivity restored, DATA LOST — triggering reconciliation")

        # Safe: errorEvent dispatches on the asyncio main loop via Connection

        # (asyncio.Protocol). Verified against ib_async 2.1.0 source — see

        # reports/f23_shutdown_survey_addendum_20260408.md

        asyncio.create_task(_handle_1101_data_lost())

        return



    # Non-connectivity errors fall through to existing handling elsewhere

    logger.debug("IBKR error %d (reqId=%s): %s", errorCode, reqId, errorString)





async def _alert_1102() -> None:

    """Send 1102 info alert. Separated to keep _on_ib_error synchronous."""

    try:

        await _alert_telegram(

            "\U0001f7e2 IBKR reconnected (1102) — data maintained, no action needed."

        )

    except Exception as exc:

        logger.warning("1102 Telegram alert failed: %s", exc)





async def _handle_1101_data_lost() -> None:

    """Handle IBKR 1101 (data lost). In most cases the disconnect event

    fires alongside 1101 and _auto_reconnect handles reconciliation.

    This handler covers the edge case where 1101 fires without a

    preceding disconnect (ib_async has been observed to do this).

    """

    try:

        await _alert_telegram(

            "\U0001f534 IBKR 1101: data lost. Reconciliation in progress."

        )

    except Exception:

        pass  # Alert failure must not block reconciliation



    global ib

    try:

        still_connected = ib is not None and ib.isConnected()

    except Exception as exc:

        logger.exception("_handle_1101: isConnected() check failed")

        try:

            await _alert_telegram(

                f"\U0001f534 1101 recovery: connection state check failed ({exc}). "

                "MANUAL REVIEW REQUIRED."

            )

        except Exception:

            pass

        return



    if not still_connected:

        # Disconnect path will handle it via _auto_reconnect

        logger.info("_handle_1101: disconnect path active, deferring to _auto_reconnect")

        return



    # Edge case: 1101 fired without disconnect. Force reconciliation here.

    try:

        from ib_async.objects import ExecutionFilter

        await asyncio.wait_for(

            ib.reqAllOpenOrdersAsync(), timeout=30.0

        )

        await asyncio.wait_for(

            ib.reqExecutionsAsync(ExecutionFilter()), timeout=30.0

        )

        from telegram import Bot

        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        await _scan_orphaned_transmitting_rows(ib, bot)

        await _alert_telegram(

            "\u2705 1101 recovery complete. Orphan scan finished."

        )

    except asyncio.TimeoutError:

        logger.warning(

            "_handle_1101_data_lost: reqAllOpenOrdersAsync/reqExecutionsAsync timed out after 30s"

        )

    except Exception as exc:

        logger.exception("_handle_1101_data_lost reconciliation failed")

        try:

            await _alert_telegram(

                f"\U0001f534 1101 recovery FAILED: {exc}. MANUAL REVIEW REQUIRED."

            )

        except Exception:

            pass





async def _do_auto_reconnect():

    logger.warning("IB disconnected — retrying in 60s…")

    await asyncio.sleep(60)

    for attempt in range(1, 6):

        try:

            ib_conn = await ensure_ib_connected()

            logger.info("Auto-reconnected (attempt %d)", attempt)

            # Sprint B Unit 8: verify account summary available post-reconnect

            try:

                summary = await ib_conn.accountSummaryAsync()

                if summary:

                    logger.info("Post-reconnect: accountSummary OK (%d items)", len(summary))

                else:

                    logger.warning("Post-reconnect: accountSummary returned empty — EL snapshots may be stale")

            except Exception as as_exc:

                logger.warning("Post-reconnect: accountSummary check failed: %s", as_exc)

            # Followup #17 Part C.5: orphan scan on autoreconnect

            try:

                await asyncio.wait_for(

                    ib_conn.reqAllOpenOrdersAsync(), timeout=30.0

                )

                from ib_async.objects import ExecutionFilter

                await asyncio.wait_for(

                    ib_conn.reqExecutionsAsync(ExecutionFilter()), timeout=30.0

                )

                from telegram import Bot

                bot = Bot(token=TELEGRAM_BOT_TOKEN)

                await _scan_orphaned_transmitting_rows(ib_conn, bot)

            except asyncio.TimeoutError:

                logger.warning(

                    "_do_auto_reconnect: reqAllOpenOrdersAsync/reqExecutionsAsync timed out after 30s"

                )

            except Exception as scan_exc:

                logger.exception("Autoreconnect orphan scan failed: %s", scan_exc)

            try:

                await _alert_telegram(

                    f"\u2705 IB Gateway reconnected (attempt {attempt}/5)."

                )

            except Exception:

                pass

            return

        except Exception as exc:

            logger.error("Reconnect attempt %d failed: %s", attempt, exc)

            await asyncio.sleep(60)

    logger.error("Gave up reconnecting after 5 attempts — use /reconnect")



    # Alert the user via Telegram

    try:

        await _alert_telegram(

            "\U0001f534 CRITICAL: IB Gateway disconnected.\n"

            "5 reconnect attempts failed.\n\n"

            "The trading desk is OFFLINE.\n"

            "- Scheduled CC staging will NOT fire\n"

            "- Fill events will NOT be processed\n"

            "- Watchdog alerts will NOT trigger\n\n"

            "Action: Check IB Gateway/TWS, then /reconnect"

        )

    except Exception as notify_exc:

        logger.error("Failed to send disconnect alert: %s", notify_exc)





async def _auto_reconnect():

    if _reconnect_lock.locked():

        logger.warning(

            "_auto_reconnect: lock contended — another reconnect is in progress, skipping"

        )

        return

    async with _reconnect_lock:

        await _do_auto_reconnect()


def _schedule_reconnect() -> None:

    global _reconnect_task

    if _reconnect_task is not None and not _reconnect_task.done():

        return

    _reconnect_task = asyncio.create_task(_auto_reconnect())





async def ensure_ib_connected() -> ib_async.IB:

    global ib

    async with _ib_connect_lock:

        if ib is not None and ib.isConnected():

            return ib



        if ib is not None:

            try:

                ib.disconnect()

            except Exception:

                pass

            ib = None



        for port, label in[(IB_TWS_PORT, "Gateway"), (IB_TWS_FALLBACK, "TWS")]:

            candidate = ib_async.IB()

            try:

                logger.info(

                    "Connecting to %s:%s (%s) clientId=%s …",

                    IB_HOST,

                    port,

                    label,

                    IB_CLIENT_ID,

                )

                candidate.disconnectedEvent += _schedule_reconnect

                await candidate.connectAsync(

                    IB_HOST,

                    port,

                    clientId=IB_CLIENT_ID,

                    timeout=10,

                )

                candidate.reqMarketDataType(4)

                await asyncio.sleep(2)

                candidate.reqPositions()

                await asyncio.sleep(1)

                # Register fill event listeners for ledger auto-update

                try:

                    candidate.execDetailsEvent.clear()

                    candidate.execDetailsEvent += _offload_fill_handler(_on_cc_fill)

                    candidate.execDetailsEvent += _offload_fill_handler(_on_csp_premium_fill)

                    candidate.execDetailsEvent += _offload_fill_handler(_on_option_close)

                    candidate.execDetailsEvent += _offload_fill_handler(_on_shares_sold)

                    candidate.execDetailsEvent += _offload_fill_handler(_on_shares_bought)

                    # R5: Order state machine handlers

                    candidate.orderStatusEvent += _r5_on_order_status

                    candidate.execDetailsEvent += _offload_fill_handler(_r5_on_exec_details)

                    candidate.commissionReportEvent += _r5_on_commission_report

                    # Sprint B3: pending_order_children writer.

                    candidate.openOrderEvent += _on_open_order_write_child

                    logger.info("Fill + R5 + B3 order state event listeners registered (9 handlers)")

                except Exception as evt_exc:

                    logger.warning("Failed to register fill events: %s", evt_exc)



                # Hydrate ib_async in-memory trades list so IB resumes pushing

                # orderStatus/openOrder events for pre-existing live orders

                # after a (re)connect. Without this, orders placed before a

                # transient disconnect become "ghost" rows in pending_orders:

                # live+Submitted at IB but invisible to our event handlers

                # (ib_perm_id stays 0, last_ib_status stuck at 'sent').

                try:

                    await asyncio.wait_for(

                        candidate.reqAllOpenOrdersAsync(), timeout=30.0

                    )

                    logger.info(

                        "reqAllOpenOrdersAsync: trades list hydrated on (re)connect"

                    )

                except asyncio.TimeoutError:

                    logger.warning(

                        "ensure_ib_connected: reqAllOpenOrdersAsync timed out after 30s — skipping hydration"

                    )

                except Exception as hydrate_exc:

                    logger.warning(

                        "reqAllOpenOrdersAsync on (re)connect failed: %s",

                        hydrate_exc,

                    )



                # F23: IBKR error event listener for 1100/1101/1102 differentiation

                try:

                    candidate.errorEvent += _on_ib_error

                    logger.info("IBKR errorEvent listener registered")

                except Exception as evt_exc:

                    logger.warning("Failed to register errorEvent: %s", evt_exc)



                ib = candidate

                logger.info("Connected via %s — accounts: %s", label, ib.managedAccounts())

                return ib

            except Exception as exc:

                logger.warning("%s failed (%s) — trying next…", label, exc)

                try:

                    candidate.disconnect()

                except Exception:

                    pass



        raise ConnectionError(

            f"Could not connect to Gateway (port {IB_TWS_PORT}) "

            f"or TWS (port {IB_TWS_FALLBACK})."

        )





# ---------------------------------------------------------------------------

# IBKR Option Chain Helpers (R4 Stage 1 — replaces yfinance for execution paths)

# ---------------------------------------------------------------------------



async def _check_rule_11_leverage(household: str) -> tuple[bool, str]:

    """Check Rule 11 leverage cap. Returns (ok, message).

    ok=True means CSP staging is allowed. ok=False means blocked."""

    try:

        from agt_equities.risk import gross_beta_leverage, LEVERAGE_LIMIT

        from agt_equities import trade_repo

        cycles = trade_repo.get_active_cycles()



        # Get spots and betas

        tickers = list({c.ticker for c in cycles if c.status == 'ACTIVE' and c.shares_held > 0})

        spots = {}

        betas = {}

        # MIGRATED 2026-04-07 Phase 3A.5c1 — replaced direct yfinance call

        # with IBKRPriceVolatilityProvider.get_spot() per Architect decision.

        # Original yfinance call preserved for 1 sprint as reference.

        # Delete in 3A.5c2 if no issues surface.

        # OLD: data = yf.download(tickers, period="1d", ...); betas from yf.Ticker.info

        try:

            from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider

            _price_prov = IBKRPriceVolatilityProvider(ib, market_data_mode="delayed")

            for tk in tickers:

                spot = _price_prov.get_spot(tk)

                if spot is not None:

                    spots[tk] = spot

                betas[tk] = 1.0  # beta=1.0 per existing evaluator convention

        except Exception:

            pass



        # Compute household NLV

        with closing(_get_db_connection()) as conn:

            nav = {}

            for r in conn.execute(

                "SELECT account_id, CAST(total AS REAL) as nav "

                "FROM master_log_nav WHERE report_date = "

                "(SELECT MAX(report_date) FROM master_log_nav)"

            ).fetchall():

                nav[r['account_id']] = r['nav']



        hh_nlv = {}

        for acct, hh in ACCOUNT_TO_HOUSEHOLD.items():

            hh_nlv.setdefault(hh, 0)

            hh_nlv[hh] += nav.get(acct, 0)



        leverage = gross_beta_leverage(cycles, spots, betas, hh_nlv)

        hh_short = household.replace("_Household", "")

        if hh_short in leverage:

            lev, status = leverage[hh_short]

            if status == 'BREACHED':

                return False, (

                    f"Rule 11 BLOCK: {hh_short} at {lev:.2f}x "

                    f"(cap {LEVERAGE_LIMIT:.2f}x). "

                    f"CSP staging halted. Reduce via Mode 1 CC harvest "

                    f"or Dynamic Exit before retrying."

                )

        return True, ""

    except Exception as exc:

        logger.warning("Rule 11 check failed: %s", exc)

        return True, ""  # fail-open: don't block on check failure





async def _ibkr_get_spot(ticker: str) -> float:

    """Get spot price via IBKR. Fail-loudly."""

    from agt_equities.ib_chains import get_spot, IBKRChainError

    ib_conn = await ensure_ib_connected()

    try:

        return await get_spot(ib_conn, ticker)

    except IBKRChainError as exc:

        logger.error("IBKR spot failed for %s: %s [%s]", ticker, exc, exc.error_class)

        raise





async def _ibkr_get_option_bid(

    ticker: str,

    strike: float,

    expiry: str,

    right: str = "C",

) -> float:

    """Fetch live bid for a single option contract via IBKR reqMktData.



    Pattern mirrors ib_chains.get_spot() but for options. No caching —

    each TRANSMIT needs the freshest possible bid.



    Raises RuntimeError if no valid bid available. Guards against IBKR

    sentinel values (-1, NaN, inf) per Gemini triage item #3.

    """

    import math

    ib_conn = await ensure_ib_connected()

    expiry_fmt = expiry.replace("-", "")

    contract = ib_async.Option(

        symbol=ticker,

        lastTradeDateOrContractMonth=expiry_fmt,

        strike=strike,

        right=right,

        exchange="SMART",

    )

    qualified = await ib_conn.qualifyContractsAsync(contract)

    if not qualified:

        raise RuntimeError(f"Could not qualify option {ticker} {strike}{right} {expiry}")



    td = ib_conn.reqMktData(qualified[0], '', False, False)

    await asyncio.sleep(2.0)



    bid = None

    for val in [td.bid, getattr(td, 'delayedBid', None)]:

        if (val is not None

                and not math.isnan(val)

                and val > 0

                and val != float('inf')):

            bid = float(val)

            break



    try:

        ib_conn.cancelMktData(qualified[0])

    except Exception:

        pass



    if bid is None:

        raise RuntimeError(

            f"No valid bid for {ticker} {strike}{right} {expiry} "

            f"(raw bid={td.bid}, ask={td.ask})"

        )

    return bid





async def _ibkr_get_spots_batch(tickers: list[str]) -> dict[str, float]:

    """Batch spot prices via IBKR. Graceful degradation — returns what it can."""

    from agt_equities.ib_chains import get_spots_batch

    ib_conn = await ensure_ib_connected()

    return await get_spots_batch(ib_conn, tickers)





async def _ibkr_get_expirations(ticker: str) -> list[str]:

    """Get option expirations via IBKR. Fail-loudly — never falls through to yfinance.



    Returns list of YYYY-MM-DD strings, sorted. Raises on IBKR failure.

    """

    from agt_equities.ib_chains import get_expirations, IBKRChainError

    ib_conn = await ensure_ib_connected()

    try:

        return await get_expirations(ib_conn, ticker)

    except IBKRChainError as exc:

        logger.error("IBKR chain fetch failed for %s: %s [%s]", ticker, exc, exc.error_class)

        raise





async def _ibkr_get_chain(

    ticker: str, expiry: str, right: str = 'C',

    min_strike: float = 0, max_strike: float = 999999,

) -> list[dict]:

    """Get option chain data via IBKR. Fail-loudly.



    Returns list of {strike, bid, ask, last, volume, openInterest, impliedVol}.

    """

    from agt_equities.ib_chains import get_chain_for_expiry, IBKRChainError

    ib_conn = await ensure_ib_connected()

    try:

        return await get_chain_for_expiry(ib_conn, ticker, expiry, right, min_strike, max_strike)

    except IBKRChainError as exc:

        logger.error("IBKR chain data failed for %s %s: %s [%s]", ticker, expiry, exc, exc.error_class)

        raise





# ---------------------------------------------------------------------------

# PATH 1 — Hardcoded Execution Parser

# ---------------------------------------------------------------------------



# Maps abbreviated actions to IB order action + open intent

_ACTION_MAP = {

    "STO": ("SELL", "open"),    # Sell to Open

    "BTO": ("BUY",  "open"),    # Buy to Open

}



_ACCOUNT_MAP = {

    "individual":      "U21971297",

    "individual 1":    "U21971297",

    "personal margin": "U21971297",

    "roth":            "U22076329",

    "roth ira":        "U22076329",

    "personal roth":   "U22076329",

    "individual 2":    "U22388499",

    "vikram":          "U22388499",

    "vikram ind":      "U22388499",

    "brother":         "U22388499",

    "brother's brokerage": "U22388499",

    "client account":  "U22388499",

    "client":          "U22388499",

    # Also allow raw account IDs

    "u21971297":     "U21971297",

    "u22076329":     "U22076329",

    "u22388499":     "U22388499",

}



# LEG pattern: STO/BTO  Qty x  YYYY-MM-DD  StrikeC/P

_LEG_RE = re.compile(

    r"^(STO|BTO)\s+(\d+)x\s+(\d{4}-\d{2}-\d{2})\s+([\d.]+)([CP])$",

    re.IGNORECASE,

)





def _parse_leg(leg_str: str) -> dict:

    """Parse a single leg string into components. Raises ValueError on bad format."""

    m = _LEG_RE.match(leg_str.strip())

    if not m:

        raise ValueError(

            f"Cannot parse leg: '{leg_str}'\n"

            f"Expected format: STO/BTO <qty>x <YYYY-MM-DD> <strike>C/P\n"

            f"Example: STO 2x 2026-04-17 145P"

        )

    action_abbr, qty_str, expiry_str, strike_str, right = m.groups()

    ib_action, _ = _ACTION_MAP[action_abbr.upper()]

    # Convert expiry YYYY-MM-DD → YYYYMMDD for IB

    expiry_ib = expiry_str.replace("-", "")

    return {

        "abbr":    action_abbr.upper(),

        "action":  ib_action,

        "qty":     int(qty_str),

        "expiry":  expiry_ib,

        "strike":  float(strike_str),

        "right":   right.upper(),

    }





def _parse_screener_payload(text: str) -> dict:

    """

    Parse a Morning Screener payload into structured order data.



    Supports opening trades only:

      - Cash Secured Put  → expects exactly LEG 1 (STO put)

      - Generic opening structures built from STO/BTO legs

    """

    lines = {

        line.split(":", 1)[0].strip().upper(): line.split(":", 1)[1].strip()

        for line in text.strip().splitlines()

        if ":" in line

    }



    # Required fields

    for field in ("ACCOUNT", "TICKER", "STRATEGY", "LEG 1"):

        if field not in lines:

            raise ValueError(f"Missing required field: {field}")



    acct_raw = lines["ACCOUNT"].strip().lower()

    acct_id  = _ACCOUNT_MAP.get(acct_raw)

    if not acct_id:

        raise ValueError(

            f"Unknown account: '{lines['ACCOUNT']}'\n"

            f"Valid names: Individual, Roth IRA, Vikram, Brother, "

            f"or raw IDs U21971297 / U22076329 / U22388499"

        )



    strategy = lines["STRATEGY"].strip()

    strategy_lower = strategy.lower()



    legs =[]

    for n in range(1, 6):

        key = f"LEG {n}"

        if key in lines:

            legs.append(_parse_leg(lines[key]))



    # ── Strategy-specific validation ──────────────────────────────────────

    if strategy_lower == "cash secured put":

        if len(legs) != 1:

            raise ValueError(

                f"Cash Secured Put expects exactly 1 leg, got {len(legs)}"

            )

        if legs[0]["abbr"] != "STO":

            raise ValueError(

                f"Cash Secured Put LEG 1 must be STO (Sell to Open), "

                f"got {legs[0]['abbr']}"

            )

        if legs[0]["right"] != "P":

            raise ValueError(

                f"Cash Secured Put LEG 1 must be a Put, got {legs[0]['right']}"

            )



    elif strategy_lower == "defensive roll":

        raise ValueError(

            "Defensive Roll is disabled under the Wheel mandate. "

            "Short puts and calls must be held to expiration/assignment."

        )



    return {

        "account":  acct_id,

        "ticker":   lines["TICKER"].upper().strip(),

        "strategy": strategy,

        "legs":     legs,

    }





async def parse_and_stage_order(text: str) -> str:

    """

    PATH 1 entry point. Parses the screener payload and writes staged

    option tickets into the AGT desk database for agt_trader.py to route.



    Handles:

      - Cash Secured Put (1-leg STO put from wheel_generator.py)

      - Generic opening multi-leg payloads

    """

    try:

        payload = _parse_screener_payload(text)

    except ValueError as e:

        return f"❌ Parse error:\n{e}"



    account  = payload["account"]

    ticker   = payload["ticker"]

    strategy = payload["strategy"]

    staged   = []

    tickets = []



    for i, leg in enumerate(payload["legs"], 1):

        ticket = {

            "timestamp":     _datetime.now().isoformat(),

            "account_id":    account,

            "account_label": ACCOUNT_LABELS.get(account, account),

            "ticker":        ticker,

            "sec_type":      "OPT",

            "action":        leg["action"],

            "quantity":      int(leg["qty"]),

            "order_type":    "LMT",

            "limit_price":   0.00,

            "expiry":        leg["expiry"],

            "strike":        float(leg["strike"]),

            "right":         leg["right"],

            "status":        "pending",

            "transmit":      False,

            "strategy":      strategy,

        }

        tickets.append(ticket)

        staged.append(

            f"📋 Staged Leg {i}: {leg['abbr']} {leg['qty']}x "

            f"{ticker} {leg['expiry']} {leg['strike']}{leg['right']} "

            f"[{account}] — queued with transmit=False"

        )



    await asyncio.to_thread(append_pending_tickets, tickets)



    # ── Strategy-specific Telegram summary ────────────────────────────────

    strategy_lower = strategy.lower()

    if strategy_lower == "cash secured put":

        header = f"✅ CSP Staged (1-leg): {ticker}"

    else:

        header = f"✅ Order Staged ({len(staged)}-leg): {strategy}"



    lines =[header, f"Strategy: {strategy}", f"Account: {account}", ""]

    lines += staged

    lines +=[

        "",

        "⚠️ Limit prices remain at $0.00 placeholders.",

        "Tickets were written to agt_desk.db with transmit=False for review.",

    ]

    return "\n".join(lines)





# ---------------------------------------------------------------------------

# PATH 2 — Tool-Calling Quant (Claude dispatches to hardcoded tools)

# ---------------------------------------------------------------------------



# ── Tool implementations ────────────────────────────────────────────────────



_LIVE_ACCOUNT_LABELS = {

    "U21971297": "Individual",

    "U22076329": "Roth IRA",

    "U22388499": "Vikram",

}

if PAPER_MODE:

    ACCOUNT_LABELS = {acct: f"Paper-{hh.replace('_Household', '')}"

                      for acct, hh in ACCOUNT_TO_HOUSEHOLD.items()}

else:

    ACCOUNT_LABELS = _LIVE_ACCOUNT_LABELS



# ---------------------------------------------------------------------------

# Margin-eligible accounts — Sprint D: MARGIN_ACCOUNTS imported from config.py

# ACCOUNT_NAMES: display labels for IB subscription routing

# ---------------------------------------------------------------------------

_LIVE_ACCOUNT_NAMES = {

    "U21971297": "Personal Brokerage",

    "U22388499": "Brother Brokerage",

}

if PAPER_MODE:

    ACCOUNT_NAMES = {acct: f"Paper-{hh.replace('_Household', '')}"

                     for acct, hh in ACCOUNT_TO_HOUSEHOLD.items()}

else:

    ACCOUNT_NAMES = _LIVE_ACCOUNT_NAMES



# ---------------------------------------------------------------------------

# Phase 3 constants — Rulebook v6, Rule 7, Rule 1, Rule 3

# ---------------------------------------------------------------------------

# Legacy fallback — used ONLY when ticker_universe table is empty.

# Primary source is ticker_universe.gics_industry_group (GICS via yfinance).

_SECTOR_MAP_FALLBACK: dict[str, str] = {

    "ADBE": "Software - Application",

    "MSFT": "Software - Infrastructure",

    "CRM":  "Software - Application",

    "PYPL": "Software - Infrastructure",

    "UBER": "Software - Application",

    "QCOM": "Semiconductors",

    "OXY":  "Oil & Gas E&P",

    "XOM":  "Oil & Gas Integrated",

    "CVX":  "Oil & Gas Integrated",

    "JPM":  "Banks - Diversified",

    "AXP":  "Credit Services",

    "WMT":  "Discount Stores",

    "COST": "Discount Stores",

    "MCD":  "Restaurants",

    "TGT":  "Discount Stores",

    "UNH":  "Healthcare Plans",

    "JNJ":  "Drug Manufacturers - General",

}




# ── Unified Covered Call Engine (2026-04-15 Yash ruling) ──

# Basis-anchored walker: anchor = paper basis (round UP to nearest chain

# strike). Band = 30%-130% annualized. Step up on >130%, stand down on <30%.

# No defensive sub-basis lane — Yash stages those manually.

CC_MIN_ANN    = 30.0   # floor annualized %

CC_MAX_ANN    = 130.0  # ceiling annualized %

CC_BID_FLOOR  = 0.03   # skip quotes below this mid

CC_TARGET_DTE = (14, 30)



# ── Rule 8 Dynamic Exit — V7 Amendments ──

DYNAMIC_EXIT_TARGET_PCT = 0.15    # 15% buffer target (not 20%)

DYNAMIC_EXIT_RULE1_LIMIT = 0.20   # 20% Rule 1 limit (unchanged)

CC_AUTO_STAGE_ENABLED = False  # Set to True after Monday validation



CONVICTION_TIERS = {

    "HIGH":    0.20,

    "NEUTRAL": 0.30,

    "LOW":     0.40,

}






ET = pytz.timezone("US/Eastern")



_CHAIN_TIMEOUT_SECONDS = 15

_YF_SEMAPHORE = asyncio.Semaphore(4)  # Max 4 concurrent yfinance calls





async def _walk_chain_limited(func, *args):

    """Walk an options chain with IBKR rate limiting."""

    async with _YF_SEMAPHORE:

        # func is now async (migrated from sync yfinance to async ib_async)

        if asyncio.iscoroutinefunction(func):

            try:

                return await asyncio.wait_for(func(*args), timeout=_CHAIN_TIMEOUT_SECONDS)

            except asyncio.TimeoutError:

                logger.warning("_walk_chain_limited: %s timed out", func.__name__)

                return None

        else:

            return await _with_timeout_async(func, *args)





async def _with_timeout_async(func, *args, timeout=_CHAIN_TIMEOUT_SECONDS):

    """Run a blocking function in a thread with timeout."""

    try:

        return await asyncio.wait_for(

            asyncio.to_thread(func, *args),

            timeout=timeout,

        )

    except asyncio.TimeoutError:

        logger.warning(

            "_with_timeout_async: %s timed out after %ds",

            getattr(func, "__name__", "unknown"), timeout,

        )

        return None

    except Exception as exc:

        logger.warning(

            "_with_timeout_async: %s raised %s",

            getattr(func, "__name__", "unknown"), exc,

        )

        return None





async def _query_margin_stats() -> dict:

    """

    Query IB for per-account margin stats.  Returns:

        {

            "accounts": {

                "U21971297": {"name": ..., "nlv": ..., "el": ..., "el_pct": ...},

                "U22388499": {"name": ..., "nlv": ..., "el": ..., "el_pct": ...},

            },

            "agg_margin_nlv": float,   # sum across margin accounts

            "agg_margin_el":  float,   # sum across margin accounts

            "agg_el_pct":     float,   # aggregate el / nlv * 100

            "all_book_nlv":   float,   # total NLV across ALL active accounts

            "error":          str|None,

        }

    """

    per_account: dict[str, dict] = {

        acct: {"name": ACCOUNT_NAMES.get(acct, acct), "nlv": 0.0, "el": 0.0}

        for acct in MARGIN_ACCOUNTS

    }

    all_book_nlv = 0.0

    all_account_nlv: dict[str, float] = {}  # ALL active accounts, not just margin

    error = None



    try:

        ib_conn = await ensure_ib_connected()

        summary = await ib_conn.accountSummaryAsync()

        if not summary:

            error = "accountSummary returned empty — IB may need /reconnect"

            logger.warning(error)

        else:

            matched_margin = set()

            _WANTED = {"NetLiquidation", "ExcessLiquidity"}

            for item in summary:

                if item.account not in ACTIVE_ACCOUNTS:

                    continue

                if item.tag not in _WANTED:

                    continue

                val = float(item.value)

                if item.tag == "NetLiquidation":

                    all_book_nlv += val

                    all_account_nlv[item.account] = val

                if item.account in MARGIN_ACCOUNTS:

                    matched_margin.add(item.account)

                    if item.tag == "NetLiquidation":

                        per_account[item.account]["nlv"] += val

                    elif item.tag == "ExcessLiquidity":

                        per_account[item.account]["el"] += val

            if not matched_margin:

                error = (

                    f"No margin accounts found in summary. "

                    f"Expected: {MARGIN_ACCOUNTS}"

                )

                logger.warning(error)

    except Exception as exc:

        error = str(exc)

        logger.warning("_query_margin_stats failed: %s", exc)



    # Compute per-account EL%

    for acct_data in per_account.values():

        nlv = acct_data["nlv"]

        el = acct_data["el"]

        acct_data["el_pct"] = round((el / nlv * 100), 2) if nlv > 0 else 0.0

        acct_data["nlv"] = round(nlv, 2)

        acct_data["el"] = round(el, 2)



    # Aggregates

    agg_nlv = sum(a["nlv"] for a in per_account.values())

    agg_el = sum(a["el"] for a in per_account.values())

    agg_pct = round((agg_el / agg_nlv * 100), 2) if agg_nlv > 0 else 0.0



    return {

        "accounts": per_account,

        "agg_margin_nlv": round(agg_nlv, 2),

        "agg_margin_el": round(agg_el, 2),

        "agg_el_pct": agg_pct,

        "all_book_nlv": round(all_book_nlv, 2),

        "all_account_nlv": all_account_nlv,

        "error": error,

    }





def _household_accounts(household_id: str) -> list[str]:

    accounts = HOUSEHOLD_MAP.get(household_id)

    if not accounts:

        raise ValueError(

            f"Unknown household '{household_id}'. "

            f"Valid: {', '.join(HOUSEHOLD_MAP)}"

        )

    return accounts





def _load_working_call_encumbrance(

    household_id: str,

    ticker: str,

) -> dict[str, int]:

    logger.warning(

        "read from live_blotter — this table may be stale. "

        "Use reqAllOpenOrdersAsync for live order state."

    )

    accounts = _household_accounts(household_id)

    placeholders = ",".join("?" for _ in accounts)

    params = [*accounts, ticker.upper()]



    with closing(_get_db_connection()) as conn:

        rows = conn.execute(

            f"""

            SELECT account_id, COALESCE(SUM(quantity), 0) AS contracts

            FROM live_blotter

            WHERE account_id IN ({placeholders})

              AND ticker = ?

              AND sec_type = 'OPT'

              AND action = 'SELL'

              AND right = 'C'

              AND status IN ('Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending')

            GROUP BY account_id

            """,

            params,

        ).fetchall()



    return {

        str(row["account_id"]): int(row["contracts"]) * 100

        for row in rows

        if row["account_id"]

    }





def _load_premium_ledger_snapshot(

    household_id: str,

    ticker: str,

    account_id: str | None = None,

) -> dict | None:

    """Load cost basis snapshot for (household, ticker), optionally scoped to account.



    ADR-006: when account_id is provided and READ_FROM_MASTER_LOG=True,

    returns per-account adjusted basis via walker. When account_id is

    provided and READ_FROM_MASTER_LOG=False, returns None with an explicit

    log — legacy premium_ledger cannot resolve per-account precision, and

    silently returning household aggregates under Act 60 is a compliance

    defect.



    When account_id is None (legacy callers: /cc digest, overweight scope,

    glide path evaluators), returns household-aggregated basis via the

    existing behavior.

    """

    if READ_FROM_MASTER_LOG:

        try:

            from agt_equities import trade_repo

            cycles = trade_repo.get_active_cycles_with_intraday_delta(

                household=household_id, ticker=ticker.upper()

            )

            if not cycles:

                return None

            c = cycles[0]



            if account_id is not None:

                # ADR-006: per-account resolution

                paper_for_acct = c.paper_basis_for_account(account_id)

                adj_for_acct = c.adjusted_basis_for_account(account_id)

                if paper_for_acct is None:

                    # Account holds no shares in this cycle

                    return None

                _, shares_for_acct = c._paper_basis_by_account.get(account_id, (0.0, 0.0))

                premium_for_acct = c.premium_for_account(account_id)

                return {

                    "household_id": c.household_id,

                    "ticker": c.ticker,

                    "account_id": account_id,

                    "initial_basis": round(paper_for_acct, 2),

                    "total_premium_collected": round(premium_for_acct, 2),

                    "shares_owned": int(shares_for_acct),

                    "adjusted_basis": round(adj_for_acct, 2) if adj_for_acct is not None else None,

                    "basis_truth_level": "WALKER_WITH_INTRADAY_DELTA",

                }



            # Legacy household-aggregated path (account_id is None)

            return {

                "household_id": c.household_id,

                "ticker": c.ticker,

                "initial_basis": round(c.paper_basis, 2) if c.paper_basis is not None else 0.0,

                "total_premium_collected": round(c.premium_total, 2),

                "shares_owned": int(c.shares_held),

                "adjusted_basis": round(c.adjusted_basis, 2) if c.adjusted_basis is not None else None,

                "basis_truth_level": "WALKER_WITH_INTRADAY_DELTA",

            }

        except Exception as exc:

            logger.warning(

                "master_log read failed for %s/%s (account=%s), falling back to legacy: %s",

                household_id, ticker, account_id, exc,

            )



    # Legacy fallback path — ADR-006: per-account requests fail closed here

    if account_id is not None:

        logger.warning(

            "ACB_LEGACY_PER_ACCOUNT_DENIED: household=%s ticker=%s account=%s — "

            "per-account ACB not available from legacy premium_ledger; "

            "READ_FROM_MASTER_LOG must be True for per-account precision",

            household_id, ticker, account_id,

        )

        return None



    # Household-aggregated legacy path (unchanged)

    with closing(_get_db_connection()) as conn:

        row = conn.execute(

            """

            SELECT household_id, ticker, initial_basis, total_premium_collected, shares_owned

            FROM premium_ledger

            WHERE household_id = ? AND ticker = ?

            """,

            (household_id, ticker.upper()),

        ).fetchone()



    if row is None:

        return None



    shares_owned = int(row["shares_owned"] or 0)

    initial_basis = float(row["initial_basis"] or 0.0)

    total_premium_collected = float(row["total_premium_collected"] or 0.0)

    adjusted_basis = (

        initial_basis - (total_premium_collected / shares_owned)

        if shares_owned > 0 else None

    )



    return {

        "household_id": str(row["household_id"]),

        "ticker": str(row["ticker"]),

        "initial_basis": round(initial_basis, 2),

        "total_premium_collected": round(total_premium_collected, 2),

        "shares_owned": shares_owned,

        "adjusted_basis": round(adjusted_basis, 2) if adjusted_basis is not None else None,

        "basis_truth_level": "LEGACY_LEDGER",

    }





# ---------------------------------------------------------------------------

# Fill event handlers — auto-update premium_ledger on every fill

# ---------------------------------------------------------------------------





def _offload_fill_handler(sync_handler):

    """

    Wrap a synchronous fill handler so its DB work runs in a

    thread pool instead of blocking the asyncio event loop.



    Sprint-1.7: any exception raised inside the executor thread is

    captured via Future.add_done_callback and logged with full

    traceback. Without this, a handler exception (e.g.

    sqlite3.OperationalError: database is locked under WAL

    contention) would be trapped on the un-awaited Future and

    silently destroyed at GC, permanently dropping the fill from

    fill_log with zero log signature.

    """

    def _log_future_exception(future):

        try:

            exc = future.exception()

        except Exception as cb_exc:

            logger.warning(

                "Fill handler done-callback failed to read Future: %s",

                cb_exc,

            )

            return

        if exc is not None:

            logger.error(

                "Fill handler %s raised in executor thread: %s",

                sync_handler.__name__, exc,

                exc_info=exc,

            )



    def wrapper(trade, fill):

        try:

            loop = asyncio.get_event_loop()

            if loop.is_running():

                future = loop.run_in_executor(None, sync_handler, trade, fill)

                future.add_done_callback(_log_future_exception)

            else:

                sync_handler(trade, fill)

        except Exception as exc:

            logger.warning("Fill handler offload failed: %s", exc)

    wrapper.__name__ = f"{sync_handler.__name__}_async"

    return wrapper





# ---------------------------------------------------------------------------

# R5: Order state machine event handlers

# ---------------------------------------------------------------------------



def _on_open_order_write_child(trade):

    """openOrderEvent handler -- Sprint B3 writer for pending_order_children.



    IBKR assigns the real orderId/permId asynchronously after placeOrder

    returns. _place_single_order seeds the child row with whatever ids were

    available synchronously; this handler runs once IBKR dispatches the

    openOrder callback and fills in any NULL id columns via COALESCE.



    For FA blocks, IBKR fires openOrder once per allocated child account,

    each with a distinct orderId/permId and the child account on

    trade.order.account. This handler finds the parent pending_order via

    trade.order.orderId (our seeded value on placement) and then updates

    the single child row matching (parent_order_id, trade.order.account).



    Writer-only. Reads untouched. Kill switch: AGT_B3_CHILDREN_WRITER=0.

    Never raises -- any failure logs a warning and returns.

    """

    try:

        from agt_equities.order_state import (

            children_writer_enabled,

            update_child_ib_ids,

        )

        if not children_writer_enabled():

            return

        order = getattr(trade, "order", None)

        if order is None:

            return

        perm_id = int(getattr(order, "permId", 0) or 0)

        order_id = int(getattr(order, "orderId", 0) or 0)

        account = str(getattr(order, "account", "") or "")

        if not account or (not perm_id and not order_id):

            return

        with closing(_get_db_connection()) as conn:

            # Resolve parent pending_order. We match on the ids we stored at

            # placement time (ib_order_id captured synchronously, ib_perm_id

            # usually 0 until this event fires) -- so orderId is the reliable

            # join key here.

            row = None

            if order_id:

                row = conn.execute(

                    "SELECT id FROM pending_orders WHERE ib_order_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (order_id,),

                ).fetchone()

            if row is None and perm_id:

                row = conn.execute(

                    "SELECT id FROM pending_orders WHERE ib_perm_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (perm_id,),

                ).fetchone()

            if row is None:

                return  # orphan openOrder (manual entry or pre-B3 row)

            parent_id = int(row[0])

            with tx_immediate(conn):

                update_child_ib_ids(

                    conn,

                    parent_order_id=parent_id,

                    account_id=account,

                    child_ib_order_id=order_id or None,

                    child_ib_perm_id=perm_id or None,

                )

    except Exception as exc:

        logger.warning("B3 openOrderEvent handler error: %s", exc)





def _r5_on_order_status(trade):

    """orderStatusEvent handler — updates pending_orders status via R5 state machine."""

    try:

        from agt_equities.order_state import append_status, OrderStatus, IBKR_STATUS_MAP

        order = trade.order

        status = trade.orderStatus.status

        perm_id = getattr(order, 'permId', None) or 0

        client_id = getattr(order, 'orderId', None) or 0

        remaining = getattr(trade.orderStatus, 'remaining', 0)



        # Map IBKR status

        new_status = IBKR_STATUS_MAP.get(status)

        if new_status is None:

            logger.debug("R5: unmapped IBKR status %r for order %s", status, perm_id)

            return



        # Handle partial fills

        if new_status == OrderStatus.FILLED and remaining and float(remaining) > 0:

            new_status = OrderStatus.PARTIALLY_FILLED



        with closing(_get_db_connection()) as conn:

            # Match by perm_id first, then client_id

            row = None

            if perm_id:

                row = conn.execute(

                    "SELECT id FROM pending_orders WHERE ib_perm_id = ? "

                    "ORDER BY id DESC LIMIT 1", (perm_id,)

                ).fetchone()

            if row is None and client_id:

                row = conn.execute(

                    "SELECT id FROM pending_orders WHERE ib_order_id = ? "

                    "ORDER BY id DESC LIMIT 1", (client_id,)

                ).fetchone()



            if row is None:

                # Orphan event

                conn.execute(

                    "INSERT INTO orphan_order_events "

                    "(event_type, ib_order_id, ib_perm_id, status, payload) "

                    "VALUES ('orderStatus', ?, ?, ?, ?)",

                    (client_id, perm_id, status,

                     f"filled={trade.orderStatus.filled} remaining={remaining}"),

                )

                # Followup #17: fallback to bucket3_dynamic_exit_log by orderRef

                # Column ownership (D4): write ib_perm_id ONLY. Never final_status.

                order_ref = getattr(order, "orderRef", "") or ""

                if order_ref and perm_id:

                    dex_row = conn.execute(

                        "SELECT audit_id FROM bucket3_dynamic_exit_log "

                        "WHERE audit_id = ? AND final_status IN ('TRANSMITTING', 'TRANSMITTED')",

                        (order_ref,),

                    ).fetchone()

                    if dex_row:

                        with tx_immediate(conn):

                            conn.execute(

                                "UPDATE bucket3_dynamic_exit_log "

                                "SET ib_perm_id = ? WHERE audit_id = ? AND ib_perm_id IS NULL",

                                (perm_id, order_ref),

                            )

                        logger.info(

                            "R5_FALLBACK_DEX action=order_status audit_id=%s ib_perm_id=%s",

                            order_ref[:8], perm_id,

                        )

                return



            append_status(

                conn, row[0], new_status, 'orderStatusEvent',

                {"ib_status": status, "filled": str(trade.orderStatus.filled),

                 "remaining": str(remaining)},

            )



    except Exception as exc:

        logger.warning("R5 orderStatusEvent handler error: %s", exc)





def _r5_on_exec_details(trade, fill):

    """execDetailsEvent handler — marks order FILLED and records fill data."""

    try:

        from agt_equities.order_state import append_status, OrderStatus

        order = trade.order

        perm_id = getattr(order, 'permId', None) or 0

        client_id = getattr(order, 'orderId', None) or 0

        exec_id = getattr(fill, 'execution', None)

        exec_id_str = getattr(exec_id, 'execId', '') if exec_id else ''

        fill_price = getattr(exec_id, 'price', 0) if exec_id else 0

        fill_qty = getattr(exec_id, 'shares', 0) if exec_id else 0

        fill_time = str(getattr(exec_id, 'time', '')) if exec_id else ''



        remaining = getattr(trade.orderStatus, 'remaining', 0)

        new_status = OrderStatus.FILLED if (not remaining or float(remaining) == 0) else OrderStatus.PARTIALLY_FILLED



        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                row = None

                if perm_id:

                    row = conn.execute(

                        "SELECT id FROM pending_orders WHERE ib_perm_id = ? "

                        "ORDER BY id DESC LIMIT 1", (perm_id,)

                    ).fetchone()

                if row is None and client_id:

                    row = conn.execute(

                        "SELECT id FROM pending_orders WHERE ib_order_id = ? "

                        "ORDER BY id DESC LIMIT 1", (client_id,)

                    ).fetchone()



                if row is None:

                    conn.execute(

                        "INSERT INTO orphan_order_events "

                        "(event_type, ib_order_id, ib_perm_id, status, payload) "

                        "VALUES ('execDetails', ?, ?, ?, ?)",

                        (client_id, perm_id, str(new_status),

                         f"exec_id={exec_id_str} price={fill_price} qty={fill_qty}"),

                    )

                    # Followup #17: fallback to bucket3_dynamic_exit_log by orderRef

                    # Column ownership (D4): write fill_price, fill_qty, fill_ts ONLY.

                    order_ref = getattr(order, "orderRef", "") or ""

                    if order_ref and exec_id_str:

                        dex_row = conn.execute(

                            "SELECT audit_id FROM bucket3_dynamic_exit_log "

                            "WHERE audit_id = ? "

                            "  AND final_status IN ('TRANSMITTING', 'TRANSMITTED')",

                            (order_ref,),

                        ).fetchone()

                        if dex_row:

                            exec_obj = getattr(fill, 'execution', None)

                            normalized_time = _normalize_ibkr_time(

                                getattr(exec_obj, 'time', None) if exec_obj else None

                            )

                            fill_ts_epoch = (

                                normalized_time.timestamp()

                                if normalized_time else time.time()

                            )

                            conn.execute(

                                "UPDATE bucket3_dynamic_exit_log "

                                "SET fill_price = ?, fill_qty = ?, fill_ts = ? "

                                "WHERE audit_id = ? AND fill_price IS NULL",

                                (float(fill_price), int(float(fill_qty)),

                                 fill_ts_epoch, order_ref),

                            )

                            logger.info(

                                "R5_FALLBACK_DEX action=exec_details audit_id=%s "

                                "price=%.4f qty=%s",

                                order_ref[:8], float(fill_price), fill_qty,

                            )

                    return



                order_id = row[0]

                append_status(

                    conn, order_id, new_status, 'execDetailsEvent',

                    {"exec_id": exec_id_str, "price": str(fill_price),

                     "qty": str(fill_qty), "time": fill_time},

                )



                # Update fill fields

                conn.execute(

                    "UPDATE pending_orders SET fill_price = ?, fill_qty = ?, fill_time = ? "

                    "WHERE id = ?",

                    (float(fill_price), int(float(fill_qty)), fill_time, order_id),

                )



    except Exception as exc:

        logger.warning("R5 execDetailsEvent handler error: %s", exc)





def _r5_on_commission_report(trade, fill, report):

    """commissionReportEvent handler — records commission after fill."""

    try:

        order = trade.order

        perm_id = getattr(order, 'permId', None) or 0

        client_id = getattr(order, 'orderId', None) or 0

        commission = getattr(report, 'commission', 0) or 0



        with closing(_get_db_connection()) as conn:

            row = None

            if perm_id:

                row = conn.execute(

                    "SELECT id FROM pending_orders WHERE ib_perm_id = ? "

                    "ORDER BY id DESC LIMIT 1", (perm_id,)

                ).fetchone()

            if row is None and client_id:

                row = conn.execute(

                    "SELECT id FROM pending_orders WHERE ib_order_id = ? "

                    "ORDER BY id DESC LIMIT 1", (client_id,)

                ).fetchone()



            if row is None:

                # Followup #17: fallback to bucket3_dynamic_exit_log by orderRef

                # Column ownership (D4): write commission ONLY. Never final_status.

                order_ref = getattr(order, "orderRef", "") or ""

                if order_ref and commission:

                    dex_row = conn.execute(

                        "SELECT audit_id FROM bucket3_dynamic_exit_log "

                        "WHERE audit_id = ? "

                        "  AND commission IS NULL "

                        "  AND final_status IN ('TRANSMITTING', 'TRANSMITTED')",

                        (order_ref,),

                    ).fetchone()

                    if dex_row:

                        with tx_immediate(conn):

                            conn.execute(

                                "UPDATE bucket3_dynamic_exit_log "

                                "SET commission = ? "

                                "WHERE audit_id = ? AND commission IS NULL",

                                (float(commission), order_ref),

                            )

                        logger.info(

                            "R5_FALLBACK_DEX action=commission audit_id=%s "

                            "commission=%.2f",

                            order_ref[:8], float(commission),

                        )

                return



            conn.execute(

                "UPDATE pending_orders SET fill_commission = ? WHERE id = ?",

                (float(commission), row[0]),

            )



    except Exception as exc:

        logger.warning("R5 commissionReportEvent handler error: %s", exc)





def _is_duplicate_fill(exec_id: str) -> bool:

    """Check if this execution ID was already processed."""

    try:

        with closing(_get_db_connection()) as conn:

            row = conn.execute(

                "SELECT 1 FROM fill_log WHERE exec_id = ?",

                (exec_id,),

            ).fetchone()

            return row is not None

    except Exception:

        return False





def _record_fill(exec_id: str, ticker: str, action: str,

                 quantity: float, price: float, premium_delta: float,

                 account_id: str, household_id: str) -> None:

    """Log a processed fill for deduplication."""

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                conn.execute(

                    """

                    INSERT OR IGNORE INTO fill_log

                        (exec_id, ticker, action, quantity, price,

                         premium_delta, account_id, household_id)

                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)

                    """,

                    (exec_id, ticker, action, quantity, price,

                     premium_delta, account_id, household_id),

                )

    except Exception as exc:

        logger.warning("_record_fill failed: %s", exc)





def _apply_fill_atomically(

    exec_id: str,

    ticker: str,

    action: str,

    quantity: float,

    price: float,

    premium_delta: float,

    account_id: str,

    household_id: str,

    inception_delta: float | None = None,

) -> bool:

    """Atomic: dedup via INSERT OR IGNORE + ledger UPSERT. Single transaction."""

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                # Step 1: Attempt dedup insert — if exec_id exists, rowcount = 0

                cur = conn.execute(

                    """

                    INSERT OR IGNORE INTO fill_log

                        (exec_id, ticker, action, quantity, price,

                         premium_delta, account_id, household_id,

                         inception_delta)

                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                    """,

                    (exec_id, ticker, action, quantity, price,

                     premium_delta, account_id, household_id,

                     inception_delta),

                )

                if cur.rowcount == 0:

                    return False  # Duplicate — already processed



                # Step 2: Upsert premium_ledger if there's a delta

                if premium_delta != 0:

                    conn.execute(

                        """

                        INSERT INTO premium_ledger

                            (household_id, ticker, initial_basis,

                             total_premium_collected, shares_owned)

                        VALUES (?, ?, 0.0, ?, 0)

                        ON CONFLICT(household_id, ticker) DO UPDATE SET

                            total_premium_collected =

                                total_premium_collected + excluded.total_premium_collected

                        """,

                        (household_id, ticker, premium_delta),

                    )



                # Step 3: Dashboard — record fill to trade_ledger

                try:

                    conn.execute("""

                        INSERT OR IGNORE INTO trade_ledger

                            (account_id, household_id, trade_date, trade_datetime,

                             symbol, underlying, asset_category, trade_type,

                             quantity, price, proceeds, realized_pnl,

                             return_category, source)

                        VALUES (?, ?, date('now'), datetime('now'),

                                ?, ?, ?, ?, ?, ?, ?, 0, ?, 'LIVE')

                    """, (

                        account_id, household_id,

                        ticker, ticker,

                        'Equity and Index Options' if action in ('SELL_CALL', 'SELL_PUT', 'BUY_CALL', 'BUY_PUT') else 'Stocks',

                        action,

                        quantity, price,

                        round(premium_delta, 2),

                        'PREMIUM' if action in ('SELL_CALL', 'SELL_PUT', 'BUY_CALL', 'BUY_PUT') else 'CAPITAL_GAIN',

                    ))

                except Exception:

                    pass  # Non-critical — don't break the fill handler



                return True

    except Exception as exc:

        logger.warning(

            "_apply_fill_atomically failed: exec_id=%s %s: %s",

            exec_id, ticker, exc,

        )

        return False





# _credit_premium and _is_duplicate_fill kept for backward compatibility

# but new fill handlers should use _apply_fill_atomically instead.





def _credit_premium(household: str, ticker: str, amount: float) -> bool:

    """Add premium to total_premium_collected. Returns True if updated."""

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                existing = conn.execute(

                    "SELECT total_premium_collected FROM premium_ledger "

                    "WHERE household_id = ? AND ticker = ?",

                    (household, ticker),

                ).fetchone()



                if existing:

                    conn.execute(

                        """

                        UPDATE premium_ledger

                        SET total_premium_collected = total_premium_collected + ?

                        WHERE household_id = ? AND ticker = ?

                        """,

                        (amount, household, ticker),

                    )

                    return True

                else:

                    conn.execute(

                        """

                        INSERT INTO premium_ledger

                            (household_id, ticker, initial_basis,

                             total_premium_collected, shares_owned)

                        VALUES (?, ?, 0.0, ?, 0)

                        """,

                        (household, ticker, amount),

                    )

                    return True

    except Exception as exc:

        logger.warning("_credit_premium failed for %s/%s: %s",

                       household, ticker, exc)

        return False





def _lookup_inception_delta(perm_id: int, client_id: int = 0) -> float | None:

    """Sprint B4: FA-block-aware three-stage inception_delta resolver.



    Resolution order:

      Stage 1: pending_order_children.child_ib_perm_id  -> parent_order_id

      Stage 2: pending_order_children.child_ib_order_id -> parent_order_id

      Stage 3: legacy flat path pending_orders.ib_perm_id / ib_order_id



    Parent payload carries ONE inception_delta shared across all children

    (Yash 2026-04-15 lock: parent-wide; per-account basis divergence is

    ADR-006/walker's concern, not child-delta attribution).



    Orphaned child rows (parent_order_id resolves but parent row is missing)

    log ORPHANED_CHILD_ROW and fall through to the legacy flat path.



    Returns None on total miss, malformed payload, or DB error. Never raises.

    """

    if not perm_id and not client_id:

        return None



    try:

        with closing(_get_db_connection()) as conn:

            parent_id: int | None = None



            if perm_id:

                r = conn.execute(

                    "SELECT parent_order_id FROM pending_order_children "

                    "WHERE child_ib_perm_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (perm_id,),

                ).fetchone()

                if r is not None:

                    parent_id = int(r[0])



            if parent_id is None and client_id:

                r = conn.execute(

                    "SELECT parent_order_id FROM pending_order_children "

                    "WHERE child_ib_order_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (client_id,),

                ).fetchone()

                if r is not None:

                    parent_id = int(r[0])



            if parent_id is not None:

                row = conn.execute(

                    "SELECT payload FROM pending_orders WHERE id = ?",

                    (parent_id,),

                ).fetchone()

                if row is None:

                    logger.warning(

                        "ORPHANED_CHILD_ROW: pending_order_children matched "

                        "permId=%s client_id=%s -> parent_order_id=%s but "

                        "pending_orders row is missing; falling through to "

                        "flat path",

                        perm_id, client_id, parent_id,

                    )

                else:

                    payload = json.loads(row[0])

                    value = payload.get("inception_delta")

                    return float(value) if value is not None else None



            row = None

            if perm_id:

                row = conn.execute(

                    "SELECT payload FROM pending_orders "

                    "WHERE ib_perm_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (perm_id,),

                ).fetchone()

            if row is None and client_id:

                row = conn.execute(

                    "SELECT payload FROM pending_orders "

                    "WHERE ib_order_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (client_id,),

                ).fetchone()

            if row is None:

                logger.info(

                    "no pending_orders match for permId=%s client_id=%s "

                    "(manual order, pre-sprint-1.2 row, or pre-staged child)",

                    perm_id, client_id,

                )

                return None



            payload = json.loads(row[0])

            value = payload.get("inception_delta")

            return float(value) if value is not None else None



    except (json.JSONDecodeError, sqlite3.Error) as e:

        logger.warning(

            "pending_orders payload lookup failed for "

            "permId=%s client_id=%s: %s",

            perm_id, client_id, e,

        )

        return None

    except (TypeError, ValueError) as e:

        logger.warning(

            "malformed inception_delta in payload for "

            "permId=%s client_id=%s: %s",

            perm_id, client_id, e,

        )

        return None





def _lookup_inception_delta_legacy(perm_id: int, client_id: int = 0) -> float | None:

    """Pre-B4 flat-path resolver preserved behind USE_FA_BLOCK_CHILDREN_READER=0

    for a 2-sprint deprecation window. Verbatim pre-B4 behavior.

    """

    if not perm_id and not client_id:

        return None



    try:

        with closing(_get_db_connection()) as conn:

            row = None

            if perm_id:

                row = conn.execute(

                    "SELECT payload FROM pending_orders "

                    "WHERE ib_perm_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (perm_id,),

                ).fetchone()

            if row is None and client_id:

                row = conn.execute(

                    "SELECT payload FROM pending_orders "

                    "WHERE ib_order_id = ? "

                    "ORDER BY id DESC LIMIT 1",

                    (client_id,),

                ).fetchone()

            if row is None:

                logger.info(

                    "no pending_orders match for permId=%s client_id=%s "

                    "(manual order or pre-sprint-1.2 row)",

                    perm_id, client_id,

                )

                return None



            payload = json.loads(row[0])

            value = payload.get("inception_delta")

            if value is None:

                return None

            return float(value)



    except (json.JSONDecodeError, sqlite3.Error) as e:

        logger.warning(

            "pending_orders payload lookup failed for "

            "permId=%s: %s",

            perm_id, e,

        )

        return None

    except (TypeError, ValueError) as e:

        logger.warning(

            "malformed inception_delta in payload for "

            "permId=%s: %s",

            perm_id, e,

        )

        return None





def _lookup_inception_delta_from_payload(perm_id: int, client_id: int = 0) -> float | None:

    """Sprint B4 shim. Dispatches between the new FA-block-aware resolver

    and the preserved legacy resolver based on USE_FA_BLOCK_CHILDREN_READER.

    Defaults to the new resolver. Name preserved so existing monkeypatches

    on telegram_bot._lookup_inception_delta_from_payload keep working.

    Strict flag semantics: anything other than "0" selects the new resolver.

    """

    if os.getenv("USE_FA_BLOCK_CHILDREN_READER", "1") == "0":

        return _lookup_inception_delta_legacy(perm_id, client_id)

    return _lookup_inception_delta(perm_id, client_id)





def _enqueue_inception_delta_miss(

    household: str,

    ticker: str,

    perm_id: int,

    client_id: int,

    acct_id: str,

    exec_id: str,

) -> None:

    """Sprint B4: best-effort push INCEPTION_DELTA_MISS onto the

    cross_daemon_alerts bus after resolver+retry budget is exhausted.

    Fire-and-forget; never raises. Severity=info (book-and-log posture;

    WHEEL evaluator handles NULL inception_delta downstream).

    """

    try:

        from agt_equities.alerts import enqueue_alert

        enqueue_alert(

            "INCEPTION_DELTA_MISS",

            {

                "household": household,

                "ticker": ticker,

                "perm_id": perm_id,

                "client_id": client_id,

                "acct_id": acct_id,

                "exec_id": exec_id,

            },

            severity="info",

        )

    except Exception as exc:

        logger.warning(

            "INCEPTION_DELTA_MISS alert enqueue failed (%s/%s acct=%s): %s",

            household, ticker, acct_id, exc,

        )





def _on_cc_fill(trade, fill):

    """SELL CALL filled — credit CC premium to ledger (atomic)."""

    try:

        contract = trade.contract

        order = trade.order

        execution = fill.execution



        if order.action != "SELL":

            return

        if getattr(contract, "secType", "") != "OPT":

            return

        if getattr(contract, "right", "") != "C":

            return



        ticker = contract.symbol.upper()

        if ticker in EXCLUDED_TICKERS:

            return



        acct_id = order.account or execution.acctNumber

        household = ACCOUNT_TO_HOUSEHOLD.get(acct_id)

        if not household:

            return



        fill_price = execution.price

        fill_qty = abs(execution.shares)

        total_premium = round(fill_price * 100 * fill_qty, 2)



        # Sprint-1.3: extract inception_delta from pending_orders payload

        # Sprint-1.6: pass client_id for ib_order_id fallback (permId race)

        # Sprint B B2: bounded retry for FA-block child permId race -- the

        # child execDetailsEvent can fire before the openOrder callback has

        # written ib_perm_id/ib_order_id to pending_orders. Retry budget:

        # 3 attempts x 0.5s = 1.5s total. On exhaustion, fill still books

        # but without inception_delta (logged at WARNING).

        perm_id = getattr(order, 'permId', None) or 0

        client_id = getattr(order, 'orderId', None) or 0

        inception_delta = None

        for _b2_attempt in range(3):

            inception_delta = _lookup_inception_delta_from_payload(perm_id, client_id)

            if inception_delta is not None:

                break

            if _b2_attempt < 2:

                time.sleep(0.5)

        if inception_delta is None:

            logger.warning(

                "inception_delta lookup miss after 3 retries: "

                "permId=%s client_id=%s (fill books without inception_delta)",

                perm_id, client_id,

            )

            # Sprint B4: surface on cross_daemon_alerts bus (book-and-log).

            _enqueue_inception_delta_miss(

                household, ticker, perm_id, client_id,

                acct_id, execution.execId,

            )



        if _apply_fill_atomically(execution.execId, ticker, "SELL_CALL",

                                  fill_qty, fill_price, total_premium,

                                  acct_id, household,

                                  inception_delta=inception_delta):

            logger.info(

                "CC premium: %s %s +$%.2f (%d contracts @ $%.2f) "

                "inception_delta=%s",

                household, ticker, total_premium, fill_qty, fill_price,

                inception_delta,

            )

    except Exception as exc:

        logger.exception("_on_cc_fill failed: %s", exc)





def _on_csp_premium_fill(trade, fill):

    """SELL PUT filled — credit CSP premium to ledger (atomic).



    Sprint B4: retrofitted with B2-style bounded retry on inception_delta

    lookup to close parity with _on_cc_fill. FA-block CSP children hit the

    same child permId race as CC children.

    """

    try:

        contract = trade.contract

        order = trade.order

        execution = fill.execution



        if order.action != "SELL":

            return

        if getattr(contract, "secType", "") != "OPT":

            return

        if getattr(contract, "right", "") != "P":

            return



        ticker = contract.symbol.upper()

        if ticker in EXCLUDED_TICKERS:

            return



        acct_id = order.account or execution.acctNumber

        household = ACCOUNT_TO_HOUSEHOLD.get(acct_id)

        if not household:

            return



        fill_price = execution.price

        fill_qty = abs(execution.shares)

        total_premium = round(fill_price * 100 * fill_qty, 2)



        # Sprint B4: mirror _on_cc_fill B2 retry. 3 x 0.5s = 1.5s budget.

        perm_id = getattr(order, 'permId', None) or 0

        client_id = getattr(order, 'orderId', None) or 0

        inception_delta = None

        for _b4_attempt in range(3):

            inception_delta = _lookup_inception_delta_from_payload(perm_id, client_id)

            if inception_delta is not None:

                break

            if _b4_attempt < 2:

                time.sleep(0.5)

        if inception_delta is None:

            logger.warning(

                "inception_delta lookup miss after 3 retries: "

                "permId=%s client_id=%s (CSP fill books without inception_delta)",

                perm_id, client_id,

            )

            _enqueue_inception_delta_miss(

                household, ticker, perm_id, client_id,

                acct_id, execution.execId,

            )



        if _apply_fill_atomically(execution.execId, ticker, "SELL_PUT",

                                  fill_qty, fill_price, total_premium,

                                  acct_id, household,

                                  inception_delta=inception_delta):

            logger.info(

                "CSP premium: %s %s +$%.2f (%d contracts @ $%.2f) "

                "inception_delta=%s",

                household, ticker, total_premium, fill_qty, fill_price,

                inception_delta,

            )

    except Exception as exc:

        logger.exception("_on_csp_premium_fill failed: %s", exc)





def _on_option_close(trade, fill):

    """

    BUY CALL or BUY PUT filled — debit the cost from ledger (atomic).

    Handles close-and-re-enter, rolling, or early closes.

    """

    try:

        contract = trade.contract

        order = trade.order

        execution = fill.execution



        if order.action != "BUY":

            return

        if getattr(contract, "secType", "") != "OPT":

            return



        right = getattr(contract, "right", "")

        if right not in ("C", "P"):

            return



        ticker = contract.symbol.upper()

        if ticker in EXCLUDED_TICKERS:

            return



        acct_id = order.account or execution.acctNumber

        household = ACCOUNT_TO_HOUSEHOLD.get(acct_id)

        if not household:

            return



        fill_price = execution.price

        fill_qty = abs(execution.shares)

        close_cost = round(fill_price * 100 * fill_qty, 2)

        action_label = "BUY_CALL" if right == "C" else "BUY_PUT"



        if _apply_fill_atomically(execution.execId, ticker, action_label,

                                  fill_qty, fill_price, -close_cost,

                                  acct_id, household):

            logger.info(

                "Option close: %s %s -$%.2f (%s %d @ $%.2f)",

                household, ticker, close_cost,

                action_label, fill_qty, fill_price,

            )

    except Exception as exc:

        logger.exception("_on_option_close failed: %s", exc)





def _on_shares_sold(trade, fill):

    """Stock SELL execution — decrement shares_owned (CC assignment, atomic)."""

    try:

        contract = trade.contract

        execution = fill.execution



        if getattr(contract, "secType", "") != "STK":

            return

        if execution.side != "SLD":

            return



        exec_id = execution.execId

        ticker = contract.symbol.upper()

        if ticker in EXCLUDED_TICKERS:

            return



        acct_id = execution.acctNumber

        household = ACCOUNT_TO_HOUSEHOLD.get(acct_id)

        if not household:

            return



        shares_sold = abs(int(execution.shares))



        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                # Atomic dedup inside transaction

                dup = conn.execute(

                    "SELECT 1 FROM fill_log WHERE exec_id = ?", (exec_id,)

                ).fetchone()

                if dup:

                    return



                existing = conn.execute(

                    "SELECT shares_owned FROM premium_ledger "

                    "WHERE household_id = ? AND ticker = ?",

                    (household, ticker),

                ).fetchone()



                if existing and existing["shares_owned"]:

                    new_shares = max(0, int(existing["shares_owned"]) - shares_sold)

                    conn.execute(

                        """

                        UPDATE premium_ledger

                        SET shares_owned = ?

                        WHERE household_id = ? AND ticker = ?

                        """,

                        (new_shares, household, ticker),

                    )

                    conn.execute(

                        """

                        INSERT OR IGNORE INTO fill_log

                            (exec_id, ticker, action, quantity, price,

                             premium_delta, account_id, household_id)

                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

                        """,

                        (exec_id, ticker, "STK_SELL", shares_sold,

                         execution.price, 0, acct_id, household),

                    )

                    logger.info(

                        "Shares sold: %s %s -%d (remaining: %d)",

                        household, ticker, shares_sold, new_shares,

                    )



                    # Archive and reset ledger when position fully exited

                    if new_shares == 0:

                        conn.execute(

                            """

                            INSERT INTO premium_ledger_history

                                (household_id, ticker, initial_basis,

                                 total_premium_collected, shares_owned)

                            SELECT household_id, ticker, initial_basis,

                                   total_premium_collected, ?

                            FROM premium_ledger

                            WHERE household_id = ? AND ticker = ?

                            """,

                            (shares_sold, household, ticker),

                        )

                        conn.execute(

                            """

                            UPDATE premium_ledger

                            SET initial_basis = 0, total_premium_collected = 0

                            WHERE household_id = ? AND ticker = ?

                            """,

                            (household, ticker),

                        )

                        logger.info(

                            "Position fully exited — archived and reset ledger: %s %s",

                            household, ticker,

                        )



    except Exception as exc:

        logger.exception("_on_shares_sold failed: %s", exc)





def _on_shares_bought(trade, fill):

    """Stock BUY execution — add shares with weighted-average basis (atomic)."""

    try:

        contract = trade.contract

        execution = fill.execution



        if getattr(contract, "secType", "") != "STK":

            return

        if execution.side != "BOT":

            return



        exec_id = execution.execId

        ticker = contract.symbol.upper()

        if ticker in EXCLUDED_TICKERS:

            return



        acct_id = execution.acctNumber

        household = ACCOUNT_TO_HOUSEHOLD.get(acct_id)

        if not household:

            return



        shares_bought = abs(int(execution.shares))

        cost_per_share = execution.price



        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                # Atomic dedup inside transaction

                dup = conn.execute(

                    "SELECT 1 FROM fill_log WHERE exec_id = ?", (exec_id,)

                ).fetchone()

                if dup:

                    return



                existing = conn.execute(

                    "SELECT shares_owned, initial_basis, total_premium_collected "

                    "FROM premium_ledger WHERE household_id = ? AND ticker = ?",

                    (household, ticker),

                ).fetchone()



                if existing:

                    old_shares = int(existing["shares_owned"] or 0)

                    new_shares = old_shares + shares_bought



                    old_basis = float(existing["initial_basis"] or 0)

                    if old_shares > 0 and old_basis > 0:

                        new_basis = round(

                            (old_basis * old_shares + cost_per_share * shares_bought)

                            / new_shares, 4

                        )

                    else:

                        new_basis = round(cost_per_share, 4)



                    conn.execute(

                        """

                        UPDATE premium_ledger

                        SET shares_owned = ?, initial_basis = ?

                        WHERE household_id = ? AND ticker = ?

                        """,

                        (new_shares, new_basis, household, ticker),

                    )

                    conn.execute(

                        """

                        INSERT OR IGNORE INTO fill_log

                            (exec_id, ticker, action, quantity, price,

                             premium_delta, account_id, household_id)

                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

                        """,

                        (exec_id, ticker, "STK_BUY", shares_bought,

                         cost_per_share, 0, acct_id, household),

                    )

                    logger.info(

                        "Shares bought: %s %s +%d @ $%.2f "

                        "(total: %d, basis: $%.4f)",

                        household, ticker, shares_bought, cost_per_share,

                        new_shares, new_basis,

                    )

                else:

                    conn.execute(

                        """

                        INSERT INTO premium_ledger

                            (household_id, ticker, initial_basis,

                             total_premium_collected, shares_owned)

                        VALUES (?, ?, ?, 0.0, ?)

                        """,

                        (household, ticker,

                         round(cost_per_share, 4), shares_bought),

                    )

                    conn.execute(

                        """

                        INSERT OR IGNORE INTO fill_log

                            (exec_id, ticker, action, quantity, price,

                             premium_delta, account_id, household_id)

                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

                        """,

                        (exec_id, ticker, "STK_BUY", shares_bought,

                         cost_per_share, 0, acct_id, household),

                    )

                    logger.info(

                        "New position: %s %s %d shares @ $%.2f",

                        household, ticker, shares_bought, cost_per_share,

                    )



    except Exception as exc:

        logger.exception("_on_shares_bought failed: %s", exc)





async def _get_household_inventory(household_id: str, ticker: str) -> dict:

    household_accounts = _household_accounts(household_id)

    ticker = ticker.upper().strip()



    ib_conn = await ensure_ib_connected()

    ib_conn.reqMarketDataType(4)

    positions = await ib_conn.reqPositionsAsync()

    working_call_shares = await asyncio.to_thread(

        _load_working_call_encumbrance,

        household_id,

        ticker,

    )



    account_inventory: dict[str, dict] = {}

    total_unencumbered_shares = 0

    total_unencumbered_cost = 0.0

    total_long_shares = 0

    total_short_call_shares = 0

    total_working_call_shares = 0



    for account_id in household_accounts:

        long_shares = 0

        cost_total = 0.0

        short_call_shares = 0



        for pos in positions:

            if pos.account != account_id or float(pos.position) == 0:

                continue



            contract = pos.contract

            if contract.symbol.upper() != ticker:

                continue



            if contract.secType == "STK" and float(pos.position) > 0:

                qty = int(round(float(pos.position)))

                long_shares += qty

                cost_total += float(pos.position) * float(pos.avgCost)

            elif (

                contract.secType == "OPT"

                and getattr(contract, "right", "").upper() == "C"

                and float(pos.position) < 0

            ):

                short_call_shares += abs(int(float(pos.position))) * 100



        working_call = int(working_call_shares.get(account_id, 0))

        unencumbered_shares = max(long_shares - short_call_shares - working_call, 0)

        paper_basis = round(cost_total / long_shares, 2) if long_shares > 0 else 0.0



        total_long_shares += long_shares

        total_short_call_shares += short_call_shares

        total_working_call_shares += working_call



        if unencumbered_shares < 100:

            continue



        supported_contracts = unencumbered_shares // 100

        total_unencumbered_shares += unencumbered_shares

        total_unencumbered_cost += paper_basis * unencumbered_shares



        account_inventory[account_id] = {

            "account_id": account_id,

            "account_label": ACCOUNT_LABELS.get(account_id, account_id),

            "long_shares": long_shares,

            "paper_basis": round(paper_basis, 2),

            "short_call_shares": short_call_shares,

            "working_call_shares": working_call,

            "unencumbered_shares": unencumbered_shares,

            "supported_contracts": supported_contracts,

        }



    weighted_paper_basis = (

        round(total_unencumbered_cost / total_unencumbered_shares, 2)

        if total_unencumbered_shares > 0 else 0.0

    )



    return {

        "household_id": household_id,

        "ticker": ticker,

        "accounts": account_inventory,

        "total_long_shares": total_long_shares,

        "total_short_call_shares": total_short_call_shares,

        "total_working_call_shares": total_working_call_shares,

        "total_unencumbered_shares": total_unencumbered_shares,

        "total_unencumbered_contracts": total_unencumbered_shares // 100,

        "weighted_paper_basis": weighted_paper_basis,

    }





async def get_portfolio_snapshot() -> str:

    """

    Full portfolio snapshot using instantaneous batch requests.

    No sleeps, no subscriptions, no loops waiting for data.



    Uses reqAccountSummaryAsync() + reqPositionsAsync() which return

    completed lists in a single awaitable call.

    """

    import json



    try:

        ib_conn = await ensure_ib_connected()



        # ── Batch requests — return instantly ──────────────────────────

        summary = await ib_conn.accountSummaryAsync()

        positions = await ib_conn.reqPositionsAsync()



        # ── Parse account summary ─────────────────────────────────────

        WANTED_TAGS = {"NetLiquidation", "ExcessLiquidity", "GrossPositionValue"}

        accounts_data = {}

        for item in summary:

            if item.account not in ACTIVE_ACCOUNTS:

                continue

            if item.tag not in WANTED_TAGS:

                continue

            acct = item.account

            if acct not in accounts_data:

                accounts_data[acct] = {

                    "label": ACCOUNT_LABELS.get(acct, acct),

                }

            accounts_data[acct][item.tag] = round(float(item.value), 2)



        # Ensure every active account has an entry even if summary was sparse

        for acct in ACTIVE_ACCOUNTS:

            if acct not in accounts_data:

                accounts_data[acct] = {"label": ACCOUNT_LABELS.get(acct, acct)}

            accounts_data[acct].setdefault("NetLiquidation", 0.0)

            accounts_data[acct].setdefault("ExcessLiquidity", 0.0)

            accounts_data[acct].setdefault("GrossPositionValue", 0.0)



        # ── Parse positions (skip closed/zero-qty ghost positions) ────

        positions_list =[]

        for pos in positions:

            if pos.account not in ACTIVE_ACCOUNTS:

                continue

            # IB keeps closed positions with qty=0 for the rest of the day

            if pos.position == 0:

                continue



            c = pos.contract

            if c.secType == "OPT":

                sym = f"{c.symbol} {c.lastTradeDateOrContractMonth} {c.strike}{c.right}"

            else:

                sym = c.symbol or c.localSymbol or str(c.conId)



            positions_list.append({

                "account":       pos.account,

                "account_label": ACCOUNT_LABELS.get(pos.account, pos.account),

                "symbol":        sym,

                "sec_type":      c.secType,

                "quantity":      float(pos.position),

                "avg_cost":      round(float(pos.avgCost), 4),

            })



        # ── Build final result ────────────────────────────────────────

        total_nlv = round(sum(

            a.get("NetLiquidation", 0) for a in accounts_data.values()

        ), 2)



        return json.dumps({

            "total_net_liquidation": total_nlv,

            "accounts":             accounts_data,

            "positions":            positions_list,

        }, default=str)



    except Exception as exc:

        logger.exception("get_portfolio_snapshot failed")

        return f'{{"error": "{exc}"}}'





async def get_market_quote(ticker: str) -> str:

    """

    Fetch the latest market price for any ticker via yfinance.

    Works for stocks (AAPL), indices (^VIX, ^GSPC), and ETFs (SPY).

    """

    try:

        t = yf.Ticker(ticker)

        info = t.fast_info



        price = getattr(info, "last_price", None)

        prev  = getattr(info, "previous_close", None)



        # Fallback to history if fast_info is empty

        if price is None:

            hist = t.history(period="5d")

            if hist.empty:

                return f'{{"error": "No data found for ticker {ticker}"}}'

            price = float(hist["Close"].iloc[-1])

            if prev is None and len(hist) >= 2:

                prev = float(hist["Close"].iloc[-2])



        result = {"ticker": ticker.upper(), "price": round(price, 2)}

        if prev is not None:

            change = round(price - prev, 2)

            pct    = round((change / prev) * 100, 2) if prev else 0.0

            result.update({

                "previous_close": round(prev, 2),

                "change":         change,

                "change_pct":     pct,

            })



        import json

        return json.dumps(result)



    except Exception as exc:

        logger.exception("get_market_quote failed for %s", ticker)

        return f'{{"error": "{exc}"}}'





async def get_top_news(ticker: str) -> str:

    """Fetch the top 3 headlines for a ticker via Finnhub."""

    try:

        today    = _date.today()

        from_str = (today - _timedelta(days=7)).isoformat()

        to_str   = today.isoformat()



        articles = finnhub_client.company_news(ticker.upper(), _from=from_str, to=to_str)

        if not articles:

            return f'{{"ticker": "{ticker.upper()}", "headlines": [], "message": "No recent news found"}}'



        top3 =[]

        for a in articles[:3]:

            top3.append({

                "headline": a.get("headline", ""),

                "source":   a.get("source", ""),

                "url":      a.get("url", ""),

                "datetime": a.get("datetime", ""),

            })



        import json

        return json.dumps({"ticker": ticker.upper(), "headlines": top3})



    except Exception as exc:

        logger.exception("get_top_news failed for %s", ticker)

        return f'{{"error": "{exc}"}}'





# INTERNAL ONLY — removed from LLM tools in security audit.

# Can still be called manually via Python console if needed.

# All trade execution flows through /approve command.

async def stage_ratio_spread(account_id: str,

                             ticker: str,

                             long_strike: float,

                             short_strike: float,

                             quantity: int) -> str:

    """

    Stage a 1x2 Ratio Call Spread as a BAG combo ticket in the AGT desk database.



    Leg 1: BUY  [quantity]  × Call @ long_strike

    Leg 2: SELL [quantity*2] × Call @ short_strike



    Resolves conIds via IB here (since the Telegram bot has a live connection),

    then writes the ticket for agt_trader.py to execute. The limit price is

    set to $0.00 — user adjusts in TWS before transmitting.

    """

    import json

    from datetime import date



    try:

        if account_id not in ACTIVE_ACCOUNTS:

            return json.dumps({"error": f"Unknown account '{account_id}'. "

                               f"Valid: {', '.join(ACTIVE_ACCOUNTS)}"})



        ib_conn = await ensure_ib_connected()



        # ── 1. Find the nearest weekly expiration via IBKR ────────

        try:

            expirations = await _ibkr_get_expirations(ticker)

        except Exception:

            expirations = []



        if not expirations:

            return json.dumps({"error": f"No option expirations found for {ticker}"})



        today = date.today()

        nearest_exp = None

        for exp_str in expirations:

            try:

                exp_date = date.fromisoformat(exp_str)

            except (ValueError, TypeError):

                continue

            if exp_date > today:

                nearest_exp = exp_str

                break



        if nearest_exp is None:

            return json.dumps({"error": f"No future expiration found for {ticker}"})



        # Convert YYYY-MM-DD → YYYYMMDD for ib_async

        ib_expiry = nearest_exp.replace("-", "")



        # ── 2. Resolve & qualify both option contracts ────────────────

        long_contract = ib_async.Option(

            symbol=ticker.upper(),

            lastTradeDateOrContractMonth=ib_expiry,

            strike=float(long_strike),

            right="C",

            exchange="SMART",

            currency="USD",

        )

        long_contract.multiplier = "100"



        short_contract = ib_async.Option(

            symbol=ticker.upper(),

            lastTradeDateOrContractMonth=ib_expiry,

            strike=float(short_strike),

            right="C",

            exchange="SMART",

            currency="USD",

        )

        short_contract.multiplier = "100"



        qualified_long = await ib_conn.qualifyContractsAsync(long_contract)

        if not qualified_long:

            return json.dumps({

                "error": f"Could not qualify long call: {ticker} {nearest_exp} ${long_strike}C"

            })



        qualified_short = await ib_conn.qualifyContractsAsync(short_contract)

        if not qualified_short:

            return json.dumps({

                "error": f"Could not qualify short call: {ticker} {nearest_exp} ${short_strike}C"

            })



        long_opt = qualified_long[0]

        short_opt = qualified_short[0]



        long_label  = long_opt.localSymbol or f"{ticker}{ib_expiry}{long_strike}C"

        short_label = short_opt.localSymbol or f"{ticker}{ib_expiry}{short_strike}C"



        # ── 3. Build the BAG ticket for the AGT desk database ───────────

        ticket = {

            "timestamp":     _datetime.now().isoformat(),

            "account_id":    account_id,

            "account_label": ACCOUNT_LABELS.get(account_id, account_id),

            "ticker":        ticker.upper(),

            "sec_type":      "BAG",

            "action":        "BUY",            # BUY the combo (net: long 1, short 2)

            "quantity":      int(quantity),     # Number of spreads

            "order_type":    "LMT",

            "limit_price":   0.00,             # Target net-zero; user adjusts in TWS

            "strategy":      "1x2 Ratio Call Spread (Stock Repair)",

            "combo_legs":[

                {

                    "conId":    long_opt.conId,

                    "ratio":    1,

                    "action":   "BUY",

                    "exchange": "SMART",

                    # Metadata for logging / human reference

                    "strike":   float(long_strike),

                    "right":    "C",

                    "expiry":   ib_expiry,

                    "label":    long_label,

                },

                {

                    "conId":    short_opt.conId,

                    "ratio":    2,

                    "action":   "SELL",

                    "exchange": "SMART",

                    "strike":   float(short_strike),

                    "right":    "C",

                    "expiry":   ib_expiry,

                    "label":    short_label,

                },

            ],

            "status": "pending",

            "transmit": False,

        }



        # ── 4. Append to the AGT desk database ──────────────────────────

        await asyncio.to_thread(append_pending_tickets, [ticket])



        logger.info("Staged ratio spread ticket: %s", ticket)



        result = {

            "success":    True,

            "strategy":   "1x2 Ratio Call Spread (Stock Repair)",

            "account_id": account_id,

            "account_label": ACCOUNT_LABELS.get(account_id, account_id),

            "ticker":     ticker.upper(),

            "expiration": nearest_exp,

            "legs":[

                {

                    "action":    "BUY",

                    "quantity":  int(quantity),

                    "contract":  long_label,

                    "strike":    float(long_strike),

                    "conId":     long_opt.conId,

                },

                {

                    "action":    "SELL",

                    "quantity":  int(quantity) * 2,

                    "contract":  short_label,

                    "strike":    float(short_strike),

                    "conId":     short_opt.conId,

                },

            ],

            "limit_price": 0.00,

            "transmit":    False,

            "message": (

                f"📋 Staged 1x2 Ratio Spread ticket: "

                f"BUY {quantity}x {long_label} / "

                f"SELL {quantity * 2}x {short_label} "

                f"@ $0.00 LMT in "

                f"{ACCOUNT_LABELS.get(account_id, account_id)} [{account_id}] "

                f"— queued for agt_trader execution"

            ),

        }



        return json.dumps(result, default=str)



    except Exception as exc:

        logger.exception("stage_ratio_spread failed")

        return f'{{"error": "{exc}"}}'





# INTERNAL ONLY — removed from LLM tools in security audit.

# Can still be called manually via Python console if needed.

# All trade execution flows through /approve command.

async def stage_trade_for_execution(account_id: str,

                                    ticker: str,

                                    action: str,

                                    quantity: int,

                                    order_type: str,

                                    limit_price: float | None = None,

                                    sec_type: str = "STK",

                                    expiry: str | None = None,

                                    strike: float | None = None,

                                    right: str | None = None) -> str:

    """

    Stage a confirmed trade by appending it to the AGT desk database.

    Does NOT place an IB order — just drops a ticket in the box for

    the execution pipeline to pick up.



    Supports STK (stock) and OPT (single-leg option) tickets.

    For multi-leg combos/spreads, use stage_ratio_spread instead.

    """

    import json



    try:

        # Validate inputs

        action = action.upper()

        if action not in ("BUY", "SELL"):

            return json.dumps({"error": f"Invalid action '{action}'. Must be BUY or SELL."})



        order_type = order_type.upper()

        if order_type not in ("MKT", "LMT"):

            return json.dumps({"error": f"Invalid order_type '{order_type}'. Must be MKT or LMT."})



        if order_type == "LMT" and limit_price is None:

            return json.dumps({"error": "limit_price is required for LMT orders."})



        if account_id not in ACTIVE_ACCOUNTS:

            return json.dumps({"error": f"Unknown account '{account_id}'. "

                               f"Valid: {', '.join(ACTIVE_ACCOUNTS)}"})



        sec_type = sec_type.upper()

        if sec_type not in ("STK", "OPT"):

            return json.dumps({"error": f"Invalid sec_type '{sec_type}'. Must be STK or OPT."})



        if sec_type == "OPT":

            if not expiry or strike is None or not right:

                return json.dumps({"error": "OPT orders require expiry, strike, and right."})

            right = right.upper()

            if right not in ("C", "P"):

                return json.dumps({"error": f"Invalid right '{right}'. Must be C or P."})

            # Normalize expiry to YYYYMMDD for ib_async

            expiry = expiry.replace("-", "")



        # Build the order ticket

        ticket = {

            "timestamp":     _datetime.now().isoformat(),

            "account_id":    account_id,

            "account_label": ACCOUNT_LABELS.get(account_id, account_id),

            "ticker":        ticker.upper(),

            "sec_type":      sec_type,

            "action":        action,

            "quantity":      int(quantity),

            "order_type":    order_type,

            "limit_price":   round(limit_price, 2) if limit_price is not None else None,

            "status":        "pending",

            "transmit":      True,

        }



        # Add option-specific fields

        if sec_type == "OPT":

            ticket["expiry"] = expiry

            ticket["strike"] = float(strike)

            ticket["right"]  = right



        # Stage atomically so trader reads never race this append.

        await asyncio.to_thread(append_pending_tickets, [ticket])



        logger.info("Staged trade ticket: %s", ticket)



        # Build human-readable label

        if sec_type == "OPT":

            label = f"{action} {quantity}x {ticker.upper()} {expiry} {strike}{right}"

        else:

            label = f"{action} {quantity} {ticker.upper()}"



        return json.dumps({

            "success":  True,

            "message":  (

                f"✅ Trade ticket staged: {label} "

                f"({order_type}{f' @ ${limit_price:.2f}' if limit_price else ''}) "

                f"in {ACCOUNT_LABELS.get(account_id, account_id)} [{account_id}]"

            ),

            "ticket":   ticket,

        }, default=str)



    except Exception as exc:

        logger.exception("stage_trade_for_execution failed")

        return f'{{"error": "{exc}"}}'





# ── Reconnaissance & Repricing ────────────────────────────────────────────



def _valid_quote_number(value: object) -> bool:

    if value is None:

        return False

    try:

        numeric = float(value)

    except (TypeError, ValueError):

        return False

    return numeric > 0 and not math.isnan(numeric)





def _extract_best_option_premium(row: pd.Series) -> float | None:

    def _num(field: str) -> float | None:

        value = row.get(field)

        if not _valid_quote_number(value):

            return None

        return float(value)



    bid = _num("bid")

    ask = _num("ask")

    if bid is not None and ask is not None:

        return round((bid + ask) / 2.0, 2)



    last_price = _num("lastPrice")

    if last_price is not None:

        return round(last_price, 2)



    if bid is not None:

        return round(bid, 2)

    if ask is not None:

        return round(ask, 2)

    return None





def _annualized_cc_roi(

    premium: float,

    dte: int,

    cost_basis: float,

    live_price: float,

) -> float:

    if dte <= 0:

        return 0.0



    capital_base = cost_basis if _valid_quote_number(cost_basis) else live_price

    if not _valid_quote_number(capital_base):

        return 0.0



    return (float(premium) / float(capital_base)) * (365.0 / float(dte)) * 100.0









def _prob_itm(

    S: float,

    K: float,

    days_to_expiry: int,

    iv: float,

    r: float = 0.043,

) -> float:

    """Calculate Black-Scholes probability of expiring In-The-Money."""

    import math



    if days_to_expiry <= 0 or iv <= 0:

        return 100.0 if S > K else 0.0



    t = days_to_expiry / 365.0

    d1 = (math.log(S / K) + (r + 0.5 * iv**2) * t) / (iv * math.sqrt(t))

    d2 = d1 - iv * math.sqrt(t)

    return ((1.0 + math.erf(d2 / math.sqrt(2.0))) / 2.0) * 100.0





def _format_share_quantity(quantity: float) -> str:

    if float(quantity).is_integer():

        return f"{int(quantity)}"

    return f"{quantity:.2f}"





def _format_strike_label(strike: float) -> str:

    return f"{int(strike)}" if float(strike).is_integer() else f"{strike:.2f}"





def _best_stock_price_from_ticker(ticker_data) -> float | None:

    delayed_bid = getattr(ticker_data, "delayedBid", None)

    delayed_ask = getattr(ticker_data, "delayedAsk", None)

    if _valid_quote_number(delayed_bid) and _valid_quote_number(delayed_ask):

        return round((float(delayed_bid) + float(delayed_ask)) / 2.0, 2)



    live_bid = getattr(ticker_data, "bid", None)

    live_ask = getattr(ticker_data, "ask", None)

    if _valid_quote_number(live_bid) and _valid_quote_number(live_ask):

        return round((float(live_bid) + float(live_ask)) / 2.0, 2)



    for candidate in (

        getattr(ticker_data, "delayedLast", None),

        getattr(ticker_data, "last", None),

        ticker_data.marketPrice() if hasattr(ticker_data, "marketPrice") else None,

        getattr(ticker_data, "close", None),

    ):

        if _valid_quote_number(candidate):

            return round(float(candidate), 2)



    return None





async def _get_ib_stock_reference_price(ib_conn: ib_async.IB, ticker: str) -> float | None:

    stock = ib_async.Stock(symbol=ticker, exchange="SMART", currency="USD")

    qualified = await ib_conn.qualifyContractsAsync(stock)

    if not qualified:

        return None



    contract = qualified[0]

    ticker_data = ib_conn.reqMktData(contract, "", False, False)

    try:

        await asyncio.sleep(2.0)

        return _best_stock_price_from_ticker(ticker_data)

    finally:

        try:

            ib_conn.cancelMktData(contract)

        except Exception:

            pass





def _load_yf_stock_reference_price(ticker: str) -> float | None:

    try:

        yf_ticker = yf.Ticker(ticker)

        info = yf_ticker.fast_info

        price = getattr(info, "last_price", None)

        if _valid_quote_number(price):

            return round(float(price), 2)



        history = yf_ticker.history(period="5d")

        if not history.empty:

            close_price = history["Close"].iloc[-1]

            if _valid_quote_number(close_price):

                return round(float(close_price), 2)

    except Exception:

        return None



    return None





async def _load_cc_ladder_snapshot(

    ticker: str,

    live_price: float,

    weighted_paper_basis: float,

    adjusted_cost_basis: float,

    strike_offset: int = 0,

) -> list[dict]:

    from agt_equities.ib_chains import IBKRChainError

    try:

        raw_expirations = await _ibkr_get_expirations(ticker)

    except (IBKRChainError, Exception) as exc:

        raise ValueError(f"IBKR chain unavailable for {ticker}: {exc}")

    today = _date.today()



    valid_expirations: list[tuple[str, int]] = []

    for exp_str in raw_expirations:

        try:

            exp_date = _date.fromisoformat(exp_str)

        except ValueError:

            continue



        dte = (exp_date - today).days

        if dte > 0:

            valid_expirations.append((exp_str, dte))



    if len(valid_expirations) < 2:

        raise ValueError(f"{ticker} does not have two valid future expirations.")



    ladders: list[dict] = []

    for exp_str, dte in valid_expirations:

        try:

            chain_data = await _ibkr_get_chain(ticker, exp_str, right='C',

                                                min_strike=live_price * 0.8,

                                                max_strike=live_price * 1.3)

        except Exception:

            continue



        if not chain_data:

            continue



        # Convert to DataFrame for compatibility with existing ladder logic

        calls = pd.DataFrame(chain_data)

        if calls.empty:

            continue



        calls["strike"] = pd.to_numeric(calls["strike"], errors="coerce")

        calls = calls.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)

        if calls.empty:

            continue



        atm_idx = int((calls["strike"] - float(live_price)).abs().idxmin())

        atm_strike = float(calls.loc[atm_idx, "strike"])

        start_idx = max(atm_idx + int(strike_offset), atm_idx)

        if start_idx >= len(calls):

            start_idx = len(calls) - 1



        rows: list[dict] = []

        last_visible_idx = start_idx - 1

        for idx in range(start_idx, len(calls)):

            row = calls.iloc[idx]

            premium = _extract_best_option_premium(row)

            if premium is None or not _valid_quote_number(premium):

                continue



            strike = float(row["strike"])

            iv = float(row.get("impliedVolatility", 0.0))

            prob_assignment = _prob_itm(live_price, strike, dte, iv)

            realized_pl_if_assigned = round(

                ((strike - float(weighted_paper_basis)) + float(premium)) * 100.0,

                2,

            )

            rows.append({

                "strike": round(strike, 2),

                "premium": round(float(premium), 2),

                "annualized_roi": round(

                    _annualized_cc_roi(

                        float(premium),

                        dte,

                        weighted_paper_basis,

                        live_price,

                    ),

                    2,

                ),

                "prob_assignment": round(prob_assignment, 1),

                "realized_pl_if_assigned": realized_pl_if_assigned,

                "post_sale_adjusted_basis": round(

                    float(adjusted_cost_basis) - float(premium),

                    2,

                ),

            })

            last_visible_idx = idx



            if len(rows) == 5:

                break



        if not rows:

            continue



        has_more = False

        for idx in range(last_visible_idx + 1, len(calls)):

            premium = _extract_best_option_premium(calls.iloc[idx])

            if premium is not None and _valid_quote_number(premium):

                has_more = True

                break



        ladders.append({

            "date": exp_str,

            "dte": int(dte),

            "atm_strike": round(atm_strike, 2),

            "strike_offset": max(int(strike_offset), 0),

            "has_more": has_more,

            "rows": rows,

        })



        if len(ladders) == 2:

            break



    if len(ladders) < 2:

        raise ValueError(f"Could not build two valid covered-call ladders for {ticker}.")



    return ladders







async def run_cc_ladder(household_id: str, ticker: str) -> str:

    """

    Build an interactive covered-call ladder for a household-level stock position.

    """

    import json



    try:

        if household_id not in HOUSEHOLD_MAP:

            return json.dumps({

                "error": (

                    f"Unknown household '{household_id}'. "

                    f"Valid: {', '.join(HOUSEHOLD_MAP)}"

                )

            })



        ticker = ticker.upper().strip()

        inventory = await _get_household_inventory(household_id, ticker)

        available_shares = int(inventory["total_unencumbered_shares"])

        available_contracts = int(inventory["total_unencumbered_contracts"])

        if available_shares < 100 or available_contracts <= 0:

            return json.dumps({

                "error": (

                    f"{ticker} in {household_id} has fewer than 100 "

                    "unencumbered household shares available for covered calls."

                )

            })



        weighted_paper_basis = float(inventory["weighted_paper_basis"])

        ledger_row = await asyncio.to_thread(

            _load_premium_ledger_snapshot,

            household_id,

            ticker,

        )

        adjusted_cost_basis = weighted_paper_basis

        ledger_initial_basis = weighted_paper_basis

        total_premium_collected = 0.0

        ledger_shares_owned = 0

        ledger_source = "live_ib"

        if ledger_row and ledger_row.get("adjusted_basis") is not None:

            adjusted_cost_basis = float(ledger_row["adjusted_basis"])

            ledger_initial_basis = float(ledger_row["initial_basis"])

            total_premium_collected = float(ledger_row["total_premium_collected"])

            ledger_shares_owned = int(ledger_row["shares_owned"])

            ledger_source = "premium_ledger"



        ib_conn = await ensure_ib_connected()

        live_price = await _get_ib_stock_reference_price(ib_conn, ticker)

        if live_price is None:

            try:

                live_price = await _ibkr_get_spot(ticker)

            except Exception:

                pass  # fall through to error below

        if live_price is None:

            return json.dumps({

                "error": f"Could not determine a reference market price for {ticker}."

            })



        ladders = await _load_cc_ladder_snapshot(

            ticker,

            float(live_price),

            float(weighted_paper_basis),

            float(adjusted_cost_basis),

            0,

        )



        return json.dumps({

            "household_id": household_id,

            "ticker": ticker,

            "inventory": inventory["accounts"],

            "total_long_shares": int(inventory["total_long_shares"]),

            "total_unencumbered_shares": available_shares,

            "total_unencumbered_contracts": available_contracts,

            "paper_cost_basis": round(float(weighted_paper_basis), 2),

            "adjusted_cost_basis": round(float(adjusted_cost_basis), 2),

            "ledger_initial_basis": round(float(ledger_initial_basis), 2),

            "ledger_total_premium_collected": round(float(total_premium_collected), 2),

            "ledger_shares_owned": ledger_shares_owned,

            "ledger_basis_source": ledger_source,

            "live_price": round(float(live_price), 2),

            "short_call_encumbered_shares": int(inventory["total_short_call_shares"]),

            "working_call_encumbered_shares": int(inventory["total_working_call_shares"]),

            "expirations": ladders,

            "summary": (

                f"Covered call ladder ready for {ticker}: "

                f"{available_contracts} household contract(s) backed by "

                f"{_format_share_quantity(available_shares)} unencumbered shares "

                f"across {household_id}."

            ),

        }, default=str)



    except Exception as exc:

        logger.exception("run_cc_ladder failed")

        return f'{{"error": "{exc}"}}'







async def get_working_orders() -> str:

    """

    Query IB for all live working orders and return a summary with

    two indicative prices per order:

      • natural_mid  — computed from the individual leg bid/ask mid-prices

      • market_mid   — the combo-level bid/ask mid (exchange-quoted)

    All data uses delayed (15-min) snapshots (reqMarketDataType 4).

    Source of truth is the exchange, not local files.



    Performance: all contracts are qualified in one batch call, market

    data is requested concurrently, and a single 2-second sleep covers

    every snapshot — total wall-clock ≈ 2–3 s regardless of order count.

    """

    import json

    import math



    try:

        ib_conn = await ensure_ib_connected()

        ib_conn.reqMarketDataType(4)       # delayed data — no subscription needed



        # ── 1. Fetch working orders ──────────────────────────────────

        await ib_conn.reqAllOpenOrdersAsync()

        trades = ib_conn.openTrades()



        WORKING = {"Submitted", "PreSubmitted", "PendingSubmit"}

        working = [t for t in trades if t.orderStatus.status in WORKING]



        if not working:

            return json.dumps({

                "working_orders":[],

                "message": "No working orders found on the exchange.",

            })



        # ── 2. Collect every contract we need to price ───────────────

        leg_contracts_by_conid: dict[int, ib_async.Contract] = {}

        bag_contracts: list[ib_async.Contract] = []        # combo-level

        single_contracts: list[ib_async.Contract] =[]    # non-BAG orders



        for t in working:

            c = t.contract

            if c.secType == "BAG" and getattr(c, "comboLegs", None):

                bag_contracts.append(c)

                for leg in c.comboLegs:

                    if leg.conId not in leg_contracts_by_conid:

                        leg_contracts_by_conid[leg.conId] = ib_async.Contract(

                            conId=leg.conId,

                            exchange=leg.exchange or "SMART",

                        )

            else:

                single_contracts.append(c)



        # ── 3. Qualify leg contracts safely via reqContractDetails ───

        unqualified = list(leg_contracts_by_conid.values())

        

        async def _resolve(conId, exchange):

            c = ib_async.Contract(conId=conId, exchange=exchange)

            details = await ib_conn.reqContractDetailsAsync(c)

            return details[0].contract if details else c



        if unqualified:

            tasks = [_resolve(c.conId, c.exchange) for c in unqualified]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:

                if isinstance(res, ib_async.Contract) and res.conId:

                    leg_contracts_by_conid[res.conId] = res



        # ── 3.5 Reconstruct Combo Legs for BAG contracts ─────────────

        for bc in bag_contracts:

            if getattr(bc, "comboLegs", None):

                for leg in bc.comboLegs:

                    qc = leg_contracts_by_conid.get(leg.conId)

                    if qc and getattr(qc, "exchange", None):

                        leg.exchange = qc.exchange

                    else:

                        leg.exchange = "SMART"



        # ── 4. Fire ALL market-data requests concurrently ────────────

        # IMPORTANT: snapshot=False is required to keep the stream open

        # so delayedBid/delayedAsk can populate over the next 2 seconds.



        def _valid(val) -> bool:

            return (val is not None

                    and isinstance(val, (int, float))

                    and val > 0

                    and not math.isnan(val)

                    and val != float("inf")

                    and val != float("-inf"))



        def _harvest(td) -> tuple[float | None, float | None]:

            """

            Aggressively extract best-available (bid, ask) from a Ticker.

            Cascade: Delayed Spread -> Live Spread -> Delayed Last -> Live Last -> Close.

            """

            # 1. Delayed Spread

            d_bid = getattr(td, "delayedBid", None)

            d_ask = getattr(td, "delayedAsk", None)

            if _valid(d_bid) and _valid(d_ask):

                return float(d_bid), float(d_ask)



            # 2. Live Spread

            l_bid = getattr(td, "bid", None)

            l_ask = getattr(td, "ask", None)

            if _valid(l_bid) and _valid(l_ask):

                return float(l_bid), float(l_ask)



            # 3. Delayed Last

            d_last = getattr(td, "delayedLast", None)

            if _valid(d_last):

                return float(d_last), float(d_last)



            # 4. Live Last

            l_last = getattr(td, "last", None)

            if _valid(l_last):

                return float(l_last), float(l_last)



            # 5. Close Price

            close_px = getattr(td, "close", None)

            if _valid(close_px):

                return float(close_px), float(close_px)



            return None, None



        # conId → Ticker  (legs)

        leg_tickers: dict[int, object] = {}

        for con_id, qc in leg_contracts_by_conid.items():

            if qc.conId:

                leg_tickers[con_id] = ib_conn.reqMktData(qc, "", False, False)



        # id(contract) → Ticker  (BAG combo-level)

        bag_tickers: dict[int, object] = {}

        for bc in bag_contracts:

            bag_tickers[id(bc)] = ib_conn.reqMktData(bc, "", False, False)



        # id(contract) → Ticker  (single-leg orders)

        single_tickers: dict[int, object] = {}

        for sc in single_contracts:

            single_tickers[id(sc)] = ib_conn.reqMktData(sc, "", False, False)



        # ── 5. ONE global sleep — all streams fill in parallel ───────

        await asyncio.sleep(2.0)



        # ── 6. Harvest prices & cancel data streams ──────────────────

        leg_prices: dict[int, tuple[float | None, float | None]] = {}

        for con_id, td in leg_tickers.items():

            leg_prices[con_id] = _harvest(td)



        bag_prices: dict[int, tuple[float | None, float | None]] = {}

        for obj_id, td in bag_tickers.items():

            bag_prices[obj_id] = _harvest(td)



        single_prices: dict[int, tuple[float | None, float | None]] = {}

        for obj_id, td in single_tickers.items():

            single_prices[obj_id] = _harvest(td)



        # Cancel all streams in one pass

        for td in (*leg_tickers.values(), *bag_tickers.values(),

                   *single_tickers.values()):

            try:

                ib_conn.cancelMktData(td.contract)

            except Exception:

                pass



        # ── 7. Build result rows ─────────────────────────────────────

        results = []

        for t in working:

            contract = t.contract

            order    = t.order

            status   = t.orderStatus



            natural_mid_str = "N/A"

            market_mid_str  = "N/A"



            # --- A. Natural mid from individual legs ─────────────────

            if contract.secType == "BAG" and getattr(contract, "comboLegs", None):

                net = 0.0

                all_legs_ok = True

                for leg in contract.comboLegs:

                    bid, ask = leg_prices.get(leg.conId, (None, None))

                    if bid is not None and ask is not None:

                        leg_mid = (bid + ask) / 2.0

                        if leg.action == "BUY":

                            net += leg_mid * leg.ratio

                        else:

                            net -= leg_mid * leg.ratio

                    else:

                        all_legs_ok = False

                        break

                if all_legs_ok:

                    natural_mid_str = f"${net:.2f}"



                # --- B. Combo market mid (exchange-quoted) ───────────

                bid, ask = bag_prices.get(id(contract), (None, None))

                if bid is not None and ask is not None:

                    market_mid_str = f"${(bid + ask) / 2:.2f}"



            else:

                # Single-leg: natural mid IS the contract mid

                bid, ask = single_prices.get(id(contract), (None, None))

                if bid is not None and ask is not None:

                    mid = (bid + ask) / 2.0

                    natural_mid_str = f"${mid:.2f}"

                    market_mid_str  = natural_mid_str



            # --- C. Human-readable contract label ────────────────────

            if contract.secType == "BAG":

                label = f"{contract.symbol} COMBO"

            elif contract.secType == "OPT":

                label = (

                    f"{contract.symbol} "

                    f"{contract.lastTradeDateOrContractMonth} "

                    f"${contract.strike}{contract.right}"

                )

            else:

                label = f"{contract.symbol} {contract.secType}"



            results.append({

                "orderId":      order.orderId,

                "permId":       order.permId,

                "ticker":       contract.symbol,

                "contract":     label,

                "account":      ACCOUNT_LABELS.get(order.account, order.account),

                "account_id":   order.account,

                "action":       order.action,

                "quantity":     int(order.totalQuantity),

                "filled":       int(status.filled),

                "remaining":    int(status.remaining),

                "order_type":   order.orderType,

                "lmt_price":    order.lmtPrice if order.orderType == "LMT" else None,

                "status":       status.status,

                "natural_mid":  natural_mid_str,

                "market_mid":   market_mid_str,

            })



        return json.dumps({

            "working_orders": results,

            "count": len(results),

        }, default=str)



    except Exception as exc:

        logger.exception("get_working_orders failed")

        return f'{{"error": "{exc}"}}'







async def update_live_order(ticker: str,

                            account_id: str,

                            new_limit_price: float) -> str:

    """

    Find the matching working order on IB for *ticker* + *account_id*,

    queue an execution control ticket to modify its limit price.

    """

    import json



    try:

        ib_conn = await ensure_ib_connected()



        await ib_conn.reqAllOpenOrdersAsync()

        trades = ib_conn.openTrades()



        WORKING = {"Submitted", "PreSubmitted", "PendingSubmit"}

        ticker_upper = ticker.upper()



        match = None

        for t in trades:

            if (t.orderStatus.status in WORKING

                    and t.contract.symbol.upper() == ticker_upper

                    and t.order.account == account_id):

                match = t

                break



        if match is None:

            return json.dumps({

                "error": (

                    f"No working order found for {ticker_upper} "

                    f"in account {ACCOUNT_LABELS.get(account_id, account_id)} "

                    f"[{account_id}]."

                )

            })



        old_price = match.order.lmtPrice

        

        ticket = {

            "action": "modify",

            "order_id": match.order.orderId,

            "new_price": new_limit_price

        }

        await asyncio.to_thread(append_pending_tickets, [ticket])



        return json.dumps({

            "success": True,

            "message": (

                f"✅ Queued reprice ticket for {ticker_upper} in "

                f"{ACCOUNT_LABELS.get(account_id, account_id)} "

                f"[{account_id}]: ${old_price:.2f} → ${new_limit_price:.2f}. "

                f"Ticket routed to execution engine."

            ),

            "orderId":         match.order.orderId,

            "old_limit_price": old_price,

            "new_limit_price": new_limit_price,

        }, default=str)



    except Exception as exc:

        logger.exception("update_live_order failed")

        return f'{{"error": "{exc}"}}'





# ── Map tool names → local async handlers ──────────────────────────────────



_TOOL_DISPATCH = {

    "get_portfolio_snapshot": lambda _: get_portfolio_snapshot(),

    "get_market_quote":      lambda args: get_market_quote(args["ticker"]),

    "get_top_news":          lambda args: get_top_news(args["ticker"]),

    "run_cc_ladder": lambda args: run_cc_ladder(

        args["household_id"], args["ticker"]

    ),

    "parse_and_stage_order": lambda args: parse_and_stage_order(

        args["text"]

    ),

    "get_working_orders": lambda _: get_working_orders(),

    "update_live_order": lambda args: update_live_order(

        args["ticker"], args["account_id"], args["new_limit_price"]

    ),

}



# ── Anthropic tool definitions ─────────────────────────────────────────────



TOOLS =[

    {

        "name": "get_portfolio_snapshot",

        "description": (

            "Fetch a full, live portfolio snapshot from Interactive Brokers. "

            "Returns JSON containing: total Net Liquidation Value (NLV), "

            "Excess Liquidity, Daily PnL per account, and an array of every "

            "active position with Symbol, Quantity, Average Cost, Market Price, "

            "Unrealized PnL, and Market Value. Use this whenever the user asks "

            "about their portfolio, holdings, positions, margin, buying power, "

            "returns, P&L, or account balances."

        ),

        "input_schema": {

            "type": "object",

            "properties": {},

            "required": [],

        },

    },

    {

        "name": "get_market_quote",

        "description": (

            "Fetch the latest market price for any ticker symbol via yfinance. "

            "Works for stocks (AAPL, MSFT), indices (^VIX, ^GSPC, ^DJI), and "

            "ETFs (SPY, QQQ). Returns current price, previous close, and change. "

            "Use this when the user asks about a specific stock price, VIX level, "

            "index value, or any market quote."

        ),

        "input_schema": {

            "type": "object",

            "properties": {

                "ticker": {

                    "type": "string",

                    "description": (

                        "The ticker symbol to quote. Use standard Yahoo Finance "

                        "symbols: AAPL for Apple, ^VIX for VIX index, ^GSPC for "

                        "S&P 500, SPY for SPDR S&P 500 ETF, etc."

                    ),

                },

            },

            "required": ["ticker"],

        },

    },

    {

        "name": "get_top_news",

        "description": (

            "Fetch the top 3 recent news headlines for a given stock ticker "

            "symbol via Finnhub. Use this when the user asks about news, "

            "headlines, or recent events for a specific stock or company."

        ),

        "input_schema": {

            "type": "object",

            "properties": {

                "ticker": {

                    "type": "string",

                    "description": "The stock ticker symbol (e.g., AAPL, TSLA, SPY).",

                },

            },

            "required": ["ticker"],

        },

    },

    {

        "name": "run_cc_ladder",

        "description": (

            "Build an interactive household-level Covered Call ladder for a "

            "stock position across every mapped IB account in that household. "

            "The tool pulls live IBKR inventory and encumbrance data, blends "

            "it with the premium ledger adjusted basis, loads the two nearest "

            "future call expirations via yfinance, and renders a Telegram "

            "dashboard with strike selection, infinite pagination, and expiry "

            "switching. Selecting a strike does NOT execute the trade - the "

            "ladder sends a separate CONFIRM/CANCEL staging prompt, and only "

            "CONFIRM splits the order into account-level transmit=False "

            "tickets in the AGT desk database."

        ),

        "input_schema": {

            "type": "object",

            "properties": {

                "household_id": {

                    "type": "string",

                    "description": (

                        "The household to analyze. "

                        "Valid values: Yash_Household or Vikram_Household."

                    ),

                },

                "ticker": {

                    "type": "string",

                    "description": (

                        "The underlying stock ticker with long shares "

                        "(for example AAPL, MSFT, NVDA)."

                    ),

                },

            },

            "required": ["household_id", "ticker"],

        },

    },

    {

        "name": "parse_and_stage_order",

        "description": (

            "Parse a fully formatted structured execution block and "

            "immediately write staged option tickets to the AGT desk database "

            "with transmit=False and limit_price=0.00 placeholders. Use this "

            "as soon as the model has already constructed a Morning Screener "

            "style execution block. Not suitable for BAG combos or ratio "

            "spreads."

        ),

        "input_schema": {

            "type": "object",

            "properties": {

                "text": {

                    "type": "string",

                    "description": (

                        "A structured order block in Morning Screener format, "

                        "for example: ACCOUNT, TICKER, STRATEGY, LEG 1."

                    ),

                },

            },

            "required":["text"],

        },

    },

    {

        "name": "get_working_orders",

        "description": (

            "Query Interactive Brokers for all live working orders on the "

            "exchange (Submitted, PreSubmitted, PendingSubmit). Returns each "

            "order's ticker, account, action, quantity, filled/remaining, "

            "current limit price, and TWO indicative mid-prices using 15-min "

            "delayed data: natural_mid and market_mid. The source of truth "

            "is IBKR. Use this when the user asks for a status update, to "

            "check pending trades, or to review working orders."

        ),

        "input_schema": {

            "type": "object",

            "properties": {},

        },

    },

    {

        "name": "update_live_order",

        "description": (

            "Reprice a live working order on Interactive Brokers. Finds the "

            "matching working order by ticker and account, updates its limit "

            "price, sets transmit=True, and submits the modification to the "

            "exchange. Use this when the user wants to update, reprice, or "

            "adjust the limit on an existing working order."

        ),

        "input_schema": {

            "type": "object",

            "properties": {

                "ticker": {

                    "type": "string",

                    "description": "The underlying ticker symbol.",

                },

                "account_id": {

                    "type": "string",

                    "description": "The IB account ID that owns the working order.",

                },

                "new_limit_price": {

                    "type": "number",

                    "description": "The new limit price for the order.",

                },

            },

            "required": ["ticker", "account_id", "new_limit_price"],

        },

    },

]

# ---------------------------------------------------------------------------

# Interactive Dashboard — view builders, coordinator, callback handler

# ---------------------------------------------------------------------------



_MAX_VIEW_CHARS = 3900  # Leave room for markup under Telegram's 4096 limit





def _truncate(text: str, limit: int = _MAX_VIEW_CHARS) -> str:

    """Truncate text to fit Telegram message limits."""

    if len(text) <= limit:

        return text

    return text[: limit - 30] + "\n... (truncated)</pre>"





def _build_orders_views(data: dict) -> dict[str, str]:

    """Build HTML views for the Working Orders dashboard."""

    orders = data.get("working_orders",[])



    if not orders:

        empty = (

            "<b>📊 Working Orders</b>\n"

            "<pre>No working orders on the exchange.</pre>"

        )

        return {"orders_summary": empty}



    # ── Summary table ───────────────────────────────────────────────

    header = (

        "<b>📊 Working Orders</b>  "

        f"<i>({len(orders)} active — 15 min delayed)</i>\n<pre>"

    )

    col = f"{'Ticker':<8}{'Acct':<12}{'Act':<5}{'Qty':>4}{'Fill':>6}{'Limit':>9}"

    lines =[header, col, "─" * 48]



    for o in orders:

        fill = f"{o.get('filled', 0)}/{o.get('quantity', 0)}"

        lmt  = o.get("lmt_price")

        lmt_str = f"${lmt:.2f}" if lmt is not None else "MKT"

        lines.append(

            f"{o.get('ticker', '?'):<8}"

            f"{o.get('account', '?'):<12}"

            f"{o.get('action', '?'):<5}"

            f"{o.get('quantity', 0):>4}"

            f"{fill:>6}"

            f"{lmt_str:>9}"

        )



        nat = o.get("natural_mid", "N/A")

        mkt = o.get("market_mid", "N/A")

        lines.append(f"  ↳ Nat {nat}  |  Mkt {mkt}")



    lines.append("</pre>")

    summary_html = "\n".join(lines)



    # ── Per-order detail view ───────────────────────────────────────

    detail_lines = ["<b>📋 Order Details</b>\n"]

    for i, o in enumerate(orders, 1):

        lmt = o.get("lmt_price")

        lmt_str = f"${lmt:.2f}" if lmt is not None else "MKT"

        nat = o.get("natural_mid", "N/A")

        mkt = o.get("market_mid", "N/A")

        detail_lines.append(

            f"<pre>"

            f"#{i}  {o.get('contract', '?')}\n"

            f"Acct:   {o.get('account', '?')} [{o.get('account_id', '?')}]\n"

            f"Action: {o.get('action', '?')}  Qty: {o.get('quantity', 0)}  "

            f"Filled: {o.get('filled', 0)}/{o.get('quantity', 0)}\n"

            f"Type:   {o.get('order_type', '?')}  Limit: {lmt_str}\n"

            f"Status: {o.get('status', '?')}  OrdId: {o.get('orderId', '?')}\n"

            f"Natural Mid: {nat}\n"

            f"Market Mid:  {mkt}"

            f"</pre>"

        )

    detail_html = "\n".join(detail_lines)



    return {

        "orders_summary": _truncate(summary_html),

        "orders_detail":  _truncate(detail_html),

    }





def _cc_confirm_keyboard(token: str) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup([[

        InlineKeyboardButton("CONFIRM", callback_data=f"cc:confirm:{token}"),

        InlineKeyboardButton("CANCEL", callback_data=f"cc:cancel:{token}"),

    ]])





def _allocate_household_contracts(

    inventory: dict[str, dict],

    requested_quantity: int,

) -> tuple[list[tuple[str, dict, int]], int]:

    remaining = max(int(requested_quantity), 0)

    allocations: list[tuple[str, dict, int]] = []

    ranked_accounts = sorted(

        (

            (account_id, account_data)

            for account_id, account_data in inventory.items()

            if int(account_data.get("supported_contracts", 0)) > 0

        ),

        key=lambda item: (

            -int(item[1].get("supported_contracts", 0)),

            str(item[0]),

        ),

    )



    for account_id, account_data in ranked_accounts:

        if remaining <= 0:

            break



        capacity = int(account_data.get("supported_contracts", 0))

        contracts = min(capacity, remaining)

        if contracts <= 0:

            continue



        allocations.append((account_id, account_data, contracts))

        remaining -= contracts



    return allocations, remaining





def _build_household_call_tickets(

    pending: dict,

    requested_quantity: int,

    strategy_name: str,

) -> tuple[list[dict], list[str], int]:

    allocations, remaining = _allocate_household_contracts(

        pending["inventory"],

        requested_quantity,

    )

    tickets: list[dict] = []

    split_lines: list[str] = []



    for account_id, account_data, contracts in allocations:

        account_label = account_data.get(

            "account_label",

            ACCOUNT_LABELS.get(account_id, account_id),

        )

        tickets.append({

            "timestamp": _datetime.now().isoformat(),

            "account_id": account_id,

            "account_label": account_label,

            "ticker": pending["ticker"],

            "sec_type": "OPT",

            "action": "SELL",

            "quantity": contracts,

            "order_type": "LMT",

            "limit_price": round(float(pending["premium"]), 2),

            "expiry": str(pending["expiry"]).replace("-", ""),

            "strike": float(pending["strike"]),

            "right": "C",

            "status": "pending",

            "transmit": False,

            "strategy": strategy_name,

        })

        split_lines.append(f"{account_label} [{account_id}]: {contracts}x")



    return tickets, split_lines, remaining













# ── Dashboard button layouts ────────────────────────────────────────────



_DASHBOARD_BUTTONS = {

    "get_working_orders": [[

            InlineKeyboardButton("Order Details", callback_data="orders_detail"),

        ],[

            InlineKeyboardButton("Refresh Prices", callback_data="orders:refresh"),

            InlineKeyboardButton("Match Natural Mid", callback_data="orders:match_mid"),

        ],[

            InlineKeyboardButton("Cancel All Working", callback_data="orders:cancel_all"),

        ],

    ],

}



_VIEW_BUILDERS = {

    "get_working_orders": _build_orders_views,

}



DASHBOARD_TOOLS = {"get_working_orders"}





async def send_dashboard(update: Update, chat_id: int,

                         tool_name: str, result_json: str) -> str:

    """

    Build and send an interactive dashboard for a framework tool result.

    Returns a short stub JSON for Claude (so it doesn't re-narrate).

    Raises on error so the caller can fall back to raw JSON.

    """

    import json as _json



    data = _json.loads(result_json)



    # If the framework returned an error, let Claude handle it

    if "error" in data:

        raise ValueError("Framework returned error — skip dashboard")



    builder = _VIEW_BUILDERS[tool_name]

    views = builder(data)



    # Determine which key is the summary view

    summary_key = next(k for k in views if k.endswith("_summary"))

    summary_html = views[summary_key]



    keyboard = InlineKeyboardMarkup(_DASHBOARD_BUTTONS[tool_name])



    msg = await update.message.reply_text(

        text=summary_html,

        parse_mode="HTML",

        reply_markup=keyboard,

    )



    # Cache for button clicks (includes raw data for action buttons)

    dashboard_cache[chat_id] = {

        "msg_id":     msg.message_id,

        "tool":       tool_name,

        "views":      views,

        "keyboard":   keyboard,

        "created_at": _datetime.now(),

        "_raw_data":  data,

    }



    # Return stub for Claude

    one_liner = data.get("summary", "Dashboard sent.")

    if not one_liner or one_liner == "Dashboard sent.":

        # Provide a reasonable fallback for tools that don't set "summary"

        one_liner = data.get("message", "Dashboard sent.")

    return _json.dumps({"dashboard_sent": True, "summary": one_liner})









# ---------------------------------------------------------------------------

# Working Orders — action callbacks (orders:refresh, orders:match_mid,

#                                     orders:cancel_all)

# ---------------------------------------------------------------------------



async def handle_orders_callback(

    update: Update, context: ContextTypes.DEFAULT_TYPE,

) -> None:

    """Handle action buttons on the Working Orders dashboard."""

    query = update.callback_query

    user = update.effective_user

    if user is None or user.id != AUTHORIZED_USER_ID:

        await query.answer("Unauthorized.", show_alert=True)

        return



    chat_id = update.effective_chat.id

    action = (query.data or "").split(":", 1)[-1]      # refresh | match_mid | cancel_all



    # ── Refresh Prices ──────────────────────────────────────────────

    if action == "refresh":

        await query.answer("Refreshing…")

        try:

            result_json = await get_working_orders()

            import json as _json

            data = _json.loads(result_json)

            if "error" in data:

                await query.edit_message_text(

                    f"<b>Refresh failed</b>\n<pre>{html.escape(str(data['error']))}</pre>",

                    parse_mode="HTML",

                )

                return



            views = _build_orders_views(data)

            keyboard = InlineKeyboardMarkup(_DASHBOARD_BUTTONS["get_working_orders"])

            summary_html = views["orders_summary"]



            await query.edit_message_text(

                text=summary_html,

                parse_mode="HTML",

                reply_markup=keyboard,

            )



            # Update cache

            dashboard_cache[chat_id] = {

                "msg_id":     query.message.message_id,

                "tool":       "get_working_orders",

                "views":      views,

                "keyboard":   keyboard,

                "created_at": _datetime.now(),

                "_raw_data":  data,

            }

        except Exception as exc:

            logger.exception("orders:refresh failed")

            await query.answer(f"Refresh error: {exc}", show_alert=True)

        return



    # ── Match Natural Mid ───────────────────────────────────────────

    if action == "match_mid":

        cache = dashboard_cache.get(chat_id)

        raw = (cache or {}).get("_raw_data", {})

        orders = raw.get("working_orders",[])



        if not orders:

            await query.answer("No working orders to reprice.", show_alert=True)

            return



        # Collect orders that have a usable natural mid

        eligible =[]

        for o in orders:

            nat_str = o.get("natural_mid", "N/A")

            if nat_str != "N/A" and nat_str.startswith("$"):

                try:

                    nat_val = float(nat_str.replace("$", ""))

                    eligible.append((o, nat_val))

                except (ValueError, TypeError):

                    pass



        if not eligible:

            await query.answer("No orders have a usable Natural Mid.", show_alert=True)

            return



        await query.answer(f"Repricing {len(eligible)} order(s)…")



        ib_conn = await ensure_ib_connected()

        await ib_conn.reqAllOpenOrdersAsync()

        all_trades = ib_conn.openTrades()

        WORKING = {"Submitted", "PreSubmitted"}



        # Check if we're in the delayed-data danger zone

        now_et = _datetime.now(ET)

        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        delayed_window = now_et.replace(hour=9, minute=45, second=0, microsecond=0)

        if now_et.weekday() < 5 and market_open <= now_et <= delayed_window:

            await query.edit_message_text(

                "\u26a0\ufe0f Match Natural Mid is blocked during the "

                "first 15 minutes after market open (9:30-9:45 AM ET). "

                "Delayed data is unreliable during this window. "

                "Wait until 9:45 AM or reprice manually in TWS."

            )

            return



        matched_count = 0

        failed_count = 0

        for o, nat_val in eligible:

            try:

                # Find the matching live trade by order ID

                target_trade = None

                for t in all_trades:

                    if (t.orderStatus.status in WORKING

                            and t.order.orderId == o.get("orderId")):

                        target_trade = t

                        break



                if not target_trade:

                    # Fallback: match by ticker + account + strike

                    for t in all_trades:

                        if (t.orderStatus.status in WORKING

                                and t.contract.symbol.upper() == o["ticker"]

                                and t.order.account == o["account_id"]

                                and getattr(t.contract, "strike", 0) == o.get("strike", 0)):

                            target_trade = t

                            break



                if target_trade:

                    target_trade.order.lmtPrice = _round_to_nickel(round(nat_val, 2))

                    # Sprint 1A: unified pre-trade gate

                    gate_ok, gate_reason = await _pre_trade_gates(

                        target_trade.order, target_trade.contract,

                        {"site": "orders_match_mid", "audit_id": None,

                         "household": ACCOUNT_TO_HOUSEHOLD.get(o.get("account_id"))},

                    )

                    if not gate_ok:

                        logger.warning("Match Mid blocked by gate: %s", gate_reason)

                        failed_count += 1

                        continue

                    assert_execution_enabled(in_process_halted=_HALTED)

                    ib_conn.placeOrder(target_trade.contract, target_trade.order)

                    matched_count += 1

                else:

                    failed_count += 1

            except ExecutionDisabledError as exd:

                logger.error("EXECUTION BLOCKED at placeOrder (match_mid): %s", exd)

                failed_count += 1

            except Exception as exc:

                logger.warning("Match mid failed for %s: %s", o.get("ticker"), exc)

                failed_count += 1



        # --- Auto-refresh: let TWS register, then rebuild dashboard ---

        await asyncio.sleep(3.0)

        import json as _json

        try:

            fresh_json = await get_working_orders()

            fresh_data = _json.loads(fresh_json)

            views = _build_orders_views(fresh_data)

            keyboard = InlineKeyboardMarkup(_DASHBOARD_BUTTONS["get_working_orders"])



            banner = (

                f"<b>\u2696\ufe0f Matched {matched_count}/{len(eligible)} "

                f"order(s) to Natural Mid</b>\n"

            )

            summary_html = banner + views.get("orders_summary", "")



            await query.edit_message_text(

                text=_truncate(summary_html),

                parse_mode="HTML",

                reply_markup=keyboard,

            )



            dashboard_cache[chat_id] = {

                "msg_id":     query.message.message_id,

                "tool":       "get_working_orders",

                "views":      views,

                "keyboard":   keyboard,

                "created_at": _datetime.now(),

                "_raw_data":  fresh_data,

            }

        except Exception as exc:

            logger.warning("orders:match_mid post-refresh failed: %s", exc)

        return



    # ── Cancel All Working ──────────────────────────────────────────

    if action == "cancel_all":

        cache = dashboard_cache.get(chat_id)

        raw = (cache or {}).get("_raw_data", {})

        orders = raw.get("working_orders", [])



        if not orders:

            await query.answer("No working orders to cancel.", show_alert=True)

            return



        await query.answer(f"Cancelling {len(orders)} order(s)…")



        import json as _json

        try:

            ib_conn = await ensure_ib_connected()

            await ib_conn.reqAllOpenOrdersAsync()

            all_trades = ib_conn.openTrades()

            WORKING = {"Submitted", "PreSubmitted"}



            cancelled = 0

            for t in all_trades:

                if t.orderStatus.status in WORKING:

                    try:

                        ib_conn.cancelOrder(t.order)

                        cancelled += 1

                    except Exception:

                        pass



            await asyncio.sleep(2)



            fresh_json = await get_working_orders()

            fresh_data = _json.loads(fresh_json)

            views = _build_orders_views(fresh_data)

            keyboard = InlineKeyboardMarkup(_DASHBOARD_BUTTONS["get_working_orders"])



            banner = (

                f"<b>\u274c Cancelled {cancelled} working order(s)</b>\n"

            )

            summary_html = banner + views.get("orders_summary", "")



            await query.edit_message_text(

                text=_truncate(summary_html),

                parse_mode="HTML",

                reply_markup=keyboard,

            )



            dashboard_cache[chat_id] = {

                "msg_id":     query.message.message_id,

                "tool":       "get_working_orders",

                "views":      views,

                "keyboard":   keyboard,

                "created_at": _datetime.now(),

                "_raw_data":  fresh_data,

            }

        except Exception as exc:

            logger.exception("orders:cancel_all failed")

            await query.answer(f"Cancel error: {exc}", show_alert=True)

        return



    await query.answer("Unknown orders action.", show_alert=True)





# ---------------------------------------------------------------------------

# Covered Call Ladder — action callbacks (cc:page, cc:exp, cc:select,

#                                      cc:confirm, cc:cancel)

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

def _build_cc_ladder_views(

    data: dict,

    expiry_index: int = 0,

    strike_offset: int = 0,

) -> tuple[str, InlineKeyboardMarkup]:

    expirations = data.get("expirations", [])

    if not expirations:

        empty = "<b>Covered Call Ladder</b>\n<pre>No ladder data available.</pre>"

        return empty, InlineKeyboardMarkup([])



    expiry_index = max(0, min(expiry_index, len(expirations) - 1))

    expiry = expirations[expiry_index]

    visible_rows = expiry.get("rows", [])

    strike_offset = int(expiry.get("strike_offset", strike_offset))



    ticker = html.escape(str(data.get("ticker", "UNKNOWN")))

    household_id = html.escape(str(data.get("household_id", "UNKNOWN")))

    share_quantity = _format_share_quantity(float(data.get("total_unencumbered_shares", 0)))

    paper_cost_basis = float(data.get("paper_cost_basis", 0.0))

    adjusted_cost_basis = float(data.get("adjusted_cost_basis", 0.0))

    live_price = float(data.get("live_price", 0.0))

    contracts = int(data.get("total_unencumbered_contracts", 0))

    encumbered = int(data.get("short_call_encumbered_shares", 0))

    working_encumbered = int(data.get("working_call_encumbered_shares", 0))



    lines = [

        f"<b>{ticker}</b>",

        "<pre>",

        f"Household ID:        {household_id}",

        f"Unencumbered Shares: {share_quantity}",

        f"Paper Cost Basis:    ${paper_cost_basis:.2f}",

        f"Adjusted Cost Basis: ${adjusted_cost_basis:.2f}",

        f"Current Market:      ${live_price:.2f}",

        "",

        f"Expiry: {expiry.get('date', '?')} (DTE {expiry.get('dte', '?')}) | Contracts: {contracts}",

    ]

    if encumbered:

        lines.append(f"Existing Short Calls: {encumbered}")

    if working_encumbered:

        lines.append(f"Working Call Shares: {working_encumbered}")

    lines.extend([

        "",

        (

            f"Showing strikes {strike_offset + 1}-"

            f"{strike_offset + len(visible_rows)} from the ATM ladder"

        ),

        "Strike     Premium   Ann ROI   Prob ITM   P/L @ Assign",

        "--------------------------------------------------------",

    ])



    for row in visible_rows:

        strike_label = f"{_format_strike_label(float(row['strike']))}C"

        premium = float(row["premium"])

        annualized_roi = float(row["annualized_roi"])

        prob = float(row["prob_assignment"])

        realized_pl = float(row["realized_pl_if_assigned"])

        lines.append(

            f"{strike_label:<10}${premium:>6.2f}   {annualized_roi:>7.2f}%   "

            f"{prob:>7.1f}%   ${realized_pl:>10.2f}"

        )



    lines.append("</pre>")

    html_text = "\n".join(lines)



    keyboard_rows = []

    for row_offset, row in enumerate(visible_rows):

        strike_label = _format_strike_label(float(row["strike"]))

        keyboard_rows.append([

            InlineKeyboardButton(

                f"Select {strike_label}C",

                callback_data=f"cc:select:{expiry_index}:{strike_offset}:{row_offset}",

            )

        ])



    if expiry.get("has_more"):

        keyboard_rows.append([

            InlineKeyboardButton(

                "Show Next 5 Strikes ⬇️",

                callback_data=f"cc:page:{expiry_index}:{strike_offset + 5}",

            )

        ])



    if len(expirations) > 1:

        other_index = 1 if expiry_index == 0 else 0

        other_date = expirations[other_index].get("date", "Next")

        keyboard_rows.append([

            InlineKeyboardButton(

                f"Switch to {other_date} Expiry 🗓️",

                callback_data=f"cc:exp:{other_index}:0",

            )

        ])



    return html_text, InlineKeyboardMarkup(keyboard_rows)





async def _reload_cc_ladder_data(data: dict, strike_offset: int) -> dict:

    fresh_expirations = await asyncio.to_thread(

        _load_cc_ladder_snapshot,

        str(data["ticker"]),

        float(data["live_price"]),

        float(data["paper_cost_basis"]),

        float(data["adjusted_cost_basis"]),

        int(strike_offset),

    )

    data["expirations"] = fresh_expirations

    return data





async def send_cc_ladder_dashboard(update: Update, chat_id: int, result_json: str) -> str:

    import json as _json



    data = _json.loads(result_json)

    if "error" in data:

        raise ValueError("Covered call ladder returned error - skip dashboard")



    html_text, keyboard = _build_cc_ladder_views(

        data,

        expiry_index=0,

        strike_offset=0,

    )

    msg = await update.message.reply_text(

        text=html_text,

        parse_mode="HTML",

        reply_markup=keyboard,

    )



    dashboard_cache[chat_id] = {

        "msg_id": msg.message_id,

        "tool": "run_cc_ladder",

        "created_at": _datetime.now(),

        "_raw_data": data,

        "cc_state": {

            "expiry_index": 0,

            "strike_offset": 0,

        },

    }



    return _json.dumps({

        "dashboard_sent": True,

        "summary": data.get("summary", "Covered call ladder sent."),

    })









# ---------------------------------------------------------------------------

async def send_reply(update: Update, text: str):

    """Send pre-formatted output (monospace)."""

    for i in range(0, len(text), 4000):

        chunk   = text[i: i + 4000]

        escaped = html.escape(chunk)

        try:

            await update.message.reply_text(f"<pre>{escaped}</pre>", parse_mode="HTML")

        except Exception:

            await update.message.reply_text(chunk)





async def send_text(update: Update, text: str):

    """Send LLM response as HTML. Falls back to tag-stripped plain text on parse failure."""

    for i in range(0, len(text), 4000):

        chunk = text[i: i + 4000]

        try:

            await update.message.reply_text(chunk, parse_mode="HTML")

        except Exception:

            # HTML parse failed — strip tags and send as plain text

            plain = re.sub(r"<[^>]+>", "", chunk)

            try:

                await update.message.reply_text(plain)

            except Exception:

                pass





def is_authorized(update: Update) -> bool:

    return (update.effective_user is not None

            and update.effective_user.id == AUTHORIZED_USER_ID)









# ---------------------------------------------------------------------------

# Telegram command handlers

# ---------------------------------------------------------------------------



_GREETINGS = {

    "hi", "hello", "hey", "yo", "sup", "what's up", "whats up",

    "howdy", "good morning", "gm", "commands", "help", "menu",

}





async def _send_command_menu(update: Update) -> None:

    # Sprint 1D: pruned command menu

    menu = (

        "<b>AGT Equities</b>\n"

        "\n"

        "<b>Trade</b>\n"

        "  /approve \u00b7 /reject \u2014 manage staged orders\n"

        "  /orders \u2014 live working orders\n"

        "  /cure \u2014 open Cure Console\n"

        "  /csp_harvest \u2014 stage BTC on profitable short puts\n"

        "\n"

        "<b>Monitor</b>\n"

        "  /status \u2014 connection + account metrics\n"

        "  /mode \u2014 desk mode + transitions\n"

        "  /vrp \u2014 volatility risk premium\n"

        "  /budget \u2014 API cost tracking\n"

        "\n"

        "<b>LLM</b>\n"

        "  <i>default \u2014 Haiku 4.5</i>\n"

        "  /think \u2014 Sonnet 4.6\n"

        "  /deep \u2014 Opus 4.6\n"

        "\n"

        "<b>System</b>\n"

        "  /reconnect \u00b7 /clear\n"

        "  /recover_transmitting \u2014 manual orphan recovery\n"

        "  /declare_peacetime \u2014 revert from WARTIME/AMBER\n"

        "  /halt \u2014 \U0001f6d1 emergency killswitch\n"

    )

    await update.message.reply_text(menu, parse_mode="HTML")





async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not is_authorized(update): return

    try:

        await _send_command_menu(update)

    except Exception as exc:

        logger.exception("cmd_start failed")





async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """Report IBKR connection status and key account metrics."""

    if not is_authorized(update):

        return

    try:

        global ib

        if ib is not None and ib.isConnected():

            accts = ib.managedAccounts()

            acct_str = ", ".join(accts) if accts else "none"

            await update.message.reply_text(

                f"IBKR: connected\n"

                f"Accounts: {acct_str}\n"

                f"Client ID: {ib.client.clientId if ib.client else '?'}"

            )

        else:

            await update.message.reply_text("IBKR: disconnected\nUse /reconnect to retry.")

    except Exception as exc:

        logger.exception("cmd_status failed")

        try:

            await update.message.reply_text(f"Status check failed: {exc}")

        except Exception:

            pass





async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not is_authorized(update): return

    try:

        ib_conn = await ensure_ib_connected()

        trades  = ib_conn.openTrades()

        if not trades:

            await update.message.reply_text("No pending orders.")

            return

        lines =[]

        for i, t in enumerate(trades, 1):

            c, o = t.contract, t.order

            px = f" @ ${o.lmtPrice:,.2f}" if o.lmtPrice else ""

            lines.append(

                f"{i}. {o.action} {o.totalQuantity} {c.symbol} "

                f"{c.secType}{px}\n"

                f"   Status: {t.orderStatus.status} | ID: {o.orderId} | Acct: {o.account}"

            )

        msg = "📋 Pending Orders:\n\n" + "\n\n".join(lines)

        await send_reply(update, msg)

    except Exception as exc:

        await update.message.reply_text(f"❌ Error: {exc}")





async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """Show API usage and cost — today + this month."""

    if not is_authorized(update):

        return

    try:

        today = str(_date.today())

        month_start = today[:8] + "01"



        with closing(_get_db_connection()) as conn:

            today_row = conn.execute(

                "SELECT input_tokens, output_tokens, api_calls "

                "FROM api_usage WHERE date = ?",

                (today,),

            ).fetchone()



            today_models = conn.execute(

                "SELECT model, input_tokens, output_tokens, api_calls "

                "FROM api_usage_by_model WHERE date = ? ORDER BY api_calls DESC",

                (today,),

            ).fetchall()



            month_models = conn.execute(

                """

                SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as outp,

                       SUM(api_calls) as calls

                FROM api_usage_by_model WHERE date >= ?

                GROUP BY model ORDER BY calls DESC

                """,

                (month_start,),

            ).fetchall()



        t_total = (int(today_row["input_tokens"]) + int(today_row["output_tokens"])) if today_row else 0

        t_calls = int(today_row["api_calls"]) if today_row else 0

        pct = (t_total / DAILY_TOKEN_BUDGET * 100) if DAILY_TOKEN_BUDGET else 0



        def _cost(rows, inp_key="input_tokens", outp_key="output_tokens"):

            c = 0.0

            for r in rows:

                p = MODEL_PRICING.get(r["model"], {"input": 1.0, "output": 5.0})

                c += (int(r[inp_key] or 0) * p["input"] + int(r[outp_key] or 0) * p["output"]) / 1_000_000

            return c



        def _label(m):

            if "haiku" in m.lower(): return "H"

            if "sonnet" in m.lower(): return "S"

            if "opus" in m.lower(): return "O"

            return "?"



        t_cost = _cost(today_models)

        m_cost = _cost(month_models, "inp", "outp")

        m_total = sum(int(r["inp"] or 0) + int(r["outp"] or 0) for r in month_models) if month_models else 0

        m_calls = sum(int(r["calls"] or 0) for r in month_models) if month_models else 0



        # Main display

        lines = [

            f"<b>Today</b>  {t_total:,}tk \u00b7 {t_calls} calls \u00b7 ${t_cost:.4f}",

            f"Budget: {pct:.1f}% of {DAILY_TOKEN_BUDGET:,}",

            "",

            f"<b>Month</b>  {m_total:,}tk \u00b7 {m_calls} calls \u00b7 ${m_cost:.4f}",

        ]



        # Per-model detail in expandable blockquote

        detail_lines = []

        if today_models:

            detail_lines.append("Today by model:")

            for r in today_models:

                label = _label(r["model"])

                tk = int(r["input_tokens"]) + int(r["output_tokens"])

                p = MODEL_PRICING.get(r["model"], {"input": 1.0, "output": 5.0})

                c = (int(r["input_tokens"]) * p["input"] + int(r["output_tokens"]) * p["output"]) / 1_000_000

                detail_lines.append(f"  [{label}] {int(r['api_calls'])} calls \u00b7 {tk:,}tk \u00b7 ${c:.4f}")



        if month_models:

            detail_lines.append("")

            detail_lines.append("Month by model:")

            for r in month_models:

                label = _label(r["model"])

                tk = int(r["inp"] or 0) + int(r["outp"] or 0)

                p = MODEL_PRICING.get(r["model"], {"input": 1.0, "output": 5.0})

                c = (int(r["inp"] or 0) * p["input"] + int(r["outp"] or 0) * p["output"]) / 1_000_000

                detail_lines.append(f"  [{label}] {int(r['calls'] or 0)} calls \u00b7 {tk:,}tk \u00b7 ${c:.4f}")



        detail_lines.append("")

        detail_lines.append("H=$1/$5M \u00b7 S=$3/$15M \u00b7 O=$15/$75M")



        if detail_lines:

            detail = "\n".join(detail_lines)

            lines.append("")

            lines.append(f"<blockquote expandable>{detail}</blockquote>")



        await update.message.reply_text("\n".join(lines), parse_mode="HTML")



    except Exception as exc:

        logger.exception("cmd_budget failed")

        try:

            await update.message.reply_text(f"Budget check failed: {exc}")

        except Exception:

            pass





async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not is_authorized(update): return

    try:

        clear_history(update.effective_chat.id)

        await update.message.reply_text("Conversation cleared.")

    except Exception as exc:

        logger.exception("cmd_clear failed")









async def cmd_reconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not is_authorized(update): return

    global ib

    try:

        if ib is not None:

            try: ib.disconnect()

            except Exception: pass

            ib = None

            await asyncio.sleep(4)

        await update.message.reply_text("⏳ Reconnecting…")

        await ensure_ib_connected()

        await update.message.reply_text(

            f"✅ Connected. Accounts: {', '.join(ib.managedAccounts())}"

        )

    except Exception as exc:

        await update.message.reply_text(f"❌ Reconnect failed: {exc}")





# ---------------------------------------------------------------------------

# /vrp — VRP Veto Report

# ---------------------------------------------------------------------------



async def cmd_vrp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """

    /vrp        — full VRP veto report on all current holdings

    /vrp ADBE   — single-ticker VRP check (need not be a holding)

    """

    if not is_authorized(update):

        return



    single_ticker = None

    if context.args:

        single_ticker = context.args[0].upper()



    try:

        ib_conn = await ensure_ib_connected()

    except Exception as exc:

        await update.message.reply_text(f"\u274c IBKR connection failed: {exc}")

        return



    from vrp_veto import (

        fetch_rv, fetch_earnings, compute_vrp_signal,

        apply_staleness_downgrade, format_full_report, format_single_report,

        discover_holdings_from_positions, write_vrp_results,

        fetch_iv_from_ibkr_async,

    )



    if single_ticker:

        status_msg = await update.message.reply_text(

            f"Running VRP check for {single_ticker}..."

        )

        try:

            holdings = discover_holdings_from_positions(ib_conn.positions())

            is_held = single_ticker in holdings



            # RV + earnings: pure HTTP, safe to offload

            rv_result, earnings_result = await asyncio.gather(

                asyncio.to_thread(fetch_rv, single_ticker),

                asyncio.to_thread(fetch_earnings, single_ticker),

            )



            # IV: must stay on main thread (ib_async is not thread-safe)

            iv_result = await fetch_iv_from_ibkr_async(ib_conn, single_ticker)



            signal = compute_vrp_signal(iv_result, rv_result, earnings_result)

            signal = apply_staleness_downgrade(signal, iv_result, rv_result)



            result = {

                "ticker": single_ticker,

                "iv": iv_result,

                "rv": rv_result,

                "earnings": earnings_result,

                "signal": signal,

            }



            report = format_single_report(result, single_ticker, is_held)

            await asyncio.to_thread(write_vrp_results, [result], "single_ticker")

            try:

                await status_msg.edit_text(report, parse_mode="HTML")

            except Exception:

                plain = re.sub(r"<[^>]+>", "", report)

                await status_msg.edit_text(plain)



        except Exception as exc:

            logger.exception("cmd_vrp single-ticker failed")

            await status_msg.edit_text(f"VRP check failed for {single_ticker}: {exc}")

    else:

        status_msg = await update.message.reply_text(

            "Running VRP veto scan on all holdings...\n"

            "This takes ~30-60 seconds (IV fetch pacing)."

        )

        try:

            holdings = discover_holdings_from_positions(ib_conn.positions())

            if not holdings:

                await status_msg.edit_text("No stock holdings found in IBKR. Nothing to scan.")

                return



            results = []

            for ticker in holdings:

                # RV + earnings: pure HTTP, safe to offload

                rv_result, earnings_result = await asyncio.gather(

                    asyncio.to_thread(fetch_rv, ticker),

                    asyncio.to_thread(fetch_earnings, ticker),

                )



                # IV: must stay on main thread (ib_async is not thread-safe)

                iv_result = await fetch_iv_from_ibkr_async(ib_conn, ticker)



                signal = compute_vrp_signal(iv_result, rv_result, earnings_result)

                signal = apply_staleness_downgrade(signal, iv_result, rv_result)



                results.append({

                    "ticker": ticker,

                    "iv": iv_result,

                    "rv": rv_result,

                    "earnings": earnings_result,

                    "signal": signal,

                })



            now_et = _datetime.now(ET)

            report = format_full_report(results, now_et.strftime("%Y-%m-%d  %H:%M ET"))

            await asyncio.to_thread(write_vrp_results, results, "on_demand")

            try:

                await status_msg.edit_text(report, parse_mode="HTML")

            except Exception:

                plain = re.sub(r"<[^>]+>", "", report)

                await status_msg.edit_text(plain)



        except Exception as exc:

            logger.exception("cmd_vrp full scan failed")

            await status_msg.edit_text(f"VRP scan failed: {exc}")





# ---------------------------------------------------------------------------

# /cc — Manual Covered Call Staging

# ---------------------------------------------------------------------------



async def cmd_cc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/cc — manually trigger the Covered Call staging and digest."""

    if not is_authorized(update): return



    status_msg = await update.message.reply_text("⏳ Scanning portfolio and calculating optimal covered calls across all modes...")

    try:

        # _run_cc_logic handles discovery, encumbrance checks, Mode 1/2 chain walking,

        # Active Defense Status injection, and pending_orders staging.

        _cc_ctx = RunContext(

            mode=RunMode.LIVE,

            run_id=uuid.uuid4().hex,

            order_sink=CollectorOrderSink(),

            decision_sink=SQLiteDecisionSink(_log_cc_cycle, _write_dynamic_exit_rows),

        )

        result = await _run_cc_logic(None, ctx=_cc_ctx)

        msg = result["main_text"]



        await status_msg.edit_text(f"<pre>{html.escape(msg)}</pre>", parse_mode="HTML")

    except Exception as exc:

        logger.exception("cmd_cc failed")

        await status_msg.edit_text(f"❌ Failed to run CC scan: {exc}")





# ---------------------------------------------------------------------------

# /scan — PXO Scanner (Engine 1: CSP Entry)

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

# Shared LLM dispatcher — parameterized by model

# ---------------------------------------------------------------------------

_MODEL_LABELS = {

    CLAUDE_MODEL_HAIKU:  ("H", "\U0001f504 Thinking\u2026"),

    CLAUDE_MODEL_SONNET: ("S", "\U0001f504 Thinking (Sonnet)\u2026"),

    CLAUDE_MODEL_OPUS:   ("O", "\U0001f504 Deep thinking (Opus)\u2026"),

}





async def _dispatch_to_llm(

    update: Update, text: str, model: str,

) -> None:

    """Run the agentic tool-use loop with the specified model."""

    chat_id = update.effective_chat.id

    mtag, status_text = _MODEL_LABELS.get(model, ("?", "\U0001f504 Thinking\u2026"))



    status = await update.message.reply_text(status_text)

    try:

        if not _check_and_track_tokens(0, 0):

            raise RuntimeError(

                f"\U0001f6ab Daily token budget ({DAILY_TOKEN_BUDGET:,}) reached. Resets at midnight."

            )



        add_to_history(chat_id, "user", text)

        today_str    = _date.today().strftime("%A, %B %d, %Y")

        dated_system = f"TODAY'S DATE: {today_str}\n\n{SYSTEM_PROMPT}"



        # ── Append Rulebook for Sonnet/Opus (reasoning models need it) ──

        if model != CLAUDE_MODEL_HAIKU and _RULEBOOK_TEXT:

            dated_system += (

                "\n\n"

                "# \u2500\u2500 PORTFOLIO RISK RULEBOOK \u2500\u2500\n"

                "The following Rulebook is the governing charter for AGT Equities. "

                "Apply these rules when evaluating trades, rolls, compliance, "

                "concentration, and portfolio strategy.\n\n"

                + _RULEBOOK_TEXT

            )



        # ── Agentic tool-use loop ───────────────────────────────────────────

        messages = list(chat_histories[chat_id])



        for round_num in range(MAX_ROUNDS):

            if stop_flags.get(chat_id):

                await update.message.reply_text("\U0001f6d1 Stopped.")

                stop_flags[chat_id] = False

                return



            response = await claude_client.messages.create(

                model      = model,

                max_tokens = MAX_TOKENS_PER_REPLY,

                system     = dated_system,

                tools      = TOOLS,

                messages   = messages,

            )



            inp = response.usage.input_tokens

            outp = response.usage.output_tokens

            _check_and_track_tokens(inp, outp, model)

            logger.info("Round %d [%s] | stop=%s | tokens=%d | today=%d/%d",

                        round_num, mtag, response.stop_reason, inp + outp,

                        _tokens_used_today, DAILY_TOKEN_BUDGET)



            # If the model is done (no tool calls), extract final text

            if response.stop_reason != "tool_use":

                final_text = ""

                for block in response.content:

                    if hasattr(block, "text"):

                        final_text += block.text

                if final_text.strip():

                    add_to_history(chat_id, "assistant", final_text)

                    await send_text(update, final_text)

                break



            # ── Process tool calls ──────────────────────────────────────────

            # Append the full assistant message (with tool_use blocks) to messages

            messages.append({"role": "assistant", "content": response.content})



            tool_results = []

            for block in response.content:

                if block.type != "tool_use":

                    continue



                tool_name = block.name

                tool_id   = block.id

                tool_args = block.input

                logger.info("Tool call: %s(%s)", tool_name, tool_args)



                handler = _TOOL_DISPATCH.get(tool_name)

                if handler:

                    try:

                        result = await handler(tool_args)

                    except Exception as exc:

                        logger.exception("Tool %s failed", tool_name)

                        result = f'{{"error": "{exc}"}}'

                else:

                    result = f'{{"error": "Unknown tool: {tool_name}"}}'



                logger.info("Tool result (%s): %s", tool_name, result[:300])



                # ── Dashboard intercept: send inline keyboard directly ───

                if tool_name == "run_cc_ladder":

                    try:

                        result = await send_cc_ladder_dashboard(

                            update, chat_id, result,

                        )

                    except Exception as dash_exc:

                        logger.warning("Covered call ladder dashboard skipped: %s",

                                       dash_exc)

                elif tool_name in DASHBOARD_TOOLS:

                    try:

                        result = await send_dashboard(

                            update, chat_id, tool_name, result,

                        )

                    except Exception as dash_exc:

                        logger.warning("Dashboard skipped (%s): %s",

                                       tool_name, dash_exc)

                        # result stays as full JSON — Claude handles it



                tool_results.append({

                    "type":        "tool_result",

                    "tool_use_id": tool_id,

                    "content":     result,

                })



            messages.append({"role": "user", "content": tool_results})



        else:

            logger.warning("Hit MAX_ROUNDS (%d) in tool loop", MAX_ROUNDS)



        try: await status.delete()

        except Exception: pass



    except Exception as exc:

        logger.exception("LLM dispatch error")

        try: await status.edit_text(f"\u274c {exc}")

        except Exception: await update.message.reply_text(f"\u274c {exc}")





# ---------------------------------------------------------------------------

# Main message handler — routes to PATH 1 or PATH 2

# ---------------------------------------------------------------------------



async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not is_authorized(update): return



    text    = update.message.text.strip()

    chat_id = update.effective_chat.id

    logger.info("Received: %s", text[:120])



    # Greeting / help detection — reply with command menu, skip LLM

    first_words = text.strip().lower().rstrip("!?.,")

    if first_words in _GREETINGS:

        await _send_command_menu(update)

        return



    # ── PATH 1: Hardcoded Execution Parser ──────────────────────────────────

    if text.upper().startswith("ACCOUNT:"):

        status = await update.message.reply_text("⏳ Parsing and staging order…")

        try:

            result = await parse_and_stage_order(text)

            try: await status.delete()

            except Exception: pass

            await send_reply(update, result)

        except Exception as exc:

            logger.exception("PATH 1 error")

            try: await status.edit_text(f"❌ {exc}")

            except Exception: await update.message.reply_text(f"❌ {exc}")

        return



    # ── PATH 2: Tool-Calling Quant ────────────────────────────────────────────



    # Natural-language stop words

    if text.lower() in STOP_WORDS:

        stop_flags[chat_id] = True

        clear_history(chat_id)

        await update.message.reply_text("🛑 Stopped. Conversation cleared.")

        return



    stop_flags[chat_id] = False  # reset on new message



    await _dispatch_to_llm(update, text, CLAUDE_MODEL_HAIKU)





# ---------------------------------------------------------------------------

# /think and /deep — model escalation commands

# ---------------------------------------------------------------------------



async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/think <question> — route to Sonnet 4.6 for complex reasoning."""

    if not is_authorized(update):

        return

    if not context.args:

        await update.message.reply_text(

            "Usage: /think <your question>\n"

            "Routes to Sonnet 4.6 for Rulebook reasoning, portfolio analysis, "

            "and complex evaluations."

        )

        return

    text = " ".join(context.args)

    logger.info("Escalated to Sonnet: %s", text[:120])

    await _dispatch_to_llm(update, text, CLAUDE_MODEL_SONNET)





async def cmd_deep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/deep <question> — route to Opus 4.6 for maximum reasoning power."""

    if not is_authorized(update):

        return

    if not context.args:

        await update.message.reply_text(

            "Usage: /deep <your question>\n"

            "Routes to Opus 4.6. Use sparingly \u2014 5x the cost of Sonnet."

        )

        return

    text = " ".join(context.args)

    logger.info("Escalated to Opus: %s", text[:120])

    await _dispatch_to_llm(update, text, CLAUDE_MODEL_OPUS)





# ---------------------------------------------------------------------------

# Dynamic GICS Universe — ticker_universe helpers

# ---------------------------------------------------------------------------





def _get_industry_groups_batch(tickers: list[str]) -> dict[str, str]:

    """Batch lookup of industry groups. Returns {ticker: industry_group}."""

    result = {t: "Unknown" for t in tickers}

    if not tickers:

        return result

    try:

        placeholders = ",".join("?" for _ in tickers)

        with closing(_get_db_connection()) as conn:

            rows = conn.execute(

                f"SELECT ticker, gics_industry_group FROM ticker_universe "

                f"WHERE ticker IN ({placeholders})",

                [t.upper() for t in tickers],

            ).fetchall()

            for row in rows:

                if row["gics_industry_group"]:

                    result[row["ticker"]] = str(row["gics_industry_group"])

    except Exception as exc:

        logger.warning("_get_industry_groups_batch failed: %s", exc)

    return result







def _refresh_ticker_universe_sync() -> dict:

    """Refresh ticker_universe — delegates to agt_equities.universe_refresh.



    A5e extraction: core logic moved to agt_equities/universe_refresh.py

    so both bot and scheduler daemon can call it without cross-importing.

    """

    from agt_equities.universe_refresh import refresh_ticker_universe

    return refresh_ticker_universe()







async def _refresh_ticker_universe() -> dict:

    return await asyncio.to_thread(_refresh_ticker_universe_sync)









# ---------------------------------------------------------------------------

# live_blotter staleness cleanup

# ---------------------------------------------------------------------------





async def _cleanup_stale_blotter() -> dict:

    """

    Remove live_blotter rows for options that no longer exist

    as open orders or positions in IBKR. Returns cleanup stats.

    """

    try:

        ib_conn = await ensure_ib_connected()

        open_orders = await ib_conn.reqAllOpenOrdersAsync()



        # Collect all IBKR open order IDs

        ib_order_ids = set()

        for trade in open_orders:

            ib_order_ids.add(trade.order.orderId)



        removed = 0

        with closing(_get_db_connection()) as conn:

            # Find blotter rows with status suggesting they're "live"

            rows = conn.execute(

                """

                SELECT order_id, ticker, status FROM live_blotter

                WHERE status IN ('Submitted', 'PreSubmitted',

                                 'PendingSubmit', 'ApiPending')

                """

            ).fetchall()



            stale_ids = []

            for row in rows:

                oid = row["order_id"]

                if oid not in ib_order_ids:

                    stale_ids.append(oid)



            if stale_ids:

                placeholders = ",".join("?" for _ in stale_ids)

                conn.execute(

                    f"""

                    UPDATE live_blotter

                    SET status = 'expired_cleaned'

                    WHERE order_id IN ({placeholders})

                    """,

                    stale_ids,

                )

                removed = len(stale_ids)



        return {"removed": removed, "checked": len(rows) if rows else 0, "error": None}



    except Exception as exc:

        logger.exception("_cleanup_stale_blotter failed")

        return {"removed": 0, "checked": 0, "error": str(exc)}









# ---------------------------------------------------------------------------

# /approve — show staged tickets with inline approve/reject buttons

# ---------------------------------------------------------------------------



async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """Show staged Mode 1 tickets with Approve buttons."""

    if not is_authorized(update):

        return

    try:

        with closing(_get_db_connection()) as conn:

            rows = conn.execute(

                """

                SELECT id, payload, created_at FROM pending_orders

                WHERE status = 'staged'

                ORDER BY id

                """

            ).fetchall()



        if not rows:

            await update.message.reply_text("No staged orders to approve.")

            return



        # Parse tickets and build keyboard

        tickets = []

        for row in rows:

            try:

                payload = json.loads(row["payload"])

                payload["_db_id"] = row["id"]

                tickets.append(payload)

            except (json.JSONDecodeError, TypeError):

                continue



        if not tickets:

            await update.message.reply_text("No valid staged orders found.")

            return



        # Build display and buttons

        lines = ["\u2501\u2501 Staged Orders \u2501\u2501", ""]

        keyboard_rows = []



        for t in tickets:

            db_id = t["_db_id"]

            ticker = t.get("ticker", "???")

            strike = t.get("strike", 0)

            expiry = t.get("expiry", "???")

            qty = t.get("quantity", 0)

            bid = t.get("limit_price", 0)

            ann = t.get("annualized_yield", 0)

            acct = t.get("account_id", "")

            label = ACCOUNT_LABELS.get(acct, acct)



            lines.append(

                f"#{db_id} {ticker} -{qty}c ${strike:.0f}C "

                f"{expiry} @ ${bid:.2f}"

            )

            lines.append(f"  {ann:.1f}% ann \u00b7 {label}")

            lines.append("")



            # Button for this order

            btn_label = f"\u2705 {ticker} ${strike:.0f}C x{qty}"

            keyboard_rows.append([

                InlineKeyboardButton(

                    btn_label,

                    callback_data=f"approve:{db_id}",

                )

            ])



        # Add "Approve All" and "Reject All" buttons

        keyboard_rows.append([

            InlineKeyboardButton(

                "\u2705 APPROVE ALL",

                callback_data="approve:all",

            ),

            InlineKeyboardButton(

                "\u274c REJECT ALL",

                callback_data="approve:reject_all",

            ),

        ])



        lines.append(f"Total: {len(tickets)} orders")

        output = "\n".join(lines)

        markup = InlineKeyboardMarkup(keyboard_rows)



        try:

            await update.message.reply_text(

                f"<pre>{html.escape(output)}</pre>",

                parse_mode="HTML",

                reply_markup=markup,

            )

        except Exception:

            await update.message.reply_text(output, reply_markup=markup)



    except Exception as exc:

        logger.exception("cmd_approve failed")

        try:

            await update.message.reply_text(f"Approve command failed: {exc}")

        except Exception:

            pass





async def _auto_execute_staged(

    ib_conn=None,

) -> tuple[int, int, list[str], str]:

    """CAS-claim every staged pending_orders row and place them via IB.



    Shared sweeper for:

      - handle_approve_callback(action="all") [manual /approve on live or paper]

      - cmd_daily [PAPER autopilot after 3-engine scan]

      - _scheduled_cc [PAPER autopilot after CC auto-stage]



    Returns (placed, failed, result_lines, status) where status is one of:

      - "none"     : no staged orders

      - "race"     : another approver claimed them first

      - "ib_fail"  : IB connection failed, claimed rows reverted to staged

      - "ok"       : orders placed (placed + failed sum to >= 1)



    ARCHITECTURE (MR !70): factored out of handle_approve_callback. Same

    read/await/write phase separation, same CAS guard. No DB connection

    held across await. Reverts claimed rows on IB failure so nothing is

    stranded.

    """

    # ── READ phase: get staged IDs ──

    with closing(_get_db_connection()) as conn:

        staged_ids = [

            r["id"] for r in conn.execute(

                "SELECT id FROM pending_orders WHERE status = 'staged' ORDER BY id"

            ).fetchall()

        ]

    if not staged_ids:

        return 0, 0, [], "none"



    # ── WRITE phase: CAS claim staged → processing ──

    placeholders = ",".join("?" * len(staged_ids))

    with closing(_get_db_connection()) as conn:

        with tx_immediate(conn):

            claimed = conn.execute(

                f"UPDATE pending_orders SET status = 'processing' "

                f"WHERE id IN ({placeholders}) AND status = 'staged'",

                staged_ids,

            ).rowcount

    if claimed == 0:

        return 0, 0, [], "race"



    # ── READ phase: fetch claimed rows ──

    with closing(_get_db_connection()) as conn:

        rows = conn.execute(

            f"SELECT id, payload FROM pending_orders "

            f"WHERE id IN ({placeholders}) AND status = 'processing' "

            f"ORDER BY id",

            staged_ids,

        ).fetchall()

    claimed_ids = [row["id"] for row in rows]



    # ── AWAIT phase: IB connect + positions cache ──

    try:

        if ib_conn is None:

            ib_conn = await ensure_ib_connected()

        cached_positions = await ib_conn.reqPositionsAsync()

    except Exception as ib_exc:

        try:

            reverted = _revert_pending_order_claims(claimed_ids)

            if reverted > 0:

                logger.warning(

                    "Reverted %d claimed rows after IB connection failure",

                    reverted,

                )

        except Exception:

            logger.exception("revert after ib_fail also failed")

        return 0, 0, [f"\u274c IB connection failed: {ib_exc}"], "ib_fail"



    # ── AWAIT phase: place orders (no conn held) ──

    placed = 0

    failed = 0

    results_lines: list[str] = []

    for row in rows:

        try:

            payload = json.loads(row["payload"])

            success, msg = await _place_single_order(

                payload, row["id"], cached_positions

            )

            if success:

                placed += 1

                results_lines.append(f"\u2705 {msg}")

            else:

                failed += 1

                results_lines.append(f"\u274c {msg}")

        except Exception as exc:

            failed += 1

            results_lines.append(f"\u274c Order #{row['id']}: {exc}")

            logger.exception("_auto_execute_staged: order #%s failed", row["id"])



    return placed, failed, results_lines, "ok"





async def handle_approve_callback(

    update: Update, context: ContextTypes.DEFAULT_TYPE

) -> None:

    """Handle approve/reject button taps for staged orders.



    ARCHITECTURE (Followup #13): read/await/write phase separation.

    No DB connection is held across any await call. All UPDATEs use

    a CAS guard (WHERE status='staged') to prevent lost-update races

    from concurrent operator clicks. No asyncio.Lock needed — the

    CAS guard survives bot restarts.

    """

    query = update.callback_query

    if not query or not query.data:

        return



    user_id = query.from_user.id if query.from_user else None

    if user_id != AUTHORIZED_USER_ID:

        await query.answer("Unauthorized.", show_alert=True)

        return



    claimed_ids: list[int] = []



    try:

        await query.answer()

        parts = query.data.split(":")

        if len(parts) != 2:

            return



        action = parts[1]



        if action == "reject_all":

            # ── WRITE phase (no await inside) ──

            with closing(_get_db_connection()) as conn:

                with tx_immediate(conn):

                    result = conn.execute(

                        "UPDATE pending_orders SET status = 'rejected' WHERE status = 'staged'"

                    )

                    count = result.rowcount

            # ── AWAIT phase (conn released) ──

            await query.edit_message_text(

                f"\u274c Rejected {count} staged orders."

            )

            return



        if action == "all":

            # MR !70: delegate to shared sweeper. Same CAS + revert-on-ib-fail.

            placed, failed, body_lines, status = await _auto_execute_staged()

            if status == "none":

                await query.edit_message_text("No staged orders remaining.")

                return

            if status == "race":

                await query.edit_message_text("Orders already being processed.")

                return

            if status == "ib_fail":

                msg = body_lines[0] if body_lines else "\u274c IB connection failed"

                await query.edit_message_text(

                    f"{msg}\nOrders reverted to staged. Try /approve again after /reconnect."

                )

                return

            results_lines = [

                "\u2501\u2501 Orders Placed \u2501\u2501",

                "",

                *body_lines,

                "",

                f"Placed: {placed} | Failed: {failed}",

            ]

            output = "\n".join(results_lines)

            try:

                await query.edit_message_text(

                    f"<pre>{html.escape(output)}</pre>",

                    parse_mode="HTML",

                )

            except Exception:

                await query.edit_message_text(output)

            return



        # Single order approval

        try:

            db_id = int(action)

        except ValueError:

            return



        # ── WRITE phase: CAS claim single row ──

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                result = conn.execute(

                    "UPDATE pending_orders SET status = 'processing' "

                    "WHERE id = ? AND status = 'staged'",

                    (db_id,),

                )



        # ── AWAIT phase (conn released) ──

        if result.rowcount == 0:

            await query.edit_message_text(

                f"Order #{db_id} already processed or not found."

            )

            return

        claimed_ids = [db_id]



        # ── READ phase: fetch claimed row ──

        with closing(_get_db_connection()) as conn:

            row = conn.execute(

                "SELECT id, payload FROM pending_orders WHERE id = ?",

                (db_id,),

            ).fetchone()



        if not row:

            _revert_pending_order_claims(claimed_ids)

            await query.edit_message_text(

                f"Order #{db_id} not found."

            )

            return



        # ── AWAIT phase: place order (no conn held) ──

        payload = json.loads(row["payload"])

        success, msg = await _place_single_order(payload, db_id)



        if success:

            await query.answer(f"\u2705 {msg}", show_alert=True)

        else:

            await query.answer(f"\u274c {msg}", show_alert=True)



    except Exception as exc:

        logger.exception("handle_approve_callback failed")

        # Safety net: revert any stranded processing rows

        try:

            reverted = _revert_pending_order_claims(claimed_ids)

            if reverted > 0:

                logger.warning(

                    "Reverted %d claimed processing rows to staged", reverted

                )

        except Exception:

            pass

        try:

            await query.answer(f"Error: {exc}", show_alert=True)

        except Exception:

            pass





# ---------------------------------------------------------------------------

# Beta Impl 3: Dynamic Exit TRANSMIT / CANCEL handlers

# ---------------------------------------------------------------------------





def _increment_revalidation_count(audit_id: str) -> None:

    """Increment re_validation_count in an ISOLATED transaction (R8).



    Uses a SEPARATE sqlite3 connection so the counter persists even if

    the caller's main flow subsequently fails. Per Architect R8 ruling:

    counter measures failure pressure, not attempt volume. Must survive

    downstream rollback so the 3-strike budget is accurate.

    """

    from agt_equities.db import get_db_connection, tx_immediate

    iso_conn = get_db_connection()

    try:

        with tx_immediate(iso_conn):

            iso_conn.execute(

                "UPDATE bucket3_dynamic_exit_log "

                "SET re_validation_count = re_validation_count + 1 "

                "WHERE audit_id = ?",

                (audit_id,),

            )

    finally:

        iso_conn.close()





def _revert_transmitting_to_cancelled(audit_id: str, reason: str) -> int:

    """Revert a TRANSMITTING row to CANCELLED after a Step 7 early-exit.



    Used when gate check fails or kill-switch fires AFTER the Step 6 CAS

    lock (ATTESTED → TRANSMITTING) has already been acquired. Idempotent:

    if the row is no longer TRANSMITTING, the UPDATE is a no-op.



    NOT used for TRANSMIT_IB_ERROR (the `except Exception as ib_err:` branch

    in handle_dex_callback) — that path is intentionally sticky because

    we don't know if the IBKR order reached the wire mid-flight.



    Returns cursor.rowcount (0 if idempotent no-op, 1 on successful revert).

    All exceptions swallowed with logger.exception — this helper must never

    raise into the caller's error handling path.

    """

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                result = conn.execute(

                    "UPDATE bucket3_dynamic_exit_log "

                    "SET final_status = 'CANCELLED', "

                    "    last_updated = CURRENT_TIMESTAMP "

                    "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                    (audit_id,),

                )

        logger.info(

            "DEX_REVERT: audit_id=%s rowcount=%d reason=%s",

            audit_id, result.rowcount, reason,

        )

        return result.rowcount

    except Exception as exc:

        logger.exception(

            "DEX_REVERT_FAILED: audit_id=%s reason=%s error=%s",

            audit_id, reason, exc,

        )

        return 0






async def handle_csp_approval_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """ADR-010 Phase 1: handle CSP approval digest button taps.

    callback_data format:
      csp_approve:<row_id>:<idx>  -- add candidate idx to approved set
      csp_skip:<row_id>:<idx>     -- remove candidate idx from approved set
      csp_submit:<row_id>         -- commit selection, flip status='approved'

    CAS guard: approve/skip update uses WHERE id=? AND status='pending'.
    Submit uses same guard -- double-submit is a no-op.
    All operations are idempotent.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = query.from_user.id if query.from_user else None
    if user_id != AUTHORIZED_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[0]  # csp_approve | csp_skip | csp_submit

    try:
        row_id = int(parts[1])
    except (IndexError, ValueError):
        await query.answer("Bad callback data.", show_alert=True)
        return

    try:
        with closing(_get_db_connection()) as conn:
            row = conn.execute(
                "SELECT status, approved_indices_json, candidates_json "
                "FROM csp_pending_approval WHERE id=?",
                (row_id,),
            ).fetchone()
    except Exception:
        logger.exception("handle_csp_approval_callback: DB read error row=%d", row_id)
        await query.answer("DB error.", show_alert=True)
        return

    if row is None:
        await query.answer("Row not found.", show_alert=True)
        return
    if row[0] != "pending":
        await query.answer(f"Already {row[0]}.", show_alert=True)
        return

    try:
        approved_indices: list[int] = json.loads(row[1] or "[]")
        n_candidates: int = len(json.loads(row[2] or "[]"))
    except (json.JSONDecodeError, TypeError):
        approved_indices = []
        n_candidates = 0

    now_str = _datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if action in ("csp_approve", "csp_skip"):
        try:
            idx = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Bad index.", show_alert=True)
            return

        if action == "csp_approve":
            if 0 <= idx < n_candidates and idx not in approved_indices:
                approved_indices.append(idx)
            label = "\u2705 Added"
        else:  # csp_skip
            approved_indices = [i for i in approved_indices if i != idx]
            label = "\u23ed Removed"

        new_json = json.dumps(sorted(approved_indices))
        try:
            with closing(_get_db_connection()) as conn:
                conn.execute(
                    "UPDATE csp_pending_approval "
                    "SET approved_indices_json=? "
                    "WHERE id=? AND status='pending'",
                    (new_json, row_id),
                )
                conn.commit()
        except Exception:
            logger.exception(
                "handle_csp_approval_callback: %s update error row=%d", action, row_id
            )
        await query.answer(label, show_alert=False)

    elif action == "csp_submit":
        new_json = json.dumps(sorted(approved_indices))
        try:
            with closing(_get_db_connection()) as conn:
                conn.execute(
                    """
                    UPDATE csp_pending_approval
                    SET status='approved',
                        approved_indices_json=?,
                        resolved_at_utc=?,
                        resolved_by='yash'
                    WHERE id=? AND status='pending'
                    """,
                    (new_json, now_str, row_id),
                )
                conn.commit()
        except Exception:
            logger.exception(
                "handle_csp_approval_callback: submit error row=%d", row_id
            )
            await query.answer("DB error on submit.", show_alert=True)
            return

        display_time = now_str[:16].replace("T", " ")
        try:
            await query.edit_message_text(
                f"\u2705 Submitted {display_time} UTC \u2014 "
                f"{len(approved_indices)} candidate(s) approved."
            )
        except Exception:
            pass  # Non-critical: edit may fail on old messages
        await query.answer("Submitted.", show_alert=False)

    else:
        await query.answer("Unknown action.", show_alert=True)


async def handle_dex_callback(

    update: Update, context: ContextTypes.DEFAULT_TYPE,

) -> None:

    """Handle TRANSMIT/CANCEL taps for ATTESTED dynamic exit rows.



    TRANSMIT executes the 9-step JIT re-validation chain (steps 0–8) per

    HANDOFF_ARCHITECT_v5 Final JIT Precedence Chain, post-Gemini rulings R1–R8.



    CANCEL transitions ATTESTED → CANCELLED atomically (R4: terminal, no revert).



    Counter increment isolation (R8): uses _increment_revalidation_count() which

    opens a separate DB connection so the counter persists even if the main JIT

    flow fails after increment. This ensures the 3-strike budget is accurate.

    """

    query = update.callback_query

    if not query or not query.data:

        return



    user_id = query.from_user.id if query.from_user else None

    if user_id != AUTHORIZED_USER_ID:

        await query.answer("Unauthorized.", show_alert=True)

        return



    await query.answer()



    # F6: capture keyboard for retryable branches (Q3 defensive — query.message may be None)

    _original_markup = query.message.reply_markup if query.message else None



    parts = (query.data or "").split(":")

    if len(parts) != 3:

        return



    action, audit_id = parts[1], parts[2]



    # ── CANCEL branch ──────────────────────────────────────────────────

    if action == "cancel":

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                result = conn.execute(

                    "UPDATE bucket3_dynamic_exit_log "

                    "SET final_status = 'CANCELLED', last_updated = CURRENT_TIMESTAMP "

                    "WHERE audit_id = ? AND final_status = 'ATTESTED'",

                    (audit_id,),

                )

        if result.rowcount == 0:

            logger.warning("CANCEL_RACE_LOST: audit_id=%s", audit_id)

            try:

                await query.edit_message_text(

                    f"\u26a0\ufe0f Cancel race: row no longer ATTESTED.\naudit_id: {audit_id[:8]}..."

                )

            except Exception:

                pass

        else:

            logger.info("CANCEL_SUCCESS: audit_id=%s", audit_id)

            _dispatched_audits.discard(audit_id)

            try:

                await query.edit_message_text(

                    f"\u274c CANCELLED dynamic exit.\naudit_id: {audit_id[:8]}..."

                )

            except Exception:

                pass

        return



    if action != "transmit":

        return



    # ── TRANSMIT branch — JIT re-validation chain (steps 0–8) ─────────

    import time as _time_mod

    from agt_equities.rule_engine import (

        evaluate_gate_1, ConvictionTier, is_ticker_locked,

    )



    # Step 0: Fetch ATTESTED row

    with closing(_get_db_connection()) as conn:

        row = conn.execute(

            "SELECT * FROM bucket3_dynamic_exit_log "

            "WHERE audit_id = ? AND final_status = 'ATTESTED'",

            (audit_id,),

        ).fetchone()



    if not row:

        logger.warning("ATTESTED_ROW_NOT_FOUND: audit_id=%s", audit_id)

        try:

            await query.edit_message_text(

                f"\u274c Row not found or no longer ATTESTED.\naudit_id: {audit_id[:8]}..."

            )

        except Exception:

            pass

        return



    ticker = row["ticker"]

    is_wartime = row["desk_mode"] == "WARTIME"



    # Followup #17: stale-attestation guard (fires BEFORE Step 6 CAS lock)

    try:

        attested_age = _datetime.now(_timezone.utc) - _parse_sqlite_utc(row["last_updated"])

        if attested_age > _timedelta(minutes=10):

            _dispatched_audits.discard(audit_id)

            try:

                await query.edit_message_text(

                    f"\u274c STALE_ATTESTATION: {ticker} attestation expired "

                    f"({attested_age.total_seconds()/60:.0f}m old). "

                    f"Re-stage from Cure Console."

                )

            except Exception:

                pass

            return

    except Exception as age_exc:

        logger.warning("Stale-attestation guard parse error: %s", age_exc)

        # Fail-open: proceed with TRANSMIT if parse fails



    # Step 1: 3-strike row-level check (WARTIME bypasses per ADR-004 §4)

    if not is_wartime and row["re_validation_count"] >= 3:

        # Transition to DRIFT_BLOCKED terminal

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                conn.execute(

                    "UPDATE bucket3_dynamic_exit_log "

                    "SET final_status = 'DRIFT_BLOCKED', last_updated = CURRENT_TIMESTAMP "

                    "WHERE audit_id = ? AND final_status = 'ATTESTED'",

                    (audit_id,),

                )

        logger.warning(

            "3_STRIKE_LOCKED: audit_id=%s ticker=%s re_validation_count=%d source=row",

            audit_id, ticker, row["re_validation_count"],

        )

        _dispatched_audits.discard(audit_id)

        try:

            await query.edit_message_text(

                f"\u274c DRIFT_BLOCKED: {ticker} locked after 3 JIT failures.\n"

                f"Re-stage from Cure Console after 5-min cooldown."

            )

        except Exception:

            pass

        return



    # Step 2: Ticker rolling-window lockout (WARTIME bypasses per ADR-004 §4)

    with closing(_get_db_connection()) as conn:

        if not is_wartime and is_ticker_locked(conn, ticker):

            logger.warning(

                "3_STRIKE_LOCKED: audit_id=%s ticker=%s source=rolling",

                audit_id, ticker,

            )

            try:

                await query.edit_message_text(

                    f"\u274c {ticker} locked (5-min cooldown from recent DRIFT_BLOCKED).\n"

                    f"Wait for cooldown to expire, then re-stage."

                )

            except Exception:

                pass

            return



    # Sprint 1D: Trust-tier cooldown (T0=10s, T1=5s, T2=0s)

    cooldown = _get_cooldown_seconds()

    if cooldown > 0:

        if audit_id in _cooldown_tasks:

            try:

                await query.answer(f"Cooldown active: {cooldown}s", show_alert=False)

            except Exception:

                pass

            return



        current_task = asyncio.current_task()

        _cooldown_tasks[audit_id] = current_task

        try:

            await query.edit_message_text(

                f"\u23f3 Arming {ticker} in {cooldown}s\u2026\n"

                f"audit_id: {audit_id[:8]}\u2026",

            )

            await asyncio.sleep(cooldown)

        except asyncio.CancelledError:

            _cooldown_tasks.pop(audit_id, None)

            try:

                await query.edit_message_text("\u274c Aborted during cooldown")

            except Exception:

                pass  # Edit may fail if message already modified

            return  # Row stays ATTESTED, operator can retap TRANSMIT

        finally:

            _cooldown_tasks.pop(audit_id, None)



        with closing(_get_db_connection()) as conn:

            recheck = conn.execute(

                "SELECT final_status FROM bucket3_dynamic_exit_log WHERE audit_id = ?",

                (audit_id,),

            ).fetchone()

        if not recheck or recheck["final_status"] != "ATTESTED":

            try:

                await query.edit_message_text(

                    f"\u274c Aborted during cooldown — row no longer ATTESTED.\n"

                    f"audit_id: {audit_id[:8]}\u2026"

                )

            except Exception:

                pass

            return



    # Step 3: IBKR connection

    try:

        ib_conn = await ensure_ib_connected()

    except Exception as ib_exc:

        logger.warning("IBKR_CONNECT_FAIL: audit_id=%s error=%s", audit_id, ib_exc)

        try:

            await query.edit_message_text(

                f"\u274c IBKR connection failed: {ib_exc}\n"

                f"Check Gateway/TWS, then try again.",

                reply_markup=_original_markup,

            )

        except Exception:

            pass

        return



    # Step 4: Live bid fetch (CC → option bid, STK_SELL → stock spot)

    action_type = row["action_type"]

    try:

        if action_type == "CC":

            live_bid = await _ibkr_get_option_bid(

                ticker, row["strike"], row["expiry"],

            )

        else:

            live_bid = await _ibkr_get_spot(ticker)

    except Exception as bid_exc:

        logger.warning(

            "LIVE_BID_FETCH_FAIL: audit_id=%s ticker=%s error=%s",

            audit_id, ticker, bid_exc,

        )

        try:

            await query.edit_message_text(

                f"\u274c Failed to fetch live {'bid' if action_type == 'CC' else 'price'} "

                f"for {ticker}: {bid_exc}",

                reply_markup=_original_markup,

            )

        except Exception:

            pass

        return



    attested_limit = row["limit_price"]



    # Step 5a: Gate 1 re-eval (CC only — STK_SELL skips per Gemini F8)

    if action_type == "CC":

        adjusted_cost_basis = (

            row["strike"] + row["limit_price"] - row["walk_away_pnl_per_share"]

        )

        g1 = evaluate_gate_1(

            ticker=ticker,

            household=row["household"],

            candidate_strike=row["strike"],

            candidate_premium=live_bid,

            contracts=row["contracts"],

            adjusted_cost_basis=adjusted_cost_basis,

            conviction_tier=ConvictionTier(row["gate1_conviction_tier"]),

            tax_liability_override=0.0,

        )

        if not g1.passed:

            _increment_revalidation_count(audit_id)

            logger.warning(

                "GATE1_JIT_FAIL: audit_id=%s ticker=%s live_bid=%.4f "

                "ratio=%.4f passed=%s",

                audit_id, ticker, live_bid, g1.ratio, g1.passed,

            )

            try:

                await query.edit_message_text(

                    f"\u274c Gate 1 FAILED at live bid ${live_bid:.2f}\n"

                    f"Ratio: {g1.ratio:.2f}x (need >1.0x)\n"

                    f"Re-stage from Cure Console if desired.",

                    reply_markup=_original_markup,

                )

            except Exception:

                pass

            return



    # Step 5b: Drift check (R1 — $0.10 absolute for CC, 0.5% relative for STK_SELL)

    drift = abs(live_bid - attested_limit)

    drift_threshold = 0.10 if action_type == "CC" else attested_limit * 0.005

    if drift > drift_threshold:

        _increment_revalidation_count(audit_id)

        logger.warning(

            "DRIFT_BLOCK: audit_id=%s ticker=%s live_bid=%.4f "

            "attested_limit=%.4f delta=%.4f threshold=%.4f",

            audit_id, ticker, live_bid, attested_limit, drift, drift_threshold,

        )

        try:

            await query.edit_message_text(

                f"\u274c Price drifted ${drift:.2f} (limit ${drift_threshold:.2f}).\n"

                f"Attested: ${attested_limit:.2f} \u2192 Live: ${live_bid:.2f}\n"

                f"Re-stage from Cure Console.",

                reply_markup=_original_markup,

            )

        except Exception:

            pass

        return



    # Step 6: Atomic ATTESTED → TRANSMITTING lock

    now_ts = _time_mod.time()

    with closing(_get_db_connection()) as conn:

        with tx_immediate(conn):

            lock_result = conn.execute(

                "UPDATE bucket3_dynamic_exit_log "

                "SET final_status = 'TRANSMITTING', last_updated = CURRENT_TIMESTAMP "

                "WHERE audit_id = ? AND final_status = 'ATTESTED'",

                (audit_id,),

            )

        if lock_result.rowcount == 0:

            logger.warning(

                "TRANSMIT_RACE_LOST: audit_id=%s expected_status=ATTESTED",

                audit_id,

            )

            try:

                await query.edit_message_text(

                    f"\u274c Race: row already claimed by another process.\naudit_id: {audit_id[:8]}..."

                )

            except Exception:

                pass

            return



    # Step 7: Place order via IBKR

    try:

        expiry_fmt = (row["expiry"] or "").replace("-", "")

        if action_type == "CC":

            contract = ib_async.Option(

                symbol=ticker,

                lastTradeDateOrContractMonth=expiry_fmt,

                strike=row["strike"],

                right="C",

                exchange="SMART",

            )

            qty = row["contracts"]

        else:

            contract = ib_async.Stock(

                symbol=ticker, exchange="SMART", currency="USD",

            )

            qty = row["shares"]



        # Followup #20: route to originating account (fail-closed on NULL)

        account_id = row["originating_account_id"]

        limit_for_order = _round_to_nickel(row['limit_price']) if action_type == "CC" else row['limit_price']

        order = build_adaptive_sell_order(qty, limit_for_order, account_id)

        order.orderRef = audit_id  # Followup #17: cryptographic 1:1 link for orphan recovery



        # Sprint 1A: unified pre-trade gate (defense-in-depth)

        gate_ok, gate_reason = await _pre_trade_gates(

            order, contract,

            {"site": "dex", "audit_id": audit_id, "household": row["household"]},

        )

        if not gate_ok:

            logger.error("DEX TRANSMIT blocked: audit_id=%s reason=%s", audit_id, gate_reason)

            _revert_transmitting_to_cancelled(audit_id, f"gate_blocked: {gate_reason}")

            try:

                await query.edit_message_text(

                    f"\U0001f6ab GATE BLOCKED: {ticker} {audit_id[:8]}...\n{gate_reason}",

                )

            except Exception:

                pass

            _dispatched_audits.discard(audit_id)

            return



        assert_execution_enabled(in_process_halted=_HALTED)

        trade = ib_conn.placeOrder(contract, order)

        ib_order_id = trade.order.orderId if trade else 0

    except ExecutionDisabledError as exd:

        logger.error("EXECUTION BLOCKED at placeOrder (dex): %s", exd)

        _revert_transmitting_to_cancelled(audit_id, f"execution_disabled: {exd}")

        try:

            await query.edit_message_text(f"\U0001f6d1 EXECUTION BLOCKED: {exd}")

        except Exception:

            pass

        await _alert_telegram(f"\U0001f6d1 Execution blocked (dex {ticker}): {exd}")

        _dispatched_audits.discard(audit_id)

        return

    except Exception as ib_err:

        # TRANSMIT_IB_ERROR: leave in TRANSMITTING, alert operator, NO auto-revert

        logger.exception(

            "TRANSMIT_IB_ERROR: audit_id=%s ticker=%s error=%s",

            audit_id, ticker, ib_err,

        )

        try:

            await query.edit_message_text(

                f"\u26a0\ufe0f IBKR order FAILED: {ib_err}\n"

                f"Row left in TRANSMITTING for manual recovery.\n"

                f"audit_id: {audit_id[:8]}..."

            )

        except Exception:

            pass

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=(

                    f"\U0001f534 TRANSMIT_IB_ERROR: {ticker}\n"

                    f"audit_id: {audit_id}\n"

                    f"Row stuck in TRANSMITTING. Manual intervention required.\n"

                    f"Error: {ib_err}"

                ),

            )

        except Exception:

            pass

        return



    # Step 8: TRANSMITTING → TRANSMITTED (Followup #17: recovery wrapper per D7)

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                result = conn.execute(

                    "UPDATE bucket3_dynamic_exit_log "

                    "SET final_status = 'TRANSMITTED', transmitted = 1, "

                    "    transmitted_ts = ?, ib_order_id = ?, "

                    "    last_updated = CURRENT_TIMESTAMP "

                    "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                    (now_ts, ib_order_id, audit_id),

                )

                if result.rowcount == 0:

                    raise RuntimeError(

                        f"TRANSMIT_STEP8_CAS_LOST audit_id={audit_id}"

                    )

    except Exception as exc:

        logger.exception(

            "TRANSMIT_STEP8_FAILED: audit_id=%s ib_order_id=%s",

            audit_id, ib_order_id,

        )

        _dispatched_audits.discard(audit_id)

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=(

                    "\U0001f6a8 TRANSMIT RECOVERY REQUIRED\n"

                    f"{ticker} {audit_id[:8]}... may be live at IBKR, "

                    f"but local TRANSMITTED write failed.\n"

                    f"ib_order_id={ib_order_id}\n"

                    f"error={exc}\n"

                    "Verify in TWS and use /recover_transmitting."

                ),

            )

        except Exception:

            pass

        try:

            await query.edit_message_text(

                f"\u26a0\ufe0f TRANSMIT RECOVERY REQUIRED: {ticker} may be live at "

                f"IBKR (ib_order_id={ib_order_id}) but local DB "

                f"write failed. Check /recover_transmitting."

            )

        except Exception:

            pass

        return



    _dispatched_audits.discard(audit_id)

    logger.info(

        "TRANSMIT_SUCCESS: audit_id=%s ticker=%s household=%s ib_order_id=%s",

        audit_id, ticker, row["household"], ib_order_id,

    )



    strike_label = f"${row['strike']:.0f}C" if action_type == "CC" else ""

    try:

        await query.edit_message_text(

            f"\u2705 TRANSMITTED: {ticker} {strike_label}\n"

            f"IB Order ID: {ib_order_id}\n"

            f"Live bid: ${live_bid:.2f} | Drift: ${drift:.2f}\n"

            f"audit_id: {audit_id[:8]}..."

        )

    except Exception:

        pass





# ---------------------------------------------------------------------------

# Sprint 1A: Unified pre-trade safety gates

# ---------------------------------------------------------------------------





async def _pre_trade_gates(

    order,

    contract,

    context: dict,

) -> tuple[bool, str]:

    """Unified pre-trade safety gates. Returns (allowed, reason).



    If allowed=False, caller MUST NOT call placeOrder and MUST alert

    operator with reason. Fail-closed: any exception → block.



    context keys:

        site: "dex" | "legacy_approve" | "v2_router" | "orders_match_mid"

        audit_id: str | None  (non-None triggers F20 NULL guard)

        household: str | None



    ADR-005: v2_router is the canonical Wartime defensive surface and

    is whitelisted alongside dex. BAG combos are permitted for V2

    STATE_3_DEFEND rolls. BTC and BAG notional use cash-paid semantics

    rather than strike-notional, since they extinguish obligations

    rather than create them.

    """

    try:

        site = context.get("site", "unknown")

        audit_id = context.get("audit_id")



        # Gate 0: Halt killswitch (Sprint 1D) — unchanged

        if _HALTED:

            return (False, "Desk halted via /halt — restart bot to resume")



        # Gate 0a: Circuit breaker (MR #1)

        try:

            from scripts.circuit_breaker import run_all_checks as _cb_run

            _cb = _cb_run()

            if _cb.get("halted"):

                viols = _cb.get("violations", [])

                reasons = "; ".join(

                    v.get("reason", v.get("check", "unknown")) for v in viols[:3]

                )

                return (False, f"Circuit breaker HALTED: {reasons}")

        except ImportError:

            logger.warning("_pre_trade_gates: circuit_breaker unavailable")

        except Exception as _cb_exc:

            logger.error("_pre_trade_gates: breaker failed: %s", _cb_exc)

            return (False, f"Circuit breaker internal error: {_cb_exc}")



        # Gate 1: Mode gate — WARTIME whitelist (ADR-005 R4)

        mode = _get_current_desk_mode()

        WARTIME_ALLOWED_SITES = ("dex", "v2_router", "legacy_approve")

        if mode == "WARTIME" and site not in WARTIME_ALLOWED_SITES:

            return (False, f"WARTIME blocks {site}; allowed: {WARTIME_ALLOWED_SITES}")



        # Gate 2: Notional ceiling ($25k cash exposure) — ADR-005 CC1

        sec_type = getattr(contract, "secType", None)

        qty = abs(getattr(order, "totalQuantity", 0) or 0)

        if qty <= 0:

            return (False, "Notional gate: zero quantity — fail-closed")



        order_action = str(getattr(order, "action", "") or "").upper()

        try:

            limit_price = float(getattr(order, "lmtPrice", 0) or 0)

        except (TypeError, ValueError):

            return (False, "Notional gate: non-numeric lmtPrice — fail-closed")



        if sec_type == "OPT":

            if order_action == "BUY":

                # BTC: cash exposure is premium paid, not strike-notional

                if limit_price < 0:

                    return (False, "Notional gate: OPT BUY with negative lmtPrice — fail-closed")

                notional = qty * limit_price * 100

            else:

                # SELL (CSP/CC): worst-case obligation is strike * 100

                strike = getattr(contract, "strike", None)

                if not strike or strike <= 0:

                    return (False, "Notional gate: OPT SELL with missing/zero strike — fail-closed")

                notional = qty * strike * 100

        elif sec_type == "STK":

            if not limit_price or limit_price <= 0:

                return (False, "Notional gate: STK with missing/zero lmtPrice — fail-closed")

            notional = qty * limit_price

        elif sec_type == "BAG":

            # ADR-005 CC1: BAG net debit/credit cash exposure.

            # Combo legs net out structurally; cap on actual cash movement.

            notional = qty * abs(limit_price) * 100

        else:

            return (False, f"Notional gate: unsupported secType {sec_type} — fail-closed")



        # (Notional ceiling removed 2026-04-16 — mode gates + margin math are real controls)



        # Gate 3: Non-wheel filter — OPT, STK, BAG (ADR-005 CC4)

        ALLOWED_SECTYPES = ("OPT", "STK", "BAG")

        if sec_type not in ALLOWED_SECTYPES:

            return (False, f"Non-wheel trade blocked (secType={sec_type}); use TWS directly")



        # Gate 4: F20 NULL guard — unchanged (DEX path only)

        if audit_id is not None:

            try:

                with closing(_get_db_connection()) as conn:

                    row = conn.execute(

                        "SELECT originating_account_id FROM bucket3_dynamic_exit_log "

                        "WHERE audit_id = ?",

                        (audit_id,),

                    ).fetchone()

                    if row and not row["originating_account_id"]:

                        with tx_immediate(conn):

                            conn.execute(

                                "UPDATE bucket3_dynamic_exit_log "

                                "SET final_status = 'CANCELLED', last_updated = CURRENT_TIMESTAMP "

                                "WHERE audit_id = ? AND final_status IN ('ATTESTED', 'TRANSMITTING')",

                                (audit_id,),

                            )

                        return (False, f"F20 NULL guard: row {audit_id[:8]}... has no originating_account_id")

            except Exception as f20_exc:

                logger.warning("F20 gate check failed: %s", f20_exc)

                return (False, f"F20 gate error: {f20_exc}")



        # Gate 5: All gates passed

        return (True, "")



    except Exception as exc:

        logger.exception("_pre_trade_gates failed: %s", exc)

        return (False, f"gate error: {exc}")





def _round_to_nickel(price: float) -> float:

    """Round OPT limit price for IBKR paper compatibility.



    Nickel ($0.05) for premiums <= $3.00, dime ($0.10) for > $3.00.

    Live mode: no-op. Stock prices should NOT use this helper.

    """

    if not PAPER_MODE:

        return price

    if price is None or price <= 0:

        return price

    increment = 0.10 if price > 3.00 else 0.05

    return round(round(price / increment) * increment, 2)







async def _place_single_order(

    payload: dict,

    db_id: int,

    cached_positions: list | None = None,

) -> tuple[bool, str]:

    """

    Place a single order with transmit=True from a pending_orders payload.

    Returns (success: bool, message: str).

    """

    ticker = payload.get("ticker", "???")

    strike = payload.get("strike", 0)

    expiry = payload.get("expiry", "")

    qty = payload.get("quantity", 0)

    bid = payload.get("limit_price", 0)

    acct_id = payload.get("account_id", "")

    label = ACCOUNT_LABELS.get(acct_id, acct_id)

    sec_type = payload.get("sec_type", "OPT")

    action = str(payload.get("action", "SELL") or "SELL").upper()

    right = str(payload.get("right", "C") or "C").upper()



    try:

        ib_conn = await ensure_ib_connected()



        expiry_fmt = expiry.replace("-", "")



        if sec_type == "OPT" and action == "SELL" and right == "C":

            # Safety check: prevent duplicate short calls

            positions = None

            try:

                positions = cached_positions

                if positions is None:

                    positions = await ib_conn.reqPositionsAsync()

                for pos in positions:

                    if (pos.account == acct_id

                            and pos.contract.symbol == ticker

                            and pos.contract.secType == "OPT"

                            and getattr(pos.contract, "right", "") == "C"

                            and pos.contract.strike == strike

                            and str(pos.contract.lastTradeDateOrContractMonth).replace("-", "") == expiry_fmt

                            and pos.position < 0):

                        # Already have this exact short call

                        with closing(_get_db_connection()) as conn:

                            with tx_immediate(conn):

                                conn.execute(

                                    "UPDATE pending_orders SET status = 'duplicate_skipped' WHERE id = ?",

                                    (db_id,),

                                )

                        return False, f"#{db_id} {ticker} ${strike:.0f}C {expiry} — duplicate, already held"

            except Exception as dup_exc:

                logger.warning("Duplicate check failed for #%d: %s (proceeding anyway)", db_id, dup_exc)



            # ── Verify account has uncovered capacity ──

            try:

                positions_list = cached_positions or positions or []

                acct_long_shares = 0

                acct_short_contracts = 0

                for pos in positions_list:

                    if pos.account != acct_id:

                        continue

                    if pos.contract.symbol.upper() != ticker:

                        continue

                    if pos.contract.secType == "STK" and pos.position > 0:

                        acct_long_shares += int(pos.position)

                    elif (pos.contract.secType == "OPT"

                          and getattr(pos.contract, "right", "") == "C"

                          and pos.position < 0):

                        acct_short_contracts += abs(int(pos.position))



                acct_uncovered = (acct_long_shares - acct_short_contracts * 100) // 100

                if qty > acct_uncovered:

                    with closing(_get_db_connection()) as conn:

                        with tx_immediate(conn):

                            conn.execute(

                                "UPDATE pending_orders SET status = 'rejected_naked' WHERE id = ?",

                                (db_id,),

                            )

                    return False, (

                        f"#{db_id} {ticker} ${strike:.0f}C: REJECTED — "

                        f"account {label} has {acct_uncovered}c uncovered capacity "

                        f"but order needs {qty}c. Would create naked short."

                    )

            except Exception as cap_exc:

                logger.warning(

                    "Capacity check failed for #%d (proceeding with caution): %s",

                    db_id, cap_exc,

                )



        if sec_type == "BAG":

            contract = ib_async.Contract(

                symbol=ticker, secType="BAG",

                exchange="SMART", currency="USD",

            )

            combo_legs = []

            for leg_data in payload.get("combo_legs", []):

                combo_legs.append(ib_async.ComboLeg(

                    conId=leg_data["conId"],

                    ratio=leg_data["ratio"],

                    action=leg_data["action"],

                    exchange=leg_data["exchange"],

                ))

            contract.comboLegs = combo_legs

            _short_exp = payload.get("short_expiry") or payload.get("expiry", "")

            _roll_urgency = "patient"

            if _short_exp and len(_short_exp) == 8:

                try:

                    _expiry_dt = _datetime(

                        int(_short_exp[:4]), int(_short_exp[4:6]), int(_short_exp[6:8]),

                        20, 0, tzinfo=_timezone.utc,

                    )

                    _roll_urgency = decide_roll_urgency(_expiry_dt)

                except Exception:

                    _roll_urgency = "urgent"

            order = build_adaptive_roll_combo(qty, float(bid), acct_id, urgency=_roll_urgency)

        elif sec_type == "OPT":

            contract = ib_async.Option(

                symbol=ticker,

                lastTradeDateOrContractMonth=expiry_fmt,

                strike=strike,

                right=right,

                exchange="SMART",

            )

            order = build_adaptive_option_order(

                action=action,

                qty=qty,

                limit_price=_round_to_nickel(float(bid)),

                account_id=acct_id,

                urgency=payload.get("urgency", "patient"),

            )

        elif sec_type == "STK":

            # MR !71: STK support for LIQUIDATE STC shares leg.

            # MKT orders ignore limit_price; LMT respects it.

            contract = ib_async.Stock(

                symbol=ticker,

                exchange="SMART",

                currency="USD",

            )

            _stk_urgency = payload.get("urgency", "patient")

            order_type = str(payload.get("order_type", "MKT") or "MKT").upper()

            if order_type == "MKT":

                order = build_adaptive_stk_order(action, qty, order_type="MKT", urgency=_stk_urgency)

            else:

                order = build_adaptive_stk_order(

                    action, qty,

                    order_type="LMT",

                    limit_price=_round_to_nickel(float(bid)) if bid else 0.0,

                    urgency=_stk_urgency,

                )

            order.account = acct_id

            order.transmit = True

        else:

            return False, f"#{db_id} Unsupported sec_type {sec_type}"



        # Sprint 1A: unified pre-trade gate.

        # ADR-005: V2 router payloads carry origin="v2_router" so they

        # route through the WARTIME-whitelisted v2_router site.

        try:

            payload_origin = str(payload.get("origin") or "legacy_approve")

        except Exception:

            payload_origin = "legacy_approve"

        gate_site = "v2_router" if payload_origin == "v2_router" else "legacy_approve"



        gate_ok, gate_reason = await _pre_trade_gates(

            order, contract,

            {"site": gate_site, "audit_id": None,

             "household": ACCOUNT_TO_HOUSEHOLD.get(acct_id)},

        )

        if not gate_ok:

            logger.warning(

                "%s blocked: #%d %s — %s",

                gate_site, db_id, ticker, gate_reason,

            )

            return False, f"#{db_id} {ticker} — gate blocked: {gate_reason}"



        # Warn if placing outside market hours — DAY orders won't execute until next session

        now_et = _datetime.now(ET)

        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)

        is_weekend = now_et.weekday() >= 5

        if is_weekend or now_et < market_open or now_et > market_close:

            logger.warning(

                "Order #%d placed outside market hours — Adaptive DAY order "

                "will activate at next session open and expire at close if unfilled",

                db_id,

            )



        assert_execution_enabled(in_process_halted=_HALTED)

        trade = ib_conn.placeOrder(contract, order)

        ib_order_id = trade.order.orderId if trade else 0

        ib_perm_id = trade.order.permId if trade else 0



        # R5: Update status to SENT and store IBKR IDs for event matching

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                from agt_equities.order_state import append_status

                conn.execute(

                    "UPDATE pending_orders SET ib_order_id = ?, ib_perm_id = ? WHERE id = ?",

                    (ib_order_id, ib_perm_id, db_id),

                )

                append_status(conn, db_id, 'sent', 'placeOrder', {

                    'ib_order_id': str(ib_order_id),

                    'ib_perm_id': str(ib_perm_id),

                })

                # Sprint B3: force-clear cutover -- seed pending_order_children

                # with the single child row this 1:1 placement represents.

                # Kill switch: AGT_B3_CHILDREN_WRITER=0. Writer-only; nothing

                # reads this table yet (CSP Allocator B5 is the first reader).

                try:

                    from agt_equities.order_state import (

                        children_writer_enabled,

                        insert_pending_order_child,

                    )

                    if children_writer_enabled() and acct_id:

                        insert_pending_order_child(

                            conn,

                            parent_order_id=db_id,

                            account_id=acct_id,

                            status='sent',

                            child_ib_order_id=ib_order_id,

                            child_ib_perm_id=ib_perm_id,

                        )

                except Exception as b3_exc:

                    logger.warning(

                        "B3 child-row insert failed for #%d: %s (non-fatal)",

                        db_id, b3_exc,

                    )



        # Add to roll watchlist ONLY for Mode 1 defensive CCs.

        # Mode 2 (welcome assignment, tax-exempt gain) and Dynamic Exit

        # (engineered assignment) do not need roll monitoring.

        mode = payload.get("mode", "")

        if mode == "MODE_1_DEFENSIVE":

            try:

                with closing(_get_db_connection()) as conn:

                    with tx_immediate(conn):

                        conn.execute(

                            """

                            INSERT INTO roll_watchlist

                                (order_id, ticker, account_id, strike, expiry, quantity, mode)

                            VALUES (?, ?, ?, ?, ?, ?, ?)

                            """,

                            (db_id, ticker, acct_id, strike, expiry, qty, mode),

                        )

            except Exception as rw_exc:

                logger.warning("roll_watchlist insert failed for #%d: %s", db_id, rw_exc)

        else:

            logger.info(

                "Order #%d (%s) not added to roll_watchlist — only Mode 1 monitored",

                db_id, mode,

            )



        if sec_type == "BAG":

            debit_credit = "Credit" if float(bid) < 0 else "Debit"

            msg = (

                f"#{db_id} {ticker} BAG x{qty} "

                f"@ ${abs(float(bid)):.2f} {debit_credit} "

                f"\u2192 {label} (IB#{ib_order_id})"

            )

        elif sec_type == "STK":

            # MR !71: STK (e.g. LIQUIDATE STC shares leg)

            msg = (

                f"#{db_id} {ticker} {action} {qty}sh STK "

                f"\u2192 {label} (IB#{ib_order_id})"

            )

        else:

            msg = (

                f"#{db_id} {ticker} {action} {qty}x "

                f"${strike:.0f}{right} {expiry} @ ${float(bid):.2f} "

                f"\u2192 {label} (IB#{ib_order_id})"

            )

        logger.info("Order placed: %s", msg)

        return True, msg



    except Exception as exc:

        logger.exception("_place_single_order failed for #%d", db_id)

        # R5: Mark as failed via state machine

        try:

            with closing(_get_db_connection()) as conn:

                with tx_immediate(conn):

                    from agt_equities.order_state import append_status

                    append_status(conn, db_id, 'failed', 'placeOrder_exception', {

                        'error': str(exc)[:200],

                    })

        except Exception:

            pass

        return False, f"#{db_id} {ticker}: {exc}"





# ---------------------------------------------------------------------------

# /reject — reject all staged orders without placing

# ---------------------------------------------------------------------------



async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """Reject all staged orders without placing them."""

    if not is_authorized(update):

        return

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                result = conn.execute(

                    "UPDATE pending_orders SET status = 'rejected' WHERE status = 'staged'"

                )

                count = result.rowcount



        if count > 0:

            await update.message.reply_text(f"\u274c Rejected {count} staged orders.")

        else:

            await update.message.reply_text("No staged orders to reject.")



    except Exception as exc:

        logger.exception("cmd_reject failed")

        try:

            await update.message.reply_text(f"Reject failed: {exc}")

        except Exception:

            pass





# ---------------------------------------------------------------------------

# /dashboard — Performance Card + Active Positions Grid

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

# /status_orders — quick view of pending_orders counts by status

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

# /rollcheck — Active Defense Status (Live Short Call Delta Tracker)

# ---------------------------------------------------------------------------



async def _get_active_defense_status(ib_conn) -> str:

    """Generates a health report of all active short calls vs the 0.40 Delta trigger."""

    from datetime import date

    import math



    lines = []

    try:

        positions = await ib_conn.reqPositionsAsync()

        short_calls = [

            p for p in positions

            if p.position < 0

            and getattr(p.contract, "secType", "") == "OPT"

            and getattr(p.contract, "right", "") == "C"

            and p.contract.symbol.upper() not in EXCLUDED_TICKERS

        ]



        if not short_calls:

            return "No active short calls."



        ib_conn.reqMarketDataType(4)



        for pos in short_calls:

            ticker = pos.contract.symbol.upper()

            strike = pos.contract.strike



            exp_fmt = str(pos.contract.lastTradeDateOrContractMonth)

            try:

                exp_date = date(int(exp_fmt[:4]), int(exp_fmt[4:6]), int(exp_fmt[6:8]))

                dte = (exp_date - date.today()).days

            except (ValueError, TypeError):

                dte = "?"



            qual_contracts = await ib_conn.qualifyContractsAsync(pos.contract)

            if not qual_contracts:

                continue



            ticker_data = ib_conn.reqMktData(qual_contracts[0], "106", False, False)

            await asyncio.sleep(2)



            delta = None

            if getattr(ticker_data, "modelGreeks", None):

                delta = ticker_data.modelGreeks.delta

            elif getattr(ticker_data, "bidGreeks", None):

                delta = ticker_data.bidGreeks.delta



            ib_conn.cancelMktData(qual_contracts[0])



            if delta is None or math.isnan(delta):

                lines.append(f"<b>{ticker}</b> -{abs(pos.position)}c ${strike:.0f}C {exp_fmt} ({dte}d)\n  Current Delta: N/A (Data unavailable)")

                continue



            abs_delta = abs(float(delta))

            distance = 0.40 - abs_delta



            if abs_delta >= 0.40:

                status_str = "🚨 ROLL TRIGGERED (>= 0.40)"

            elif abs_delta >= 0.30:

                status_str = f"⚠️ ELEVATED RISK ({distance:.2f} away from trigger)"

            else:

                status_str = f"✅ Safe ({distance:.2f} away from trigger)"



            lines.append(f"<b>{ticker}</b> -{abs(pos.position)}c ${strike:.0f}C {exp_fmt} ({dte}d)\n  Current Delta: {abs_delta:.2f} | {status_str}")



    except Exception as exc:

        logger.warning("Active defense status check failed: %s", exc)

        return f"Error fetching defense status: {exc}"



    if not lines:

        return "No active short calls evaluated."



    return "\n".join(lines)





async def cmd_rollcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/rollcheck — manually trigger the V2 5-State router for live short calls."""

    if not is_authorized(update): return



    status_msg = await update.message.reply_text("⏳ Running the V2 router across live short calls...")

    try:

        ib_conn = await ensure_ib_connected()



        async def _rollcheck_priority_cb(kind: str, payload: dict) -> None:

            # WHEEL-7 pager — out-of-band from the bundled V2 Router alerts below.

            await _page_critical_event(context.bot, kind, payload)



        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
        _rollcheck_ctx = RunContext(
            mode=RunMode.LIVE,
            run_id=uuid.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),
            decision_sink=NullDecisionSink(),
        )
        alerts = await roll_scanner.scan_and_stage_defensive_rolls(
            ib_conn,
            ctx=_rollcheck_ctx,
            priority_cb=_rollcheck_priority_cb,
            ibkr_get_spot=_ibkr_get_spot,
            load_premium_ledger=_load_premium_ledger_snapshot,
            get_desk_mode=_get_current_desk_mode,
            ibkr_get_expirations=_ibkr_get_expirations,
            ibkr_get_chain=_ibkr_get_chain,
            account_labels=ACCOUNT_LABELS,
            is_halted=_HALTED,
        )



        body = "\n\n".join(alerts) if alerts else "No V2 router actions triggered."

        msg = "━━ V2 Router Alerts ━━\n\n" + body

        await status_msg.edit_text(f"<pre>{html.escape(msg)}</pre>", parse_mode="HTML")

    except Exception as exc:

        logger.exception("cmd_rollcheck failed")

        await status_msg.edit_text(f"❌ Failed to run roll check: {exc}")





# ---------------------------------------------------------------------------

# /csp_harvest — manual CSP profit-take sweep (M2, 2026-04-11)

# ---------------------------------------------------------------------------



async def cmd_csp_harvest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/csp_harvest — scan open short puts and stage BTC tickets when

    profit-capture thresholds are hit. Mirrors the V2 router STATE_2

    HARVEST flow for puts instead of calls. Tickets land in

    pending_tickets via append_pending_tickets for normal /approve flow.

    """

    if not is_authorized(update):

        return



    from agt_equities.csp_harvest import scan_csp_harvest_candidates
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink



    status_msg = await update.message.reply_text(

        "\u23f3 Scanning short puts for profit-take harvest..."

    )

    try:

        ib_conn = await ensure_ib_connected()



        import uuid
        ctx = RunContext(
            mode=RunMode.LIVE,
            run_id=uuid.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),
            decision_sink=NullDecisionSink(),
        )
        result = await scan_csp_harvest_candidates(ib_conn, ctx=ctx)



        staged = result.get("staged", [])

        skipped = result.get("skipped", [])

        errors = result.get("errors", [])

        alerts = result.get("alerts", [])



        lines = ["\u2501\u2501 CSP Harvest \u2501\u2501"]

        lines.append(

            f"Staged: {len(staged)} | Skipped: {len(skipped)} | Errors: {len(errors)}"

        )

        if alerts:

            lines.append("")

            lines.extend(alerts)

        if not staged and not alerts:

            lines.append("No positions met harvest thresholds.")



        msg = "\n".join(lines)

        await status_msg.edit_text(

            f"<pre>{html.escape(msg)}</pre>", parse_mode="HTML"

        )

    except Exception as exc:

        logger.exception("cmd_csp_harvest failed")

        await status_msg.edit_text(f"\u274c CSP harvest failed: {exc}")





# ---------------------------------------------------------------------------

# /daily — unified System 1 scan (CC + roll + CSP harvest in one pass)

# ---------------------------------------------------------------------------



async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/daily — one-pass scan across all open positions.



    Runs three engines in sequence:

      1. CC staging (new covered call writes via cc_engine)

      2. Roll check (open short calls via roll_engine.evaluate)

      3. CSP harvest (open short puts profit-take)



    Paper (PAPER_MODE + PAPER_AUTO_EXECUTE): auto-executes staged rows

    immediately after the 3-engine pass via _auto_execute_staged. No

    /approve gate — paper's job is to exercise bot → IBKR end-to-end.



    Live: everything lands in the normal /approve queue.

    """

    if not is_authorized(update):

        return



    status_msg = await update.message.reply_text(

        "\u23f3 Running daily scan (CC + rolls + CSP harvest)..."

    )

    sections: list[str] = ["\u2501\u2501 Daily Scan \u2501\u2501"]



    # MR #1: Circuit breaker pre-flight

    try:

        from scripts.circuit_breaker import run_all_checks as _cb_run

        _cb = _cb_run()

        if _cb.get("halted"):

            viols = _cb.get("violations", [])

            reasons = "; ".join(v.get("reason", v.get("check", "?")) for v in viols[:3])

            await status_msg.edit_text(

                f"\u274c Circuit breaker HALTED \u2014 daily scan blocked.\n{reasons}"

            )

            return

    except ImportError:

        logger.warning("cmd_daily: circuit_breaker unavailable \u2014 proceeding")

    except Exception as _cb_exc:

        await status_msg.edit_text(

            f"\u274c Circuit breaker internal error: {_cb_exc} \u2014 scan aborted."

        )

        return



    try:

        ib_conn = await ensure_ib_connected()

    except Exception as exc:

        logger.exception("cmd_daily: IB connect failed")

        await status_msg.edit_text(f"\u274c Daily scan failed: IB connect error: {exc}")

        return



    # --- 1. CC staging ---

    try:

        _cc_ctx = RunContext(

            mode=RunMode.LIVE,

            run_id=uuid.uuid4().hex,

            order_sink=CollectorOrderSink(),

            decision_sink=SQLiteDecisionSink(_log_cc_cycle, _write_dynamic_exit_rows),

        )

        cc_result = await _run_cc_logic(None, ctx=_cc_ctx)

        cc_text = cc_result.get("main_text", "No CC output.")

        # Trim to first 15 lines for digest

        cc_lines = cc_text.strip().splitlines()

        if len(cc_lines) > 15:

            cc_summary = "\n".join(cc_lines[:15]) + f"\n... ({len(cc_lines) - 15} more lines)"

        else:

            cc_summary = "\n".join(cc_lines)

        sections.append(f"\n\u25b6 COVERED CALLS\n{cc_summary}")

    except Exception as exc:

        logger.exception("cmd_daily: CC scan failed")

        sections.append(f"\n\u25b6 COVERED CALLS\n\u274c Error: {exc}")



    # --- 2. Roll check ---

    try:

        async def _daily_priority_cb(kind: str, payload: dict) -> None:

            await _page_critical_event(context.bot, kind, payload)



        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
        _daily_roll_ctx = RunContext(
            mode=RunMode.LIVE,
            run_id=uuid.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),
            decision_sink=NullDecisionSink(),
        )
        roll_alerts = await roll_scanner.scan_and_stage_defensive_rolls(
            ib_conn,
            ctx=_daily_roll_ctx,
            priority_cb=_daily_priority_cb,
            ibkr_get_spot=_ibkr_get_spot,
            load_premium_ledger=_load_premium_ledger_snapshot,
            get_desk_mode=_get_current_desk_mode,
            ibkr_get_expirations=_ibkr_get_expirations,
            ibkr_get_chain=_ibkr_get_chain,
            account_labels=ACCOUNT_LABELS,
            is_halted=_HALTED,
        )

        if roll_alerts:

            roll_text = "\n".join(roll_alerts)

        else:

            roll_text = "No roll actions triggered."

        sections.append(f"\n\u25b6 ROLL CHECK\n{roll_text}")

    except Exception as exc:

        logger.exception("cmd_daily: roll check failed")

        sections.append(f"\n\u25b6 ROLL CHECK\n\u274c Error: {exc}")



    # --- 3. CSP harvest ---

    try:

        from agt_equities.csp_harvest import scan_csp_harvest_candidates
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink



        import uuid
        ctx = RunContext(
            mode=RunMode.LIVE,
            run_id=uuid.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),
            decision_sink=NullDecisionSink(),
        )
        harvest_result = await scan_csp_harvest_candidates(ib_conn, ctx=ctx)

        staged = harvest_result.get("staged", [])

        harvest_alerts = harvest_result.get("alerts", [])

        if staged or harvest_alerts:

            harvest_text = f"Staged: {len(staged)}"

            if harvest_alerts:

                harvest_text += "\n" + "\n".join(harvest_alerts)

        else:

            harvest_text = "No CSP positions met harvest thresholds."

        sections.append(f"\n\u25b6 CSP HARVEST\n{harvest_text}")

    except Exception as exc:

        logger.exception("cmd_daily: CSP harvest failed")

        sections.append(f"\n\u25b6 CSP HARVEST\n\u274c Error: {exc}")



    # --- 4. Paper autopilot: auto-execute staged rows (no /approve gate) ---

    if PAPER_MODE and PAPER_AUTO_EXECUTE:

        try:

            ap_placed, ap_failed, ap_lines, ap_status = await _auto_execute_staged(ib_conn)

            if ap_status == "none":

                ap_text = "No orders staged for auto-execute."

            elif ap_status == "race":

                ap_text = "Another sweeper claimed the orders first."

            elif ap_status == "ib_fail":

                ap_text = ap_lines[0] if ap_lines else "IB connection failed."

            else:

                ap_text = f"Placed: {ap_placed} | Failed: {ap_failed}"

                if ap_lines:

                    trimmed = ap_lines[:10]

                    ap_text += "\n" + "\n".join(trimmed)

                    if len(ap_lines) > 10:

                        ap_text += f"\n... ({len(ap_lines) - 10} more)"

            sections.append(f"\n\u25b6 AUTO-EXECUTE\n{ap_text}")

        except Exception as exc:

            logger.exception("cmd_daily: paper auto-execute failed")

            sections.append(f"\n\u25b6 AUTO-EXECUTE\n\u274c Error: {exc}")



    # --- Digest ---

    msg = "\n".join(sections)

    try:

        await status_msg.edit_text(

            f"<pre>{html.escape(msg)}</pre>", parse_mode="HTML",

        )

    except Exception:

        # Message too long — split

        for i in range(0, len(msg), 4000):

            chunk = msg[i:i + 4000]

            if i == 0:

                await status_msg.edit_text(

                    f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML",

                )

            else:

                await update.message.reply_text(

                    f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML",

                )





# ---------------------------------------------------------------------------

# /report — full autonomous pipeline status report (on-demand)

# ---------------------------------------------------------------------------



async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/report — Comprehensive pipeline status for Yash to triage in Cowork.



    Pulls from: circuit breaker, readiness gate, session logs, active cycles,

    pending orders, IB connectivity, and the weekly directive.

    Pure read-only — no side effects.

    """

    if not is_authorized(update):

        return



    status_msg = await update.message.reply_text(

        "Compiling full pipeline report..."

    )

    sections: list[str] = []



    # ── 1. Header ──

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    ET = _tz(_td(hours=-4))  # EDT

    now_et = _dt.now(ET)

    sections.append(

        f"AGT EQUITIES — PIPELINE REPORT\n"

        f"Generated: {now_et.strftime('%Y-%m-%d %H:%M ET')}\n"

        f"{'=' * 40}"

    )



    # ── 2. Circuit Breaker Status ──

    try:

        import subprocess, sys as _sys

        from pathlib import Path as _Path

        cb_path = _Path(__file__).parent / "scripts" / "circuit_breaker.py"

        if cb_path.exists():

            result = subprocess.run(

                [_sys.executable, str(cb_path)],

                capture_output=True, text=True, timeout=15,

                cwd=str(_Path(__file__).parent),

            )

            # raw_decode consumes the first JSON value from stdout, ignoring

            # any trailing human-readable status lines printed by __main__.

            if result.stdout:

                try:

                    cb_data, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())

                except json.JSONDecodeError:

                    cb_data = {}

            else:

                cb_data = {}

            if cb_data.get("halted"):

                cb_status = "HALTED — CIRCUIT BREAKER TRIPPED"

            elif not cb_data.get("ok"):

                viols = cb_data.get("violations", [])

                cb_status = f"VIOLATIONS ({len(viols)}): " + "; ".join(

                    v.get("reason", "unknown") for v in viols

                )

            else:

                cb_status = "ALL CLEAR"

            warns = cb_data.get("warnings", [])

            warn_str = ""

            if warns:

                warn_str = "\n  Warnings: " + "; ".join(

                    w.get("warning", "") for w in warns

                )

            sections.append(f"\n[CIRCUIT BREAKER] {cb_status}{warn_str}")



            # Extract individual check details

            checks = cb_data.get("checks", {})

            if checks:

                for name, detail in checks.items():

                    status_icon = "OK" if detail.get("ok") else "FAIL"

                    extra = ""

                    if name == "daily_orders" and "count" in detail:

                        extra = f" ({detail['count']}/{detail['limit']})"

                    elif name == "daily_notional" and "notional" in detail:

                        extra = f" (${detail['notional']:,.0f}/${detail['limit']:,.0f})"

                    elif name == "directive" and detail.get("has_directive"):

                        age = detail.get("age_days", "?")

                        stale = " STALE" if detail.get("stale") else ""

                        extra = f" ({age}d old{stale})"

                    sections.append(f"  {status_icon:>4} {name}{extra}")

        else:

            sections.append("\n[CIRCUIT BREAKER] scripts/circuit_breaker.py not found")

    except Exception as exc:

        sections.append(f"\n[CIRCUIT BREAKER] Error: {exc}")



    # ── 3. IB Gateway Status ──

    try:

        global ib

        if ib is not None and ib.isConnected():

            accts = ib.managedAccounts()

            acct_str = ", ".join(accts) if accts else "none"

            cid = ib.client.clientId if ib.client else "?"

            sections.append(f"\n[IB GATEWAY] Connected (clientId={cid})\n  Accounts: {acct_str}")

        else:

            sections.append("\n[IB GATEWAY] DISCONNECTED")

    except Exception as exc:

        sections.append(f"\n[IB GATEWAY] Error: {exc}")



    # ── 4. Readiness Gate ──

    try:

        with closing(_get_db_connection()) as conn:

            conn.row_factory = sqlite3.Row

            rows = conn.execute(

                "SELECT segment, status, last_tested, evidence "

                "FROM readiness_gate ORDER BY id"

            ).fetchall()

        if rows:

            validated = sum(1 for r in rows if r["status"] == "validated")

            total = len(rows)

            sections.append(f"\n[READINESS GATE] {validated}/{total}")

            for r in rows:

                icon = "OK" if r["status"] == "validated" else "  "

                tested = r["last_tested"] or "never"

                sections.append(f"  [{icon:>2}] {r['segment']:<25} {tested}")

        else:

            sections.append("\n[READINESS GATE] No segments configured")

    except Exception as exc:

        sections.append(f"\n[READINESS GATE] Error: {exc}")



    # ── 5. Active Cycles (position summary) ──

    # MR !67: cycles is list[walker.Cycle] (dataclass), not dict. Use attr access.

    try:

        from agt_equities import trade_repo

        cycles = trade_repo.get_active_cycles()

        hh_summary: dict[str, dict] = {}

        for c in cycles:

            hh = c.household_id or "unknown"

            if hh not in hh_summary:

                hh_summary[hh] = {"short_puts": 0, "short_calls": 0, "shares": 0.0, "tickers": set()}

            hh_summary[hh]["tickers"].add(c.ticker or "?")

            hh_summary[hh]["short_puts"] += int(c.open_short_puts or 0)

            hh_summary[hh]["short_calls"] += int(c.open_short_calls or 0)

            shares = float(c.shares_held or 0)

            if shares > 0:

                hh_summary[hh]["shares"] += shares



        sections.append(f"\n[ACTIVE CYCLES] {len(cycles)} total")

        for hh, s in hh_summary.items():

            sections.append(

                f"  {hh}: {len(s['tickers'])} tickers, "

                f"{s['short_puts']} CSP, {s['short_calls']} CC, "

                f"{s['shares']:.0f} shares"

            )

    except Exception as exc:

        sections.append(f"\n[ACTIVE CYCLES] Error: {exc}")



    # ── 6. Pending Orders ──

    try:

        with closing(_get_db_connection()) as conn:

            conn.row_factory = sqlite3.Row

            orders = conn.execute(

                "SELECT id, status, payload FROM pending_orders "

                "WHERE date(created_at) = date('now') "

                "ORDER BY id DESC"

            ).fetchall()

        by_status: dict[str, int] = {}

        for o in orders:

            st = o["status"]

            by_status[st] = by_status.get(st, 0) + 1

        total_orders = len(orders)

        status_parts = ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))

        sections.append(

            f"\n[TODAY'S ORDERS] {total_orders} total"

            + (f" ({status_parts})" if status_parts else "")

        )

    except Exception as exc:

        sections.append(f"\n[TODAY'S ORDERS] Error: {exc}")



    # ── 7. NLV per Account ──

    try:

        with closing(_get_db_connection()) as conn:

            conn.row_factory = sqlite3.Row

            nlv_rows = conn.execute(

                "SELECT account_id, nlv, excess_liquidity, nlv_timestamp "

                "FROM v_available_nlv"

            ).fetchall()

        if nlv_rows:

            sections.append(f"\n[NLV SNAPSHOT]")

            for r in nlv_rows:

                sections.append(

                    f"  {r['account_id']}: ${r['nlv']:,.0f} "

                    f"(EL: ${r['excess_liquidity']:,.0f}) "

                    f"@ {(r['nlv_timestamp'] or '')[:16]}"

                )

        else:

            sections.append("\n[NLV SNAPSHOT] No data in v_available_nlv")

    except Exception as exc:

        sections.append(f"\n[NLV SNAPSHOT] Error: {exc}")



    # ── 8. Recent Session Logs (last 10) ──

    try:

        with closing(_get_db_connection()) as conn:

            conn.row_factory = sqlite3.Row

            logs = conn.execute(

                "SELECT task_name, run_at, summary, errors "

                "FROM autonomous_session_log "

                "ORDER BY id DESC LIMIT 10"

            ).fetchall()

        if logs:

            sections.append(f"\n[SESSION LOG] Last {len(logs)} runs:")

            for r in logs:

                errs = r["errors"]

                has_err = errs and errs != "null" and errs != "[]"

                icon = "ERR" if has_err else " ok"

                summary = (r["summary"] or "no summary")[:60]

                sections.append(

                    f"  [{icon}] {r['run_at'][:16]} {r['task_name']}: {summary}"

                )

        else:

            sections.append("\n[SESSION LOG] No entries yet")

    except Exception as exc:

        sections.append(f"\n[SESSION LOG] Error: {exc}")



    # ── 9. Incident Queue (ADR-007 Step 5) ──

    # Replaces the legacy [DIRECTIVE] block. Prose _WEEKLY_ARCHITECT_DIRECTIVE.md

    # was retired in commit 30ea993a; the structured `incidents` table authored

    # by the Step 4 heartbeat runner is now the source of truth.

    try:

        from agt_equities import incidents_repo as _ir

        active_rows = _ir.list_by_status([

            _ir.STATUS_OPEN, _ir.STATUS_AUTHORING, _ir.STATUS_AWAITING,

            _ir.STATUS_ARCHITECT, _ir.STATUS_REJECTED_ONCE,

            _ir.STATUS_REJECTED_TWICE,

        ])

        sections.append("\n[INCIDENTS]")

        if not active_rows:

            sections.append("  No active incidents.")

        else:

            counts: dict[str, int] = {}

            for r in active_rows:

                s = r.get("status") or "?"

                counts[s] = counts.get(s, 0) + 1

            parts = [f"{s}={n}" for s, n in sorted(counts.items())]

            sections.append(f"  Active: {', '.join(parts)}  (total {len(active_rows)})")

            rows_sorted = sorted(

                active_rows,

                key=lambda r: (r.get("last_action_at") or r.get("detected_at") or ""),

                reverse=True,

            )

            for r in rows_sorted[:5]:

                iid = r.get("id")

                key = (r.get("incident_key") or "?")[:40]

                inv = (r.get("invariant_id") or "-")[:20]

                status = r.get("status") or "?"

                when = (r.get("last_action_at") or r.get("detected_at") or "-")[:16]

                mr = r.get("mr_iid")

                mr_str = f" MR!{mr}" if mr else ""

                sections.append(

                    f"  #{iid} {key}  inv={inv}  {status}  {when}{mr_str}"

                )

            sections.append("  /list_rem  /approve_rem <id>  /reject_rem <id> <reason>")

    except Exception as exc:

        sections.append(f"\n[INCIDENTS] Error: {exc}")



    # ── 10. Safety Rails Summary ──

    try:

        from pathlib import Path as _Path

        rails_path = _Path(__file__).parent / "_SAFETY_RAILS.md"

        if rails_path.exists():

            sections.append(f"\n[SAFETY RAILS] Active (_SAFETY_RAILS.md present)")

        else:

            sections.append(f"\n[SAFETY RAILS] WARNING: _SAFETY_RAILS.md NOT FOUND")

    except Exception as exc:

        sections.append(f"\n[SAFETY RAILS] Error: {exc}")



    sections.append(f"\n{'=' * 40}\nEnd of report. Paste into Cowork to triage.")



    # ── Send ──

    msg = "\n".join(sections)

    try:

        await status_msg.edit_text(

            f"<pre>{html.escape(msg)}</pre>", parse_mode="HTML",

        )

    except Exception:

        # Message too long — split into chunks

        for i in range(0, len(msg), 4000):

            chunk = msg[i:i + 4000]

            if i == 0:

                await status_msg.edit_text(

                    f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML",

                )

            else:

                await update.message.reply_text(

                    f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML",

                )





# /cycles TICKER — show CC cycle history from cc_cycle_log

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

# /fills — view recent fill log entries

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

# /ledger — view premium ledger state

# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------

# V7: Conviction, Escalation, Overweight, Dynamic Exit

# ---------------------------------------------------------------------------





def _compute_conviction_tier(ticker: str) -> dict:

    """Thin wrapper — delegates to ``agt_equities.conviction``."""

    from agt_equities.conviction import compute_conviction_tier

    return compute_conviction_tier(ticker)





def _get_effective_conviction(ticker: str) -> dict:

    """Thin wrapper — delegates to ``agt_equities.conviction``."""

    from agt_equities.conviction import get_effective_conviction

    return get_effective_conviction(ticker)





def _persist_conviction(ticker: str, conviction: dict) -> None:

    """Thin wrapper — delegates to ``agt_equities.conviction``."""

    from agt_equities.conviction import persist_conviction

    persist_conviction(ticker, conviction)





def _compute_escalation_tier(position_pct: float) -> dict:

    """Evaluation frequency based on concentration."""

    if position_pct > 40:

        return {"tier": "EVERY_CYCLE", "frequency": "Every Monday", "cycles": 1}

    elif position_pct > 25:

        return {"tier": "EVERY_2_CYCLES", "frequency": "Every 2 weeks", "cycles": 2}

    else:

        return {"tier": "STANDARD", "frequency": "3 consecutive low-yield", "cycles": 3}





def allocate_excess_proportional(

    excess_contracts: int,

    accounts_with_shares: dict,

) -> dict:

    """Allocate excess contracts across accounts proportional to shares held.



    Followup #20: sub-account routing. Each returned entry becomes a

    separate staged row with its own originating_account_id.



    Rules:

    - Integer contracts only (each contract = 100 shares)

    - Accounts with < 100 shares are skipped (can't write 1 contract)

    - Fractional remainders go to the largest holder (stable sort by

      shares desc, then account_id asc for determinism)

    - Returns {account_id: contracts} with zero-values omitted

    """

    eligible = {

        a: info["shares"] if isinstance(info, dict) else info

        for a, info in accounts_with_shares.items()

        if (info["shares"] if isinstance(info, dict) else info) >= 100

    }

    if not eligible or excess_contracts <= 0:

        return {}



    total = sum(eligible.values())

    if total <= 0:

        return {}



    raw = {a: excess_contracts * s / total for a, s in eligible.items()}

    floored = {a: int(v) for a, v in raw.items()}

    remainder = excess_contracts - sum(floored.values())



    if remainder > 0:

        order = sorted(eligible.items(), key=lambda kv: (-kv[1], kv[0]))

        for i in range(remainder):

            acct = order[i % len(order)][0]

            floored[acct] += 1



    return {a: c for a, c in floored.items() if c > 0}





def _compute_overweight_scope(

    current_shares: int,

    current_price: float,

    household_nlv: float,

    trigger_rule: str = "RULE_1",

    available_contracts: int | None = None,

    adjusted_basis: float = 0.0,

) -> dict:

    """

    Overweight calculation with 15% buffer.

    Returns target/excess shares and contracts.

    """

    position_value = current_shares * current_price

    position_pct = (position_value / household_nlv * 100) if household_nlv > 0 else 0



    if trigger_rule in ("RULE_3", "RULE_4"):

        return {

            "scope": "FULL_POSITION",

            "reason": f"{trigger_rule} is binary -- full position eligible",

            "target_pct": DYNAMIC_EXIT_TARGET_PCT * 100,

            "current_pct": round(position_pct, 1),

            "target_shares": 0,

            "excess_shares": current_shares,

            "excess_contracts": current_shares // 100,

            "remaining_shares": 0,

            "remaining_pct": 0,

        }



    if household_nlv <= 0 or current_price <= 0:

        return {

            "scope": "ERROR",

            "reason": "Missing NLV or price",

            "target_pct": DYNAMIC_EXIT_TARGET_PCT * 100,

            "current_pct": 0,

            "target_shares": 0,

            "excess_shares": 0,

            "excess_contracts": 0,

            "remaining_shares": current_shares,

            "remaining_pct": 0,

        }



    # ── Drawdown Exception (Rule 1) ──

    # If stock has fallen 30%+ from basis AND position is under 30% of NLV,

    # the overweight is from price decline, not over-allocation.

    # Do not force a Dynamic Exit into weakness.

    if adjusted_basis > 0 and trigger_rule == "RULE_1":

        drawdown_pct = (current_price - adjusted_basis) / adjusted_basis

        if drawdown_pct <= -0.30 and position_pct <= 30:

            return {

                "scope": "DRAWDOWN_EXCEPTION",

                "reason": (

                    f"Stock down {drawdown_pct*100:.0f}% from basis. "

                    f"Position at {position_pct:.1f}% (under 30% cap). "

                    f"Drawdown Exception applies — no forced exit."

                ),

                "target_pct": DYNAMIC_EXIT_TARGET_PCT * 100,

                "current_pct": round(position_pct, 1),

                "target_shares": current_shares,

                "excess_shares": 0,

                "excess_contracts": 0,

                "remaining_shares": current_shares,

                "remaining_pct": round(position_pct, 1),

            }



    target_value = household_nlv * DYNAMIC_EXIT_TARGET_PCT

    target_shares = int(target_value / current_price)

    excess_shares = max(0, current_shares - target_shares)

    excess_contracts = excess_shares // 100



    # Cap at available (unencumbered) contracts

    if available_contracts is not None:

        actual_excess = min(excess_contracts, available_contracts)

        if actual_excess < excess_contracts:

            scope = "OVERWEIGHT_ENCUMBERED" if actual_excess == 0 else "OVERWEIGHT_ONLY"

        else:

            scope = "OVERWEIGHT_ONLY"

        excess_contracts = actual_excess

    else:

        scope = "OVERWEIGHT_ONLY"



    if excess_contracts == 0 and position_pct > DYNAMIC_EXIT_RULE1_LIMIT * 100:

        if scope != "OVERWEIGHT_ENCUMBERED":

            scope = "OVERWEIGHT_SUB_LOT"



    remaining_shares = current_shares - (excess_contracts * 100)

    remaining_value = remaining_shares * current_price

    remaining_pct = (remaining_value / household_nlv * 100) if household_nlv > 0 else 0



    return {

        "scope": scope,

        "reason": f"Target {DYNAMIC_EXIT_TARGET_PCT*100:.0f}% = {target_shares}sh. Excess: {excess_shares}sh = {excess_contracts}c.",

        "target_pct": DYNAMIC_EXIT_TARGET_PCT * 100,

        "current_pct": round(position_pct, 1),

        "target_shares": target_shares,

        "excess_shares": excess_shares,

        "excess_contracts": excess_contracts,

        "remaining_shares": remaining_shares,

        "remaining_pct": round(remaining_pct, 1),

    }





def _write_dynamic_exit_rows(entries: list[dict]) -> None:

    """Write assembled dynamic-exit row dicts to bucket3_dynamic_exit_log.

    Called by _stage_dynamic_exit_candidate (ctx=None backward-compat path)
    and by SQLiteDecisionSink.record_dynamic_exit (live sink path).
    """

    if not entries:

        return

    with closing(_get_db_connection()) as conn:

        with tx_immediate(conn):

            for row in entries:

                conn.execute(

                    "INSERT INTO bucket3_dynamic_exit_log "

                    "(audit_id, trade_date, ticker, household, desk_mode, "

                    " action_type, household_nlv, underlying_spot_at_render, "

                    " gate1_freed_margin, gate1_realized_loss, "

                    " gate1_conviction_tier, gate1_conviction_modifier, "

                    " gate1_ratio, gate2_target_contracts, "

                    " walk_away_pnl_per_share, strike, expiry, "

                    " contracts, shares, limit_price, "

                    " render_ts, staged_ts, final_status, source, "

                    " originating_account_id) "

                    "VALUES (?, ?, ?, ?, ?, "

                    " ?, ?, ?, "

                    " ?, ?, "

                    " ?, ?, "

                    " ?, ?, "

                    " ?, ?, ?, "

                    " ?, ?, ?, "

                    " ?, ?, ?, ?, ?)",

                    (

                        row["audit_id"],

                        row["trade_date"],

                        row["ticker"],

                        row["household"],

                        row["desk_mode"],

                        row["action_type"],

                        row["household_nlv"],

                        row["underlying_spot_at_render"],

                        row["gate1_freed_margin"],

                        row["gate1_realized_loss"],

                        row["gate1_conviction_tier"],

                        row["gate1_conviction_modifier"],

                        row["gate1_ratio"],

                        row["gate2_target_contracts"],

                        row["walk_away_pnl_per_share"],

                        row["strike"],

                        row["expiry"],

                        row["contracts"],

                        row["shares"],

                        row["limit_price"],

                        row["render_ts"],

                        row["staged_ts"],

                        row["final_status"],

                        row["source"],

                        row["originating_account_id"],

                    ),

                )


async def _stage_dynamic_exit_candidate(

    ticker: str,

    hh_name: str,

    hh_data: dict,

    position: dict,

    source: str,

    *,

    ctx: "RunContext | None" = None,

) -> dict:

    """

    Evaluate and STAGE a dynamic exit candidate into bucket3_dynamic_exit_log.



    Computes conviction, escalation, overweight scope, and Gate 1. If a viable

    strike passes Gate 1, writes a STAGED row. Returns a result dict:

      {"staged": bool, "audit_id": str|None, "summary": str, "excess_contracts": int}



    source must be one of: 'scheduled_watchdog', 'manual_inspection', 'cc_overweight'.

    """

    import time

    import uuid



    try:

        hh_short = hh_name.replace("_Household", "")

        hh_nlv = hh_data["household_nlv"]

        spot = position["spot_price"]

        shares = position["total_shares"]

        adj_basis = position["adjusted_basis"]

        position_value = shares * spot

        position_pct = (position_value / hh_nlv * 100) if hh_nlv > 0 else 0



        # Conviction (check override first, compute if needed — single call)

        conviction = await asyncio.to_thread(_get_effective_conviction, ticker)

        if conviction.get("source") == "COMPUTED":

            await asyncio.to_thread(_persist_conviction, ticker, conviction)



        modifier = conviction["modifier"]



        # Escalation

        escalation = _compute_escalation_tier(position_pct)



        # Overweight scope

        scope = _compute_overweight_scope(

            shares, spot, hh_nlv, "RULE_1",

            available_contracts=position.get("available_contracts"),

            adjusted_basis=adj_basis,

        )

        excess_contracts = scope["excess_contracts"]



        # ── Non-stageable scopes — return summary without writing a row ──

        if scope["scope"] == "DRAWDOWN_EXCEPTION":

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": 0,

                "summary": (

                    f"{ticker} ({hh_short}): DRAWDOWN EXCEPTION — "

                    f"no forced exit. {scope['reason']}"

                ),

            }



        if scope["scope"] == "OVERWEIGHT_ENCUMBERED":

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": 0,

                "summary": (

                    f"{ticker} ({hh_short}): ENCUMBERED — "

                    f"{scope['excess_shares']}sh excess but 0c available. "

                    f"Let existing CCs expire first."

                ),

            }



        if scope["scope"] == "OVERWEIGHT_SUB_LOT":

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": 0,

                "summary": (

                    f"{ticker} ({hh_short}): SUB-LOT — "

                    f"overweight < 100sh. Recover naturally."

                ),

            }



        if excess_contracts <= 0:

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": 0,

                "summary": f"{ticker} ({hh_short}): no excess contracts",

            }



        # ── Gate 1 evaluation at each viable strike ──

        best_strike = None

        best_ratio = 0.0

        best_bid = 0.0

        best_exp = None

        best_dte = 999

        best_freed = 0.0

        best_walk_away_per_share = 0.0

        gate1_pass = False



        try:

            expiries = await _ibkr_get_expirations(ticker)

            if not expiries:

                return {

                    "staged": False, "audit_id": None,

                    "excess_contracts": excess_contracts,

                    "summary": f"{ticker} ({hh_short}): no expirations available",

                }

            today_d = _date.today()



            # Find best expiry in 14-30 DTE window, closest to 21

            candidate_exp = None

            candidate_dte = 999

            for exp_str in expiries:

                try:

                    exp_date = _date.fromisoformat(exp_str)

                    dte = (exp_date - today_d).days

                    if 14 <= dte <= 30 and abs(dte - 21) < abs(candidate_dte - 21):

                        candidate_exp = exp_str

                        candidate_dte = dte

                except ValueError:

                    continue



            if not candidate_exp:

                return {

                    "staged": False, "audit_id": None,

                    "excess_contracts": excess_contracts,

                    "summary": f"{ticker} ({hh_short}): no viable expiry in 14-30 DTE",

                }



            yf_tkr = yf.Ticker(ticker)

            chain = await _with_timeout_async(yf_tkr.option_chain, candidate_exp, timeout=15)

            if chain is None:

                return {

                    "staged": False, "audit_id": None,

                    "excess_contracts": excess_contracts,

                    "summary": f"{ticker} ({hh_short}): option chain timed out",

                }



            calls = chain.calls

            calls["strike"] = pd.to_numeric(calls["strike"], errors="coerce")



            lower = adj_basis * 0.95

            viable = calls[

                (calls["strike"] >= lower) & (calls["strike"] <= adj_basis)

            ].sort_values("strike", ascending=False)



            for _, row in viable.head(5).iterrows():

                strike = float(row["strike"])

                bid = float(row.get("bid", 0) or 0)

                if bid <= 0:

                    continue



                # Sprint B Unit 3: dedup Gate 1 — call canonical evaluate_gate_1

                from agt_equities.rule_engine import evaluate_gate_1, ConvictionTier

                g1 = evaluate_gate_1(

                    ticker=ticker,

                    household=hh_name,

                    candidate_strike=strike,

                    candidate_premium=bid,

                    contracts=excess_contracts,

                    adjusted_cost_basis=adj_basis,

                    conviction_tier=ConvictionTier(conviction["tier"]),

                )

                ratio = g1.ratio

                g1_pass = g1.passed

                best_walk_away_per_share_candidate = _compute_walk_away_pnl(

                    adj_basis, strike, bid, quantity=1, multiplier=1

                ).walk_away_pnl_per_share



                if g1_pass and ratio > best_ratio:

                    best_ratio = ratio

                    best_strike = strike

                    best_bid = bid

                    best_exp = candidate_exp

                    best_dte = candidate_dte

                    best_freed = g1.freed_margin

                    best_walk_away_per_share = best_walk_away_per_share_candidate

                    gate1_pass = True



        except Exception as chain_exc:

            logger.warning("Gate 1 chain walk failed for %s: %s", ticker, chain_exc)

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": excess_contracts,

                "summary": f"{ticker} ({hh_short}): chain error — {chain_exc}",

            }



        if not gate1_pass or best_strike is None:

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": excess_contracts,

                "summary": (

                    f"{ticker} ({hh_short}): all strikes FAIL Gate 1. "

                    f"EXIT REJECTED."

                ),

            }



        # ── Followup #20: per-account allocation ──

        acct_shares = position.get("accounts_with_shares", {})

        allocation = allocate_excess_proportional(excess_contracts, acct_shares)

        if not allocation:

            # Fallback: single account from household primary (legacy path)

            hh_accounts = HOUSEHOLD_MAP.get(hh_name, [])

            fallback_acct = hh_accounts[0] if hh_accounts else None

            if fallback_acct:

                allocation = {fallback_acct: excess_contracts}

            else:

                return {

                    "staged": False, "audit_id": None,

                    "excess_contracts": excess_contracts,

                    "summary": f"{ticker} ({hh_short}): no eligible account for staging",

                }



        # ── Write STAGED rows — atomic transaction, one row per account ──

        now_ts = time.time()

        desk_mode = _get_current_desk_mode()

        total_realized = (

            round(abs(best_walk_away_per_share) * 100 * excess_contracts, 2)

            if best_walk_away_per_share < 0 else 0.0

        )



        exit_rows = []

        staged_audit_ids = []

        trade_date = date.today().isoformat()

        for account_id, acct_contracts in allocation.items():

            row_audit_id = str(uuid.uuid4())

            scale = acct_contracts / excess_contracts

            row_freed = round(best_freed * scale, 2)

            row_realized = round(total_realized * scale, 2)

            row_shares = acct_contracts * 100

            exit_rows.append({

                "audit_id": row_audit_id,

                "trade_date": trade_date,

                "ticker": ticker,

                "household": hh_name,

                "desk_mode": desk_mode,

                "action_type": "CC",

                "household_nlv": round(hh_nlv, 2),

                "underlying_spot_at_render": round(spot, 4),

                "gate1_freed_margin": row_freed,

                "gate1_realized_loss": row_realized,

                "gate1_conviction_tier": conviction["tier"],

                "gate1_conviction_modifier": round(modifier, 4),

                "gate1_ratio": round(best_ratio, 4),

                "gate2_target_contracts": acct_contracts,

                "walk_away_pnl_per_share": round(best_walk_away_per_share, 4),

                "strike": round(best_strike, 2),

                "expiry": best_exp,

                "contracts": acct_contracts,

                "shares": row_shares,

                "limit_price": round(best_bid, 4),

                "render_ts": now_ts,

                "staged_ts": now_ts,

                "final_status": "STAGED",

                "source": source,

                "originating_account_id": account_id,

            })

            staged_audit_ids.append(row_audit_id)

            logger.info(

                "STAGED: %s %s %dc -> %s (%s)",

                ticker, hh_short, acct_contracts, account_id,

                ACCOUNT_LABELS.get(account_id, account_id),

            )



        try:

            if ctx is not None:

                ctx.decision_sink.record_dynamic_exit(exit_rows, run_id=ctx.run_id)

            else:

                _write_dynamic_exit_rows(exit_rows)

        except Exception as db_exc:

            logger.error("Failed to stage dynamic exit for %s: %s", ticker, db_exc)

            return {

                "staged": False, "audit_id": None,

                "excess_contracts": excess_contracts,

                "summary": f"{ticker} ({hh_short}): DB write failed — {db_exc}",

            }



        acct_detail = ", ".join(

            f"{c}c->{ACCOUNT_LABELS.get(a, a)}"

            for a, c in allocation.items()

        )

        summary = (

            f"\U0001f6a8 STAGED: {ticker} ({hh_short}) "

            f"-{excess_contracts}c ${best_strike:.0f}C {best_exp} "

            f"@ ${best_bid:.2f} | Gate 1 PASS ({best_ratio:.1f}x) "

            f"| {position_pct:.1f}% conc"

            f"\n  Routed: {acct_detail}"

        )

        return {

            "staged": True,

            "audit_id": staged_audit_ids[0] if len(staged_audit_ids) == 1 else staged_audit_ids,

            "excess_contracts": excess_contracts,

            "summary": summary,

        }



    except Exception as exc:

        logger.exception("_stage_dynamic_exit_candidate failed for %s", ticker)

        return {

            "staged": False, "audit_id": None,

            "excess_contracts": 0,

            "summary": f"Dynamic exit staging failed for {ticker}: {exc}",

        }

















# ---------------------------------------------------------------------------

# Phase 3: _discover_positions (shared discovery engine)

# ---------------------------------------------------------------------------



async def _discover_positions(

    household_filter: str | None = None,

    include_staged: bool = True,

) -> dict:

    """

    Core data layer consumed by /health and /mode1.

    Returns per-household position records with mode classification,

    premium ledger data, spot prices, and encumbrance state.

    include_staged=False omits unapproved staged orders from encumbrance

    (used by /health so pending orders don't hide available contracts).

    """

    try:

        ib_conn = await ensure_ib_connected()

        positions = await ib_conn.reqPositionsAsync()

        mstats = await _query_margin_stats()

    except Exception as exc:

        logger.exception("_discover_positions: IB query failed")

        return {"households": {}, "all_book_nlv": 0.0, "error": str(exc)}



    # ── Sprint 2 Fix 7: Query IBKR working SELL CALL orders ──

    working_sell_calls: dict[str, int] = {}  # "hh|ticker" -> contracts

    working_per_account: dict[str, int] = {}  # "acct|ticker" -> remaining_contracts

    try:

        open_orders = await ib_conn.reqAllOpenOrdersAsync()

        for trade_obj in open_orders:

            o = trade_obj.order if hasattr(trade_obj, "order") else None

            c = trade_obj.contract if hasattr(trade_obj, "contract") else None

            if not o or not c:

                continue

            if getattr(o, "action", "") != "SELL":

                continue

            if getattr(c, "secType", "") != "OPT":

                continue

            if getattr(c, "right", "") != "C":

                continue

            acct = getattr(o, "account", "")

            hh = ACCOUNT_TO_HOUSEHOLD.get(acct)

            if not hh:

                continue

            root = c.symbol.upper()

            wk = f"{hh}|{root}"

            # Status filter first, then remaining — never resurrect filled orders

            status_obj = getattr(trade_obj, "orderStatus", None)

            order_status = getattr(status_obj, "status", "")

            if order_status not in ("Submitted", "PreSubmitted", "PendingSubmit"):

                continue  # Not a working order — skip

            remaining = abs(int(getattr(status_obj, "remaining", 0) or 0))

            if remaining <= 0:

                continue  # Fully filled — already counted in positions

            working_sell_calls[wk] = working_sell_calls.get(wk, 0) + remaining

            ak = f"{acct}|{root}"

            working_per_account[ak] = working_per_account.get(ak, 0) + remaining

    except Exception as exc:

        logger.warning("_discover_positions: working orders query failed: %s", exc)



    # ── Sprint 2 Fix 7: Query staged/processing SELL CALL orders from pending_orders ──

    staged_sell_calls: dict[str, int] = {}  # "hh|ticker" -> contracts

    staged_per_account: dict[str, int] = {}  # "acct|ticker" -> contracts

    if include_staged:

        try:

            with closing(_get_db_connection()) as conn:

                staged_rows = conn.execute(

                    """

                    SELECT payload FROM pending_orders

                    WHERE status IN ('staged', 'processing')

                    """

                ).fetchall()



            for row in staged_rows:

                try:

                    raw = row["payload"] if isinstance(row, dict) else row[0]

                    p = json.loads(raw) if isinstance(raw, str) else raw



                    # Only count SELL CALL options

                    if (p.get("action") == "SELL"

                            and p.get("sec_type") == "OPT"

                            and p.get("right") == "C"):

                        p_ticker = (p.get("ticker") or "").upper()

                        p_qty = int(p.get("quantity") or 0)

                        p_hh = p.get("household") or ACCOUNT_TO_HOUSEHOLD.get(

                            p.get("account_id", ""), ""

                        )

                        if p_hh and p_ticker and p_qty > 0:

                            sk = f"{p_hh}|{p_ticker}"

                            staged_sell_calls[sk] = staged_sell_calls.get(sk, 0) + p_qty

                        ak = f"{p.get('account_id', '')}|{p_ticker}"

                        staged_per_account[ak] = staged_per_account.get(ak, 0) + p_qty

                except (json.JSONDecodeError, TypeError, ValueError):

                    continue

        except Exception as exc:

            logger.warning("_discover_positions: staged orders query failed: %s", exc)



    # ── Sprint B Unit 2: DEX encumbrance from bucket3_dynamic_exit_log ──

    dex_sell_calls: dict[str, int] = {}  # "hh|ticker" -> contracts

    try:

        with closing(_get_db_connection()) as conn:

            dex_rows = conn.execute(

                "SELECT ticker, household, contracts, shares, action_type "

                "FROM bucket3_dynamic_exit_log "

                "WHERE final_status IN ('STAGED', 'ATTESTED', 'TRANSMITTING')"

            ).fetchall()

        for dr in dex_rows:

            tk = dr["ticker"]

            hh = dr["household"]

            # CC: contracts encumber shares; STK_SELL: shares encumber directly

            if dr["action_type"] == "CC":

                enc = dr["contracts"] or 0

            else:

                enc = (dr["shares"] or 0) // 100  # STK_SELL: convert shares to contract-equivalent

            if hh and tk and enc > 0:

                dk = f"{hh}|{tk}"

                dex_sell_calls[dk] = dex_sell_calls.get(dk, 0) + enc

    except Exception as exc:

        logger.warning("_discover_positions: DEX encumbrance query failed: %s", exc)



    # ── Group raw positions by household + root ticker ──

    raw: dict[str, dict[str, dict]] = {}  # household -> ticker -> accumulator

    for pos in positions:

        if pos.position == 0:

            continue

        acct = pos.account

        if acct not in ACCOUNT_TO_HOUSEHOLD:

            continue

        hh = ACCOUNT_TO_HOUSEHOLD[acct]

        if household_filter and hh != household_filter:

            continue

        c = pos.contract

        root = c.symbol.upper()

        if root in EXCLUDED_TICKERS:

            continue



        key = f"{hh}|{root}"

        if key not in raw:

            raw[key] = {

                "household": hh,

                "ticker": root,

                "sector": "Unknown",  # populated below via batch lookup

                "stk_shares": 0,

                "avg_cost_ibkr": 0.0,

                "short_calls": [],

                "short_puts": [],

                "accounts_with_shares": {},

            }

        rec = raw[key]



        if c.secType == "STK" and pos.position > 0:

            qty = int(pos.position)

            rec["stk_shares"] += qty

            rec["avg_cost_ibkr"] = float(pos.avgCost)

            acct_entry = rec["accounts_with_shares"].setdefault(acct, {

                "account_id": acct,

                "label": ACCOUNT_LABELS.get(acct, acct),

                "shares": 0,

                "avg_cost_ibkr": 0.0,

            })

            acct_entry["shares"] += qty

            # Per-account cost basis from IBKR (WHEEL-5 fix: no blending)

            acct_entry["avg_cost_ibkr"] = float(pos.avgCost)



        elif c.secType == "OPT" and pos.position < 0:

            right = getattr(c, "right", "")

            if right not in ("C", "P"):

                continue

            con_id = getattr(c, "conId", 0)

            raw_avg = pos.avgCost

            avg_cost_val = float(raw_avg) if raw_avg and not math.isnan(raw_avg) else 0.0

            short_entry = {

                "strike": float(c.strike),

                "expiry": str(c.lastTradeDateOrContractMonth),

                "contracts": abs(int(pos.position)),

                "right": right,

                "account": acct,

                "unrealized_pnl": None,         # populated by reqPnLSingle batch below

                "avg_cost": avg_cost_val,        # per-contract cost from reqPositions

                "con_id": con_id,

            }

            if right == "C":

                rec["short_calls"].append(short_entry)

            else:

                rec.setdefault("short_puts", []).append(short_entry)



    # ── Batch reqPnLSingle for all short option positions ──

    # reqAccountUpdates only supports ONE account at a time, so portfolio()

    # only has data for one account. reqPnLSingle works across all accounts.

    _opt_subs: list[tuple[str, int, object]] = []  # (acct, conId, pnlObj)

    try:

        for rec_val in raw.values():

            for entry in rec_val.get("short_calls", []) + rec_val.get("short_puts", []):

                _acct = entry.get("account", "")

                _cid = entry.get("con_id", 0)

                if _acct and _cid:

                    try:

                        pnl_obj = ib_conn.reqPnLSingle(_acct, "", _cid)

                        _opt_subs.append((_acct, _cid, pnl_obj))

                    except Exception:

                        continue



        if _opt_subs:

            for _ in range(4):

                ib_conn.sleep(0.5)



        _opt_pnl: dict[tuple[str, int], float] = {}

        for _acct, _cid, pnl_obj in _opt_subs:

            try:

                val = getattr(pnl_obj, "unrealizedPnL", None)

                if val is not None and not math.isnan(val):

                    _opt_pnl[(_acct, _cid)] = float(val)

            except Exception:

                continue



        # Apply PnL values to short option entries

        for rec_val in raw.values():

            for entry in rec_val.get("short_calls", []) + rec_val.get("short_puts", []):

                key = (entry.get("account", ""), entry.get("con_id", 0))

                pnl_val = _opt_pnl.get(key)

                if pnl_val is not None:

                    entry["unrealized_pnl"] = pnl_val



        # Cancel all subscriptions

        for _acct, _cid, pnl_obj in _opt_subs:

            try:

                ib_conn.cancelPnLSingle(pnl_obj)

            except Exception:

                pass

    except Exception as opt_pnl_exc:

        logger.warning("Option PnL batch fetch failed: %s", opt_pnl_exc)



    # ── Batch sector lookup from ticker_universe (with fallback) ──

    all_root_tickers = list({v["ticker"] for v in raw.values()})

    ig_map = _get_industry_groups_batch(all_root_tickers)

    for key, rec in raw.items():

        root = rec["ticker"]

        ig = ig_map.get(root, "Unknown")

        rec["sector"] = ig if ig != "Unknown" else _SECTOR_MAP_FALLBACK.get(root, "Unknown")



    # ── Fetch spot prices (IBKR batch, yfinance fallback for display) ──

    unique_tickers = list({v["ticker"] for v in raw.values()})

    spot_prices: dict[str, float] = {}

    if unique_tickers:

        try:

            spot_prices = await _ibkr_get_spots_batch(unique_tickers)

        except Exception as exc:

            logger.warning("_discover_positions: IBKR batch spots failed: %s", exc)

        # MIGRATED 2026-04-07 Phase 3A.5c1 — replaced yfinance fallback

        # with IBKRPriceVolatilityProvider.get_spot() per Architect decision.

        # OLD: data = yf.download(" ".join(missed), period="1d", ...)

        missed = [t for t in unique_tickers if t not in spot_prices]

        if missed:

            try:

                from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider

                _prov = IBKRPriceVolatilityProvider(ib, market_data_mode="delayed")

                for tkr in missed:

                    spot = _prov.get_spot(tkr)

                    if spot is not None:

                        spot_prices[tkr] = round(spot, 2)

            except Exception:

                pass



    # Use pre-fetched NLV from _query_margin_stats (no second IB call)

    _all_account_nlv = mstats.get("all_account_nlv", {})



    # Pre-fetch position data from Walker cycles (or legacy fallback)

    _ledger_cache: dict[tuple[str, str], dict] = {}

    if READ_FROM_MASTER_LOG:

        try:

            from agt_equities import trade_repo

            for c in trade_repo.get_active_cycles():

                if c.cycle_type != 'WHEEL':

                    continue

                lkey = (c.household_id, c.ticker)

                _ledger_cache[lkey] = {

                    "initial_basis": c.paper_basis or 0,

                    "total_premium_collected": c.premium_total,

                    "shares_owned": int(c.shares_held),

                    "adjusted_basis": round(c.adjusted_basis, 4) if c.adjusted_basis else None,

                    "_cycle": c,  # WHEEL-5: keep for paper_basis_for_account()

                }

        except Exception as ml_exc:

            logger.warning("Walker batch pre-fetch failed, falling back to legacy: %s", ml_exc)

            _ledger_cache = {}



    if not _ledger_cache:

        try:

            with closing(_get_db_connection()) as conn:

                ledger_rows = conn.execute(

                    "SELECT household_id, ticker, initial_basis, "

                    "total_premium_collected, shares_owned "

                    "FROM premium_ledger"

                ).fetchall()

            for lr in ledger_rows:

                lkey = (lr["household_id"], lr["ticker"])

                shares = int(lr["shares_owned"] or 0)

                initial = float(lr["initial_basis"] or 0)

                prem = float(lr["total_premium_collected"] or 0)

                adj = (initial - prem / shares) if shares > 0 else None

                _ledger_cache[lkey] = {

                    "initial_basis": initial,

                    "total_premium_collected": prem,

                    "shares_owned": shares,

                    "adjusted_basis": round(adj, 4) if adj is not None else None,

                }

        except Exception as ledger_exc:

            logger.warning("Batch ledger pre-fetch failed: %s", ledger_exc)



    # ── Build final records with ledger join + mode classification ──

    household_buckets: dict[str, dict] = {}

    for key, rec in raw.items():

        hh = rec["household"]

        tkr = rec["ticker"]

        total_shares = rec["stk_shares"]

        if total_shares <= 0:

            continue



        # Premium ledger join (from batch cache)

        ledger = _ledger_cache.get((hh, tkr))

        if ledger and ledger.get("adjusted_basis") is not None:

            initial_basis = ledger["initial_basis"]

            total_prem = ledger["total_premium_collected"]

            adj_basis = ledger["adjusted_basis"]

        else:

            initial_basis = rec["avg_cost_ibkr"]

            total_prem = 0.0

            adj_basis = rec["avg_cost_ibkr"]



        # WHEEL-5: populate per-account paper_basis in accounts_with_shares

        _w5_cycle = ledger.get("_cycle") if ledger else None

        for _acct_id, _acct_info in rec["accounts_with_shares"].items():

            if _w5_cycle is not None:

                try:

                    _per_acct_basis = _w5_cycle.paper_basis_for_account(_acct_id)

                    if _per_acct_basis is not None:

                        _acct_info["paper_basis"] = round(_per_acct_basis, 4)

                        continue

                except Exception:

                    pass

            # Fallback: use IBKR per-account avgCost

            _acct_info.setdefault("paper_basis", _acct_info.get("avg_cost_ibkr", initial_basis))



        spot = spot_prices.get(tkr, 0.0)



        # Mode classification

        if adj_basis <= 0:

            mode = "FULLY_AMORTIZED"

        elif spot >= adj_basis:

            mode = "MODE_2"

        else:

            mode = "MODE_1"



        filled_contracts = sum(sc["contracts"] for sc in rec["short_calls"])

        pos_key = f"{hh}|{tkr}"

        working_contracts = working_sell_calls.get(pos_key, 0)

        staged_contracts = staged_sell_calls.get(pos_key, 0)

        dex_contracts = dex_sell_calls.get(pos_key, 0)  # Sprint B Unit 2

        covered_contracts = filled_contracts + working_contracts + staged_contracts + dex_contracts

        uncov_shares = max(0, total_shares - (covered_contracts * 100))



        position_rec = {

            "household": hh,

            "ticker": tkr,

            "sector": rec["sector"],

            "total_shares": total_shares,

            "avg_cost_ibkr": round(rec["avg_cost_ibkr"], 2),

            "initial_basis": round(initial_basis, 2),

            "total_premium_collected": round(total_prem, 2),

            "adjusted_basis": round(adj_basis, 2),

            "spot_price": spot,

            "market_value": round(total_shares * spot, 2),

            "mode": mode,

            "existing_short_calls": rec["short_calls"],

            "existing_short_puts": rec.get("short_puts", []),

            "covered_contracts": covered_contracts,

            "uncovered_shares": uncov_shares,

            "available_contracts": uncov_shares // 100,

            "accounts_with_shares": rec["accounts_with_shares"],

            "working_per_account": working_per_account,

            "staged_per_account": staged_per_account,

        }



        if hh not in household_buckets:

            hh_accounts = HOUSEHOLD_MAP.get(hh, [])

            hh_margin_nlv = 0.0

            hh_margin_el = 0.0

            for aid in hh_accounts:

                acct_data = mstats["accounts"].get(aid)

                if acct_data:

                    if aid in MARGIN_ACCOUNTS:

                        hh_margin_nlv += acct_data["nlv"]

                    hh_margin_el += acct_data["el"]

            # Full household NLV from pre-computed summary

            hh_full_nlv = sum(

                _all_account_nlv.get(aid, 0.0) for aid in hh_accounts

            ) or hh_margin_nlv



            household_buckets[hh] = {

                "household_nlv": round(hh_full_nlv, 2),

                "household_margin_nlv": round(hh_margin_nlv, 2),

                "household_margin_el": round(hh_margin_el, 2),

                "household_el_pct": round(

                    (hh_margin_el / hh_margin_nlv * 100) if hh_margin_nlv > 0 else 0.0, 2

                ),

                "positions": [],

            }



        household_buckets[hh]["positions"].append(position_rec)



    # Sort positions within each household by weight descending

    all_book_nlv = mstats["all_book_nlv"]

    for hh_data in household_buckets.values():

        hh_data["positions"].sort(

            key=lambda p: p["market_value"],

            reverse=True,

        )



    return {

        "households": household_buckets,

        "all_book_nlv": all_book_nlv,

        "error": mstats.get("error"),

    }





# ---------------------------------------------------------------------------

# Phase 3: /health command

# ---------------------------------------------------------------------------





def _fmt_k(val: float) -> str:

    """Format dollar amount as compact K/M string."""

    if abs(val) >= 1_000_000:

        return f"${val / 1_000_000:.1f}M"

    elif abs(val) >= 1_000:

        return f"${val / 1_000:.1f}K"

    else:

        return f"${val:,.0f}"





async def _fetch_position_pnl(account_ids: list[str]) -> dict[str, dict]:

    """

    Subscribe to reqPnLSingle for every long STK position held by account_ids.



    Returns:

        {"U21971297|ADBE": {"daily": -312.0, "unrealized": -4100.0, "realized": 0.0}, ...}

    Keyed by "acct|SYMBOL" so the caller can sum across accounts per ticker.

    """

    result: dict[str, dict] = {}

    subscriptions: list[tuple[str, object]] = []

    account_set = set(account_ids)

    try:

        ib_conn = await ensure_ib_connected()

        positions = await ib_conn.reqPositionsAsync()



        for pos in positions:

            acct = pos.account

            if acct not in account_set:

                continue

            c = pos.contract

            if getattr(c, "secType", "") != "STK" or pos.position <= 0:

                continue

            con_id = getattr(c, "conId", None)

            if not con_id:

                continue

            symbol = c.symbol.upper()

            key = f"{acct}|{symbol}"

            try:

                pnl_obj = ib_conn.reqPnLSingle(acct, "", con_id)

                subscriptions.append((key, pnl_obj))

            except Exception as exc:

                logger.warning("reqPnLSingle failed for %s %s: %s", acct, symbol, exc)



        if subscriptions:

            await asyncio.sleep(2)



        for key, pnl_obj in subscriptions:

            def _safe(v):

                if v is None or not isinstance(v, (int, float)):

                    return 0.0

                if abs(v) > 1e15:  # IBKR "unset" sentinel

                    return 0.0

                return float(v)

            result[key] = {

                "daily":      _safe(getattr(pnl_obj, "dailyPnL",      None)),

                "unrealized": _safe(getattr(pnl_obj, "unrealizedPnL", None)),

                "realized":   _safe(getattr(pnl_obj, "realizedPnL",   None)),

            }



        for key, pnl_obj in subscriptions:

            try:

                ib_conn.cancelPnLSingle(pnl_obj)

            except Exception:

                pass  # Best-effort cleanup



    except Exception as exc:

        logger.warning("_fetch_position_pnl failed: %s", exc)



    return result





async def _fetch_account_pnl(account_ids: list[str]) -> dict[str, dict]:

    """

    Subscribe to IBKR reqPnL for each account, wait for data, return results.



    Returns:

        {

            "U21971297": {"daily": -1247.50, "unrealized": -18300.0, "realized": 0.0},

            ...

        }

    Empty dict values if subscription fails or times out.

    """

    result: dict[str, dict] = {}

    pnl_objects = []

    try:

        ib_conn = await ensure_ib_connected()

        for acct_id in account_ids:

            try:

                pnl_obj = ib_conn.reqPnL(acct_id)

                pnl_objects.append((acct_id, pnl_obj))

            except Exception as exc:

                logger.warning("reqPnL failed for %s: %s", acct_id, exc)

                result[acct_id] = {"daily": 0.0, "unrealized": 0.0, "realized": 0.0}



        await asyncio.sleep(2)



        for acct_id, pnl_obj in pnl_objects:

            daily = getattr(pnl_obj, "dailyPnL", None)

            unrealized = getattr(pnl_obj, "unrealizedPnL", None)

            realized = getattr(pnl_obj, "realizedPnL", None)



            def _safe(v):

                if v is None or not isinstance(v, (int, float)):

                    return 0.0

                if abs(v) > 1e15:  # IBKR "unset" sentinel

                    return 0.0

                return float(v)



            result[acct_id] = {

                "daily": _safe(daily),

                "unrealized": _safe(unrealized),

                "realized": _safe(realized),

            }



        for acct_id, pnl_obj in pnl_objects:

            try:

                ib_conn.cancelPnL(pnl_obj)

            except Exception:

                pass  # Best-effort cleanup



    except Exception as exc:

        logger.warning("_fetch_account_pnl failed: %s", exc)

        for acct_id in account_ids:

            if acct_id not in result:

                result[acct_id] = {"daily": 0.0, "unrealized": 0.0, "realized": 0.0}



    return result









# ---------------------------------------------------------------------------

# /cc command — unified basis-anchored covered call engine

# ---------------------------------------------------------------------------



async def _walk_cc_chain(

    ticker: str,

    spot: float,

    paper_basis: float,

    target_dte_range: tuple[int, int] = CC_TARGET_DTE,

) -> dict | None:

    """

    Unified basis-anchored covered call walker.



    Algorithm (per 2026-04-15 Yash ruling):

      1. Anchor = smallest chain strike >= paper_basis (round UP). Never walk

         below paper basis. Defensive sub-basis writes are out of scope for

         the LLM — Yash stages those manually.

      2. Walk ASCENDING from the anchor strike:

           - If mid < CC_BID_FLOOR ($0.03): skip (garbage quote).

           - If annualized > CC_MAX_ANN (130%): too much premium for this

             strike. Step up (ticker is likely being offered a fat premium

             because the market expects a rip through the strike).

           - If CC_MIN_ANN <= annualized <= CC_MAX_ANN: BAND HIT. Return.

           - If annualized < CC_MIN_ANN (30%): STAND DOWN. Returning a

             below-floor observation so the digest can surface the gap

             instead of a silent skip.

      3. No delta gate (Yash: reconsider only if we see problem trades).

      4. No earnings gate (deferred to follow-up).



    Anchor is paper_basis (initial_basis) NOT adjusted_basis. The assigned-

    paper-basis anchor is the rulebook's intent; ACB amortization is a P&L

    accounting artifact, not a strike-selection anchor.



    Returns on hit:

      {branch: "BASIS_ANCHOR" | "BASIS_STEP_UP", ticker, expiry, dte, strike,

       bid, annualized, otm_pct, walk_away_pnl, dte_range, inception_delta}



    Returns on stand-down (no viable strike in 30-130 band, but we have

    data to show Yash):

      {below_floor: True, best_strike, best_annualized, floor_pct: 30.0,

       dte, ticker}



    Returns None on hard failure (no expiries, empty chain, IB exception).

    """

    try:

        raw_expiries = await _ibkr_get_expirations(ticker)

        today = _date.today()



        min_dte, max_dte = target_dte_range

        candidates: list[tuple[str, int]] = []

        for exp_str in raw_expiries:

            try:

                exp_date = _date.fromisoformat(exp_str)

            except ValueError:

                continue

            dte = (exp_date - today).days

            if min_dte <= dte <= max_dte:

                candidates.append((exp_str, dte))



        if not candidates:

            return None



        mid_target = (min_dte + max_dte) // 2

        exp_str, dte = min(candidates, key=lambda x: abs(x[1] - mid_target))



        # Cap chain breadth at max(spot*1.5, paper_basis*1.3) — annualized

        # decays fast as strike climbs, so 150% spot / 130% basis covers the

        # step-up search comfortably.

        chain_ceiling = max(spot * 1.5, paper_basis * 1.3)

        try:

            chain_data = await _ibkr_get_chain(

                ticker, exp_str, right='C',

                min_strike=paper_basis,

                max_strike=chain_ceiling,

            )

            calls = pd.DataFrame(chain_data)

        except Exception:

            return None



        if calls is None or not isinstance(calls, pd.DataFrame) or calls.empty:

            return None



        calls = calls.copy()

        calls["strike"] = pd.to_numeric(calls["strike"], errors="coerce")

        calls = calls.dropna(subset=["strike"])



        # Anchor = smallest strike >= paper_basis (round UP).

        viable = calls[calls["strike"] >= paper_basis].sort_values(

            "strike", ascending=True

        )



        if viable.empty:

            return None



        anchor_strike_val = float(viable.iloc[0]["strike"])

        best_observed: dict | None = None  # for stand-down reporting



        for _, row in viable.iterrows():

            strike = float(row["strike"])

            raw_bid = row.get("bid")

            raw_ask = row.get("ask")

            bid = float(raw_bid) if pd.notna(raw_bid) else 0.0

            ask = float(raw_ask) if pd.notna(raw_ask) else 0.0



            mid = round((bid + ask) / 2.0, 2) if bid and ask else bid



            if mid < CC_BID_FLOOR:

                # Garbage quote — keep walking up in case deeper strikes

                # have real markets. (Unusual but cheap to guard.)

                continue



            annualized = (mid / strike) * (365 / dte) * 100 if strike > 0 else 0

            otm_pct = ((strike - spot) / spot) * 100 if spot > 0 else 0



            # Track best observed regardless of floor — for stand-down digest.

            if best_observed is None or annualized > best_observed["best_annualized"]:

                best_observed = {

                    "below_floor": True,

                    "ticker": ticker,

                    "best_strike": round(strike, 2),

                    "best_annualized": round(annualized, 2),

                    "floor_pct": CC_MIN_ANN,

                    "dte": dte,

                }



            if annualized > CC_MAX_ANN:

                # Too much premium — step up.

                continue



            if annualized < CC_MIN_ANN:

                # We've walked past the band (or anchor itself was below

                # floor). Stand down — do not pick a sub-30% strike.

                break



            # 30 <= ann <= 130: BAND HIT.

            branch = "BASIS_ANCHOR" if strike == anchor_strike_val else "BASIS_STEP_UP"



            walk_away_pnl = _compute_walk_away_pnl(

                paper_basis, strike, mid, quantity=1, multiplier=1

            ).walk_away_pnl_per_share



            try:

                raw_delta = row.get("delta")

                inception_delta = float(raw_delta) if raw_delta is not None else None

            except (TypeError, ValueError) as _id_exc:

                logger.warning(

                    "inception_delta extraction failed for %s %.1f%s: %s",

                    ticker, strike, "C", _id_exc,

                )

                inception_delta = None



            return {

                "branch": branch,

                "ticker": ticker,

                "expiry": exp_str,

                "dte": dte,

                "strike": round(strike, 2),

                "bid": mid,  # mid per V2 Execution Spec

                "annualized": round(annualized, 2),

                "otm_pct": round(otm_pct, 2),

                "walk_away_pnl": round(walk_away_pnl, 2),

                "dte_range": f"{min_dte}-{max_dte}",

                "inception_delta": inception_delta,

            }



        # Fell off the end of the ascending walk without a band hit.

        return best_observed

    except Exception as exc:

        logger.warning("_walk_cc_chain failed for %s: %s", ticker, exc)

        return None





async def _run_cc_logic(household_filter: str | None = None, *, ctx: "RunContext") -> dict:

    """

    Unified basis-anchored CC pipeline.



    One code path per uncovered position — no Mode 1 / Mode 2 split. Strike

    selection is basis-anchored (paper basis, round UP to nearest chain

    strike). Band = CC_MIN_ANN..CC_MAX_ANN annualized (30%-130%). Below-floor

    observations surface as stand-downs so Yash can see how close we are

    rather than getting a silent skip.



    Defensive (sub-basis) writes are out of scope per 2026-04-15 Yash

    ruling; he stages those manually. Existing MODE_1_DEFENSIVE rows in

    cc_cycle_log and pending_orders remain valid for historical reads

    (Rule 8 trigger, roll_watchlist etc.) — the engine just no longer

    produces new ones.

    """

    # Retry discovery once on IB failure

    disco = await _discover_positions(household_filter)

    if disco.get("error") and "connect" in str(disco["error"]).lower():

        logger.warning("IB connection issue, retrying discovery in 5s...")

        await asyncio.sleep(5)

        disco = await _discover_positions(household_filter)

    if disco.get("error"):

        logger.warning("CC discovery warning: %s", disco["error"])



    # Flatten all uncovered positions into a single target list — no mode split.

    cc_targets: list[dict] = []

    for hh_data in disco["households"].values():

        for p in hh_data["positions"]:

            if p["available_contracts"] < 1:

                continue

            # Need a paper basis to anchor. If missing, skip loudly.

            if p.get("initial_basis", 0) <= 0:

                continue

            cc_targets.append(p)



    if not cc_targets:

        return {

            "main_text": "No positions with uncovered shares for CC staging.",

        }



    staged: list[dict] = []

    skipped: list[dict] = []



    # ── Earnings-week entry block (WHEEL-4 gate) ──

    # Don't sell NEW CCs during the ISO calendar week of upcoming earnings.

    # Defensive rolls are unaffected — roll_engine handles the defense regime

    # regardless of earnings. Missing/stale cache (next_earnings=None) fails open.

    _ew_today = date.today()

    _ew_filtered: list[dict] = []

    for _ewp in cc_targets:

        try:

            _ew_ex, _ew_amt, _ew_earn = roll_scanner._read_corp_calendar_cache(_ewp["ticker"])

        except Exception:

            _ew_earn = None

        if _ew_earn is not None and _ew_today.isocalendar()[:2] == _ew_earn.isocalendar()[:2]:

            skipped.append({

                "ticker": _ewp["ticker"],

                "reason": f"EARNINGS_WEEK blocked: earnings={_ew_earn.isoformat()}",

                "household": _ewp["household"],

                "mode": _ewp.get("mode", ""),

                "spot": _ewp["spot_price"],

                "adjusted_basis": _ewp.get("adjusted_basis", 0),

                "initial_basis": _ewp.get("initial_basis", 0),

            })

        else:

            _ew_filtered.append(_ewp)

    cc_targets = _ew_filtered



    # ── Dynamic Exit carve-out (unchanged from prior implementation) ──

    dynamic_exit_staged: list[dict] = []

    excess_carveout: dict[str, int] = {}



    for hh_name, hh_data in disco["households"].items():

        hh_nlv = hh_data.get("household_nlv", 0)

        if hh_nlv <= 0:

            continue

        for p in hh_data.get("positions", []):

            position_value = p["total_shares"] * p["spot_price"]

            position_pct = (position_value / hh_nlv * 100)



            if position_pct <= DYNAMIC_EXIT_RULE1_LIMIT * 100:

                continue



            scope = _compute_overweight_scope(

                p["total_shares"], p["spot_price"], hh_nlv, "RULE_1",

                available_contracts=p.get("available_contracts"),

                adjusted_basis=p.get("adjusted_basis", 0),

            )



            if scope["excess_contracts"] > 0:

                key = f"{p['household']}|{p['ticker']}"

                excess_carveout[key] = scope["excess_contracts"]



                try:

                    stage_result = await _stage_dynamic_exit_candidate(

                        p["ticker"], hh_name, hh_data, p,

                        source="cc_overweight",

                        ctx=ctx,

                    )

                    dynamic_exit_staged.append(stage_result)

                except Exception as de_exc:

                    logger.warning("Dynamic exit staging failed for %s: %s",

                                   p["ticker"], de_exc)



    # ── WHEEL-5: Per-account basis-anchored chain walks (parallel) ──

    # Fetch IB chain once per ticker, then run pick_cc_strike per account

    # with that account's paper_basis. This fixes the household-blended

    # basis bug (UBER Roth@$73 vs Individual@$86 example).



    async def _fetch_chain_for_ticker(ticker, spot, min_basis, dte_range):

        """Fetch IB options chain and return (chain_strikes, expiry, dte) or None."""

        try:

            raw_expiries = await _ibkr_get_expirations(ticker)

            today = _date.today()

            min_dte, max_dte = dte_range

            cands = []

            for exp_str in raw_expiries:

                try:

                    exp_date = _date.fromisoformat(exp_str)

                except ValueError:

                    continue

                dte = (exp_date - today).days

                if min_dte <= dte <= max_dte:

                    cands.append((exp_str, dte))

            if not cands:

                return None

            mid_target = (min_dte + max_dte) // 2

            exp_str, dte = min(cands, key=lambda x: abs(x[1] - mid_target))



            chain_ceiling = max(spot * 1.5, min_basis * 1.3) if min_basis > 0 else spot * 1.5

            try:

                chain_data = await _ibkr_get_chain(

                    ticker, exp_str, right='C',

                    min_strike=max(0, min_basis * 0.95),  # slight buffer below lowest basis

                    max_strike=chain_ceiling,

                )

                calls = pd.DataFrame(chain_data)

            except Exception:

                return None

            if calls is None or not isinstance(calls, pd.DataFrame) or calls.empty:

                return None

            calls = calls.copy()

            calls["strike"] = pd.to_numeric(calls["strike"], errors="coerce")

            calls = calls.dropna(subset=["strike"])

            chain_strikes = tuple(

                ChainStrike(

                    strike=float(row["strike"]),

                    bid=float(row.get("bid") or 0) if pd.notna(row.get("bid")) else 0.0,

                    ask=float(row.get("ask") or 0) if pd.notna(row.get("ask")) else 0.0,

                    delta=float(row.get("delta")) if row.get("delta") is not None and pd.notna(row.get("delta")) else None,

                )

                for _, row in calls.iterrows()

            )

            return chain_strikes, exp_str, dte

        except Exception as exc:

            logger.warning("_fetch_chain_for_ticker failed for %s: %s", ticker, exc)

            return None



    async def _walk_target_per_account(p):

        """WHEEL-5: fetch chain once, run pick_cc_strike per account.



        Returns (p, per_account_results: list[(acct_id, CCResult)], skip_reason).

        """

        ticker = p["ticker"]

        spot = p["spot_price"]



        if spot <= 0:

            return p, [], "No spot price"



        # Find the lowest basis across all accounts (for chain fetch range)

        accounts = p.get("accounts_with_shares", {})

        if not accounts:

            return p, [], "No accounts with shares"

        min_basis = min(

            a.get("paper_basis", p.get("initial_basis", 0))

            for a in accounts.values()

        )

        if min_basis <= 0:

            min_basis = p.get("initial_basis", 0)

        if min_basis <= 0:

            return p, [], "No valid basis"



        chain_result = await _walk_chain_limited(

            _fetch_chain_for_ticker, ticker, spot, min_basis, CC_TARGET_DTE

        )

        if chain_result is None:

            return p, [], "No viable chain"

        chain_strikes, expiry, dte = chain_result



        # Run pick_cc_strike per account with account-specific basis

        per_account_results = []

        for acct_id, acct_info in accounts.items():

            acct_basis = acct_info.get("paper_basis", p.get("initial_basis", 0))

            if acct_basis <= 0:

                continue

            inp = CCPickerInput(

                ticker=ticker,

                account_id=acct_id,

                paper_basis=acct_basis,

                spot=spot,

                dte=dte,

                expiry=expiry,

                chain=chain_strikes,

                min_ann=CC_MIN_ANN,

                max_ann=CC_MAX_ANN,

                bid_floor=CC_BID_FLOOR,

            )

            result = pick_cc_strike(inp)

            per_account_results.append((acct_id, result))



        return p, per_account_results, None



    results = await asyncio.gather(

        *[_walk_target_per_account(p) for p in cc_targets],

        return_exceptions=True,

    )



    for item in results:

        if isinstance(item, Exception):

            logger.warning("CC chain walk failed: %s", item)

            continue



        p, per_account_results, skip_reason = item



        if skip_reason:

            skipped.append({

                "ticker": p["ticker"],

                "reason": skip_reason,

                "household": p["household"],

                "mode": p.get("mode", ""),

                "spot": p["spot_price"],

                "adjusted_basis": p.get("adjusted_basis", 0),

                "initial_basis": p.get("initial_basis", 0),

            })

            continue



        remaining_available = p["available_contracts"]

        carve_key = f"{p['household']}|{p['ticker']}"

        carved = excess_carveout.get(carve_key, 0)

        if carved > 0:

            remaining_available = max(0, remaining_available - carved)

            if remaining_available == 0:

                skipped.append({

                    "ticker": p["ticker"],

                    "reason": f"All contracts reserved for Dynamic Exit ({carved}c)",

                    "household": p["household"],

                    "mode": p.get("mode", ""),

                    "spot": p["spot_price"],

                    "adjusted_basis": p.get("adjusted_basis", 0),

                    "initial_basis": p.get("initial_basis", 0),

                })

                continue



        ticker = p["ticker"]

        working_pa = p.get("working_per_account", {})

        staged_pa = p.get("staged_per_account", {})



        for acct_id, cc_result in per_account_results:

            if remaining_available < 1:

                break



            # STAND_DOWN for this account — add to skipped with account detail

            if isinstance(cc_result, CCStandDown):

                skipped.append({

                    "ticker": ticker,

                    "reason": (

                        f"STAND DOWN \u2014 {acct_id}: best {cc_result.best_strike:.2f}C "

                        f"@ {cc_result.best_annualized:.1f}% ann "

                        f"(< {cc_result.floor_pct:.0f}% floor, "

                        f"DTE {cc_result.dte})"

                    ),

                    "household": p["household"],

                    "mode": p.get("mode", ""),

                    "spot": p["spot_price"],

                    "adjusted_basis": p.get("adjusted_basis", 0),

                    "initial_basis": p["accounts_with_shares"].get(acct_id, {}).get(

                        "paper_basis", p.get("initial_basis", 0)

                    ),

                    "account_id": acct_id,

                })

                continue



            # CCWrite — build ticket for this account

            acct_info = p["accounts_with_shares"].get(acct_id, {})

            acct_shares = acct_info.get("shares", 0)



            acct_filled = sum(

                sc["contracts"]

                for sc in p.get("existing_short_calls", [])

                if sc.get("account") == acct_id

            )

            acct_working = working_pa.get(f"{acct_id}|{ticker}", 0)

            acct_staged = staged_pa.get(f"{acct_id}|{ticker}", 0)

            acct_encumbered = acct_filled + acct_working + acct_staged



            uncovered_shares = max(0, acct_shares - (acct_encumbered * 100))

            acct_contracts = min(uncovered_shares // 100, remaining_available)

            if acct_contracts < 1:

                continue

            remaining_available -= acct_contracts



            # Convert CCWrite to dict for backward compat with staging pipeline

            result_dict = {

                "branch": cc_result.branch,

                "ticker": cc_result.ticker,

                "expiry": cc_result.expiry,

                "dte": cc_result.dte,

                "strike": cc_result.strike,

                "bid": cc_result.mid,

                "annualized": cc_result.annualized,

                "otm_pct": cc_result.otm_pct,

                "walk_away_pnl": cc_result.walk_away_pnl,

                "dte_range": f"{CC_TARGET_DTE[0]}-{CC_TARGET_DTE[1]}",

                "inception_delta": cc_result.inception_delta,

            }



            ticket = {

                "account_id": acct_id,

                "household": p["household"],

                "ticker": ticker,

                "action": "SELL",

                "sec_type": "OPT",

                "right": "C",

                "strike": cc_result.strike,

                "expiry": cc_result.expiry,

                "quantity": acct_contracts,

                "limit_price": cc_result.mid,

                "annualized_yield": cc_result.annualized,

                "mode": "MODE_2_HARVEST",

                "status": "staged",

            }

            staged.append({**ticket, **result_dict})



    # Write all staged tickets to SQLite

    if staged:

        try:

            with closing(_get_db_connection()) as conn:

                with tx_immediate(conn):

                    conn.execute(

                        "UPDATE pending_orders SET status = 'superseded' WHERE status = 'staged'"

                    )

            await asyncio.to_thread(append_pending_tickets, staged)



            cycle_log_entries = []

            for s in staged:

                entry = dict(s)

                entry["flag"] = "HARVEST_OK"

                cycle_log_entries.append(entry)



            for sk in skipped:

                reason = sk.get("reason", "")

                if "stand down" in reason.lower():

                    flag = "STAND_DOWN"

                elif "no viable strike" in reason.lower():

                    flag = "NO_VIABLE_STRIKE"

                elif "no spot" in reason.lower():

                    flag = "SKIPPED"

                else:

                    flag = "SKIPPED"

                skip_entry = {

                    "ticker": sk["ticker"],

                    "household": sk.get("household", ""),

                    "mode": sk.get("mode", "") or "MODE_2_HARVEST",

                    "flag": flag,

                    "spot_price": sk.get("spot", 0),

                    "adjusted_basis": sk.get("adjusted_basis", 0),

                }

                cycle_log_entries.append(skip_entry)



            ctx.decision_sink.record_cc_cycle(cycle_log_entries, run_id=ctx.run_id)

        except Exception as db_exc:

            logger.exception("Failed to write staged tickets to DB: %s", db_exc)



    # Build output message

    lines = []



    if staged:

        lines.append("\u2501\u2501 Covered Calls \u2501\u2501")

        lines.append("")

        for s in staged:

            label = ACCOUNT_LABELS.get(s["account_id"], s["account_id"])

            pnl_label = (

                f"+${s['walk_away_pnl']:.2f}"

                if s.get("walk_away_pnl", 0) >= 0

                else f"-${abs(s['walk_away_pnl']):.2f}"

            )

            branch = s.get("branch", "BASIS_ANCHOR")

            branch_tag = "anchor" if branch == "BASIS_ANCHOR" else "step-up"

            lines.append(

                f"{s['ticker']} | SELL -{s['quantity']}c "

                f"${s['strike']:.0f}C {s['expiry']} @ ${s['bid']:.2f}"

            )

            lines.append(

                f"  {s['annualized']:.1f}% ann \u00b7 "

                f"{s['otm_pct']:.1f}% OTM \u00b7 {s['dte']}d \u00b7 "

                f"{branch_tag} \u00b7 "

                f"walk-away {pnl_label}/sh \u00b7 {label}"

            )

            lines.append("")



    if dynamic_exit_staged:

        staged_count = sum(1 for r in dynamic_exit_staged if r["staged"])

        total_candidates = len(dynamic_exit_staged)

        lines.append(

            f"\u2501\u2501 Dynamic Exits: {staged_count}/{total_candidates} STAGED \u2501\u2501"

        )

        for r in dynamic_exit_staged:

            lines.append(f"  {r['summary']}")

        lines.append("")



    if skipped:

        lines.append("Skipped:")

        for sk in skipped:

            lines.append(f"  {sk['ticker']}: {sk['reason']}")

        lines.append("")



    lines.append(f"Staged: {len(staged)}")

    lines.append("/approve to send \u00b7 /reject to clear")



    try:

        ib_conn = await ensure_ib_connected()

        defense_status = await _get_active_defense_status(ib_conn)

        lines.insert(0, "\n")

        lines.insert(0, defense_status)

        lines.insert(0, "\u2501\u2501 Active Defense Status \u2501\u2501")

    except Exception as def_exc:

        logger.warning("Failed to inject defense status into CC digest: %s", def_exc)



    return {

        "main_text": "\n".join(lines),

    }





# Backward compat alias

_run_mode1_logic = _run_cc_logic













# ---------------------------------------------------------------------------

# Master Log Refactor v3: flex_sync scheduler + /reconcile command

# ---------------------------------------------------------------------------



async def _scheduled_flex_sync(context: ContextTypes.DEFAULT_TYPE) -> None:

    """EOD flex sync — pulls IBKR Flex data into master_log_* tables."""

    try:

        from agt_equities.flex_sync import run_sync, SyncMode

        result = run_sync(SyncMode.INCREMENTAL)

        msg = (

            f"Flex sync complete (sync_id={result.sync_id})\n"

            f"Status: {result.status}\n"

            f"Sections: {result.sections_processed}\n"

            f"Rows received: {result.rows_received}\n"

            f"Rows inserted: {result.rows_inserted}"

        )

        if result.error_message:

            msg += f"\nError: {result.error_message}"

        await context.bot.send_message(

            chat_id=AUTHORIZED_USER_ID,

            text=f"<pre>{html.escape(msg)}</pre>",

            parse_mode="HTML",

        )

    except Exception as exc:

        logger.exception("Scheduled flex_sync failed: %s", exc)

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=f"<pre>Flex sync FAILED: {html.escape(str(exc)[:500])}</pre>",

                parse_mode="HTML",

            )

        except Exception:

            pass









# ---------------------------------------------------------------------------

# feat(remediation): /approve_rem /reject_rem /list_rem  — incidents queue

# ---------------------------------------------------------------------------

# ADR-007 Step 5 (2026-04-16): these handlers now read/write the structured

# `incidents` table via agt_equities.incidents_repo. The legacy

# remediation_incidents table is still dual-written by incidents_repo for

# one more sprint, so in-flight rows remain reachable. The GitLab API

# helpers (gitlab_lower_approval_rule, gitlab_merge_mr, gitlab_close_mr)

# are re-used from agt_equities.remediation — they're schema-agnostic.

#

# Argument shape:

#   /approve_rem <arg>           arg = numeric incidents.id OR legacy

#   /reject_rem  <arg> <reason>  ALL_CAPS incident_key (back-compat).

#

# Per Yash's go on the Step 5 plan, both id forms are accepted for one

# sprint; numeric id is preferred (leading # is tolerated).





def _resolve_incident_arg(arg: str) -> dict | None:

    """Resolve a /approve_rem or /reject_rem arg to an incidents row.



    Accepts numeric `incidents.id` (with optional leading '#') or the

    legacy ALL_CAPS `incident_key`. Numeric path is preferred; if the

    arg is all-digits it routes to `incidents_repo.get(id)`. Otherwise

    it routes to `get_by_key(key, active_only=True)`, which returns the

    most recent non-closed row for that key — matching legacy

    remediation_incidents single-row-per-id semantics.



    Returns the row dict or None if unknown. Exceptions propagate so

    the handler can surface them to Telegram.

    """

    from agt_equities import incidents_repo as _ir

    s = (arg or "").strip().lstrip("#")

    if not s:

        return None

    if s.isdigit():

        return _ir.get(int(s))

    return _ir.get_by_key(s, active_only=True)





async def cmd_list_rem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/list_rem — show incidents currently awaiting Yash approval."""

    if not is_authorized(update):

        return

    try:

        from agt_equities import incidents_repo as _ir

        rows = await asyncio.to_thread(

            _ir.list_by_status, [_ir.STATUS_AWAITING],

        )

    except Exception as exc:

        logger.exception("/list_rem: list_by_status failed")

        await update.message.reply_text(f"/list_rem error: {exc}")

        return



    if not rows:

        await update.message.reply_text("No incidents awaiting approval.")

        return



    lines = ["\u2501\u2501 Awaiting Approval \u2501\u2501"]

    for r in rows:

        iid = r.get("id")

        key = r.get("incident_key") or "?"

        inv = r.get("invariant_id") or "-"

        mr = r.get("mr_iid") or "-"

        last = (r.get("last_action_at") or r.get("detected_at") or "-")[:16]

        lines.append(

            f"#{iid}  {key}  |  inv {inv}  |  MR !{mr}  |  {last}"

        )

    lines.append("")

    lines.append("/approve_rem <id>    /reject_rem <id> <reason>")

    lines.append("(id = numeric #N or legacy ALL_CAPS key)")

    await update.message.reply_text("\n".join(lines))





async def cmd_approve_rem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/approve_rem <id-or-key> — lower approvals, merge MR, flip incident row.



    Argument accepts numeric incidents.id (with optional leading '#') or

    the legacy ALL_CAPS incident_key for in-flight rows carried over

    from the remediation_incidents era.

    """

    if not is_authorized(update):

        return

    args = context.args or []

    if not args:

        await update.message.reply_text(

            "Usage: /approve_rem <id-or-key>   (see /list_rem)"

        )

        return

    arg = args[0].strip()



    try:

        from agt_equities import incidents_repo as _ir

        from agt_equities import remediation as _rem  # GitLab API helpers only

        row = await asyncio.to_thread(_resolve_incident_arg, arg)

    except Exception as exc:

        logger.exception("/approve_rem: lookup failed")

        await update.message.reply_text(f"/approve_rem error: {exc}")

        return



    if row is None:

        await update.message.reply_text(f"Unknown incident: {arg}")

        return

    if row.get("status") != _ir.STATUS_AWAITING:

        await update.message.reply_text(

            f"#{row.get('id')} {row.get('incident_key')} is in state "

            f"'{row.get('status')}' -- only 'awaiting_approval' can be merged."

        )

        return



    mr_iid = row.get("mr_iid")

    if not mr_iid:

        await update.message.reply_text(

            f"#{row.get('id')} has no MR iid on record -- cannot merge."

        )

        return



    try:

        await asyncio.to_thread(_rem.gitlab_lower_approval_rule, int(mr_iid))

        merge_resp = await asyncio.to_thread(_rem.gitlab_merge_mr, int(mr_iid))

    except Exception as exc:

        logger.exception("/approve_rem: GitLab merge failed")

        await update.message.reply_text(

            f"Merge failed for MR !{mr_iid}: {exc}\n"

            f"Row left at 'awaiting_approval' -- retry or merge manually."

        )

        return



    merged_state = (merge_resp or {}).get("state", "unknown")

    if merged_state != "merged":

        await update.message.reply_text(

            f"GitLab did not confirm merge (state={merged_state}). "

            f"Row left at 'awaiting_approval'."

        )

        return



    try:

        await asyncio.to_thread(_ir.mark_merged, int(row["id"]))

    except Exception as exc:

        logger.exception("/approve_rem: incidents_repo.mark_merged failed")

        await update.message.reply_text(

            f"MR !{mr_iid} merged, but DB update failed: {exc}\n"

            f"Fix manually: UPDATE incidents SET status='merged' "

            f"WHERE id={row['id']}."

        )

        return



    await update.message.reply_text(

        f"\u2705 Merged #{row['id']} {row.get('incident_key')} (MR !{mr_iid})."

    )





async def cmd_reject_rem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/reject_rem <id-or-key> <reason...> — close MR, advance reject state.



    Argument accepts numeric incidents.id or legacy ALL_CAPS incident_key.

    Reason is appended to incidents.rejection_history (ADR-007 Sec4.7) so

    the Author/Critic loop in Step 6 can feed it forward.

    """

    if not is_authorized(update):

        return

    args = context.args or []

    if len(args) < 2:

        await update.message.reply_text(

            "Usage: /reject_rem <id-or-key> <reason...>"

        )

        return

    arg = args[0].strip()

    reason = " ".join(args[1:]).strip()

    if not reason:

        await update.message.reply_text("Reason required -- be specific.")

        return



    try:

        from agt_equities import incidents_repo as _ir

        from agt_equities import remediation as _rem  # GitLab API helpers only

        row = await asyncio.to_thread(_resolve_incident_arg, arg)

    except Exception as exc:

        logger.exception("/reject_rem: lookup failed")

        await update.message.reply_text(f"/reject_rem error: {exc}")

        return



    if row is None:

        await update.message.reply_text(f"Unknown incident: {arg}")

        return



    mr_iid = row.get("mr_iid")

    if mr_iid:

        try:

            await asyncio.to_thread(_rem.gitlab_close_mr, int(mr_iid))

        except Exception as exc:

            # Non-fatal: still advance the state machine so the next

            # authoring cycle sees the rejection. Yash can close the

            # stale MR manually.

            logger.warning("/reject_rem: gitlab_close_mr failed: %s", exc)



    try:

        updated = await asyncio.to_thread(

            _ir.mark_rejected, int(row["id"]), reason,

        )

    except Exception as exc:

        logger.exception("/reject_rem: mark_rejected failed")

        await update.message.reply_text(f"/reject_rem error: {exc}")

        return



    await update.message.reply_text(

        f"\u274c Rejected #{row['id']} {row.get('incident_key')} -> "

        f"{updated.get('status')} (MR !{mr_iid or '-'} closed).\n"

        f"Reason logged: {reason}"

    )





# ---------------------------------------------------------------------------

# Phase 3A: Mode commands

# ---------------------------------------------------------------------------







async def cmd_declare_peacetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/declare_peacetime <audit_memo> — revert from WARTIME/AMBER to PEACETIME."""

    if not is_authorized(update):

        return

    try:

        from agt_equities.mode_engine import get_current_mode, log_mode_transition, MODE_PEACETIME

        memo = " ".join(context.args) if context.args else ""

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                old_mode = get_current_mode(conn)

                if old_mode == MODE_PEACETIME:

                    await update.message.reply_text(f"Already in PEACETIME mode.")

                    return

                if old_mode == "WARTIME" and not memo:

                    await update.message.reply_text(

                        f"\u26d4 WARTIME \u2192 PEACETIME requires an audit memo.\n"

                        f"Usage: /declare_peacetime <reason why wartime conditions have cleared>"

                    )

                    return

                log_mode_transition(conn, old_mode, MODE_PEACETIME,

                                    trigger_rule="manual",

                                    notes=f"/declare_peacetime: {memo}" if memo else "/declare_peacetime")

        await _push_mode_transition(context.application, old_mode, MODE_PEACETIME,

                                     trigger=f"Manual revert" + (f": {memo}" if memo else ""))

        await update.message.reply_text(

            f"\u2705 PEACETIME restored.\n"

            f"Previous mode: {old_mode}\n"

            + (f"Audit memo: {memo}\n" if memo else "")

        )

    except Exception as exc:

        logger.exception("/declare_peacetime failed: %s", exc)

        await update.message.reply_text(f"Failed: {exc}")





async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/mode — show current desk mode and reasoning."""

    if not is_authorized(update):

        return

    try:

        from agt_equities.mode_engine import get_current_mode, get_recent_transitions

        with closing(_get_db_connection()) as conn:

            mode = get_current_mode(conn)

            transitions = get_recent_transitions(conn, limit=3)



        emoji = {"PEACETIME": "\u2705", "AMBER": "\u26a0\ufe0f", "WARTIME": "\U0001f6a8"}.get(mode, "\u2753")

        lines = [f"{emoji} Current mode: {mode}"]

        if transitions:

            lines.append("")

            lines.append("Recent transitions:")

            for t in transitions:

                ts = t.get("timestamp", "?")[:16]

                lines.append(f"  {ts}: {t.get('old_mode')} \u2192 {t.get('new_mode')}"

                             f" ({t.get('trigger_rule', '\u2014')})")

                if t.get("notes"):

                    lines.append(f"    {t['notes'][:80]}")

        await update.message.reply_text("\n".join(lines))

    except Exception as exc:

        logger.exception("/mode failed: %s", exc)

        await update.message.reply_text(f"Failed: {exc}")





def _detect_deck_host() -> str:

    """Return the host to use in deck URLs sent to the operator.



    Priority:

      1. AGT_DECK_HOST env var (explicit override — set to Tailscale IP,

         hostname, or any reachable address the operator wants).

      2. Primary outbound LAN IP via UDP-socket trick (no packet sent,

         just selects the interface the OS would use for internet traffic).

      3. 127.0.0.1 fallback (last resort — only works on the deck machine).



    Note: fallback to 127.0.0.1 is logged at WARNING level so we notice

    if auto-detect is silently broken on a given host.

    """

    override = os.environ.get("AGT_DECK_HOST", "").strip()

    if override:

        return override

    try:

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        try:

            # Does NOT actually send a packet — connect() on UDP just

            # selects the outbound interface.

            s.connect(("8.8.8.8", 80))

            return s.getsockname()[0]

        finally:

            s.close()

    except Exception as exc:

        logger.warning(

            "_detect_deck_host: LAN auto-detect failed (%s), falling back to 127.0.0.1",

            exc,

        )

        return "127.0.0.1"





async def cmd_cure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/cure — returns link to the Deck Cure Console."""

    if not is_authorized(update):

        return

    try:

        deck_token = os.environ.get("AGT_DECK_TOKEN", "")

        deck_host = _detect_deck_host()

        deck_port = os.environ.get("AGT_DECK_PORT", "8787")

        mode = _get_current_desk_mode()

        emoji = {"PEACETIME": "\u2705", "AMBER": "\u26a0\ufe0f", "WARTIME": "\U0001f6a8"}.get(mode, "\u2753")

        url = f"http://{deck_host}:{deck_port}/cure"

        if deck_token:

            url += f"?t={deck_token}"

        await update.message.reply_text(

            f"{emoji} Mode: {mode}\n\n"

            f"Cure Console: {url}"

        )

    except Exception as exc:

        logger.exception("/cure failed: %s", exc)

        await update.message.reply_text(f"Failed: {exc}")









# ---------------------------------------------------------------------------

# Phase 3: Scheduled 9:45 AM ET daily job

# ---------------------------------------------------------------------------



async def _scheduled_cc(context: ContextTypes.DEFAULT_TYPE) -> None:

    """Daily 9:45 AM ET — auto-stage Defensive + Harvest CCs.



    Paper (PAPER_MODE + PAPER_AUTO_EXECUTE): auto-executes staged rows

    immediately after CC staging via _auto_execute_staged. No /approve gate.



    Live: CC rows land in /approve queue.

    """

    try:

        now_et = _datetime.now(ET)

        if now_et.weekday() >= 5:

            logger.info("Scheduled CC: skipping weekend")

            return



        if not CC_AUTO_STAGE_ENABLED:

            logger.info("Auto-staging disabled (CC_AUTO_STAGE_ENABLED=False). Run /cc manually.")

            return



        _cc_ctx = RunContext(

            mode=RunMode.LIVE,

            run_id=uuid.uuid4().hex,

            order_sink=CollectorOrderSink(),

            decision_sink=SQLiteDecisionSink(_log_cc_cycle, _write_dynamic_exit_rows),

        )

        result = await _run_cc_logic(household_filter=None, ctx=_cc_ctx)

        result_text = result["main_text"]



        await context.bot.send_message(

            chat_id=AUTHORIZED_USER_ID,

            text=f"<pre>{html.escape(result_text)}</pre>",

            parse_mode="HTML",

        )



        # MR !70: paper autopilot. Drain any staged rows (CC or otherwise)

        # through IB without a human gate. Live skips this branch.

        if PAPER_MODE and PAPER_AUTO_EXECUTE:

            try:

                ap_placed, ap_failed, ap_lines, ap_status = await _auto_execute_staged()

                if ap_status == "ok":

                    exec_msg = f"[PAPER AUTO-EXEC] Placed: {ap_placed} | Failed: {ap_failed}"

                    if ap_lines:

                        exec_msg += "\n" + "\n".join(ap_lines[:10])

                        if len(ap_lines) > 10:

                            exec_msg += f"\n... ({len(ap_lines) - 10} more)"

                elif ap_status == "ib_fail":

                    exec_msg = ap_lines[0] if ap_lines else "[PAPER AUTO-EXEC] IB connection failed"

                elif ap_status == "race":

                    exec_msg = "[PAPER AUTO-EXEC] No-op (race: another sweeper claimed)"

                else:

                    exec_msg = "[PAPER AUTO-EXEC] No staged orders"

                await context.bot.send_message(

                    chat_id=AUTHORIZED_USER_ID,

                    text=f"<pre>{html.escape(exec_msg)}</pre>",

                    parse_mode="HTML",

                )

            except Exception as auto_exc:

                logger.exception("Scheduled CC auto-execute failed")

                try:

                    await context.bot.send_message(

                        chat_id=AUTHORIZED_USER_ID,

                        text=f"[PAPER AUTO-EXEC] Failed: {auto_exc}",

                    )

                except Exception:

                    pass



    except Exception as exc:

        logger.exception("Scheduled CC failed")

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=f"Scheduled CC staging failed: {exc}",

            )

        except Exception:

            logger.exception("Failed to send scheduled CC error notification")





# Backward compat alias

_scheduled_mode1 = _scheduled_cc





async def _scheduled_csp_scan(context: ContextTypes.DEFAULT_TYPE) -> None:

    """MR !71: Daily 9:35 AM ET — CSP entry scan + allocator staging.



    Paper (PAPER_MODE + PAPER_AUTO_EXECUTE): auto-executes staged CSP rows

    via _auto_execute_staged. No /approve gate.



    Live: CSP rows land in /approve queue (allocator seam MR !69 uses

    Telegram-digest approval_gate — separate ticket).



    Shares plumbing with cmd_scan. Keeps the 6-phase screener + bridge-2

    extras + CSP_GATE_REGISTRY by invoking the same pipeline helpers.

    """

    try:

        now_et = _datetime.now(ET)

        if now_et.weekday() >= 5:

            logger.info("Scheduled CSP scan: skipping weekend")

            return



        # Use AGT_SCAN_LIVE=0 kill switch to disable auto-staging

        if os.getenv("AGT_SCAN_LIVE", "1") != "1":

            logger.info("Scheduled CSP scan disabled (AGT_SCAN_LIVE=0)")

            return



        from pxo_scanner import _load_scan_universe, scan_csp_candidates

        from agt_equities.scan_bridge import (

            adapt_scanner_candidates,

            build_watchlist_sector_map,

            make_bridge2_extras_provider,

        )

        from agt_equities.csp_allocator import (

            _fetch_household_buying_power_snapshot,

            run_csp_allocator,

        )

        from agt_equities.csp_approval_gate import telegram_approval_gate as _tg_gate

        from agt_equities.runtime import RunContext, RunMode

        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink

        from agt_equities.scan_extras import (

            fetch_earnings_map,

            build_correlation_pairs,

        )



        watchlist = await asyncio.to_thread(_load_scan_universe)

        rows = await asyncio.to_thread(scan_csp_candidates, watchlist, 10, 50)

        if not rows:

            logger.info("Scheduled CSP scan: no candidates from screener")

            return



        candidates = adapt_scanner_candidates(rows)

        if not candidates:

            logger.info("Scheduled CSP scan: no candidates survived adapter")

            return



        def _fetch_vix() -> float:

            try:

                hist = yf.Ticker("^VIX").history(period="1d")

                if len(hist) and "Close" in hist.columns:

                    return float(hist["Close"].iloc[-1])

            except Exception:

                pass

            return 20.0

        vix = await asyncio.to_thread(_fetch_vix)



        disco = await _discover_positions(None)

        ib_conn = await ensure_ib_connected()

        snapshots = await _fetch_household_buying_power_snapshot(ib_conn, disco)

        if not snapshots:

            logger.warning("Scheduled CSP scan: household snapshots empty")

            return



        candidate_tickers = [c.ticker for c in candidates]

        all_holding_tickers: set[str] = set()

        for _hh_snap in snapshots.values():

            all_holding_tickers.update(_hh_snap.get("existing_positions", {}).keys())

            all_holding_tickers.update(_hh_snap.get("existing_csps", {}).keys())



        earnings_map = await asyncio.to_thread(fetch_earnings_map, candidate_tickers)

        correlation_pairs = await asyncio.to_thread(

            build_correlation_pairs, candidate_tickers, sorted(all_holding_tickers),

        )



        sector_map = build_watchlist_sector_map(watchlist)

        extras_provider = make_bridge2_extras_provider(

            sector_map, earnings_map, correlation_pairs,

        )



        # ADR-008 MR 2: ctx carries the order sink. Live scheduled scan

        # wires SQLiteOrderSink so ctx.order_sink.stage(tickets, ...) is

        # byte-identical to the prior append_pending_tickets(tickets)

        # staging path.

        ctx = RunContext(

            mode=RunMode.LIVE,

            run_id=uuid.uuid4().hex,

            order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),

            decision_sink=NullDecisionSink(),

        )

        _require_approval = (

            os.environ.get("AGT_CSP_REQUIRE_APPROVAL", "false").lower() == "true"

        )

        _gate = _tg_gate if _require_approval else None



        import functools as _functools

        _allocator_call = _functools.partial(

            run_csp_allocator,

            ray_candidates=candidates,

            snapshots=snapshots,

            vix=vix,

            extras_provider=extras_provider,

            ctx=ctx,

            approval_gate=_gate,

        )

        result = await asyncio.to_thread(_allocator_call)



        staged_n = result.total_staged_contracts

        digest = "\n".join(result.digest_lines or ["(no allocator output)"])

        header = (

            f"\u2501\u2501 scheduled CSP scan {now_et.strftime('%Y-%m-%d %H:%M')} "

            f"\u2501\u2501\nCandidates: {len(candidates)} \u00b7 VIX: {vix:.1f} "

            f"\u00b7 Staged: {staged_n}\n"

        )

        await context.bot.send_message(

            chat_id=AUTHORIZED_USER_ID,

            text=f"<pre>{html.escape(header + digest)}</pre>",

            parse_mode="HTML",

        )



        # Paper autopilot: drain staged rows through IB, no /approve.

        if PAPER_MODE and PAPER_AUTO_EXECUTE and staged_n:

            try:

                ap_placed, ap_failed, ap_lines, ap_status = await _auto_execute_staged()

                exec_msg = (

                    f"[PAPER AUTO-EXEC] status={ap_status} "

                    f"placed={ap_placed} failed={ap_failed}"

                )

                if ap_lines:

                    exec_msg += "\n" + "\n".join(ap_lines[:10])

                    if len(ap_lines) > 10:

                        exec_msg += f"\n... ({len(ap_lines) - 10} more)"

                await context.bot.send_message(

                    chat_id=AUTHORIZED_USER_ID,

                    text=f"<pre>{html.escape(exec_msg)}</pre>",

                    parse_mode="HTML",

                )

            except Exception as auto_exc:

                logger.exception("Scheduled CSP scan auto-execute failed")

                try:

                    await context.bot.send_message(

                        chat_id=AUTHORIZED_USER_ID,

                        text=f"[PAPER AUTO-EXEC] Failed: {auto_exc}",

                    )

                except Exception:

                    pass



    except Exception as exc:

        logger.exception("Scheduled CSP scan failed")

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=f"Scheduled CSP scan failed: {exc}",

            )

        except Exception:

            logger.exception("Failed to send scheduled CSP scan error notification")





# ---------------------------------------------------------------------------

# WHEEL-4 glue helpers

# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------

# WHEEL-7 — CRITICAL_PAGER + LiquidateResult approval flow

# ---------------------------------------------------------------------------

# The V2 Router evaluator emits AlertResult(severity="CRITICAL") on cascade

# exhaustion or unexpected exceptions, and LiquidateResult when a defensive

# roll has no acceptable target and the cascade recommends a full position

# unwind. Prior to WHEEL-7 both variants only appeared in the bundled 3:30

# PM watchdog digest — no @here-style priority, no actionable keyboard.

#

# WHEEL-7 wires:

#   * AlertResult(CRITICAL) → immediate out-of-band priority message

#     (no keyboard, informational pager).

#   * LiquidateResult → immediate priority message with [STAGE LIQUIDATE]

#     [REJECT] inline keyboard. STAGE synthesizes two pending tickets

#     (BTC calls + STC underlying), both transmit=False — operator must

#     still run /approve to fire. REJECT logs and drops the event.

#

# Token store is in-memory, 30-minute TTL. Bot restart → token lost →

# operator re-runs /rollcheck (no silent data loss).

# ---------------------------------------------------------------------------



_LIQ_STAGING_BY_TOKEN: dict[str, dict] = {}

_LIQ_TOKEN_TTL_S = 30 * 60  # 30 minutes





def _liq_gc() -> None:

    """Drop expired liquidation tokens in-place."""

    now = _datetime.now().timestamp()

    expired = [

        t for t, p in _LIQ_STAGING_BY_TOKEN.items()

        if now - p.get("_created_ts", 0) > _LIQ_TOKEN_TTL_S

    ]

    for t in expired:

        try:

            del _LIQ_STAGING_BY_TOKEN[t]

        except KeyError:

            pass





def _liq_keyboard(token: str) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup([[

        InlineKeyboardButton("STAGE LIQUIDATE", callback_data=f"liq:stage:{token}"),

        InlineKeyboardButton("REJECT", callback_data=f"liq:reject:{token}"),

    ]])





def _build_liquidate_tickets(payload: dict) -> list[dict]:

    """Synthesize BTC calls + STC shares tickets from a LiquidateResult payload.



    Both tickets ship transmit=False so the operator must explicitly

    /approve before anything hits the wire. The BTC leg uses the

    evaluator's btc_limit; the STC leg is a MKT order (sized by `shares`)

    to close the underlying immediately after the calls cover. Operator

    can hand-edit either before approving.

    """

    acct_id = payload.get("account_id") or ""

    acct_label = ACCOUNT_LABELS.get(acct_id, acct_id)

    ticker = payload.get("ticker") or ""

    contracts = int(payload.get("contracts") or 0)

    shares = int(payload.get("shares") or 0)

    btc_limit = float(payload.get("btc_limit") or 0.0)

    strike = float(payload.get("strike") or 0.0)

    expiry = payload.get("expiry") or ""



    tickets: list[dict] = []

    if contracts > 0 and strike > 0 and expiry:

        tickets.append({

            "timestamp": _datetime.now().isoformat(),

            "account_id": acct_id,

            "account_label": acct_label,

            "ticker": ticker,

            "sec_type": "OPT",

            "action": "BUY",

            "quantity": contracts,

            "order_type": "LMT",

            "limit_price": round(btc_limit, 2),

            "expiry": str(expiry).replace("-", ""),

            "strike": strike,

            "right": "C",

            "status": "staged",

            "transmit": False,                       # operator /approve gate

            "strategy": "WHEEL-7 Liquidate BTC",

            "mode": "LIQUIDATE",

            "origin": "roll_engine",

            "v2_state": "LIQUIDATE",

            "v2_rationale": payload.get("reason", ""),

        })

    if shares > 0:

        tickets.append({

            "timestamp": _datetime.now().isoformat(),

            "account_id": acct_id,

            "account_label": acct_label,

            "ticker": ticker,

            "sec_type": "STK",

            "action": "SELL",

            "quantity": shares,

            "order_type": "MKT",

            "status": "staged",

            "transmit": False,                       # operator /approve gate

            "strategy": "WHEEL-7 Liquidate STC",

            "mode": "LIQUIDATE",

            "origin": "roll_engine",

            "v2_state": "LIQUIDATE",

            "v2_rationale": payload.get("reason", ""),

        })

    return tickets





async def _page_critical_event(bot, kind: str, payload: dict) -> None:

    """Send an out-of-band priority message for a V2 Router critical event.



    kind is one of 'CRITICAL' | 'LIQUIDATE'. On LIQUIDATE the payload is

    stashed in _LIQ_STAGING_BY_TOKEN keyed on a short uuid4 token so the

    callback handler can look it up when the operator hits STAGE/REJECT.

    """

    try:

        _liq_gc()

        if kind == "CRITICAL":

            ticker = payload.get("ticker", "?")

            acct = payload.get("account_id", "?")

            reason = payload.get("reason", "")

            text = (

                "\U0001f6a8 <b>CRITICAL PAGER</b>\n"

                f"Ticker: <code>{html.escape(str(ticker))}</code>\n"

                f"Account: <code>{html.escape(str(acct))}</code>\n"

                f"Reason: <pre>{html.escape(str(reason))}</pre>"

            )

            await bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=text,

                parse_mode="HTML",

                disable_notification=False,

            )

            return



        if kind == "LIQUIDATE":

            import uuid as _uuid

            token = _uuid.uuid4().hex[:10]

            payload = dict(payload)

            payload["_created_ts"] = _datetime.now().timestamp()

            _LIQ_STAGING_BY_TOKEN[token] = payload

            ticker = payload.get("ticker", "?")

            acct = payload.get("account_id", "?")

            contracts = payload.get("contracts", 0)

            shares = payload.get("shares", 0)

            btc = payload.get("btc_limit", 0.0)

            stc_ref = payload.get("stc_market_ref", 0.0)

            net = payload.get("net_proceeds_per_share", 0.0)

            reason = payload.get("reason", "")

            text = (

                "\U0001f6a8 <b>LIQUIDATE REQUEST</b>\n"

                f"Ticker: <code>{html.escape(str(ticker))}</code> "

                f"| Account: <code>{html.escape(str(acct))}</code>\n"

                f"BTC: <code>{contracts}c @ {float(btc):.2f}</code>\n"

                f"STC: <code>{shares}sh @ MKT (ref ~{float(stc_ref):.2f})</code>\n"

                f"Net proceeds: <code>{float(net):.2f}/sh</code>\n"

                f"Reason: <pre>{html.escape(str(reason))}</pre>"

            )

            # MR !71: paper autopilot. Skip the STAGE/REJECT keyboard and

            # auto-stage + drain via _auto_execute_staged. Live still goes

            # through the manual keyboard gate for safety.

            if PAPER_MODE and PAPER_AUTO_EXECUTE:

                try:

                    _LIQ_STAGING_BY_TOKEN.pop(token, None)  # token unused in auto path

                    tickets = _build_liquidate_tickets(payload)

                    if not tickets:

                        await bot.send_message(

                            chat_id=AUTHORIZED_USER_ID,

                            text=text + "\n\n\u26a0\ufe0f <b>PAPER</b>: no tickets synthesized (0 contracts/0 shares).",

                            parse_mode="HTML",

                            disable_notification=False,

                        )

                        return

                    await asyncio.to_thread(append_pending_tickets, tickets)

                    ap_placed, ap_failed, ap_lines, ap_status = await _auto_execute_staged()

                    exec_msg = (

                        f"[PAPER AUTO-EXEC] status={ap_status} "

                        f"placed={ap_placed} failed={ap_failed}"

                    )

                    if ap_lines:

                        exec_msg += "\n" + "\n".join(ap_lines[:10])

                        if len(ap_lines) > 10:

                            exec_msg += f"\n... ({len(ap_lines) - 10} more)"

                    await bot.send_message(

                        chat_id=AUTHORIZED_USER_ID,

                        text=text + f"\n\n\u2705 <b>PAPER AUTO-EXEC</b>\n<pre>{html.escape(exec_msg)}</pre>",

                        parse_mode="HTML",

                        disable_notification=False,

                    )

                except Exception as exc:

                    logger.exception("MR !71 LIQUIDATE paper autopilot failed")

                    try:

                        await bot.send_message(

                            chat_id=AUTHORIZED_USER_ID,

                            text=text + f"\n\n\u274c <b>PAPER AUTO-EXEC FAILED</b>\n<pre>{html.escape(str(exc))}</pre>",

                            parse_mode="HTML",

                            disable_notification=False,

                        )

                    except Exception:

                        pass

                return

            # Live path: manual STAGE/REJECT keyboard

            text += (

                "\nSTAGE = create pending tickets (transmit=False, /approve to fire).\n"

                "REJECT = drop event, position held."

            )

            await bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=text,

                parse_mode="HTML",

                reply_markup=_liq_keyboard(token),

                disable_notification=False,

            )

            return

    except Exception as exc:

        logger.exception("WHEEL-7 _page_critical_event failed: %s", exc)





async def handle_liq_callback(

    update: Update, context: ContextTypes.DEFAULT_TYPE,

) -> None:

    """Callback handler for liq:stage:<token> and liq:reject:<token>."""

    query = update.callback_query

    user = update.effective_user

    if user is None or user.id != AUTHORIZED_USER_ID:

        try:

            await query.answer("Unauthorized.", show_alert=True)

        except Exception:

            pass

        return



    data = (query.data or "").split(":", 2)

    if len(data) != 3 or data[0] != "liq":

        await query.answer("Malformed callback.", show_alert=True)

        return

    action, token = data[1], data[2]



    _liq_gc()

    payload = _LIQ_STAGING_BY_TOKEN.pop(token, None)

    if payload is None:

        try:

            await query.answer("Token expired. Re-run /rollcheck.", show_alert=True)

            await query.edit_message_reply_markup(reply_markup=None)

        except Exception:

            pass

        return



    if action == "reject":

        try:

            await query.answer("Rejected.")

            await query.edit_message_text(

                query.message.text_html + "\n\n\u274c <b>REJECTED</b> — position held.",

                parse_mode="HTML",

            )

        except Exception as exc:

            logger.warning("WHEEL-7 reject edit failed: %s", exc)

        return



    if action == "stage":

        try:

            tickets = _build_liquidate_tickets(payload)

            if not tickets:

                await query.answer("No tickets synthesized.", show_alert=True)

                return

            await asyncio.to_thread(append_pending_tickets, tickets)

            legs = ", ".join(

                f"{t['action']} {t['quantity']} {t['sec_type']}" for t in tickets

            )

            await query.answer("Staged.")

            await query.edit_message_text(

                query.message.text_html

                + f"\n\n\u2705 <b>STAGED</b>: {html.escape(legs)}. "

                "Run <code>/approve</code> to fire.",

                parse_mode="HTML",

            )

        except Exception as exc:

            logger.exception("WHEEL-7 stage failed: %s", exc)

            try:

                await query.answer(f"Stage failed: {exc}", show_alert=True)

            except Exception:

                pass

        return



    await query.answer("Unknown action.", show_alert=True)










async def _scheduled_watchdog(context: ContextTypes.DEFAULT_TYPE) -> None:

    """Daily 3:30 PM ET — roll alerts, mode transitions, Rule 8 triggers."""

    try:

        now_et = _datetime.now(ET)

        if now_et.weekday() >= 5:

            return



        alerts: list[str] = []

        today = _date.today()



        # ── CSP harvest sweep (M2): stage BTC on profitable short puts ──

        try:

            from agt_equities.csp_harvest import scan_csp_harvest_candidates
            from agt_equities.runtime import RunContext, RunMode
            from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink

            ib_conn = await ensure_ib_connected()



            import uuid
            ctx = RunContext(
                mode=RunMode.LIVE,
                run_id=uuid.uuid4().hex,
                order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),
                decision_sink=NullDecisionSink(),
            )
            csp_result = await scan_csp_harvest_candidates(ib_conn, ctx=ctx)

            for a in csp_result.get("alerts", []):

                alerts.append(a)

        except Exception as csp_exc:

            logger.warning("Watchdog CSP harvest failed: %s", csp_exc)



        # ── Cache cleanup: purge expired dashboard + confirmation entries ──

        try:

            now_dt = _datetime.now()

            expired_dash = [

                cid for cid, entry in dashboard_cache.items()

                if (now_dt - entry.get("created_at", now_dt)).total_seconds() > DASHBOARD_TTL

            ]

            for cid in expired_dash:

                dashboard_cache.pop(cid, None)



            expired_cc = [

                key for key, entry in cc_confirmation_cache.items()

                if (now_dt - entry.get("created_at", now_dt)).total_seconds() > DASHBOARD_TTL

            ]

            for key in expired_cc:

                cc_confirmation_cache.pop(key, None)



            if expired_dash or expired_cc:

                logger.info(

                    "Cache sweep: cleared %d dashboard + %d CC confirmation entries",

                    len(expired_dash), len(expired_cc),

                )

        except Exception as cache_exc:

            logger.warning("Cache sweep failed: %s", cache_exc)



        # ── Roll alerts: active watchlist entries expiring within 5 days ──

        try:

            with closing(_get_db_connection()) as conn:

                rows = conn.execute(

                    """

                    SELECT id, ticker, account_id, strike, expiry, quantity, mode

                    FROM roll_watchlist

                    WHERE status = 'active'

                    ORDER BY expiry ASC

                    """

                ).fetchall()



            for r in rows:

                try:

                    exp_date = _date.fromisoformat(r["expiry"])

                    dte = (exp_date - today).days

                except (ValueError, TypeError):

                    continue



                if dte <= 3:

                    label = ACCOUNT_LABELS.get(r["account_id"], r["account_id"] or "?")

                    alerts.append(

                        f"\u26a0\ufe0f ROLL NOW: {r['ticker']} "

                        f"-{r['quantity']}c ${r['strike']:.0f}C "

                        f"{r['expiry']} ({dte}d) | {label}"

                    )

                elif dte <= 5:

                    label = ACCOUNT_LABELS.get(r["account_id"], r["account_id"] or "?")

                    alerts.append(

                        f"\u23f0 Prepare roll: {r['ticker']} "

                        f"-{r['quantity']}c ${r['strike']:.0f}C "

                        f"{r['expiry']} ({dte}d) | {label}"

                    )



                # Auto-resolve expired entries

                if dte < 0:

                    with closing(_get_db_connection()) as conn:

                        with tx_immediate(conn):

                            conn.execute(

                                "UPDATE roll_watchlist SET status = 'expired', resolved_at = datetime('now') WHERE id = ?",

                                (r["id"],),

                            )

        except Exception as rw_exc:

            logger.warning("Watchdog roll check failed: %s", rw_exc)



        # ── Mode transitions: check for positions that changed mode ──

        try:

            disco = await _discover_positions(None)

            with closing(_get_db_connection()) as conn:

                for hh_data in disco.get("households", {}).values():

                    for p in hh_data.get("positions", []):

                        ticker = p["ticker"]

                        current_mode = p["mode"]

                        hh = p["household"]



                        last_row = conn.execute(

                            """

                            SELECT to_mode FROM mode_transitions

                            WHERE ticker = ? AND household = ?

                            ORDER BY created_at DESC LIMIT 1

                            """,

                            (ticker, hh),

                        ).fetchone()



                        last_mode = last_row["to_mode"] if last_row else None



                        if last_mode and last_mode != current_mode:

                            conn.execute(

                                """

                                INSERT INTO mode_transitions

                                    (ticker, household, from_mode, to_mode, spot, adjusted_basis)

                                VALUES (?, ?, ?, ?, ?, ?)

                                """,

                                (ticker, hh, last_mode, current_mode,

                                 p.get("spot_price"), p.get("adjusted_basis")),

                            )

                            alerts.append(

                                f"\U0001f504 Mode change: {ticker} "

                                f"{last_mode} \u2192 {current_mode} "

                                f"(spot ${p.get('spot_price', 0):.2f})"

                            )

                        elif last_mode is None:

                            # Seed initial mode

                            conn.execute(

                                """

                                INSERT INTO mode_transitions

                                    (ticker, household, from_mode, to_mode, spot, adjusted_basis)

                                VALUES (?, ?, ?, ?, ?, ?)

                                """,

                                (ticker, hh, current_mode, current_mode,

                                 p.get("spot_price"), p.get("adjusted_basis")),

                            )

        except Exception as mt_exc:

            logger.warning("Watchdog mode transition check failed: %s", mt_exc)



        # ── Clear overweight markers for positions that recovered ──

        try:

            if disco:

                for hh_data in disco.get("households", {}).values():

                    hh_nlv = hh_data.get("household_nlv", 0)

                    if hh_nlv <= 0:

                        continue

                    for p in hh_data.get("positions", []):

                        position_value = p["total_shares"] * p["spot_price"]

                        position_pct = (position_value / hh_nlv * 100)



                        if position_pct <= DYNAMIC_EXIT_RULE1_LIMIT * 100:

                            # Position is no longer overweight — clear marker

                            with closing(_get_db_connection()) as conn:

                                with tx_immediate(conn):

                                    conn.execute(

                                        """

                                        UPDATE mode_transitions

                                        SET to_mode = 'RECOVERED'

                                        WHERE ticker = ? AND household = ?

                                          AND to_mode = 'OVERWEIGHT'

                                        """,

                                        (p["ticker"], p["household"]),

                                    )

        except Exception as rec_exc:

            logger.warning("Overweight recovery check failed: %s", rec_exc)



        # ── Rule 8 trigger: 3+ consecutive low-yield cycles ──

        try:

            with closing(_get_db_connection()) as conn:

                tickers_with_cycles = conn.execute(

                    """

                    SELECT DISTINCT ticker FROM cc_cycle_log

                    WHERE mode = 'MODE_1_DEFENSIVE'

                    """

                ).fetchall()



                for row in tickers_with_cycles:

                    tkr = row["ticker"]

                    recent = conn.execute(

                        """

                        SELECT flag FROM cc_cycle_log

                        WHERE ticker = ? AND mode = 'MODE_1_DEFENSIVE'

                        ORDER BY created_at DESC LIMIT 3

                        """,

                        (tkr,),

                    ).fetchall()



                    if len(recent) >= 3:

                        all_low = all(

                            (r["flag"] or "NORMAL") in ("LOW_YIELD", "NO_VIABLE_STRIKE", "SKIPPED")

                            for r in recent

                        )

                        if all_low:

                            alerts.append(

                                f"\U0001f6a8 Rule 8 trigger: {tkr} — "

                                f"3+ consecutive LOW-YIELD cycles. "

                                f"Evaluate Dynamic Exit."

                            )

        except Exception as r8_exc:

            logger.warning("Watchdog Rule 8 check failed: %s", r8_exc)



        # ── V7: Auto-generate Dynamic Exit payloads at escalated frequency ──

        try:

            if disco:  # reuse discovery from mode transition check

                for hh_name, hh_data in disco.get("households", {}).items():

                    hh_nlv = hh_data.get("household_nlv", 0)

                    if hh_nlv <= 0:

                        continue

                    for p in hh_data.get("positions", []):

                        position_value = p["total_shares"] * p["spot_price"]

                        position_pct = (position_value / hh_nlv * 100)



                        if position_pct <= DYNAMIC_EXIT_RULE1_LIMIT * 100:

                            continue



                        # Drawdown Exception — don't trigger if stock is down 30%+ and under 30%

                        adj_basis = p.get("adjusted_basis", 0)

                        if adj_basis > 0:

                            drawdown = (p["spot_price"] - adj_basis) / adj_basis

                            if drawdown <= -0.30 and position_pct <= 30:

                                continue



                        escalation = _compute_escalation_tier(position_pct)



                        # Calendar-based trigger: when was position first overweight?

                        with closing(_get_db_connection()) as conn:

                            ow_record = conn.execute(

                                """

                                SELECT overweight_since FROM mode_transitions

                                WHERE ticker = ? AND household = ?

                                  AND to_mode = 'OVERWEIGHT'

                                ORDER BY created_at DESC LIMIT 1

                                """,

                                (p["ticker"], p["household"]),

                            ).fetchone()



                            if ow_record and ow_record["overweight_since"]:

                                try:

                                    ow_date = _date.fromisoformat(

                                        ow_record["overweight_since"][:10]

                                    )

                                    days_overweight = (_date.today() - ow_date).days

                                except (ValueError, TypeError):

                                    days_overweight = 0

                            else:

                                # First time seeing this overweight — record it

                                days_overweight = 0

                                conn.execute(

                                    """

                                    INSERT INTO mode_transitions

                                        (ticker, household, from_mode, to_mode,

                                         spot, adjusted_basis, overweight_since)

                                    VALUES (?, ?, 'MODE_1', 'OVERWEIGHT', ?, ?, ?)

                                    """,

                                    (

                                        p["ticker"], p["household"],

                                        p["spot_price"],

                                        p.get("adjusted_basis", 0),

                                        _date.today().isoformat(),

                                    ),

                                )



                        # Evaluate based on calendar + escalation tier

                        # EVERY_CYCLE (>40%): evaluate after 7+ days

                        # EVERY_2_CYCLES (25-40%): evaluate after 14+ days

                        # STANDARD (<25%): evaluate after 21+ days

                        ESCALATION_DAYS = {

                            "EVERY_CYCLE": 7,

                            "EVERY_2_CYCLES": 14,

                            "STANDARD": 21,

                        }

                        required_days = ESCALATION_DAYS.get(

                            escalation["tier"], 21

                        )



                        if days_overweight >= required_days:

                            stage_result = await _stage_dynamic_exit_candidate(

                                p["ticker"], hh_name, hh_data, p,

                                source="scheduled_watchdog",

                            )

                            alerts.append(

                                f"{stage_result['summary']} "

                                f"({days_overweight}d overweight)"

                            )

                            if stage_result["staged"]:

                                alerts.append("Review in Cure Console \u2192 /cure")

                            await asyncio.sleep(3)

        except Exception as de_exc:

            logger.warning("Watchdog Dynamic Exit check failed: %s", de_exc)



        # ── V2 Defensive Roll Protocol (Evaluated after DB housekeeping) ──

        try:

            ib_conn = await ensure_ib_connected()



            async def _watchdog_priority_cb(kind: str, payload: dict) -> None:

                # WHEEL-7 pager — out-of-band from the bundled digest below.

                await _page_critical_event(context.bot, kind, payload)



            from agt_equities.runtime import RunContext, RunMode
            from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
            _watchdog_roll_ctx = RunContext(
                mode=RunMode.LIVE,
                run_id=uuid.uuid4().hex,
                order_sink=SQLiteOrderSink(staging_fn=append_pending_tickets),
                decision_sink=NullDecisionSink(),
            )
            roll_alerts = await roll_scanner.scan_and_stage_defensive_rolls(
                ib_conn,
                ctx=_watchdog_roll_ctx,
                priority_cb=_watchdog_priority_cb,
                ibkr_get_spot=_ibkr_get_spot,
                load_premium_ledger=_load_premium_ledger_snapshot,
                get_desk_mode=_get_current_desk_mode,
                ibkr_get_expirations=_ibkr_get_expirations,
                ibkr_get_chain=_ibkr_get_chain,
                account_labels=ACCOUNT_LABELS,
                is_halted=_HALTED,
            )

            alerts.extend(roll_alerts)

        except Exception as roll_exc:

            logger.warning("Watchdog defensive roll protocol failed: %s", roll_exc)



        # Send alerts

        if alerts:

            msg = "\u2501\u2501 3:30 PM Watchdog \u2501\u2501\n\n" + "\n\n".join(alerts)

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=msg,

            )

        else:

            logger.info("Watchdog: no alerts at 3:30 PM")



    except Exception as exc:

        logger.exception("Scheduled watchdog failed")

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=f"Watchdog failed: {exc}",

            )

        except Exception:

            logger.exception("Failed to send watchdog error notification")





async def _refresh_conviction_data() -> dict:

    """Weekly: refresh conviction for held tickers only."""

    try:

        ib_conn = await ensure_ib_connected()

        positions = await ib_conn.reqPositionsAsync()



        held = set()

        for pos in positions:

            if pos.position != 0 and pos.contract.secType == "STK":

                tkr = pos.contract.symbol.upper()

                if tkr not in EXCLUDED_TICKERS:

                    held.add(tkr)



        updated = 0

        failed = 0

        for tkr in held:

            try:

                c = _compute_conviction_tier(tkr)

                _persist_conviction(tkr, c)

                updated += 1

            except Exception as tkr_exc:

                logger.warning("Conviction refresh failed for %s: %s", tkr, tkr_exc)

                failed += 1



        return {"updated": updated, "failed": failed, "total": len(held), "error": None}

    except Exception as exc:

        logger.exception("_refresh_conviction_data failed")

        return {"updated": 0, "failed": 0, "total": 0, "error": str(exc)}





async def _scheduled_conviction_refresh(context: ContextTypes.DEFAULT_TYPE) -> None:

    """Sunday 8 PM ET — refresh conviction data."""

    try:

        result = await _refresh_conviction_data()

        msg = f"Conviction refresh: {result['updated']}/{result['total']} tickers"

        if result["failed"]:

            msg += f" ({result['failed']} failed)"

        if result["error"]:

            msg += f"\nError: {result['error']}"

        await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=msg)

    except Exception as exc:

        logger.exception("Scheduled conviction refresh failed")





async def _scheduled_universe_refresh(context: ContextTypes.DEFAULT_TYPE) -> None:

    """Monthly 1st at 6:00 AM ET — refresh ticker_universe from Wikipedia + yfinance."""

    try:

        logger.info("Scheduled universe refresh starting...")

        result = await _refresh_ticker_universe()



        if result["error"]:

            msg = (

                f"Universe refresh (scheduled) completed with errors:\n"

                f"Added: {result['added']} | Updated: {result['updated']}\n"

                f"Error: {result['error']}"

            )

        else:

            msg = (

                f"Universe refresh (scheduled) complete.\n"

                f"Added: {result['added']} | Updated: {result['updated']} | "

                f"Total: {result['total']}"

            )



        logger.info("Scheduled universe refresh: %s", msg)

        await context.bot.send_message(

            chat_id=AUTHORIZED_USER_ID,

            text=msg,

        )

    except Exception as exc:

        logger.exception("Scheduled universe refresh failed")

        try:

            await context.bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text=f"Scheduled universe refresh failed: {exc}",

            )

        except Exception:

            logger.exception("Failed to send universe refresh error notification")





# ---------------------------------------------------------------------------

# Entry point

# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------

# Followup #17: /recover_transmitting operator command (D6, D9, D11)

# ---------------------------------------------------------------------------





async def cmd_recover_transmitting(

    update: Update, context: ContextTypes.DEFAULT_TYPE,

) -> None:

    """Manual recovery for rows stuck in TRANSMITTING state.



    Usage:

      /recover_transmitting <audit_id> filled [ib_order_id]

      /recover_transmitting <audit_id> abandoned



    Trusts operator unconditionally (D6). Operator-provided ib_order_id

    overwrites any existing value.

    """

    if not is_authorized(update):

        return



    args = context.args or []

    if len(args) < 2:

        await update.message.reply_text(

            "Usage:\n"

            "  /recover_transmitting <audit_id> filled [ib_order_id]\n"

            "  /recover_transmitting <audit_id> abandoned"

        )

        return



    audit_id = args[0]

    action = args[1].lower()

    ib_order_id_arg = None

    if len(args) > 2:

        try:

            ib_order_id_arg = int(args[2])

        except ValueError:

            await update.message.reply_text("ib_order_id must be an integer.")

            return



    if action not in ("filled", "abandoned"):

        await update.message.reply_text("Action must be 'filled' or 'abandoned'.")

        return



    new_status = "TRANSMITTED" if action == "filled" else "ABANDONED"

    operator_id = update.effective_user.id



    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                row = conn.execute(

                    "SELECT final_status FROM bucket3_dynamic_exit_log "

                    "WHERE audit_id = ?",

                    (audit_id,),

                ).fetchone()



                if row is None:

                    # Must reply AFTER conn closes (cross-await rule)

                    pass

                elif row["final_status"] != "TRANSMITTING":

                    pass

                else:

                    pre_status = row["final_status"]



                    # Status flip with CAS guard

                    if action == "filled":

                        result = conn.execute(

                            "UPDATE bucket3_dynamic_exit_log "

                            "SET final_status = ?, ib_order_id = ?, "

                            "    last_updated = CURRENT_TIMESTAMP "

                            "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                            (new_status, ib_order_id_arg, audit_id),

                        )

                    else:

                        result = conn.execute(

                            "UPDATE bucket3_dynamic_exit_log "

                            "SET final_status = ?, "

                            "    last_updated = CURRENT_TIMESTAMP "

                            "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                            (new_status, audit_id),

                        )



                    if result.rowcount != 1:

                        raise RuntimeError(

                            f"recover_transmitting CAS failed: audit_id={audit_id}"

                        )



                    # Audit log INSERT (same transaction — rolls back together)

                    conn.execute(

                        "INSERT INTO recovery_audit_log "

                        "(audit_id, operator_user_id, recovery_action, "

                        " pre_status, post_status, ib_order_id_provided) "

                        "VALUES (?, ?, ?, ?, ?, ?)",

                        (audit_id, operator_id, action, pre_status,

                         new_status, ib_order_id_arg),

                    )



        # Replies after conn is closed (cross-await safe)

        if row is None:

            await update.message.reply_text(f"audit_id {audit_id[:12]}... not found.")

            return

        if row["final_status"] != "TRANSMITTING":

            await update.message.reply_text(

                f"Row not in TRANSMITTING (current: {row['final_status']}). "

                f"No action taken."

            )

            return



        _dispatched_audits.discard(audit_id)

        await update.message.reply_text(

            f"\u2705 Recovery applied: {audit_id[:8]}... \u2192 {new_status}"

            + (f"\nib_order_id: {ib_order_id_arg}" if ib_order_id_arg else "")

        )

        logger.info(

            "RECOVERY: audit_id=%s %s->%s by user %s",

            audit_id, "TRANSMITTING", new_status, operator_id,

        )

    except Exception as exc:

        logger.exception("cmd_recover_transmitting failed: %s", exc)

        try:

            await update.message.reply_text(f"Recovery failed: {exc}")

        except Exception:

            pass





# ---------------------------------------------------------------------------

# Sprint 1D: /halt killswitch

# ---------------------------------------------------------------------------



async def cmd_halt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/halt — emergency killswitch. Stops all scheduled jobs and blocks trades.



    Sets in-process _HALTED flag + persists disabled state to execution_state DB.

    """

    global _HALTED

    if not is_authorized(update):

        return

    _HALTED = True

    logger.warning("DESK HALTED by operator via /halt")



    cancelled = 0

    try:

        jq = context.application.job_queue

        if jq:

            for job in jq.jobs():

                job.schedule_removal()

                cancelled += 1

    except Exception as exc:

        logger.warning("/halt job cancellation error: %s", exc)



    # Persist to DB (survives restart)

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                conn.execute(

                    "INSERT OR REPLACE INTO execution_state "

                    "(id, disabled, set_by, set_at, reason) "

                    "VALUES (1, 1, ?, datetime('now'), ?)",

                    (str(update.effective_user.id), "/halt command"),

                )

    except Exception as exc:

        logger.warning("/halt DB persist failed: %s", exc)



    await update.message.reply_text(

        f"\U0001f6d1 DESK HALTED\n"

        f"Cancelled {cancelled} scheduled jobs.\n"

        f"All trade gates blocked. IB connection preserved.\n"

        f"Execution disabled in DB (persists across restarts).\n"

        f"Use /resume CONFIRM to re-enable."

    )





async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/resume CONFIRM — re-enable execution after /halt. Requires explicit CONFIRM token."""

    global _HALTED

    if not is_authorized(update):

        return



    args = (update.message.text or "").split()

    if len(args) < 2 or args[1] != "CONFIRM":

        # Show current state and require confirmation

        env_ok = os.getenv("AGT_EXECUTION_ENABLED", "false").strip().lower() == "true"

        await update.message.reply_text(

            f"\u26a0\ufe0f Resume requires explicit confirmation.\n\n"

            f"Current state:\n"

            f"  In-process _HALTED: {_HALTED}\n"

            f"  Env AGT_EXECUTION_ENABLED: {env_ok}\n"

            f"  DB execution_state: check /halt history\n\n"

            f"To re-enable: /resume CONFIRM"

        )

        return



    _HALTED = False

    logger.warning("DESK RESUMED by operator via /resume CONFIRM")



    # Clear DB disable

    try:

        with closing(_get_db_connection()) as conn:

            with tx_immediate(conn):

                conn.execute(

                    "INSERT OR REPLACE INTO execution_state "

                    "(id, disabled, set_by, set_at, reason) "

                    "VALUES (1, 0, ?, datetime('now'), ?)",

                    (str(update.effective_user.id), "/resume CONFIRM"),

                )

    except Exception as exc:

        logger.warning("/resume DB update failed: %s", exc)



    await update.message.reply_text(

        f"\u2705 DESK RESUMED\n"

        f"In-process halt cleared. DB execution enabled.\n"

        f"Scheduled jobs NOT restored (restart bot to restore jobs)."

    )





# Followup #17: orphan scan state sets for resolution policy (D3)

_OPEN_FILLED_STATES = frozenset({"Filled"})

_OPEN_DEAD_STATES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})

_OPEN_LIVE_STATES = frozenset({

    "Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"

})







async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    """/scan — CSP entry scan. Stages allocator output for /approve."""

    if not is_authorized(update):

        return



    status_msg = await update.message.reply_text("\U0001f50d Running CSP scan…")



    try:

        # ── 1. Load watchlist + run scanner (sync, yfinance) in a thread ──

        from pxo_scanner import (

            _load_scan_universe,

            scan_csp_candidates,

            MIN_DTE, MAX_DTE, MIN_ANNUALIZED_ROI,

        )

        watchlist = await asyncio.to_thread(_load_scan_universe)

        rows = await asyncio.to_thread(

            scan_csp_candidates, watchlist, 10, 50,

        )



        if not rows:

            await status_msg.edit_text(

                "\u2139\ufe0f No CSP candidates meet Heitkoetter criteria "

                f"(DTE {MIN_DTE}-{MAX_DTE}, yield \u2265 {MIN_ANNUALIZED_ROI}%)."

            )

            return



        # ── 2. Adapt dicts -> ScanCandidate objects ──

        from agt_equities.scan_bridge import (

            adapt_scanner_candidates,

            build_watchlist_sector_map,

            make_bridge2_extras_provider,

        )

        candidates = adapt_scanner_candidates(rows)

        if not candidates:

            await status_msg.edit_text(

                "\u26a0\ufe0f Scanner produced rows but none survived adapter "

                "validation (missing ticker/strike/expiry/premium/ann_roi)."

            )

            return



        # ── 3. Fetch VIX (yfinance, 20.0 fallback) ──

        def _fetch_vix() -> float:

            try:

                hist = yf.Ticker("^VIX").history(period="1d")

                if len(hist) and "Close" in hist.columns:

                    return float(hist["Close"].iloc[-1])

            except Exception as exc:

                logger.warning("cmd_scan: VIX fetch failed: %s", exc)

            return 20.0

        vix = await asyncio.to_thread(_fetch_vix)



        # ── 4. Discover positions + build per-household snapshots ──

        disco = await _discover_positions(None)

        if disco.get("error"):

            logger.warning("cmd_scan: _discover_positions warning: %s", disco["error"])



        ib_conn = await ensure_ib_connected()

        from agt_equities.csp_allocator import (

            _fetch_household_buying_power_snapshot,

            run_csp_allocator,

        )

        from agt_equities.runtime import RunContext, RunMode

        from agt_equities.sinks import (

            CollectorOrderSink,

            NullDecisionSink,

            SQLiteOrderSink,

        )

        snapshots = await _fetch_household_buying_power_snapshot(ib_conn, disco)

        if not snapshots:

            await status_msg.edit_text(

                "\u26a0\ufe0f Could not build household buying-power snapshots "

                "(accountSummaryAsync failed or no accounts in HOUSEHOLD_MAP)."

            )

            return



        # ── 4b. Fetch bridge-2 extras (earnings, correlations) ──

        await status_msg.edit_text("\U0001f50d Fetching earnings + correlations\u2026")

        from agt_equities.scan_extras import (

            fetch_earnings_map,

            build_correlation_pairs,

        )

        candidate_tickers = [c.ticker for c in candidates]



        # Collect all holding tickers across households for correlation

        all_holding_tickers: set[str] = set()

        for _hh_snap in snapshots.values():

            all_holding_tickers.update(_hh_snap.get("existing_positions", {}).keys())

            all_holding_tickers.update(_hh_snap.get("existing_csps", {}).keys())



        earnings_map = await asyncio.to_thread(

            fetch_earnings_map, candidate_tickers,

        )

        correlation_pairs = await asyncio.to_thread(

            build_correlation_pairs,

            candidate_tickers,

            sorted(all_holding_tickers),

        )



        # ── 5. Build extras_provider + run allocator ──

        sector_map = build_watchlist_sector_map(watchlist)

        extras_provider = make_bridge2_extras_provider(

            sector_map, earnings_map, correlation_pairs,

        )



        # B5.c-bridge-2: live staging (default) or dry-run via env flag.

        # ADR-008 MR 2: dry-run is now an in-memory CollectorOrderSink so

        # the allocator still sees a real OrderSink contract while no row

        # lands in pending_orders. Byte-identical to pre-MR-2 behavior

        # (which set staging_callback=None, skipping staging).

        _scan_live = os.getenv("AGT_SCAN_LIVE", "1") == "1"

        if _scan_live:

            _csp_order_sink = SQLiteOrderSink(

                staging_fn=append_pending_tickets,

            )

        else:

            _csp_order_sink = CollectorOrderSink()

        ctx = RunContext(

            mode=RunMode.LIVE,

            run_id=uuid.uuid4().hex,

            order_sink=_csp_order_sink,

            decision_sink=NullDecisionSink(),

        )



        result = run_csp_allocator(

            ray_candidates=candidates,

            snapshots=snapshots,

            vix=vix,

            extras_provider=extras_provider,

            ctx=ctx,

        )



        # ── 6. Post digest ──

        staged_n = result.total_staged_contracts

        mode_tag = "STAGED" if _scan_live and staged_n else "dry-run"

        header = [

            f"\u2501\u2501 /scan ({mode_tag}) \u2501\u2501",

            f"Candidates scanned: {len(candidates)}  \u00b7  VIX: {vix:.1f}",

            "",

        ]

        digest = "\n".join(header + (result.digest_lines or ["(no allocator output)"]))

        await status_msg.delete()

        await send_text(

            update,

            f"<pre>{html.escape(digest)}</pre>",

        )

        if _scan_live and staged_n:

            await update.message.reply_text(

                f"\u2705 {staged_n} contract(s) staged. Use /approve to review and transmit.",

            )



    except Exception as exc:

        logger.exception("cmd_scan failed")

        try:

            await status_msg.edit_text(f"\u274c /scan failed: {exc}")

        except Exception:

            try:

                await update.message.reply_text(f"\u274c /scan failed: {exc}")

            except Exception:

                pass





# ---------------------------------------------------------------------------

# Shared LLM dispatcher — parameterized by model

# ---------------------------------------------------------------------------

_MODEL_LABELS = {

    CLAUDE_MODEL_HAIKU:  ("H", "\U0001f504 Thinking\u2026"),

    CLAUDE_MODEL_SONNET: ("S", "\U0001f504 Thinking (Sonnet)\u2026"),

    CLAUDE_MODEL_OPUS:   ("O", "\U0001f504 Deep thinking (Opus)\u2026"),

}





async def _scan_orphaned_transmitting_rows(ib_conn, app_bot):

    """Resolve orphaned TRANSMITTING rows after restart.



    BINDING: Gateway = since-midnight only for executions().

    Cross-midnight orphans CANNOT be auto-resolved on Gateway.

    Manual recovery via /recover_transmitting is the only path.



    Column ownership (D4): writes ONLY final_status + last_updated.

    R5 handlers write fill columns separately.

    originating_account_id: write-once at staging, never modified after (F20).

    """

    with closing(_get_db_connection()) as conn:

        orphans = conn.execute(

            "SELECT audit_id, ticker, household, action_type, strike, expiry, "

            "       contracts, shares, limit_price "

            "FROM bucket3_dynamic_exit_log WHERE final_status = 'TRANSMITTING'"

        ).fetchall()



    if not orphans:

        logger.info("orphan_scan: no TRANSMITTING rows — clean startup")

        return



    logger.warning("orphan_scan: found %d TRANSMITTING row(s)", len(orphans))



    # Use cached openTrades + executions (populated by reqAllOpenOrdersAsync

    # and reqExecutionsAsync called in post_init before this function)

    open_trades = ib_conn.openTrades()

    exec_list = ib_conn.executions()



    auto_resolved = []

    needs_manual = []



    for orphan in orphans:

        audit_id = orphan["audit_id"]

        ticker = orphan["ticker"]



        open_match = next(

            (t for t in open_trades if t.order.orderRef == audit_id), None

        )

        exec_match = next(

            (e for e in exec_list if e.orderRef == audit_id), None

        )



        if open_match:

            status = open_match.orderStatus.status

            if status in _OPEN_FILLED_STATES:

                with closing(_get_db_connection()) as conn:

                    with tx_immediate(conn):

                        r = conn.execute(

                            "UPDATE bucket3_dynamic_exit_log "

                            "SET final_status = 'TRANSMITTED', "

                            "    last_updated = CURRENT_TIMESTAMP "

                            "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                            (audit_id,),

                        )

                        if r.rowcount > 0:

                            auto_resolved.append((audit_id, ticker, "filled-via-openTrades"))

                            logger.info("orphan_scan: %s -> TRANSMITTED (filled)", audit_id)

            elif status in _OPEN_DEAD_STATES:

                with closing(_get_db_connection()) as conn:

                    with tx_immediate(conn):

                        r = conn.execute(

                            "UPDATE bucket3_dynamic_exit_log "

                            "SET final_status = 'ABANDONED', "

                            "    last_updated = CURRENT_TIMESTAMP "

                            "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                            (audit_id,),

                        )

                        if r.rowcount > 0:

                            auto_resolved.append((audit_id, ticker, f"dead-at-ibkr ({status})"))

                            logger.info("orphan_scan: %s -> ABANDONED (%s)", audit_id, status)

            elif status in _OPEN_LIVE_STATES:

                filled = getattr(open_match.orderStatus, 'filled', 0)

                remaining = getattr(open_match.orderStatus, 'remaining', 0)

                if filled and remaining:

                    needs_manual.append((audit_id, ticker,

                                        f"partial-fill (filled={filled}, remaining={remaining})"))

                else:

                    needs_manual.append((audit_id, ticker, f"live-unfilled ({status})"))

                logger.warning("orphan_scan: %s live at IBKR (%s)", audit_id, status)

            else:

                needs_manual.append((audit_id, ticker, f"unknown-status ({status})"))

        elif exec_match:

            with closing(_get_db_connection()) as conn:

                with tx_immediate(conn):

                    r = conn.execute(

                        "UPDATE bucket3_dynamic_exit_log "

                        "SET final_status = 'TRANSMITTED', "

                        "    last_updated = CURRENT_TIMESTAMP "

                        "WHERE audit_id = ? AND final_status = 'TRANSMITTING'",

                        (audit_id,),

                    )

                    if r.rowcount > 0:

                        auto_resolved.append((audit_id, ticker, "filled-via-executions"))

                        logger.info("orphan_scan: %s -> TRANSMITTED (executions)", audit_id)

        else:

            # NOT FOUND — DO NOT AUTO-ABANDON. EVER. (D5 binding)

            needs_manual.append((audit_id, ticker, "not-found-at-ib"))

            logger.warning(

                "orphan_scan: %s NOT FOUND at IBKR — manual /recover_transmitting required",

                audit_id,

            )



    # Single consolidated alert

    if auto_resolved or needs_manual:

        lines = ["\U0001f50d Startup orphan scan complete\n"]

        if auto_resolved:

            lines.append(f"Auto-resolved: {len(auto_resolved)}")

            for aid, tk, reason in auto_resolved:

                lines.append(f"  {tk} {aid[:8]}... \u2192 {reason}")

        if needs_manual:

            lines.append(f"\nNEEDS OPERATOR REVIEW: {len(needs_manual)}")

            for aid, tk, reason in needs_manual:

                lines.append(f"  {tk} {aid[:8]}... ({reason})")

            lines.append("\nUse: /recover_transmitting <audit_id> filled|abandoned")

            lines.append(

                "\nCross-midnight Gateway limitation: orders from prior days "

                "require manual verification."

            )

        try:

            await app_bot.send_message(

                chat_id=AUTHORIZED_USER_ID,

                text="\n".join(lines),

            )

        except Exception as tg_exc:

            logger.error("orphan_scan: Telegram alert failed: %s", tg_exc)





async def _pin_mode_on_startup(ib_conn=None) -> str | None:

    """Cold-start mode pin: enter WARTIME on boot if leverage is elevated."""

    try:

        from agt_equities import trade_repo

        from agt_equities.mode_engine import MODE_WARTIME, get_current_mode, log_mode_transition

        from agt_equities.rule_engine import LEVERAGE_LIMIT, compute_leverage_pure

        from agt_equities.state_builder import build_state



        with closing(_get_db_connection()) as conn:

            current_mode = get_current_mode(conn)

        if current_mode == MODE_WARTIME:

            logger.info("Cold-start wartime pin: already in WARTIME, no action")

            return None



        if ib_conn is None:

            ib_conn = await ensure_ib_connected()



        live_nlv: dict[str, float] = {}

        try:

            summary = await ib_conn.accountSummaryAsync()

            for item in summary or []:

                if item.account not in ACTIVE_ACCOUNTS or item.tag != "NetLiquidation":

                    continue

                try:

                    live_nlv[item.account] = float(item.value)

                except (TypeError, ValueError):

                    continue

        except Exception as exc:

            logger.warning("Cold-start wartime pin: accountSummary failed: %s", exc)



        snapshot = build_state(

            db_path=str(DB_PATH),

            live_nlv=live_nlv or None,

        )



        tickers = sorted({

            c.ticker for c in snapshot.active_cycles

            if c.status == "ACTIVE" and c.shares_held > 0

        })

        spots = await _ibkr_get_spots_batch(tickers) if tickers else {}



        breaches: list[tuple[str, float]] = []

        for household in snapshot.household_nav:

            leverage = compute_leverage_pure(

                snapshot.active_cycles,

                spots,

                snapshot.beta_by_symbol,

                snapshot.household_nav,

                household,

            )

            if leverage >= LEVERAGE_LIMIT:

                breaches.append((household, leverage))



        if not breaches:

            logger.info("Cold-start wartime pin: no household at or above %.2fx", LEVERAGE_LIMIT)

            return None



        breach_household, breach_leverage = max(breaches, key=lambda item: item[1])

        reason = "Cold-start pin: leverage >= 1.50x"



        with closing(_get_db_connection()) as conn:

            current_mode = get_current_mode(conn)

            if current_mode == MODE_WARTIME:

                logger.info("Cold-start wartime pin: already in WARTIME, no action")

                return None

            log_mode_transition(

                conn,

                current_mode,

                MODE_WARTIME,

                trigger_rule="cold_start_pin",

                trigger_household=breach_household,

                trigger_value=round(breach_leverage, 4),

                notes=reason,

            )



        logger.warning(

            "Cold-start wartime pin: %s leverage %.2fx >= %.2fx",

            breach_household,

            breach_leverage,

            LEVERAGE_LIMIT,

        )

        return (

            "\U0001f6a8 COLD-START WARTIME PIN\n"

            f"{breach_household} leverage {breach_leverage:.2f}x >= {LEVERAGE_LIMIT:.2f}x.\n"

            f"{reason}"

        )

    except Exception as exc:

        logger.exception("Cold-start wartime pin failed: %s", exc)

        return None





async def post_init(app) -> None:

    # Heartbeat registration must precede all failable init steps, including
    # IB connect. The heartbeat IS the init-success signal for observers;
    # gating it behind ensure_ib_connected() inverts the semantic and makes
    # the bot invisible during IB outages -- exactly when the heartbeat
    # signal matters most.

    try:

        from agt_equities.heartbeat import register_bot_heartbeat

        jq_hb = app.job_queue

        if jq_hb is not None:

            register_bot_heartbeat(jq_hb)

        else:

            logger.warning("post_init: JobQueue missing — bot_heartbeat not registered")

    except Exception as exc:

        logger.error("post_init: register_bot_heartbeat failed: %s", exc)



    try:

        ib_conn = await ensure_ib_connected()

    except Exception as exc:

        logger.error("Could not connect on startup: %s", exc)

        logger.error("Use /reconnect once Gateway/TWS is ready.")

        return



    # Followup #17: populate open order + execution caches for orphan scan

    try:

        await ib_conn.reqAllOpenOrdersAsync()

        from ib_async.objects import ExecutionFilter

        await ib_conn.reqExecutionsAsync(ExecutionFilter())

    except Exception as exc:

        logger.warning("post_init: reqAllOpenOrders/reqExecutions failed: %s", exc)



    # Followup #17: scan for orphaned TRANSMITTING rows

    try:

        await _scan_orphaned_transmitting_rows(ib_conn, app.bot)

    except Exception as exc:

        logger.error("Orphan scan failed: %s — bot continues without scan", exc)



    # Priority 4: cold-start wartime pin before polling/watchdog loops

    try:

        alert = await _pin_mode_on_startup(ib_conn)

        if alert:

            await _alert_telegram(alert)

    except Exception as exc:

        logger.error("Cold-start wartime pin alert failed: %s — continuing", exc)



# ---------------------------------------------------------------------------

# Beta Impl 3: ATTESTED row poller (R6 — 10s interval, idempotent delivery)

# ---------------------------------------------------------------------------





async def _sweep_attested_ttl_job(context: ContextTypes.DEFAULT_TYPE) -> None:

    """Continuous sweeper for stale STAGED + ATTESTED rows (R7 — 10min TTL).



    Runs every 60s. STAGED sweep was previously only at /cc preamble (9:45 AM).

    Now both STAGED and ATTESTED rows are swept continuously. The /cc preamble

    sweep becomes a redundant safety net (acceptable per Q4 ruling).

    """

    if _HALTED:

        return

    try:

        with closing(_get_db_connection()) as conn:

            from agt_equities.rule_engine import sweep_stale_dynamic_exit_stages

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





async def _check_invariants_tick_job(context: ContextTypes.DEFAULT_TYPE) -> None:

    """MR !84: ADR-007 invariant suite tick (60s) owned by the bot when

    USE_SCHEDULER_DAEMON=0. Mirrors the scheduler's heartbeat tick so

    the ``incidents`` table is populated regardless of which process owns

    the gated job set. Detection is cheap, non-idempotent-safe via the

    incidents_repo key; the authoring/approval rate limit applies

    downstream per ADR-007 §9.3. Never raises — one unguarded exception

    in the bot's JobQueue is a live-capital hazard.

    """

    if _HALTED:

        return

    try:

        from agt_equities.invariants.tick import check_invariants_tick

        check_invariants_tick(detector="telegram_bot.invariants_tick")

    except Exception as exc:

        logger.error("invariants_tick error: %s", exc)





async def _drain_cross_daemon_alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:

    """A5d: bot-side consumer for the cross_daemon_alerts bus.



    Runs every 2s via jq.run_repeating. Drains pending alerts emitted by

    agt_scheduler producers (A5c onward) and dispatches each by `kind` to

    the operator's Telegram via context.bot.send_message. Marks each row

    'sent' on successful delivery, 'failed' on delivery exception (the

    alerts module retries on subsequent drain up to MAX_ATTEMPTS=3 then

    transitions terminal 'failed' for operator triage).



    Safe no-op when the table is empty. Safe under USE_SCHEDULER_DAEMON=0:

    no producers are running on the scheduler side, so the bus stays empty.

    """

    if _HALTED:

        return

    try:

        from agt_equities.alerts import (

            drain_pending_alerts,

            mark_alert_sent,

            mark_alert_failed,

            format_alert_text,

        )

    except Exception as exc:

        logger.error("alerts module import failed: %s", exc)

        return

    try:

        alerts = drain_pending_alerts(limit=20)

    except Exception as exc:

        logger.error("drain_pending_alerts failed: %s", exc)

        return

    # MR #2: late-import stage_gmail_draft so unit tests that import

    # telegram_bot without an alerts module (rare) don't blow up here.

    try:

        from agt_equities.alerts import stage_gmail_draft

    except Exception:

        stage_gmail_draft = None  # type: ignore[assignment]

    for a in alerts:

        aid = a.get("id")

        try:

            text = format_alert_text(a)

            await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)

            try:

                mark_alert_sent(aid)

            except Exception as exc:

                logger.error("mark_alert_sent(%s) failed: %s", aid, exc)

            # MR #2: escalate crit-severity alerts to a Gmail draft file.

            # Never blocks Telegram delivery; failures are logged-only.

            if stage_gmail_draft is not None:

                try:

                    severity = str(a.get("severity") or "").lower()

                    if severity == "crit":

                        stage_gmail_draft(a)

                except Exception as exc:

                    logger.warning(

                        "stage_gmail_draft(alert=%s) failed: %s", aid, exc

                    )

        except Exception as exc:

            logger.error("cross_daemon_alerts dispatch %s failed: %s", aid, exc)

            try:

                mark_alert_failed(aid, str(exc))

            except Exception as exc2:

                logger.error("mark_alert_failed(%s) failed: %s", aid, exc2)





async def _poll_attested_rows(context: ContextTypes.DEFAULT_TYPE) -> None:

    """Poll for ATTESTED rows and push TRANSMIT/CANCEL keyboards to operator.



    Runs every 10s via jq.run_repeating per R6 ruling. Uses module-level

    _dispatched_audits set for dedup — each ATTESTED row gets exactly one

    keyboard push. On bot restart, set is empty so all ATTESTED rows

    re-deliver; TRANSMITTING atomic lock prevents double-execution.



    Cleanup: any audit_id in _dispatched_audits whose row is no longer ATTESTED

    (transmitted, cancelled, abandoned, drift_blocked) is purged each tick to

    prevent unbounded set growth (triage item #5).

    """

    if _HALTED:

        return

    try:

        with closing(_get_db_connection()) as conn:

            rows = conn.execute(

                "SELECT audit_id, ticker, household, action_type, "

                "       strike, expiry, contracts, shares, limit_price "

                "FROM bucket3_dynamic_exit_log "

                "WHERE final_status = 'ATTESTED'"

            ).fetchall()



        current_attested_ids = {r["audit_id"] for r in rows}



        for row in rows:

            audit_id = row["audit_id"]

            if audit_id in _dispatched_audits:

                continue



            try:

                ticker = row["ticker"]

                if row["action_type"] == "CC":

                    detail = (

                        f"{row['contracts']}x {ticker} ${row['strike']:.0f}C "

                        f"{row['expiry']} @ ${row['limit_price']:.2f}"

                    )

                else:

                    detail = f"{row['shares']}sh {ticker} @ ${row['limit_price']:.2f}"



                text = (

                    f"\u26a0\ufe0f ATTESTED Dynamic Exit\n"

                    f"Household: {row['household'].replace('_Household', '')}\n"

                    f"{detail}\n"

                    f"audit_id: {audit_id[:8]}..."

                )



                keyboard = InlineKeyboardMarkup([[

                    InlineKeyboardButton(

                        "\U0001f4e4 TRANSMIT",

                        callback_data=f"dex:transmit:{audit_id}",

                    ),

                    InlineKeyboardButton(

                        "\u274c CANCEL",

                        callback_data=f"dex:cancel:{audit_id}",

                    ),

                ]])



                await context.bot.send_message(

                    chat_id=AUTHORIZED_USER_ID,

                    text=text,

                    reply_markup=keyboard,

                )

                _dispatched_audits.add(audit_id)

            except Exception as row_exc:

                logger.warning(

                    "attested_poller: failed to dispatch audit_id=%s: %s",

                    audit_id, row_exc,

                )

                # Do NOT add to _dispatched_audits — retry next tick



        # Cleanup: purge dispatched IDs whose rows are no longer ATTESTED

        stale_dispatched = _dispatched_audits - current_attested_ids

        _dispatched_audits.difference_update(stale_dispatched)



    except Exception as exc:

        logger.error("attested_poller error: %s", exc)





# ---------------------------------------------------------------------------

# Sprint 1B: EL snapshots writer (30s polling job)

# ---------------------------------------------------------------------------



_el_last_write: dict[str, float] = {}  # {account_id: last_write_epoch}

_apex_last_alert: dict[str, float] = {}  # {account_id: last_alert_epoch}

_EL_WRITE_DEBOUNCE_SECONDS = 30





async def _el_snapshot_writer_job(

    context: ContextTypes.DEFAULT_TYPE | None,

) -> None:

    """Poll accountSummary and write EL snapshots to DB for Cure Console.



    Runs every 30s. Writes one row per account to el_snapshots.

    Debounces per-account writes to prevent duplicate rows.

    """

    if _HALTED:

        return

    try:

        ib_conn = await ensure_ib_connected()

        summary = await ib_conn.accountSummaryAsync()

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

            acct_data[item.account][item.tag] = float(item.value)



        for acct_id, data in acct_data.items():

            excess_liquidity = float(data.get("ExcessLiquidity") or 0.0)

            nlv = float(data.get("NetLiquidation") or 0.0)

            if nlv <= 0:

                continue



            if acct_id in MARGIN_ACCOUNTS:

                el_pct = excess_liquidity / nlv



                if el_pct <= 0.08:

                    # TODO: Implement synchronous tied-unwind execution:

                    # 1. Identify paired covered stock / short-call inventory to unwind.

                    # 2. Compute autonomous tied-unwind sizing across affected accounts.

                    # 3. Stage + transmit the unwind path without Telegram approval.

                    if now - _apex_last_alert.get(acct_id, 0.0) > 900:

                        try:

                            if getattr(context, "bot", None):

                                await context.bot.send_message(

                                    chat_id=AUTHORIZED_USER_ID,

                                    text="[🚨 APEX SURVIVAL: Excess Liquidity < 8%. Executing Tied-Unwinds!]",

                                )

                            _apex_last_alert[acct_id] = now

                        except Exception as alert_exc:

                            logger.warning("State 0 alert failed for %s: %s", acct_id, alert_exc)

                    continue

                else:

                    if acct_id in _apex_last_alert:

                        del _apex_last_alert[acct_id]

            else:

                if acct_id in _apex_last_alert:

                    del _apex_last_alert[acct_id]



            # Debounce: skip if last write <30s ago

            last = _el_last_write.get(acct_id, 0)

            if now - last < _EL_WRITE_DEBOUNCE_SECONDS:

                continue



            hh = ACCOUNT_TO_HOUSEHOLD.get(acct_id, "Unknown")

            try:

                with closing(_get_db_connection()) as conn:

                    with tx_immediate(conn):

                        conn.execute(

                            "INSERT INTO el_snapshots "

                            "(account_id, household, excess_liquidity, nlv, buying_power, source) "

                            "VALUES (?, ?, ?, ?, ?, 'ibkr_live')",

                            (

                                acct_id,

                                hh,

                                excess_liquidity,

                                nlv,

                                data.get("BuyingPower"),

                            ),

                        )

                _el_last_write[acct_id] = now

            except Exception as db_exc:

                logger.warning("el_snapshot write failed for %s: %s", acct_id, db_exc)



    except Exception as exc:

        # Non-fatal: IB may be disconnected, just skip this tick

        logger.debug("el_snapshot_writer: %s", exc)







# ---------------------------------------------------------------------------

# MR #1: Singleton lockfile — prevent dual-instance collisions on clientId=1

# ---------------------------------------------------------------------------





def _pid_is_alive(pid: int) -> bool:

    """Return True if the given PID is alive on the current OS."""

    if pid <= 0:

        return False

    try:

        if sys.platform.startswith("win"):

            import subprocess as _sp

            result = _sp.run(

                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],

                capture_output=True, text=True, timeout=5,

            )

            return str(pid) in (result.stdout or "")

        else:

            os.kill(pid, 0)

            return True

    except Exception:

        return False





def _acquire_singleton_lock() -> None:

    """Enforce single-instance bot. Abort on live collision; reclaim stale."""

    from pathlib import Path as _Path

    import atexit as _atexit



    lock_path = _Path(__file__).resolve().parent / ".bot.pid"

    try:

        if lock_path.exists():

            raw = lock_path.read_text(encoding="utf-8").strip()

            try:

                existing_pid = int(raw)

            except ValueError:

                existing_pid = -1

            if existing_pid > 0 and _pid_is_alive(existing_pid):

                msg = (

                    f"Singleton lock: another bot is already running "

                    f"(PID={existing_pid}). Kill it (or `nssm stop AGTBotService`) "

                    f"before starting a new instance."

                )

                logger.error(msg)

                print(msg, file=sys.stderr)

                sys.exit(1)

            else:

                logger.warning(

                    "Singleton lock: stale .bot.pid (PID=%r not alive); overwriting.",

                    raw,

                )

    except SystemExit:

        raise

    except Exception as exc:

        logger.warning("Singleton lock: unreadable .bot.pid (%s); will overwrite.", exc)



    try:

        lock_path.write_text(str(os.getpid()), encoding="utf-8")

        logger.info("Singleton lock acquired: PID=%d path=%s", os.getpid(), lock_path)

    except OSError as exc:

        logger.warning("Singleton lock: failed to write .bot.pid (%s); proceeding.", exc)

        return



    def _release() -> None:

        try:

            if lock_path.exists():

                current = lock_path.read_text(encoding="utf-8").strip()

                if current == str(os.getpid()):

                    lock_path.unlink()

        except Exception:

            pass



    _atexit.register(_release)





def main() -> None:

    # MR !90: evict any zombie telegram_bot.py holding .bot.pid / IBKR clientId=1

    # before the singleton lock check. NSSM's restart of the outer venv launcher

    # can leave the inner grandchild python.exe alive; that zombie would

    # fail the next bot's IBKR connect with a clientId collision. See

    # agt_equities/zombie_evict.py for the Windows semantics note.

    from agt_equities.zombie_evict import evict_zombie_daemons

    _zr = evict_zombie_daemons(

        cmdline_marker="telegram_bot.py",

        self_pid=os.getpid(),

        logger=logger,

    )

    if _zr.zombies_survived_sigkill:

        logger.error(

            "Zombie eviction incomplete: survivors=%s; refusing to boot",

            _zr.zombies_survived_sigkill,

        )

        # SystemExit(7) rather than sys.exit(7) because this module does not

        # import `sys` at module top (see note in reports/mr90_zombie_evict_

        # report.md re: latent NameError swallowed by _pid_is_alive's blanket

        # except; pre-existing, out of scope for MR !90).

        raise SystemExit(7)



    # MR #1: single-instance enforcement before any IB / Telegram contact

    _acquire_singleton_lock()

    init_db()  # A4: lazy DB init at daemon boot, not import

    logger.info(

        "Starting AGT Equities Bridge — Hybrid Architecture "

        "(default: Haiku 4.5 | /think: Sonnet 4.6 | /deep: Opus 4.6 | "

        "Rulebook: %s)\u2026",

        "loaded" if _RULEBOOK_TEXT else "NOT FOUND",

    )



    bot = AGTFormattedBot(token=TELEGRAM_BOT_TOKEN)

    app = (

        ApplicationBuilder()

        .bot(bot)

        .post_init(post_init)

        .post_shutdown(_graceful_shutdown)

        .build()

    )



    # Sprint 1D: pruned command registry (20 handlers + 2 callbacks killed)

    app.add_handler(CommandHandler("start",     cmd_start))

    app.add_handler(CommandHandler("status",    cmd_status))

    app.add_handler(CommandHandler("orders",    cmd_orders))

    app.add_handler(CommandHandler("rollcheck", cmd_rollcheck))

    app.add_handler(CommandHandler("csp_harvest", cmd_csp_harvest))

    app.add_handler(CommandHandler("cc",        cmd_cc))

    app.add_handler(CommandHandler("budget",    cmd_budget))

    app.add_handler(CommandHandler("clear",     cmd_clear))

    app.add_handler(CommandHandler("reconnect", cmd_reconnect))

    app.add_handler(CommandHandler("vrp",       cmd_vrp))

    app.add_handler(CommandHandler("think",     cmd_think))

    app.add_handler(CommandHandler("deep",      cmd_deep))

    app.add_handler(CommandHandler("approve",   cmd_approve))

    app.add_handler(CommandHandler("reject",    cmd_reject))

    app.add_handler(CommandHandler("declare_peacetime", cmd_declare_peacetime))

    app.add_handler(CommandHandler("mode",      cmd_mode))

    app.add_handler(CommandHandler("cure",      cmd_cure))

    app.add_handler(CommandHandler("recover_transmitting", cmd_recover_transmitting))

    app.add_handler(CommandHandler("halt",      cmd_halt))

    app.add_handler(CommandHandler("resume",    cmd_resume))

    app.add_handler(CommandHandler("daily",     cmd_daily))

    app.add_handler(CommandHandler("report",    cmd_report))

    app.add_handler(CommandHandler("list_rem",    cmd_list_rem))

    app.add_handler(CommandHandler("approve_rem", cmd_approve_rem))

    app.add_handler(CommandHandler("reject_rem",  cmd_reject_rem))

    app.add_handler(CallbackQueryHandler(handle_orders_callback, pattern=r"^orders:"))

    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve:"))

    app.add_handler(CallbackQueryHandler(handle_dex_callback, pattern=r"^dex:"))

    app.add_handler(CallbackQueryHandler(handle_liq_callback, pattern=r"^liq:"))
    app.add_handler(CallbackQueryHandler(handle_csp_approval_callback, pattern=r"^csp_(?:approve|skip|submit):"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))



    # ── Scheduled jobs ──

    jq = app.job_queue

    if jq is not None:

        jq.run_daily(

            callback=_scheduled_cc,

            time=_time(hour=9, minute=45, tzinfo=ET),

            days=(1, 2, 3, 4, 5),

            name="cc_daily",

        )

        logger.info("Scheduled: cc_daily at 9:45 AM ET (Mon-Fri)")

        # MR !71: CSP entry scan kicks off before CC so candidates stage

        # while market is fresh. Paper auto-executes via _auto_execute_staged.

        jq.run_daily(

            callback=_scheduled_csp_scan,

            time=_time(hour=9, minute=35, tzinfo=ET),

            days=(1, 2, 3, 4, 5),

            name="csp_scan_daily",

        )

        logger.info("Scheduled: csp_scan_daily at 9:35 AM ET (Mon-Fri)")

        jq.run_daily(

            callback=_scheduled_watchdog,

            time=_time(hour=15, minute=30, tzinfo=ET),

            days=(1, 2, 3, 4, 5),

            name="watchdog_daily",

        )

        logger.info("Scheduled: watchdog_daily at 3:30 PM ET (Mon-Fri)")

        if not _use_scheduler_daemon():

            jq.run_monthly(

                callback=_scheduled_universe_refresh,

                when=_time(hour=6, minute=0, tzinfo=ET),

                day=1,

                name="universe_monthly",

            )

            logger.info("Scheduled: universe_monthly on 1st at 6:00 AM ET")

        else:

            logger.info(

                "Skipped universe_monthly registration: "

                "USE_SCHEDULER_DAEMON=1 (owned by agt_scheduler daemon)"

            )

        if not _use_scheduler_daemon():

            jq.run_daily(

                callback=_scheduled_conviction_refresh,

                time=_time(hour=20, minute=0, tzinfo=ET),

                days=(0,),  # Sunday only

                name="conviction_weekly",

            )

            logger.info("Scheduled: conviction_weekly at 8:00 PM ET (Sunday)")

        else:

            logger.info(

                "Skipped conviction_weekly registration: "

                "USE_SCHEDULER_DAEMON=1 (owned by agt_scheduler daemon)"

            )

        if not _use_scheduler_daemon():

            jq.run_daily(

                callback=_scheduled_flex_sync,

                time=_time(hour=17, minute=0, tzinfo=ET),

                days=(1, 2, 3, 4, 5),

                name="flex_sync_eod",

            )

            logger.info("Scheduled: flex_sync_eod at 5:00 PM ET (Mon-Fri)")

        else:

            logger.info(

                "Skipped flex_sync_eod registration: "

                "USE_SCHEDULER_DAEMON=1 (owned by agt_scheduler daemon)"

            )

        jq.run_repeating(

            callback=_poll_attested_rows,

            interval=10,

            first=10,

            name="attested_poller",

        )

        logger.info("Scheduled: attested_poller every 10s")

        if not _use_scheduler_daemon():

            # A5e: scheduler daemon owns this when USE_SCHEDULER_DAEMON=1.

            jq.run_repeating(

                callback=_sweep_attested_ttl_job,

                interval=60,

                first=30,

                name="attested_sweeper",

            )

            logger.info("Scheduled: attested_sweeper every 60s")

        else:

            logger.info(

                "Skipped attested_sweeper registration: USE_SCHEDULER_DAEMON=1 "

                "(owned by agt_scheduler daemon)"

            )

        if not _use_scheduler_daemon():

            # A5e: scheduler daemon owns this when USE_SCHEDULER_DAEMON=1

            # (A5d.d ported el_snapshot_writer + APEX_SURVIVAL bus alert).

            jq.run_repeating(

                callback=_el_snapshot_writer_job,

                interval=30,

                first=15,

                name="el_snapshot_writer",

            )

            logger.info("Scheduled: el_snapshot_writer every 30s")

        else:

            logger.info(

                "Skipped el_snapshot_writer registration: USE_SCHEDULER_DAEMON=1 "

                "(owned by agt_scheduler daemon)"

            )

        jq.run_repeating(

            callback=_drain_cross_daemon_alerts_job,

            interval=2,

            first=5,

            name="cross_daemon_alerts_drain",

        )

        logger.info("Scheduled: cross_daemon_alerts_drain every 2s")



        # MR !84: invariants tick — bot owns when USE_SCHEDULER_DAEMON=0,

        # daemon owns when =1 via its heartbeat. Either way the

        # ``incidents`` table populates. first=30 lets post_init settle.

        if not _use_scheduler_daemon():

            jq.run_repeating(

                callback=_check_invariants_tick_job,

                interval=60,

                first=30,

                name="invariants_tick",

            )

            logger.info("Scheduled: invariants_tick every 60s")

        else:

            logger.info(

                "Skipped invariants_tick registration: USE_SCHEDULER_DAEMON=1 "

                "(owned by agt_scheduler heartbeat)"

            )



        # A5e: beta_cache_refresh + corporate_intel_refresh -- scheduler daemon

        # owns these when USE_SCHEDULER_DAEMON=1.  Bot retains them as

        # fallback for the 4-week cutover window (flag=0 default).

        if not _use_scheduler_daemon():

            async def _beta_cache_refresh_job(context):

                if _HALTED:

                    return

                try:

                    from agt_equities.beta_cache import refresh_beta_cache

                    tickers = []

                    try:

                        from agt_equities import trade_repo

                        from pathlib import Path

                        cycles = trade_repo.get_active_cycles()

                        tickers = list({c.ticker for c in cycles if c.status == 'ACTIVE'})

                    except Exception:

                        pass

                    if tickers:

                        await asyncio.to_thread(refresh_beta_cache, tickers)

                except Exception as exc:

                    logger.warning("beta_cache_refresh_job failed: %s", exc)



            from datetime import time as _dt_time

            jq.run_daily(

                callback=_beta_cache_refresh_job,

                time=_dt_time(4, 0, tzinfo=ET),

                name="beta_cache_refresh",

            )

            logger.info("Scheduled: beta_cache_refresh daily at 04:00")

            async def _beta_startup_check(context):

                await _beta_cache_refresh_job(context)

            jq.run_once(_beta_startup_check, when=10, name="beta_startup")



            async def _corporate_intel_refresh_job(context):

                if _HALTED:

                    return

                try:

                    from agt_equities.providers.yfinance_corporate_intelligence import (

                        YFinanceCorporateIntelligenceProvider,

                    )

                    tickers = []

                    try:

                        from agt_equities import trade_repo

                        from pathlib import Path as _P

                        cycles = trade_repo.get_active_cycles()

                        tickers = list({c.ticker for c in cycles if c.status == 'ACTIVE'})

                    except Exception:

                        pass

                    if tickers:

                        provider = YFinanceCorporateIntelligenceProvider()

                        for tk in tickers:

                            try:

                                await asyncio.to_thread(provider.get_corporate_calendar, tk)

                            except Exception as tk_exc:

                                logger.warning("corporate_intel refresh failed for %s: %s", tk, tk_exc)

                        logger.info("corporate_intel: refreshed %d tickers", len(tickers))

                except Exception as exc:

                    logger.warning("corporate_intel_refresh_job failed: %s", exc)



            jq.run_daily(

                callback=_corporate_intel_refresh_job,

                time=_dt_time(5, 0, tzinfo=ET),

                name="corporate_intel_refresh",

            )

            logger.info("Scheduled: corporate_intel_refresh daily at 05:00")

            async def _corporate_intel_startup(context):

                await _corporate_intel_refresh_job(context)

            jq.run_once(_corporate_intel_startup, when=15, name="corporate_intel_startup")

        else:

            logger.info(

                "Skipped beta_cache_refresh + corporate_intel_refresh registration: "

                "USE_SCHEDULER_DAEMON=1 (owned by agt_scheduler daemon)"

            )

    else:

        logger.warning("JobQueue not available — scheduled jobs not registered")



    # Sprint 1C+1D outbound formatting now handled by AGTFormattedBot subclass

    # (replaces monkey-patch that broke on PTB 22.7 TelegramObject._frozen lockdown).

    # Followup #14 still open: ~53 reply_text sites bypass _format_outbound.

    logger.info("Outbound formatting via AGTFormattedBot (paper=%s, mode prefix=active)", PAPER_MODE)



    app.run_polling(allowed_updates=Update.ALL_TYPES)





if __name__ == "__main__":

    main()

