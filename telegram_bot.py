

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
import time
from collections import defaultdict
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from datetime import date as _date, datetime as _datetime, time as _time, timedelta as _timedelta, timezone as _timezone
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
    MARGIN_ACCOUNTS,
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

# Sprint 1D: STAGED alert coalescing buffer
_staged_alert_buffer: list[dict] = []
_staged_alert_last_flush: float = 0.0
STAGED_COALESCE_WINDOW = 60  # seconds

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

def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


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
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=FULL;")
            conn.execute("PRAGMA wal_autocheckpoint=4000;")
            conn.execute("PRAGMA busy_timeout=5000;")

            # Cleanup Sprint A Purge 5: operational DDL moved to schema.py
            from agt_equities.schema import register_operational_tables
            register_operational_tables(conn)
            # ── Master Log Refactor v3: Bucket 2 + Bucket 3 new tables ──
            from agt_equities.schema import register_master_log_tables
            register_master_log_tables(conn)

    _cleanup_test_orders()
    _load_todays_usage()


def _cleanup_test_orders():
    """Mark all stale staged orders as superseded on boot."""
    try:
        with closing(_get_db_connection()) as conn:
            with conn:
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
        with conn:
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
        with conn:
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
            with conn:
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



init_db()

# Sprint 1C: loud paper mode startup log
if PAPER_MODE:
    logger.warning("=" * 60)
    logger.warning("PAPER MODE ACTIVE — port %d — all orders simulated", IB_TWS_PORT)
    logger.warning("=" * 60)
else:
    logger.info("LIVE MODE — primary port %d, fallback %d", IB_TWS_PORT, IB_TWS_FALLBACK)


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
            with conn:
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
        await ib.reqAllOpenOrdersAsync()
        await ib.reqExecutionsAsync(ExecutionFilter())
        from telegram import Bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await _scan_orphaned_transmitting_rows(ib, bot)
        await _alert_telegram(
            "\u2705 1101 recovery complete. Orphan scan finished."
        )
    except Exception as exc:
        logger.exception("_handle_1101_data_lost reconciliation failed")
        try:
            await _alert_telegram(
                f"\U0001f534 1101 recovery FAILED: {exc}. MANUAL REVIEW REQUIRED."
            )
        except Exception:
            pass


async def _auto_reconnect():
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
                await ib_conn.reqAllOpenOrdersAsync()
                from ib_async.objects import ExecutionFilter
                await ib_conn.reqExecutionsAsync(ExecutionFilter())
                from telegram import Bot
                bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await _scan_orphaned_transmitting_rows(ib_conn, bot)
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
                    logger.info("Fill + R5 order state event listeners registered (8 handlers)")
                except Exception as evt_exc:
                    logger.warning("Failed to register fill events: %s", evt_exc)

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
        trade_repo.DB_PATH = DB_PATH
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
EXCLUDED_TICKERS = {"IBKR", "TRAW.CVR", "SPX", "SLS", "GTLB"}
MODE1_MIN_ANNUALIZED_PCT = 10.0
MODE1_MIN_OTM_PCT = 5.0   # Defensive buffer — 3% was too tight
MODE1_LOW_YIELD_PCT = 8.0  # Warn on 5-8% annualized (floor is 5%)
MODE1_ABSOLUTE_BID_FLOOR = 0.03

# ── Harvest (Mode 2 / Fully Amortized) — 30%/130% Heitkoetter band ──
HARVEST_MIN_ANNUALIZED_PCT  = 30.0
HARVEST_MAX_ANNUALIZED_PCT  = 130.0
HARVEST_ABSOLUTE_BID_FLOOR  = 0.10
HARVEST_TARGET_DTE          = (14, 30)

# ── Rule 8 Dynamic Exit — V7 Amendments ──
DYNAMIC_EXIT_TARGET_PCT = 0.15    # 15% buffer target (not 20%)
DYNAMIC_EXIT_RULE1_LIMIT = 0.20   # 20% Rule 1 limit (unchanged)
CC_AUTO_STAGE_ENABLED = False  # Set to True after Monday validation

CONVICTION_TIERS = {
    "HIGH":    0.20,
    "NEUTRAL": 0.30,
    "LOW":     0.40,
}

CONVICTION_OVERRIDE_EXPIRY_DAYS = 90

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
            trade_repo.DB_PATH = DB_PATH
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
    """
    def wrapper(trade, fill):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.run_in_executor(None, sync_handler, trade, fill)
            else:
                sync_handler(trade, fill)
        except Exception as exc:
            logger.warning("Fill handler offload failed: %s", exc)
    wrapper.__name__ = f"{sync_handler.__name__}_async"
    return wrapper


# ---------------------------------------------------------------------------
# R5: Order state machine event handlers
# ---------------------------------------------------------------------------

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
                        with conn:
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
            with conn:
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
                        with conn:
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
            with conn:
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
) -> bool:
    """Atomic: dedup via INSERT OR IGNORE + ledger UPSERT. Single transaction."""
    try:
        with closing(_get_db_connection()) as conn:
            with conn:
                # Step 1: Attempt dedup insert — if exec_id exists, rowcount = 0
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO fill_log
                        (exec_id, ticker, action, quantity, price,
                         premium_delta, account_id, household_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (exec_id, ticker, action, quantity, price,
                     premium_delta, account_id, household_id),
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
            with conn:
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

        if _apply_fill_atomically(execution.execId, ticker, "SELL_CALL",
                                  fill_qty, fill_price, total_premium,
                                  acct_id, household):
            logger.info(
                "CC premium: %s %s +$%.2f (%d contracts @ $%.2f)",
                household, ticker, total_premium, fill_qty, fill_price,
            )
    except Exception as exc:
        logger.exception("_on_cc_fill failed: %s", exc)


def _on_csp_premium_fill(trade, fill):
    """SELL PUT filled — credit CSP premium to ledger (atomic)."""
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

        if _apply_fill_atomically(execution.execId, ticker, "SELL_PUT",
                                  fill_qty, fill_price, total_premium,
                                  acct_id, household):
            logger.info(
                "CSP premium: %s %s +$%.2f (%d contracts @ $%.2f)",
                household, ticker, total_premium, fill_qty, fill_price,
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
            with conn:
                conn.execute("BEGIN IMMEDIATE")  # CLEANUP-6: acquire RESERVED lock before SELECT
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
            with conn:
                conn.execute("BEGIN IMMEDIATE")  # CLEANUP-6: acquire RESERVED lock before SELECT
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
        result = await _run_cc_logic(None)
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
    """
    Refresh ticker_universe from Wikipedia S&P 500 + NASDAQ-100 + yfinance GICS.
    Returns {"added": int, "updated": int, "total": int, "error": str|None}
    """
    try:
        import requests as _req
        _wiki_session = _req.Session()
        _wiki_session.headers.update({
            "User-Agent": "AGTEquitiesBot/1.0 (research; contact: admin@agt.pr)"
        })
        tickers: dict[str, dict] = {}

        # ── S&P 500 from Wikipedia ──
        try:
            _sp500_html = _wiki_session.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=15,
            ).text
            from io import StringIO as _SIO
            sp500_tables = pd.read_html(_SIO(_sp500_html), match="Symbol")
            if sp500_tables:
                sp500_df = sp500_tables[0]
                for _, row in sp500_df.iterrows():
                    sym = str(row.get("Symbol", "")).strip().replace(".", "-")
                    if not sym:
                        continue
                    tickers[sym] = {
                        "company_name": str(row.get("Security", "")),
                        "gics_sector_wiki": str(row.get("GICS Sector", "")),
                        "gics_sub_wiki": str(row.get("GICS Sub-Industry", "")),
                        "indexes": ["SP500"],
                    }
        except Exception as exc:
            logger.warning("S&P 500 Wikipedia scrape failed: %s", exc)

        # ── NASDAQ-100 from Wikipedia ──
        try:
            _ndx_html = _wiki_session.get(
                "https://en.wikipedia.org/wiki/Nasdaq-100",
                timeout=15,
            ).text
            from io import StringIO as _SIO2
            ndx_tables = pd.read_html(_SIO2(_ndx_html), match="Ticker")
            if ndx_tables:
                ndx_df = ndx_tables[-1]
                ticker_col = None
                for col in ndx_df.columns:
                    if "ticker" in str(col).lower() or "symbol" in str(col).lower():
                        ticker_col = col
                        break
                if ticker_col is None:
                    ticker_col = ndx_df.columns[0]
                company_col = None
                for col in ndx_df.columns:
                    if any(k in str(col).lower() for k in ("company", "security", "name")):
                        company_col = col
                        break
                for _, row in ndx_df.iterrows():
                    sym = str(row[ticker_col]).strip().replace(".", "-")
                    if not sym or len(sym) > 6:
                        continue
                    if sym in tickers:
                        tickers[sym]["indexes"].append("NDX100")
                    else:
                        name = str(row[company_col]) if company_col else ""
                        tickers[sym] = {
                            "company_name": name,
                            "gics_sector_wiki": "",
                            "gics_sub_wiki": "",
                            "indexes": ["NDX100"],
                        }
        except Exception as exc:
            logger.warning("NASDAQ-100 Wikipedia scrape failed: %s", exc)

        if not tickers:
            return {"added": 0, "updated": 0, "total": 0,
                    "error": "Both Wikipedia scrapes failed"}

        all_syms = list(tickers.keys())
        added = 0
        updated = 0

        with closing(_get_db_connection()) as conn:
            with conn:
                existing = {
                    row["ticker"]
                    for row in conn.execute("SELECT ticker FROM ticker_universe").fetchall()
                }

                CHUNK_SIZE = 20
                now_iso = _datetime.now().isoformat()

                for i in range(0, len(all_syms), CHUNK_SIZE):
                    chunk = all_syms[i:i + CHUNK_SIZE]
                    for sym in chunk:
                        entry = tickers[sym]
                        gics_sector = entry.get("gics_sector_wiki", "")
                        gics_industry_group = ""

                        try:
                            yf_info = yf.Ticker(sym).info
                            yf_sector = yf_info.get("sector", "")
                            yf_industry = yf_info.get("industry", "")
                            if yf_sector:
                                gics_sector = yf_sector
                            if yf_industry:
                                gics_industry_group = yf_industry
                        except Exception:
                            gics_industry_group = entry.get("gics_sub_wiki", "")

                        index_str = ",".join(entry["indexes"])

                        if sym in existing:
                            conn.execute(
                                """UPDATE ticker_universe
                                   SET company_name=?, gics_sector=?,
                                       gics_industry_group=?, index_membership=?,
                                       last_updated=?
                                   WHERE ticker=?""",
                                (entry["company_name"], gics_sector,
                                 gics_industry_group, index_str, now_iso, sym),
                            )
                            updated += 1
                        else:
                            conn.execute(
                                """INSERT INTO ticker_universe
                                       (ticker, company_name, gics_sector,
                                        gics_industry_group, index_membership,
                                        last_updated)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (sym, entry["company_name"], gics_sector,
                                 gics_industry_group, index_str, now_iso),
                            )
                            added += 1

                    time.sleep(1.0)

        total = added + updated
        logger.info("ticker_universe refresh: %d added, %d updated, %d total",
                    added, updated, total)
        return {"added": added, "updated": updated, "total": total, "error": None}

    except Exception as exc:
        logger.exception("_refresh_ticker_universe_sync failed")
        return {"added": 0, "updated": 0, "total": 0, "error": str(exc)}



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
                with conn:
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
            # ── READ phase: get staged IDs ──
            with closing(_get_db_connection()) as conn:
                staged_ids = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM pending_orders WHERE status = 'staged' ORDER BY id"
                    ).fetchall()
                ]

            # ── AWAIT phase (conn released) ──
            if not staged_ids:
                await query.edit_message_text("No staged orders remaining.")
                return

            # ── WRITE phase: CAS claim staged → processing ──
            placeholders = ",".join("?" * len(staged_ids))
            with closing(_get_db_connection()) as conn:
                with conn:
                    claimed = conn.execute(
                        f"UPDATE pending_orders SET status = 'processing' "
                        f"WHERE id IN ({placeholders}) AND status = 'staged'",
                        staged_ids,
                    ).rowcount

            # ── AWAIT phase (conn released) ──
            if claimed == 0:
                await query.edit_message_text("Orders already being processed.")
                return

            # ── READ phase: fetch claimed rows ──
            with closing(_get_db_connection()) as conn:
                rows = conn.execute(
                    f"SELECT id, payload FROM pending_orders "
                    f"WHERE id IN ({placeholders}) AND status = 'processing' "
                    f"ORDER BY id",
                    staged_ids,
                ).fetchall()
                claimed_ids = [row["id"] for row in rows]

            # ── AWAIT phase: place orders (no conn held) ──
            placed = 0
            failed = 0
            results_lines = ["\u2501\u2501 Orders Placed \u2501\u2501", ""]

            try:
                ib_conn = await ensure_ib_connected()
                cached_positions = await ib_conn.reqPositionsAsync()
            except Exception as ib_exc:
                # Revert claimed rows back to staged so they're not stranded
                try:
                    reverted = _revert_pending_order_claims(claimed_ids)
                    if reverted > 0:
                        logger.warning(
                            "Reverted %d claimed rows after IB connection failure",
                            reverted,
                        )
                except Exception:
                    pass
                await query.edit_message_text(
                    f"\u274c IB connection failed: {ib_exc}\n"
                    f"Orders reverted to staged. Try /approve again after /reconnect."
                )
                return

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

            results_lines.append("")
            results_lines.append(f"Placed: {placed} | Failed: {failed}")

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
            with conn:
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
    iso_conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        iso_conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET re_validation_count = re_validation_count + 1 "
            "WHERE audit_id = ?",
            (audit_id,),
        )
        iso_conn.commit()
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
            with conn:
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
            with conn:
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
            with conn:
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
        with conn:
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
        order = _build_adaptive_sell_order(qty, limit_for_order, account_id)
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
            with conn:
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

PRE_TRADE_NOTIONAL_CEILING = 25_000


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

        if notional > PRE_TRADE_NOTIONAL_CEILING:
            return (False, f"Notional ${notional:,.0f} exceeds ${PRE_TRADE_NOTIONAL_CEILING:,} ceiling")

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
                        with conn:
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


def _build_adaptive_option_order(
    action: str,
    qty: int,
    limit_price: float,
    account_id: str,
    priority: str = "Patient",
) -> ib_async.Order:
    """Build a single-leg adaptive option order."""
    order = ib_async.Order()
    order.action = str(action or "SELL").upper()
    order.totalQuantity = qty
    order.orderType = "LMT"
    order.lmtPrice = round(limit_price, 2)
    order.algoStrategy = "Adaptive"
    order.algoParams = [ib_async.TagValue("adaptivePriority", priority)]
    order.tif = "DAY"
    order.account = account_id
    order.transmit = True
    return order


def _build_adaptive_sell_order(
    qty: int,
    limit_price: float,
    account_id: str,
    priority: str = "Patient",
) -> ib_async.Order:
    """Builds a single-leg adaptive order."""
    return _build_adaptive_option_order(
        action="SELL",
        qty=qty,
        limit_price=limit_price,
        account_id=account_id,
        priority=priority,
    )


def _build_adaptive_roll_combo(
    qty: int,
    limit_price: float,
    account_id: str,
    priority: str = "Urgent",
) -> ib_async.Order:
    """
    Builds an IBKR BAG combo order for a Roll.
    Action = BUY executes the legs exactly as defined (Buy 1, Sell 1).
    Positive limit = net debit. Negative limit = net credit.
    """
    order = ib_async.Order()
    order.action = "BUY"
    order.totalQuantity = qty
    order.orderType = "LMT"
    order.lmtPrice = round(limit_price, 2)
    order.algoStrategy = "Adaptive"
    order.algoParams = [ib_async.TagValue("adaptivePriority", priority)]
    order.tif = "DAY"
    order.account = account_id
    order.transmit = True
    return order


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
                            with conn:
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
                        with conn:
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
            order = _build_adaptive_roll_combo(qty, float(bid), acct_id, priority="Urgent")
        elif sec_type == "OPT":
            contract = ib_async.Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=expiry_fmt,
                strike=strike,
                right=right,
                exchange="SMART",
            )
            order = _build_adaptive_option_order(
                action=action,
                qty=qty,
                limit_price=_round_to_nickel(float(bid)),
                account_id=acct_id,
            )
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
            with conn:
                from agt_equities.order_state import append_status
                conn.execute(
                    "UPDATE pending_orders SET ib_order_id = ?, ib_perm_id = ? WHERE id = ?",
                    (ib_order_id, ib_perm_id, db_id),
                )
                append_status(conn, db_id, 'sent', 'placeOrder', {
                    'ib_order_id': str(ib_order_id),
                    'ib_perm_id': str(ib_perm_id),
                })

        # Add to roll watchlist ONLY for Mode 1 defensive CCs.
        # Mode 2 (welcome assignment, tax-exempt gain) and Dynamic Exit
        # (engineered assignment) do not need roll monitoring.
        mode = payload.get("mode", "")
        if mode == "MODE_1_DEFENSIVE":
            try:
                with closing(_get_db_connection()) as conn:
                    with conn:
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
                with conn:
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
            with conn:
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
        alerts = await _scan_and_stage_defensive_rolls(ib_conn)

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

    status_msg = await update.message.reply_text(
        "\u23f3 Scanning short puts for profit-take harvest..."
    )
    try:
        ib_conn = await ensure_ib_connected()

        def _stage(tickets: list[dict]) -> None:
            append_pending_tickets(tickets)

        result = await scan_csp_harvest_candidates(ib_conn, staging_callback=_stage)

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
    """
    Compute conviction tier from yfinance fundamentals.
    Returns {"tier": str, "modifier": float, "inputs": {...}}.
    """
    try:
        yf_tkr = yf.Ticker(ticker)
        info = yf_tkr.info or {}

        # EPS revision trend
        trailing_eps = info.get("trailingEps")
        forward_eps = info.get("forwardEps")
        if trailing_eps and forward_eps and abs(trailing_eps) > 0:
            eps_growth = (forward_eps - trailing_eps) / abs(trailing_eps)
            if eps_growth > 0.05:
                eps_trend = "POSITIVE"
            elif eps_growth < -0.05:
                eps_trend = "NEGATIVE"
            else:
                eps_trend = "FLAT"
        else:
            eps_trend = "UNAVAILABLE"

        # Revenue growth
        revenue_growth = info.get("revenueGrowth")
        if revenue_growth is not None:
            if revenue_growth > 0.10:
                rev_vs_sector = "ABOVE"
            elif revenue_growth > 0.0:
                rev_vs_sector = "AT"
            else:
                rev_vs_sector = "BELOW"
        else:
            rev_vs_sector = "UNAVAILABLE"

        # Analyst consensus
        rec_key = info.get("recommendationKey", "").lower()
        if rec_key in ("strong_buy", "buy"):
            analyst_shift = "UPGRADE"
        elif rec_key in ("sell", "strong_sell", "underperform"):
            analyst_shift = "DOWNGRADE"
        else:
            analyst_shift = "STABLE"

        # Margin level (point-in-time, not trend)
        op_margin = info.get("operatingMargins")
        if op_margin is not None:
            if op_margin > 0.15:
                margin_trend = "HIGH_MARGIN"
            elif op_margin > 0.05:
                margin_trend = "MID_MARGIN"
            else:
                margin_trend = "LOW_MARGIN"
        else:
            margin_trend = "UNAVAILABLE"

        # Tier assignment
        high_qual = (
            eps_trend == "POSITIVE"
            and rev_vs_sector in ("ABOVE", "AT")
            and analyst_shift != "DOWNGRADE"
        )
        low_qual = (
            eps_trend == "NEGATIVE"
            or rev_vs_sector == "BELOW"
            or margin_trend == "LOW_MARGIN"
            or analyst_shift == "DOWNGRADE"
        )

        if high_qual:
            tier = "HIGH"
        elif low_qual:
            tier = "LOW"
        else:
            tier = "NEUTRAL"

        return {
            "tier": tier,
            "modifier": CONVICTION_TIERS[tier],
            "inputs": {
                "eps_revision_trend": eps_trend,
                "revenue_growth_vs_sector": rev_vs_sector,
                "analyst_consensus_shift": analyst_shift,
                "margin_trend": margin_trend,
            },
        }
    except Exception as exc:
        logger.warning("_compute_conviction_tier failed for %s: %s", ticker, exc)
        return {
            "tier": "NEUTRAL",
            "modifier": CONVICTION_TIERS["NEUTRAL"],
            "inputs": {
                "eps_revision_trend": "UNAVAILABLE",
                "revenue_growth_vs_sector": "UNAVAILABLE",
                "analyst_consensus_shift": "UNAVAILABLE",
                "margin_trend": "UNAVAILABLE",
            },
        }


def _get_effective_conviction(ticker: str) -> dict:
    """
    Get conviction tier, checking for active CIO overrides first.
    Override expires after CONVICTION_OVERRIDE_EXPIRY_DAYS.
    """
    try:
        with closing(_get_db_connection()) as conn:
            # Check for active override
            override = conn.execute(
                """
                SELECT overridden_tier, justification, expires_at
                FROM conviction_overrides
                WHERE ticker = ? AND active = 1
                ORDER BY created_at DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()

            if override:
                # Check expiry
                try:
                    expires = _parse_override_expiry(override["expires_at"])
                    if _datetime.now(_timezone.utc) > expires:
                        # Expire the override
                        conn.execute(
                            "UPDATE conviction_overrides SET active = 0 WHERE ticker = ? AND active = 1",
                            (ticker,),
                        )
                    else:
                        tier = override["overridden_tier"]
                        return {
                            "tier": tier,
                            "modifier": CONVICTION_TIERS.get(tier, 0.30),
                            "source": "CIO_OVERRIDE",
                            "justification": override["justification"],
                            "expires": override["expires_at"][:10],
                        }
                except (ValueError, TypeError):
                    pass

        # No active override — compute from fundamentals
        computed = _compute_conviction_tier(ticker)
        computed["source"] = "COMPUTED"
        return computed

    except Exception as exc:
        logger.warning("_get_effective_conviction failed for %s: %s", ticker, exc)
        return {
            "tier": "NEUTRAL",
            "modifier": 0.30,
            "source": "DEFAULT",
        }


def _persist_conviction(ticker: str, conviction: dict) -> None:
    """Save computed conviction to ticker_universe."""
    try:
        inputs = conviction.get("inputs", {})
        with closing(_get_db_connection()) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE ticker_universe
                    SET conviction_tier = ?,
                        eps_revision_trend = ?,
                        revenue_growth_vs_sector = ?,
                        analyst_consensus_shift = ?,
                        margin_trend = ?,
                        conviction_updated_at = ?
                    WHERE ticker = ?
                    """,
                    (
                        conviction["tier"],
                        inputs.get("eps_revision_trend"),
                        inputs.get("revenue_growth_vs_sector"),
                        inputs.get("analyst_consensus_shift"),
                        inputs.get("margin_trend"),
                        _datetime.now().isoformat(),
                        ticker,
                    ),
                )
    except Exception as exc:
        logger.warning("_persist_conviction failed for %s: %s", ticker, exc)


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


async def _stage_dynamic_exit_candidate(
    ticker: str,
    hh_name: str,
    hh_data: dict,
    position: dict,
    source: str,
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

        staged_audit_ids = []
        try:
            with closing(_get_db_connection()) as conn:
                with conn:
                    for account_id, acct_contracts in allocation.items():
                        row_audit_id = str(uuid.uuid4())
                        scale = acct_contracts / excess_contracts
                        row_freed = round(best_freed * scale, 2)
                        row_realized = round(total_realized * scale, 2)
                        row_shares = acct_contracts * 100

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
                            "VALUES (?, date('now'), ?, ?, ?, "
                            " 'CC', ?, ?, "
                            " ?, ?, "
                            " ?, ?, "
                            " ?, ?, "
                            " ?, ?, ?, "
                            " ?, ?, ?, "
                            " ?, ?, 'STAGED', ?, ?)",
                            (
                                row_audit_id, ticker, hh_name, desk_mode,
                                round(hh_nlv, 2), round(spot, 4),
                                row_freed, row_realized,
                                conviction["tier"], round(modifier, 4),
                                round(best_ratio, 4), acct_contracts,
                                round(best_walk_away_per_share, 4),
                                round(best_strike, 2), best_exp,
                                acct_contracts, row_shares,
                                round(best_bid, 4),
                                now_ts, now_ts, source,
                                account_id,
                            ),
                        )
                        staged_audit_ids.append(row_audit_id)
                        logger.info(
                            "STAGED: %s %s %dc -> %s (%s)",
                            ticker, hh_short, acct_contracts, account_id,
                            ACCOUNT_LABELS.get(account_id, account_id),
                        )
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
            })
            acct_entry["shares"] += qty

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
            trade_repo.DB_PATH = DB_PATH
            for c in trade_repo.get_active_cycles():
                if c.cycle_type != 'WHEEL':
                    continue
                lkey = (c.household_id, c.ticker)
                _ledger_cache[lkey] = {
                    "initial_basis": c.paper_basis or 0,
                    "total_premium_collected": c.premium_total,
                    "shares_owned": int(c.shares_held),
                    "adjusted_basis": round(c.adjusted_basis, 4) if c.adjusted_basis else None,
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
# Phase 3: /mode1 command — chain walk + order staging
# ---------------------------------------------------------------------------

async def _walk_mode1_chain(
    ticker: str,
    spot: float,
    adjusted_basis: float,
    target_dte_range: tuple[int, int] = (14, 30),
) -> dict | None:
    """
    Walk the options chain for a Mode 1 CC candidate.
    Anchor = adjusted cost basis (ACB).
    Walk DOWN from the ACB + 10% spot buffer, never below ACB.
    Returns the best strike dict or None if nothing viable.
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

        # Pick the expiry closest to the midpoint of our target range
        mid_target = (min_dte + max_dte) // 2
        exp_str, dte = min(candidates, key=lambda x: abs(x[1] - mid_target))

        # Strike range per Rulebook Rule 7 Mode 1 + V2 Router refactor:
        # No write-time ACB floor — assignment-below-basis protection
        # lives in the V2 Router rolling path, not here. Walker hunts
        # from 3% OTM above spot up to 20% above adjusted basis,
        # prefers highest strike with viable premium.
        strike_floor = max(0.0, spot * 1.03)
        strike_ceiling = max(strike_floor, adjusted_basis * 1.20)

        try:
            chain_data = await _ibkr_get_chain(ticker, exp_str, right='C',
                                                min_strike=strike_floor,
                                                max_strike=strike_ceiling)
            calls = pd.DataFrame(chain_data)
        except Exception:
            return None

        if calls is None or not isinstance(calls, pd.DataFrame) or calls.empty:
            return None

        calls = calls.copy()
        calls["strike"] = pd.to_numeric(calls["strike"], errors="coerce")
        calls = calls.dropna(subset=["strike"])

        # Walk down from the buffered ceiling toward ACB, never below ACB.
        viable = calls[
            (calls["strike"] >= strike_floor)
            & (calls["strike"] <= strike_ceiling)
        ].sort_values(
            "strike", ascending=False
        )

        for _, row in viable.iterrows():
            strike = float(row["strike"])

            raw_bid = row.get("bid")
            raw_ask = row.get("ask")
            bid = float(raw_bid) if pd.notna(raw_bid) else 0.0
            ask = float(raw_ask) if pd.notna(raw_ask) else 0.0

            mid = round((bid + ask) / 2.0, 2) if bid and ask else bid

            if mid < MODE1_ABSOLUTE_BID_FLOOR:
                continue

            annualized = (mid / spot) * (365 / dte) * 100 if spot > 0 else 0
            otm_pct = ((strike - spot) / spot) * 100

            if annualized >= MODE1_MIN_ANNUALIZED_PCT:
                low_yield = annualized < MODE1_LOW_YIELD_PCT
                return {
                    "ticker": ticker,
                    "expiry": exp_str,
                    "dte": dte,
                    "strike": round(strike, 2),
                    "bid": mid,  # Overridden to Mid per V2 Execution Spec
                    "annualized": round(annualized, 2),
                    "otm_pct": round(otm_pct, 2),
                    "low_yield": low_yield,
                    "dte_range": f"{min_dte}-{max_dte}",
                }

        return None
    except Exception as exc:
        logger.warning("_walk_mode1_chain failed for %s: %s", ticker, exc)
        return None


async def _walk_harvest_chain(
    ticker: str,
    spot: float,
    assigned_basis: float,
    target_dte_range: tuple[int, int] = HARVEST_TARGET_DTE,
) -> dict | None:
    """
    Walk the options chain for a Mode 2 / Fully Amortized Harvest CC.
    Anchor = assigned basis (initial_basis).
    Select the HIGHEST strike that still yields within the 30-130% band.
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

        try:
            chain_data = await _ibkr_get_chain(ticker, exp_str, right='C',
                                                min_strike=assigned_basis,
                                                max_strike=max(spot * 1.3, assigned_basis))
            calls = pd.DataFrame(chain_data)
        except Exception:
            return None

        if calls is None or not isinstance(calls, pd.DataFrame) or calls.empty:
            return None

        calls = calls.copy()
        calls["strike"] = pd.to_numeric(calls["strike"], errors="coerce")
        calls = calls.dropna(subset=["strike"])

        viable = calls[calls["strike"] >= assigned_basis].sort_values(
            "strike", ascending=False
        )

        for _, row in viable.iterrows():
            strike = float(row["strike"])
            raw_bid = row.get("bid")
            raw_ask = row.get("ask")
            bid = float(raw_bid) if pd.notna(raw_bid) else 0.0
            ask = float(raw_ask) if pd.notna(raw_ask) else 0.0

            mid = round((bid + ask) / 2.0, 2) if bid and ask else bid

            if mid < HARVEST_ABSOLUTE_BID_FLOOR:
                continue

            annualized = (mid / strike) * (365 / dte) * 100 if strike > 0 else 0
            otm_pct = ((strike - spot) / spot) * 100 if spot > 0 else 0

            if annualized < HARVEST_MIN_ANNUALIZED_PCT:
                continue
            if annualized > HARVEST_MAX_ANNUALIZED_PCT:
                continue

            # Walk-away P&L per share = strike + premium - assigned_basis
            walk_away_pnl = _compute_walk_away_pnl(
                assigned_basis, strike, mid, quantity=1, multiplier=1
            ).walk_away_pnl_per_share

            return {
                "ticker": ticker,
                "expiry": exp_str,
                "dte": dte,
                "strike": round(strike, 2),
                "bid": mid,  # Overridden to Mid per V2 Execution Spec
                "annualized": round(annualized, 2),
                "otm_pct": round(otm_pct, 2),
                "walk_away_pnl": round(walk_away_pnl, 2),
                "dte_range": f"{min_dte}-{max_dte}",
            }

        return None
    except Exception as exc:
        logger.warning("_walk_harvest_chain failed for %s: %s", ticker, exc)
        return None


async def _run_cc_logic(household_filter: str | None = None) -> dict:
    """
    Unified CC pipeline — stages both Defensive (Mode 1) and Harvest
    (Mode 2 / Fully Amortized) covered calls in a single pass.
    Returns dict with "main_text" (str).
    """
    # Retry discovery once on IB failure
    disco = await _discover_positions(household_filter)
    if disco.get("error") and "connect" in str(disco["error"]).lower():
        logger.warning("IB connection issue, retrying discovery in 5s...")
        await asyncio.sleep(5)
        disco = await _discover_positions(household_filter)
    if disco.get("error"):
        logger.warning("CC discovery warning: %s", disco["error"])

    # Classify targets by mode
    defensive_targets: list[dict] = []
    harvest_targets: list[dict] = []
    for hh_data in disco["households"].values():
        for p in hh_data["positions"]:
            if p["available_contracts"] < 1:
                continue
            if p["mode"] == "MODE_1":
                defensive_targets.append(p)
            elif p["mode"] in ("MODE_2", "FULLY_AMORTIZED"):
                harvest_targets.append(p)

    if not defensive_targets and not harvest_targets:
        return {
            "main_text": "No positions with uncovered shares for CC staging.",
        }

    staged_defensive: list[dict] = []
    staged_harvest: list[dict] = []
    skipped: list[dict] = []

    # ── Detect overweight positions for Dynamic Exit carve-out ──
    dynamic_exit_staged: list[dict] = []
    excess_carveout: dict[str, int] = {}  # "household|ticker" -> excess_contracts

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

                # Stage dynamic exit candidate (replaces CIO payload generation)
                try:
                    stage_result = await _stage_dynamic_exit_candidate(
                        p["ticker"], hh_name, hh_data, p,
                        source="cc_overweight",
                    )
                    dynamic_exit_staged.append(stage_result)
                except Exception as de_exc:
                    logger.warning("Dynamic exit staging failed for %s: %s",
                                   p["ticker"], de_exc)

    # ── Defensive (Mode 1) — parallel chain walks ──
    async def _walk_defensive_target(p):
        """Walk chain for one defensive target. Returns (p, result, skip_reason)."""
        ticker = p["ticker"]
        spot = p["spot_price"]
        adj_basis = p["adjusted_basis"]

        if spot <= 0:
            return p, None, "No spot price"

        gap_pct = ((adj_basis - spot) / adj_basis * 100) if adj_basis > 0 else 0
        dte_range = (21, 30) if gap_pct > 15 else (7, 21)

        result = await _walk_chain_limited(
            _walk_mode1_chain, ticker, spot, adj_basis, dte_range
        )
        if result is None:
            result = await _walk_chain_limited(
                _walk_mode1_chain, ticker, spot, adj_basis, (45, 60)
            )
        if result is None:
            return p, None, "No viable strike (defensive)"

        return p, result, None

    defensive_results = await asyncio.gather(
        *[_walk_defensive_target(p) for p in defensive_targets],
        return_exceptions=True,
    )

    for item in defensive_results:
        if isinstance(item, Exception):
            logger.warning("Defensive chain walk failed: %s", item)
            continue

        p, result, skip_reason = item

        if skip_reason:
            skipped.append({
                "ticker": p["ticker"],
                "reason": skip_reason,
                "household": p["household"],
                "mode": "DEFENSIVE",
                "spot": p["spot_price"],
                "adjusted_basis": p.get("adjusted_basis", 0),
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
                    "mode": "DEFENSIVE",
                    "spot": p["spot_price"],
                    "adjusted_basis": p.get("adjusted_basis", 0),
                })
                continue

        ticker = p["ticker"]
        working_pa = p.get("working_per_account", {})
        staged_pa = p.get("staged_per_account", {})

        for acct_id, acct_info in p["accounts_with_shares"].items():
            if remaining_available < 1:
                break
            acct_shares = acct_info["shares"]

            # Per-account encumbrance: filled + working + staged
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

            ticket = {
                "account_id": acct_id,
                "household": p["household"],
                "ticker": ticker,
                "action": "SELL",
                "sec_type": "OPT",
                "right": "C",
                "strike": result["strike"],
                "expiry": result["expiry"],
                "quantity": acct_contracts,
                "limit_price": result["bid"],
                "annualized_yield": result["annualized"],
                "mode": "MODE_1_DEFENSIVE",
                "status": "staged",
            }
            staged_defensive.append({**ticket, **result})

    # ── Harvest (Mode 2 / Fully Amortized) — parallel chain walks ──
    async def _walk_harvest_target(p):
        """Walk chain for one harvest target. Returns (p, result, skip_reason)."""
        ticker = p["ticker"]
        spot = p["spot_price"]
        initial_basis = p["initial_basis"]

        if spot <= 0:
            return p, None, "No spot price"

        result = await _walk_chain_limited(
            _walk_harvest_chain, ticker, spot, initial_basis, HARVEST_TARGET_DTE
        )
        if result is None:
            return p, None, "No viable strike (harvest)"

        return p, result, None

    harvest_results = await asyncio.gather(
        *[_walk_harvest_target(p) for p in harvest_targets],
        return_exceptions=True,
    )

    for item in harvest_results:
        if isinstance(item, Exception):
            logger.warning("Harvest chain walk failed: %s", item)
            continue

        p, result, skip_reason = item

        if skip_reason:
            skipped.append({
                "ticker": p["ticker"],
                "reason": skip_reason,
                "household": p["household"],
                "mode": "HARVEST",
                "spot": p["spot_price"],
                "adjusted_basis": p.get("adjusted_basis", 0),
            })
            continue

        remaining_available = p["available_contracts"]
        ticker = p["ticker"]
        working_pa = p.get("working_per_account", {})
        staged_pa = p.get("staged_per_account", {})

        for acct_id, acct_info in p["accounts_with_shares"].items():
            if remaining_available < 1:
                break
            acct_shares = acct_info["shares"]

            # Per-account encumbrance: filled + working + staged
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

            ticket = {
                "account_id": acct_id,
                "household": p["household"],
                "ticker": ticker,
                "action": "SELL",
                "sec_type": "OPT",
                "right": "C",
                "strike": result["strike"],
                "expiry": result["expiry"],
                "quantity": acct_contracts,
                "limit_price": result["bid"],
                "annualized_yield": result["annualized"],
                "mode": "MODE_2_HARVEST",
                "status": "staged",
            }
            staged_harvest.append({**ticket, **result})

    # Write all staged tickets to SQLite
    all_staged = staged_defensive + staged_harvest
    if all_staged:
        try:
            with closing(_get_db_connection()) as conn:
                with conn:
                    conn.execute(
                        "UPDATE pending_orders SET status = 'superseded' WHERE status = 'staged'"
                    )
            await asyncio.to_thread(append_pending_tickets, all_staged)

            # Log BOTH staged and skipped to cycle log
            cycle_log_entries = []
            for s in all_staged:
                entry = dict(s)
                if s.get("low_yield"):
                    entry["flag"] = "LOW_YIELD"
                else:
                    entry["flag"] = "NORMAL" if s.get("mode") == "MODE_1_DEFENSIVE" else "HARVEST_OK"
                cycle_log_entries.append(entry)

            for sk in skipped:
                reason = sk.get("reason", "")
                if "no viable strike" in reason.lower():
                    flag = "NO_VIABLE_STRIKE"
                elif "no spot" in reason.lower():
                    flag = "SKIPPED"
                else:
                    flag = "SKIPPED"
                skip_entry = {
                    "ticker": sk["ticker"],
                    "household": sk.get("household", ""),
                    "mode": sk.get("mode", "DEFENSIVE"),
                    "flag": flag,
                    "spot_price": sk.get("spot", 0),
                    "adjusted_basis": sk.get("adjusted_basis", 0),
                }
                cycle_log_entries.append(skip_entry)

            await asyncio.to_thread(_log_cc_cycle, cycle_log_entries)
        except Exception as db_exc:
            logger.exception("Failed to write staged tickets to DB: %s", db_exc)

    # Build output message
    lines = []

    if staged_defensive:
        lines.append("\u2501\u2501 Defensive CCs (Mode 1) \u2501\u2501")
        lines.append("")
        for s in staged_defensive:
            label = ACCOUNT_LABELS.get(s["account_id"], s["account_id"])
            lines.append(
                f"{s['ticker']} | SELL -{s['quantity']}c "
                f"${s['strike']:.0f}C {s['expiry']} @ ${s['bid']:.2f}"
            )
            lines.append(
                f"  {s['annualized']:.1f}% ann \u00b7 "
                f"{s['otm_pct']:.1f}% OTM \u00b7 {s['dte']}d \u00b7 "
                f"{label}"
            )
            if s.get("low_yield"):
                lines.append("  \u26a0\ufe0f LOW-YIELD \u2014 consider extended DTE")
            lines.append("")

    if staged_harvest:
        lines.append("\u2501\u2501 Harvest CCs (Mode 2) \u2501\u2501")
        lines.append("")
        for s in staged_harvest:
            label = ACCOUNT_LABELS.get(s["account_id"], s["account_id"])
            pnl_label = f"+${s['walk_away_pnl']:.2f}" if s.get("walk_away_pnl", 0) >= 0 else f"-${abs(s['walk_away_pnl']):.2f}"
            lines.append(
                f"{s['ticker']} | SELL -{s['quantity']}c "
                f"${s['strike']:.0f}C {s['expiry']} @ ${s['bid']:.2f}"
            )
            lines.append(
                f"  {s['annualized']:.1f}% ann \u00b7 "
                f"{s['otm_pct']:.1f}% OTM \u00b7 {s['dte']}d \u00b7 "
                f"walk-away {pnl_label}/sh \u00b7 {label}"
            )
            lines.append("")

    if dynamic_exit_staged:
        staged_count = sum(1 for r in dynamic_exit_staged if r["staged"])
        total_candidates = len(dynamic_exit_staged)
        lines.append(f"\u2501\u2501 Dynamic Exits: {staged_count}/{total_candidates} STAGED \u2501\u2501")
        for r in dynamic_exit_staged:
            lines.append(f"  {r['summary']}")
        lines.append("")

    if skipped:
        lines.append("Skipped:")
        for sk in skipped:
            lines.append(f"  {sk['ticker']}: {sk['reason']}")
        lines.append("")

    total = len(staged_defensive) + len(staged_harvest)
    lines.append(f"Defensive: {len(staged_defensive)} | Harvest: {len(staged_harvest)} | Total: {total}")
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
            with conn:
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
    """Daily 9:45 AM ET — auto-stage Defensive + Harvest CCs."""
    try:
        now_et = _datetime.now(ET)
        if now_et.weekday() >= 5:
            logger.info("Scheduled CC: skipping weekend")
            return

        if not CC_AUTO_STAGE_ENABLED:
            logger.info("Auto-staging disabled (CC_AUTO_STAGE_ENABLED=False). Run /cc manually.")
            return

        result = await _run_cc_logic(household_filter=None)
        result_text = result["main_text"]

        await context.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=f"<pre>{html.escape(result_text)}</pre>",
            parse_mode="HTML",
        )

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


async def _scan_and_stage_defensive_rolls(ib_conn) -> list[str]:
    """
    V2 master router for open short calls.
    Routes each live short call through States 1-3 in lexicographical order.
    Returns a list of alert strings for Telegram.

    ADR-005: V2 is WARTIME-allowed via the v2_router site in
    _pre_trade_gates. Mode is logged here for operator visibility but
    is NOT gated at staging — execution gate enforces it.
    """
    if _HALTED:
        logger.info("V2 router: skipped (desk halted)")
        return ["[V2 ROUTER] Skipped — desk halted via /halt."]

    from datetime import date
    import math

    try:
        current_mode = _get_current_desk_mode()
    except Exception as mode_exc:
        logger.warning("V2 router: mode lookup failed (%s) — assuming PEACETIME", mode_exc)
        current_mode = "PEACETIME"

    alerts: list[str] = [f"━━ V2 Router [mode={current_mode}] ━━"]
    logger.info("V2 router: scan starting in mode=%s", current_mode)
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
            return []

        ib_conn.reqMarketDataType(4)  # Set once for the connection
        ledger_cache: dict[tuple[str, str], dict | None] = {}  # key: (account_id, ticker) per ADR-006

        for pos in short_calls:
            ticker = pos.contract.symbol.upper()
            strike = float(pos.contract.strike)
            qty = abs(int(pos.position))
            acct_id = pos.account
            household = ACCOUNT_TO_HOUSEHOLD.get(acct_id, "")

            # Calculate DTE
            exp_fmt = str(pos.contract.lastTradeDateOrContractMonth)
            try:
                exp_date = date(int(exp_fmt[:4]), int(exp_fmt[4:6]), int(exp_fmt[6:8]))
                dte = (exp_date - date.today()).days
            except (ValueError, TypeError):
                continue
            if dte < 0:
                continue

            # Fetch Spot
            try:
                spot = float(await _ibkr_get_spot(ticker))
            except Exception as exc:
                logger.warning("Failed to fetch spot for %s roll evaluation: %s", ticker, exc)
                continue

            # Fetch Greeks and top-of-book for the live short call
            qual_contracts = await ib_conn.qualifyContractsAsync(pos.contract)
            if not qual_contracts:
                continue

            ticker_data = ib_conn.reqMktData(qual_contracts[0], "106", False, False)
            await asyncio.sleep(2)  # Linear wait acceptable for EOD watchdog (<10 positions)

            ask = getattr(ticker_data, "ask", getattr(ticker_data, "delayedAsk", None))
            bid = getattr(ticker_data, "bid", getattr(ticker_data, "delayedBid", None))

            delta = None
            if getattr(ticker_data, "modelGreeks", None):
                delta = ticker_data.modelGreeks.delta
            elif getattr(ticker_data, "bidGreeks", None):
                delta = ticker_data.bidGreeks.delta

            ib_conn.cancelMktData(qual_contracts[0])

            if ask is None or bid is None or delta is None or math.isnan(ask) or math.isnan(bid) or math.isnan(delta):
                continue

            ask = float(ask)
            bid = float(bid)
            delta = abs(float(delta))
            intrinsic_value = max(0.0, spot - strike)
            extrinsic_value = ask - intrinsic_value
            spread = max(0.0, ask - bid)

            # ADR-006: per-account key — household-aggregated basis is an
            # Act 60 Chapter 2 compliance defect. The scan iterates positions
            # per-account, so cache keys must be per-account too.
            ledger_key = (acct_id, ticker)
            if ledger_key not in ledger_cache:
                ledger_cache[ledger_key] = (
                    await asyncio.to_thread(
                        _load_premium_ledger_snapshot,
                        household, ticker, acct_id,
                    )
                    if household else None
                )
            ledger_snapshot = ledger_cache.get(ledger_key)
            assigned_basis = None
            adjusted_basis = None
            if ledger_snapshot:
                raw_initial = ledger_snapshot.get("initial_basis")
                raw_adjusted = ledger_snapshot.get("adjusted_basis")
                if raw_initial is not None:
                    assigned_basis = float(raw_initial)
                if raw_adjusted is not None:
                    adjusted_basis = float(raw_adjusted)

            # STATE 1 — ASSIGN (Act 60 Velocity)
            if assigned_basis is not None and spot >= assigned_basis and delta >= 0.85:
                alerts.append(
                    f"[ASSIGN] {ticker} Delta > 0.85. Letting shares get called away."
                )
                continue

            # STATE 1 — ASSIGN (Microstructure Trap)
            if intrinsic_value > 0 and (
                extrinsic_value <= spread or extrinsic_value <= 0.05
            ):
                alerts.append(
                    f"[ASSIGN] {ticker} Extrinsic exhausted. Parity breached. Defense standing down."
                )
                continue

            # STATE 2 — HARVEST (Capital Efficiency)
            initial_credit = abs(float(getattr(pos, "avgCost", 0.0) or 0.0)) / 100.0
            pnl_pct = (
                (initial_credit - ask) / initial_credit
                if initial_credit > 0
                else 0.0
            )
            ray = (ask / strike) * (365 / dte) if strike > 0 and dte > 0 else float("inf")
            if pnl_pct >= 0.85 or ray < 0.10:
                ticket = {
                    "timestamp": _datetime.now().isoformat(),
                    "account_id": acct_id,
                    "account_label": ACCOUNT_LABELS.get(acct_id, acct_id),
                    "ticker": ticker,
                    "sec_type": "OPT",
                    "action": "BUY",
                    "order_type": "LMT",
                    "right": "C",
                    "strike": strike,
                    "expiry": exp_fmt,
                    "quantity": qty,
                    "limit_price": round(ask, 2),
                    "status": "staged",
                    "transmit": True,
                    "strategy": "V2 Harvest BTC",
                    "mode": "STATE_2_HARVEST",
                    "origin": "v2_router",
                    "v2_state": "HARVEST",
                    "v2_rationale": (
                        f"pnl_pct={pnl_pct:.3f} ray={ray:.3f} "
                        f"initial_credit={initial_credit:.2f} ask={ask:.2f}"
                    ),
                }
                try:
                    await asyncio.to_thread(append_pending_tickets, [ticket])
                    alerts.append(f"[HARVEST] {ticker} Capital dead. Staging BTC.")
                except Exception as stage_exc:
                    logger.warning("Failed to stage BTC for %s: %s", ticker, stage_exc)
                continue

            # STATE 3 — DEFEND (EV-Accretive Roll)
            if adjusted_basis is not None and spot < adjusted_basis and delta >= 0.40:
                best_roll = None
                try:
                    expirations = await _ibkr_get_expirations(ticker)
                except Exception as chain_exc:
                    logger.warning("Failed to fetch expirations for %s defense: %s", ticker, chain_exc)
                    expirations = []

                for tgt_exp in expirations:
                    try:
                        tgt_date = date.fromisoformat(tgt_exp)
                    except ValueError:
                        continue

                    tgt_dte = (tgt_date - date.today()).days
                    if tgt_dte <= dte or tgt_dte >= 90:
                        continue

                    try:
                        chain_data = await _ibkr_get_chain(
                            ticker,
                            tgt_exp,
                            right='C',
                            min_strike=strike,
                            max_strike=max(strike * 1.5, spot * 1.5, (adjusted_basis or strike) * 1.5),
                        )
                    except Exception as chain_exc:
                        logger.warning(
                            "Failed to fetch future chain for %s %s: %s",
                            ticker, tgt_exp, chain_exc,
                        )
                        continue

                    for c in chain_data or []:
                        try:
                            roll_strike = float(c.get("strike"))
                        except (TypeError, ValueError):
                            continue
                        if roll_strike <= strike:
                            continue

                        raw_sell_bid = c.get("bid")
                        if raw_sell_bid is None or pd.isna(raw_sell_bid):
                            continue

                        sell_bid = float(raw_sell_bid)
                        debit_paid = round(ask - sell_bid, 2)
                        if debit_paid <= 0:
                            continue

                        intrinsic_gained = roll_strike - strike
                        ev_ratio = (intrinsic_gained - debit_paid) / debit_paid
                        if ev_ratio < 2.0:
                            continue

                        candidate = {
                            "sell_expiry": tgt_exp,
                            "sell_expiry_ib": tgt_exp.replace("-", ""),
                            "sell_strike": roll_strike,
                            "sell_bid": sell_bid,
                            "debit_paid": debit_paid,
                            "intrinsic_gained": intrinsic_gained,
                            "ev_ratio": ev_ratio,
                            "dte": tgt_dte,
                        }
                        if best_roll is None or (
                            candidate["ev_ratio"],
                            candidate["sell_strike"],
                            -candidate["dte"],
                        ) > (
                            best_roll["ev_ratio"],
                            best_roll["sell_strike"],
                            -best_roll["dte"],
                        ):
                            best_roll = candidate

                if best_roll:
                    sell_contract = ib_async.Option(
                        symbol=ticker,
                        lastTradeDateOrContractMonth=best_roll["sell_expiry_ib"],
                        strike=best_roll["sell_strike"],
                        right="C",
                        exchange="SMART",
                    )
                    qual_sell = await ib_conn.qualifyContractsAsync(sell_contract)
                    if qual_sell:
                        ticket = {
                            "timestamp": _datetime.now().isoformat(),
                            "account_id": acct_id,
                            "account_label": ACCOUNT_LABELS.get(acct_id, acct_id),
                            "ticker": ticker,
                            "sec_type": "BAG",
                            "action": "BUY",
                            "quantity": qty,
                            "order_type": "LMT",
                            "limit_price": round(best_roll["debit_paid"], 2),
                            "status": "staged",
                            "transmit": True,
                            "strategy": "V2 EV-Accretive Roll",
                            "mode": "STATE_3_DEFEND",
                            "origin": "v2_router",
                            "v2_state": "DEFEND",
                            "v2_rationale": (
                                f"adjusted_basis={adjusted_basis:.2f} spot={spot:.2f} "
                                f"delta={delta:.3f} ev_ratio={best_roll['ev_ratio']:.2f} "
                                f"debit={best_roll['debit_paid']:.2f}"
                            ),
                            "strike": best_roll["sell_strike"],
                            "expiry": best_roll["sell_expiry_ib"],
                            "right": "C",
                            "combo_legs": [
                                {
                                    "conId": qual_contracts[0].conId or pos.contract.conId,
                                    "ratio": 1,
                                    "action": "BUY",
                                    "exchange": "SMART",
                                    "strike": strike,
                                    "expiry": exp_fmt,
                                },
                                {
                                    "conId": qual_sell[0].conId,
                                    "ratio": 1,
                                    "action": "SELL",
                                    "exchange": "SMART",
                                    "strike": best_roll["sell_strike"],
                                    "expiry": best_roll["sell_expiry_ib"],
                                },
                            ],
                        }
                        try:
                            await asyncio.to_thread(append_pending_tickets, [ticket])
                            alerts.append(f"[DEFEND] {ticker} EV-Accretive Roll staged.")
                        except Exception as stage_exc:
                            logger.warning("Failed to stage defensive roll for %s: %s", ticker, stage_exc)

    except Exception as exc:
        logger.warning("Defensive roll scan failed: %s", exc)

    return alerts


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
            ib_conn = await ensure_ib_connected()

            def _stage_csp_harvest(tickets: list[dict]) -> None:
                append_pending_tickets(tickets)

            csp_result = await scan_csp_harvest_candidates(
                ib_conn, staging_callback=_stage_csp_harvest,
            )
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
                        with conn:
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
                                with conn:
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
            roll_alerts = await _scan_and_stage_defensive_rolls(ib_conn)
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
            with conn:
                conn.execute("BEGIN IMMEDIATE")  # D11: lock before SELECT

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
            with conn:
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
            with conn:
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


# ---------------------------------------------------------------------------
# Sprint 1D: STAGED alert coalescing flush job
# ---------------------------------------------------------------------------

async def _flush_staged_alerts_job(context) -> None:
    """Flush buffered STAGED alerts as a single digest message."""
    if _HALTED:
        return
    global _staged_alert_last_flush
    now = time.time()
    if not _staged_alert_buffer:
        return
    if now - _staged_alert_last_flush < STAGED_COALESCE_WINDOW:
        return

    lines = [f"\u26a0\ufe0f {len(_staged_alert_buffer)} STAGED"]
    for row in _staged_alert_buffer:
        tk = row.get("ticker", "?")
        act = row.get("action_type", "?")
        qty = row.get("contracts") or row.get("shares") or "?"
        unit = "c" if act == "CC" else "sh"
        strike = f"${row['strike']:.0f}C" if row.get("strike") else ""
        limit_p = f"@ ${row['limit_price']:.2f}" if row.get("limit_price") else ""
        hh = (row.get("household") or "").replace("_Household", "")
        lines.append(f"\u00b7 {tk} Sell {qty}{unit} {strike} {limit_p} | {hh}")

    text = "\n".join(lines)
    try:
        await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
    except Exception as exc:
        logger.warning("staged_alert_flush: send failed: %s", exc)

    _staged_alert_buffer.clear()
    _staged_alert_last_flush = now


# Followup #17: orphan scan state sets for resolution policy (D3)
_OPEN_FILLED_STATES = frozenset({"Filled"})
_OPEN_DEAD_STATES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})
_OPEN_LIVE_STATES = frozenset({
    "Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"
})


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
                    with conn:
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
                    with conn:
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
                with conn:
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

        trade_repo.DB_PATH = DB_PATH
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
                    with conn:
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


def main() -> None:
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
    app.add_handler(CallbackQueryHandler(handle_orders_callback, pattern=r"^orders:"))
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(handle_dex_callback, pattern=r"^dex:"))
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
        jq.run_daily(
            callback=_scheduled_watchdog,
            time=_time(hour=15, minute=30, tzinfo=ET),
            days=(1, 2, 3, 4, 5),
            name="watchdog_daily",
        )
        logger.info("Scheduled: watchdog_daily at 3:30 PM ET (Mon-Fri)")
        jq.run_monthly(
            callback=_scheduled_universe_refresh,
            when=_time(hour=6, minute=0, tzinfo=ET),
            day=1,
            name="universe_monthly",
        )
        logger.info("Scheduled: universe_monthly on 1st at 6:00 AM ET")
        jq.run_daily(
            callback=_scheduled_conviction_refresh,
            time=_time(hour=20, minute=0, tzinfo=ET),
            days=(0,),  # Sunday only
            name="conviction_weekly",
        )
        logger.info("Scheduled: conviction_weekly at 8:00 PM ET (Sunday)")
        jq.run_daily(
            callback=_scheduled_flex_sync,
            time=_time(hour=17, minute=0, tzinfo=ET),
            days=(1, 2, 3, 4, 5),
            name="flex_sync_eod",
        )
        logger.info("Scheduled: flex_sync_eod at 5:00 PM ET (Mon-Fri)")
        jq.run_repeating(
            callback=_poll_attested_rows,
            interval=10,
            first=10,
            name="attested_poller",
        )
        logger.info("Scheduled: attested_poller every 10s")
        jq.run_repeating(
            callback=_sweep_attested_ttl_job,
            interval=60,
            first=30,
            name="attested_sweeper",
        )
        logger.info("Scheduled: attested_sweeper every 60s")
        jq.run_repeating(
            callback=_el_snapshot_writer_job,
            interval=30,
            first=15,
            name="el_snapshot_writer",
        )
        logger.info("Scheduled: el_snapshot_writer every 30s")
        jq.run_repeating(
            callback=_flush_staged_alerts_job,
            interval=15,
            first=20,
            name="staged_alert_flush",
        )
        logger.info("Scheduled: staged_alert_flush every 15s")

        # Sprint 1F Fix 2: daily beta cache refresh (04:00 local, pre-market)
        async def _beta_cache_refresh_job(context):
            if _HALTED:
                return
            try:
                from agt_equities.beta_cache import refresh_beta_cache
                tickers = []
                try:
                    from agt_equities import trade_repo
                    from pathlib import Path
                    trade_repo.DB_PATH = str(Path(__file__).resolve().parent / "agt_desk.db")
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
            time=_dt_time(4, 0),
            name="beta_cache_refresh",
        )
        logger.info("Scheduled: beta_cache_refresh daily at 04:00")
        async def _beta_startup_check(context):
            await _beta_cache_refresh_job(context)
        jq.run_once(_beta_startup_check, when=10, name="beta_startup")

        # Sprint 1F Fix 6: daily R7 corporate intel refresh (05:00 local)
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
                    trade_repo.DB_PATH = str(_P(__file__).resolve().parent / "agt_desk.db")
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
            time=_dt_time(5, 0),
            name="corporate_intel_refresh",
        )
        logger.info("Scheduled: corporate_intel_refresh daily at 05:00")
        async def _corporate_intel_startup(context):
            await _corporate_intel_refresh_job(context)
        jq.run_once(_corporate_intel_startup, when=15, name="corporate_intel_startup")
    else:
        logger.warning("JobQueue not available — scheduled jobs not registered")

    # Sprint 1C+1D outbound formatting now handled by AGTFormattedBot subclass
    # (replaces monkey-patch that broke on PTB 22.7 TelegramObject._frozen lockdown).
    # Followup #14 still open: ~53 reply_text sites bypass _format_outbound.
    logger.info("Outbound formatting via AGTFormattedBot (paper=%s, mode prefix=active)", PAPER_MODE)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
