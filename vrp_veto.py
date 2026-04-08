"""
VRP Veto Tool — Volatility Risk Premium filter for covered-call screening.

Standalone module: importable functions + __main__ for scheduled daily run.
Never places orders. Never touches agt_desk.db.
"""

import asyncio
import logging
import math
from contextlib import closing
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import finnhub
import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent
_ENV_PATH = _BASE_DIR / ".env"
load_dotenv(_ENV_PATH, override=True)

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")

_VRP_DB_PATH = _BASE_DIR / "vrp_analytics.db"

EXCLUDED_TICKERS = {"IBKR", "TRAW.CVR", "SPX", "SLS", "GTLB"}

# VRP thresholds
VRP_OK_THRESHOLD = 5.0        # > 5.0 → OK to sell
VRP_THIN_THRESHOLD = 2.0      # 2.0–5.0 → THIN
# 0.0–2.0 → VERY_THIN
# < 0.0 → DO_NOT_SELL

# IBKR connection (standalone mode only)
_IB_HOST = "127.0.0.1"
_IB_GATEWAY_PORT = 4001
_IB_TWS_PORT = 7496
_IB_CLIENT_ID = 50

# Earnings look-ahead window
_EARNINGS_WINDOW_DAYS = 45

# RV staleness threshold (calendar days since last close)
_RV_STALE_DAYS = 3

# yfinance timeout
_YF_TIMEOUT = 15

logger = logging.getLogger("vrp_veto")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_vrp_db() -> None:
    """Create the vrp_daily table if it doesn't exist."""
    with closing(sqlite3.connect(str(_VRP_DB_PATH))) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vrp_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    run_timestamp TEXT NOT NULL,
                    run_type TEXT NOT NULL DEFAULT 'scheduled',
                    ticker TEXT NOT NULL,
                    iv REAL,
                    iv_source TEXT,
                    rv_20d REAL,
                    rv_source TEXT,
                    rv_last_close_date TEXT,
                    rv_stale INTEGER DEFAULT 0,
                    vrp REAL,
                    signal TEXT NOT NULL,
                signal_downgraded INTEGER DEFAULT 0,
                earnings_date TEXT,
                earnings_source TEXT,
                days_to_earnings INTEGER
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vrp_daily_date_ticker
            ON vrp_daily(run_date, ticker)
        """)


def write_vrp_results(results: list, run_type: str = "scheduled") -> None:
    """Append VRP scan results to vrp_analytics.db."""
    init_vrp_db()
    now = datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    run_ts = now.isoformat(timespec="seconds")

    rows = []
    for r in results:
        sig = r.get("signal", {})
        iv_r = r.get("iv", {})
        rv_r = r.get("rv", {})
        earn = r.get("earnings", {})
        rows.append((
            run_date,
            run_ts,
            run_type,
            r.get("ticker", ""),
            iv_r.get("iv"),
            iv_r.get("source"),
            rv_r.get("rv"),
            rv_r.get("source"),
            rv_r.get("last_close_date"),
            1 if rv_r.get("stale") else 0,
            sig.get("vrp"),
            sig.get("signal", "NO_DATA"),
            1 if sig.get("downgraded") else 0,
            earn.get("earnings_date"),
            earn.get("source"),
            earn.get("days_to_earnings"),
        ))

    with closing(sqlite3.connect(str(_VRP_DB_PATH))) as conn:
        with conn:
            conn.executemany("""
                INSERT INTO vrp_daily (
                    run_date, run_timestamp, run_type, ticker,
                    iv, iv_source, rv_20d, rv_source,
                    rv_last_close_date, rv_stale, vrp, signal,
                    signal_downgraded, earnings_date, earnings_source,
                    days_to_earnings
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_rv(ticker: str) -> dict:
    """
    Calculate 20-day realized volatility (annualized %).
    Source chain: yfinance → Finnhub stock candles.
    """
    # Source A — yfinance
    source = None
    closes = None
    error_parts = []

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: yf.download(ticker, period="1y", progress=False))
            try:
                hist = future.result(timeout=_YF_TIMEOUT)
            except (FuturesTimeout, Exception) as exc:
                hist = pd.DataFrame()
                error_parts.append(f"yfinance: {exc}")

        if hist is not None and not hist.empty:
            col = "Close"
            if isinstance(hist.columns, pd.MultiIndex):
                hist.columns = hist.columns.get_level_values(0)
            if col in hist.columns:
                closes = hist[col].dropna()
                source = "yfinance"
    except Exception as exc:
        error_parts.append(f"yfinance: {exc}")

    # Source B — Finnhub stock candles (fallback)
    if closes is None or len(closes) < 25:
        if FINNHUB_API_KEY:
            try:
                fc = finnhub.Client(api_key=FINNHUB_API_KEY)
                now_ts = int(time.time())
                one_year_ago_ts = now_ts - (365 * 86400)
                candles = fc.stock_candles(ticker, "D", one_year_ago_ts, now_ts)
                if candles and candles.get("s") == "ok" and candles.get("c"):
                    closes = pd.Series(
                        candles["c"],
                        index=pd.to_datetime(candles["t"], unit="s"),
                    )
                    source = "finnhub"
            except Exception as exc:
                error_parts.append(f"finnhub: {exc}")

    if closes is None or len(closes) < 25:
        return {
            "rv": None, "source": None, "last_close_date": None,
            "stale": True, "error": " + ".join(error_parts) or "insufficient data",
        }

    # RV calculation
    log_returns = np.log(closes / closes.shift(1)).dropna()

    if len(log_returns) < 20:
        return {
            "rv": None, "source": source, "last_close_date": None,
            "stale": True,
            "error": f"Only {len(log_returns)} trading days available, need 20",
        }

    rv_20d = float(log_returns.rolling(window=20).std().iloc[-1] * np.sqrt(252) * 100)

    # Staleness detection
    last_idx = closes.index[-1]
    if hasattr(last_idx, "date"):
        last_close_date = last_idx.date()
    else:
        last_close_date = last_idx
    days_gap = (date.today() - last_close_date).days
    rv_is_stale = days_gap > _RV_STALE_DAYS

    return {
        "rv": round(rv_20d, 1),
        "source": source,
        "last_close_date": last_close_date.isoformat(),
        "stale": rv_is_stale,
        "error": None,
    }


def _yf_fetch_atm_iv(ticker: str):
    """Fetch ATM implied volatility from yfinance option chain."""
    yf_tkr = yf.Ticker(ticker)
    expiries = yf_tkr.options
    if not expiries:
        return None

    today = date.today()
    target_exp = None
    for exp_str in expiries:
        try:
            dte = (date.fromisoformat(exp_str) - today).days
            if dte >= 7:
                target_exp = exp_str
                break
        except ValueError:
            continue
    if target_exp is None:
        target_exp = expiries[0]

    chain = yf_tkr.option_chain(target_exp)
    last_price = yf_tkr.fast_info.get("lastPrice") or yf_tkr.fast_info.get("last_price")
    if last_price is None or last_price <= 0:
        return None

    calls = chain.calls
    puts = chain.puts
    if calls.empty:
        return None

    atm_idx = (calls["strike"] - last_price).abs().idxmin()
    atm_strike = calls.loc[atm_idx, "strike"]

    iv_values = []
    for df in [calls, puts]:
        row = df.loc[df["strike"] == atm_strike]
        if not row.empty:
            v = row["impliedVolatility"].values[0]
            if v is not None and v > 0.01:
                iv_values.append(v)

    if iv_values:
        return round(sum(iv_values) / len(iv_values) * 100, 1)
    return None


def fetch_iv(ib, ticker: str) -> dict:
    """
    Fetch implied volatility from IBKR (synchronous, for standalone mode).
    Fallback to yfinance ATM option chain IV.
    """
    from ib_async import Stock, util

    # Source A — IBKR model IV via generic tick 106
    try:
        contract = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(contract)
        ib.reqMarketDataType(4)  # delayed
        md = ib.reqMktData(contract, genericTickList="106")
        util.sleep(4)

        raw_iv = md.impliedVolatility
        ib.cancelMktData(contract)
        util.sleep(1)

        if raw_iv is not None and not math.isnan(raw_iv) and raw_iv >= 0.01:
            iv = round(raw_iv * 100, 1)
            return {"iv": iv, "source": "ibkr_delayed", "stale": False, "error": None}
    except Exception as exc:
        logger.warning("IBKR IV fetch failed for %s: %s", ticker, exc)

    # Source B — yfinance ATM option IV
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_yf_fetch_atm_iv, ticker)
            try:
                yf_iv = future.result(timeout=_YF_TIMEOUT)
            except (FuturesTimeout, Exception):
                yf_iv = None

        if yf_iv is not None:
            return {"iv": yf_iv, "source": "yfinance_chain", "stale": True, "error": None}
    except Exception as exc:
        logger.warning("yfinance IV fallback failed for %s: %s", ticker, exc)

    return {"iv": None, "source": None, "stale": True, "error": "IBKR IV=None, yfinance chain failed"}


async def fetch_iv_from_ibkr_async(ib, ticker: str) -> dict:
    """
    Fetch implied volatility from IBKR (async, for bot mode).
    Uses await asyncio.sleep() instead of util.sleep().
    Fallback to yfinance ATM option chain IV (via to_thread).
    """
    from ib_async import Stock

    # Source A — IBKR model IV via generic tick 106
    try:
        contract = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(contract)
        ib.reqMarketDataType(4)
        md = ib.reqMktData(contract, genericTickList="106")
        await asyncio.sleep(4)

        raw_iv = md.impliedVolatility
        ib.cancelMktData(contract)
        await asyncio.sleep(1)

        if raw_iv is not None and not math.isnan(raw_iv) and raw_iv >= 0.01:
            iv = round(raw_iv * 100, 1)
            return {"iv": iv, "source": "ibkr_delayed", "stale": False, "error": None}
    except Exception as exc:
        logger.warning("IBKR IV async fetch failed for %s: %s", ticker, exc)

    # Source B — yfinance ATM option IV
    try:
        yf_iv = await asyncio.to_thread(_yf_fetch_atm_iv, ticker)
        if yf_iv is not None:
            return {"iv": yf_iv, "source": "yfinance_chain", "stale": True, "error": None}
    except Exception as exc:
        logger.warning("yfinance IV async fallback failed for %s: %s", ticker, exc)

    return {"iv": None, "source": None, "stale": True, "error": "IBKR IV=None, yfinance chain failed"}


def fetch_earnings(ticker: str) -> dict:
    """
    Check whether earnings fall within the next 45 days.
    Source chain: Finnhub → yfinance (also used as cross-check).
    """
    today = date.today()
    look_ahead = today + timedelta(days=_EARNINGS_WINDOW_DAYS)

    fh_date, fh_days = None, None
    fh_error = False
    yf_date, yf_days = None, None
    yf_error = False

    # Source A — Finnhub
    if FINNHUB_API_KEY:
        try:
            fc = finnhub.Client(api_key=FINNHUB_API_KEY)
            resp = fc.earnings_calendar(
                _from=today.isoformat(),
                to=look_ahead.isoformat(),
                symbol=ticker.upper(),
            )
            entries = resp.get("earningsCalendar", [])
            if entries:
                fh_date_str = min(e["date"] for e in entries if e.get("date"))
                fh_dt = date.fromisoformat(fh_date_str)
                fh_days = (fh_dt - today).days
                if 0 <= fh_days <= _EARNINGS_WINDOW_DAYS:
                    fh_date = fh_date_str
                else:
                    fh_date, fh_days = None, None
        except Exception as exc:
            logger.warning("Finnhub earnings lookup failed for %s: %s", ticker, exc)
            fh_error = True
    else:
        fh_error = True

    # Source B — yfinance
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is not None and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if isinstance(dates, list) and dates:
                yf_dt = dates[0]
                if hasattr(yf_dt, "date"):
                    yf_dt = yf_dt.date()
                elif isinstance(yf_dt, str):
                    yf_dt = date.fromisoformat(yf_dt)
                yf_days_val = (yf_dt - today).days
                if 0 <= yf_days_val <= _EARNINGS_WINDOW_DAYS:
                    yf_date = yf_dt.isoformat()
                    yf_days = yf_days_val
    except Exception as exc:
        logger.warning("yfinance earnings lookup failed for %s: %s", ticker, exc)
        yf_error = True

    # Conflict resolution
    final_date, final_days, source = None, None, None

    if fh_date and yf_date:
        fh_d = date.fromisoformat(fh_date)
        yf_d = date.fromisoformat(yf_date)
        diff = abs((fh_d - yf_d).days)
        if diff <= 3:
            final_date, final_days, source = fh_date, fh_days, "finnhub"
        else:
            logger.warning(
                "%s earnings mismatch — Finnhub: %s, yfinance: %s. Using earlier.",
                ticker, fh_date, yf_date,
            )
            if fh_d <= yf_d:
                final_date, final_days, source = fh_date, fh_days, "both_disagree_used_earlier"
            else:
                final_date, final_days, source = yf_date, yf_days, "both_disagree_used_earlier"
    elif fh_date:
        final_date, final_days, source = fh_date, fh_days, "finnhub"
    elif yf_date:
        final_date, final_days, source = yf_date, yf_days, "yfinance"

    # Suppression logic
    suppressed = False
    if final_date:
        # Earnings found within window → suppress
        suppressed = True
    elif fh_error and yf_error:
        # Both sources errored → unknown, suppress
        suppressed = True
    # else: no earnings in window from working source(s) → no suppression

    return {
        "earnings_date": final_date,
        "days_to_earnings": final_days,
        "source": source,
        "suppressed": suppressed,
    }


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------

def compute_vrp_signal(iv_result: dict, rv_result: dict, earnings_result: dict) -> dict:
    """Compute VRP and assign a signal classification."""
    # Earnings suppression
    if earnings_result["suppressed"]:
        if earnings_result["earnings_date"]:
            return {
                "vrp": None,
                "signal": "EARNINGS_SKIP",
                "emoji": "\u26d4",
                "label": f"EARNINGS IN {earnings_result['days_to_earnings']}d \u2014 SKIP",
                "downgraded": False,
            }
        else:
            return {
                "vrp": None,
                "signal": "EARNINGS_UNKNOWN",
                "emoji": "\u26a0\ufe0f",
                "label": "EARNINGS DATE UNKNOWN \u2014 SKIP",
                "downgraded": False,
            }

    # Missing data
    if iv_result.get("iv") is None or rv_result.get("rv") is None:
        missing = []
        if iv_result.get("iv") is None:
            missing.append("IV")
        if rv_result.get("rv") is None:
            missing.append("RV")
        return {
            "vrp": None,
            "signal": "NO_DATA",
            "emoji": "\u2753",
            "label": f"NO DATA ({', '.join(missing)} unavailable)",
            "downgraded": False,
        }

    # Compute VRP
    vrp = round(iv_result["iv"] - rv_result["rv"], 1)

    if vrp > VRP_OK_THRESHOLD:
        signal, emoji, label = "OK", "\u2705", "OK to sell"
    elif vrp >= VRP_THIN_THRESHOLD:
        signal, emoji, label = "THIN", "\u26a0\ufe0f", "THIN"
    elif vrp >= 0.0:
        signal, emoji, label = "VERY_THIN", "\u26a0\ufe0f", "VERY THIN"
    else:
        signal, emoji, label = "DO_NOT_SELL", "\ud83d\udeab", "DO NOT SELL"

    return {
        "vrp": vrp,
        "signal": signal,
        "emoji": emoji,
        "label": label,
        "downgraded": False,
    }


def apply_staleness_downgrade(signal_result: dict, iv_result: dict, rv_result: dict) -> dict:
    """Downgrade marginal signals when data is stale."""
    data_is_stale = iv_result.get("stale", False) or rv_result.get("stale", False)
    if data_is_stale and signal_result["signal"] in ("THIN", "VERY_THIN"):
        signal_result["signal"] = "DO_NOT_SELL"
        signal_result["emoji"] = "\ud83d\udeab"
        signal_result["label"] = "DO NOT SELL (stale data in margin zone)"
        signal_result["downgraded"] = True
    return signal_result


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

def discover_holdings_from_positions(positions: list) -> list:
    """
    Filter IBKR positions to unique long-stock tickers.
    Takes the already-fetched positions list (no IBKR call needed).
    """
    tickers = set()
    for pos in positions:
        contract = pos.contract
        if (
            getattr(contract, "secType", "") == "STK"
            and pos.position > 0
            and contract.symbol not in EXCLUDED_TICKERS
        ):
            tickers.add(contract.symbol)
    return sorted(tickers)


def discover_holdings(ib) -> list:
    """Discover current stock holdings from IBKR. For standalone mode."""
    positions = ib.positions()
    return discover_holdings_from_positions(positions)


def scan_single_ticker(ib, ticker: str) -> dict:
    """Run full VRP check on a single ticker (synchronous, standalone mode)."""
    rv_result = fetch_rv(ticker)
    earnings_result = fetch_earnings(ticker)
    iv_result = fetch_iv(ib, ticker)

    signal = compute_vrp_signal(iv_result, rv_result, earnings_result)
    signal = apply_staleness_downgrade(signal, iv_result, rv_result)

    return {
        "ticker": ticker,
        "iv": iv_result,
        "rv": rv_result,
        "earnings": earnings_result,
        "signal": signal,
    }


def scan_all_holdings(ib, holdings: list = None) -> list:
    """Run VRP check on all holdings (synchronous, standalone mode)."""
    if holdings is None:
        holdings = discover_holdings(ib)
    results = []
    for ticker in holdings:
        logger.info("Scanning %s ...", ticker)
        try:
            result = scan_single_ticker(ib, ticker)
            results.append(result)
        except Exception as exc:
            logger.exception("scan_single_ticker failed for %s", ticker)
            results.append({
                "ticker": ticker,
                "iv": {"iv": None, "source": None, "stale": True, "error": str(exc)},
                "rv": {"rv": None, "source": None, "last_close_date": None, "stale": True, "error": str(exc)},
                "earnings": {"earnings_date": None, "days_to_earnings": None, "source": None, "suppressed": False},
                "signal": {"vrp": None, "signal": "NO_DATA", "emoji": "\u2753",
                           "label": f"NO DATA (error: {exc})", "downgraded": False},
            })
    return results


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

_SIGNAL_SORT = {
    "OK": 0, "THIN": 1, "VERY_THIN": 2, "DO_NOT_SELL": 3,
    "EARNINGS_SKIP": 4, "EARNINGS_UNKNOWN": 5, "NO_DATA": 6,
}


def format_full_report(results: list, run_time: str) -> str:
    """Format the full VRP report as Telegram HTML."""
    if not results:
        return "<b>VRP Veto</b>\nNo holdings to report."

    def _sort_key(r):
        sig = r["signal"]["signal"]
        vrp = r["signal"].get("vrp") or 0
        return (_SIGNAL_SORT.get(sig, 9), -vrp)

    results = sorted(results, key=_sort_key)

    lines = [f"<b>VRP Veto</b>  {run_time}", ""]

    for r in results:
        sig = r["signal"]
        ticker = r["ticker"]
        emoji = sig["emoji"]

        if sig["signal"] in ("EARNINGS_SKIP", "EARNINGS_UNKNOWN"):
            days = r["earnings"].get("days_to_earnings", "?")
            lines.append(f"{emoji} <code>{ticker:<6}</code> earnings {days}d \u2014 skip")
        elif sig["signal"] == "NO_DATA":
            lines.append(f"{emoji} <code>{ticker:<6}</code> no data")
        else:
            iv = r["iv"]["iv"]
            rv = r["rv"]["rv"]
            vrp = sig["vrp"]
            sign = "+" if vrp >= 0 else ""
            label = sig["label"]
            lines.append(
                f"{emoji} <code>{ticker:<6}</code>"
                f" IV {iv:5.1f}  RV {rv:5.1f}  "
                f"VRP {sign}{vrp:.1f}  \u2014 {label}"
            )

    # Data notes in expandable blockquote
    notes = []
    for r in results:
        t = r["ticker"]
        if r.get("rv", {}).get("stale"):
            d = r["rv"].get("last_close_date", "?")
            notes.append(f"{t} RV: close {d} [stale]")
        if r.get("iv", {}).get("source") == "yfinance_chain":
            notes.append(f"{t} IV: yfinance fallback")
        if r.get("signal", {}).get("downgraded"):
            notes.append(f"{t}: downgraded \u2014 stale data in margin zone")

    if notes:
        detail = "\n".join(notes)
        lines.append("")
        lines.append(f"<blockquote expandable>\u26a0\ufe0f Data notes:\n{detail}</blockquote>")

    lines.append("")
    lines.append("<i>Scheduled events only. Check catalysts manually.</i>")

    return "\n".join(lines)


def format_single_report(result: dict, ticker: str, is_held: bool) -> str:
    """Format single-ticker VRP check as Telegram HTML."""
    sig = result["signal"]
    emoji = sig["emoji"]

    if sig["signal"] in ("EARNINGS_SKIP", "EARNINGS_UNKNOWN"):
        days = result["earnings"].get("days_to_earnings", "?")
        main = f"{emoji} <b>{ticker}</b> \u2014 earnings in {days}d, skipped"
    elif sig["signal"] == "NO_DATA":
        err = result.get("iv", {}).get("error", "") or result.get("rv", {}).get("error", "")
        main = f"{emoji} <b>{ticker}</b> \u2014 no data ({err})"
    else:
        iv_val = result["iv"]["iv"]
        rv_val = result["rv"]["rv"]
        vrp = sig["vrp"]
        sign = "+" if vrp >= 0 else ""
        iv_src = result["iv"].get("source", "?")
        rv_src = result["rv"].get("source", "?")
        rv_date = result["rv"].get("last_close_date", "?")

        main = (
            f"{emoji} <b>{ticker}</b>  VRP {sign}{vrp:.1f}\n"
            f"IV {iv_val:.1f} ({iv_src}) \u00b7 RV {rv_val:.1f} ({rv_src}, {rv_date})\n"
            f"{sig['label']}"
        )

        if sig.get("downgraded"):
            main += f"\n\ud83d\udeab Downgraded: stale data in margin zone"

    held_note = "" if is_held else f"\n<i>Not a current holding.</i>"
    return f"{main}{held_note}"


# ---------------------------------------------------------------------------
# Telegram sender (standalone mode only)
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    """Send a message via Telegram Bot API (standalone mode). Tries HTML first."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_USER_ID:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID not set")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_USER_ID,
                "text": chunk,
                "parse_mode": "HTML",
            }, timeout=15)
            if resp.status_code != 200:
                # HTML parse may have failed — retry as plain text
                import re as _re
                plain = _re.sub(r"<[^>]+>", "", chunk)
                resp2 = requests.post(url, json={
                    "chat_id": TELEGRAM_USER_ID,
                    "text": plain,
                }, timeout=15)
                if resp2.status_code != 200:
                    logger.error("Telegram send failed: %s %s", resp2.status_code, resp2.text)
        except Exception as exc:
            logger.error("Telegram send error: %s", exc)


# ---------------------------------------------------------------------------
# __main__ — Standalone scheduled run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # File handler for standalone mode only
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _file_handler = RotatingFileHandler(
        _BASE_DIR / "vrp_veto.log", maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)

    # Weekend check
    ET = pytz.timezone("US/Eastern")
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        logger.info("Weekend — skipping VRP run.")
        sys.exit(0)

    ib = None
    try:
        from ib_async import IB, util

        ib = IB()
        connected = False
        for port, label in [(_IB_GATEWAY_PORT, "Gateway"), (_IB_TWS_PORT, "TWS")]:
            try:
                logger.info("Connecting to %s:%s (%s) clientId=%s ...", _IB_HOST, port, label, _IB_CLIENT_ID)
                ib.connect(_IB_HOST, port, clientId=_IB_CLIENT_ID, timeout=10)
                ib.reqMarketDataType(4)
                util.sleep(2)
                ib.reqPositions()
                util.sleep(1)
                connected = True
                logger.info("Connected to %s on port %s", label, port)
                break
            except Exception as exc:
                logger.warning("Failed to connect to %s (%s): %s", label, port, exc)

        if not connected:
            msg = "\ud83d\udd34 VRP Veto: IBKR connection failed on Gateway (4001) and TWS (7496). No report today."
            logger.error(msg)
            send_telegram(msg)
            sys.exit(1)

        # Discover and scan
        holdings = discover_holdings(ib)
        if not holdings:
            msg = "\u2139\ufe0f VRP Veto: No stock holdings found."
            logger.info(msg)
            send_telegram(msg)
            sys.exit(0)

        results = scan_all_holdings(ib, holdings)

        # Check if market appears closed (all IV = None)
        all_iv_none = all(r["iv"].get("iv") is None for r in results)
        if all_iv_none:
            msg = "\u2139\ufe0f VRP Veto: Market appears closed (no IV data). Skipping today."
            logger.info(msg)
            send_telegram(msg)
            sys.exit(0)

        # Format and send
        run_time = now_et.strftime("%Y-%m-%d  %H:%M ET")
        report = format_full_report(results, run_time)
        send_telegram(report)

        # Write to DB
        write_vrp_results(results, "scheduled")
        logger.info("VRP report sent and written to DB (%d tickers)", len(results))

    except Exception as exc:
        logger.exception("VRP Veto standalone run failed")
        try:
            send_telegram(f"\ud83d\udd34 VRP Veto CRASHED: {exc}")
        except Exception:
            pass
        sys.exit(1)
    finally:
        if ib is not None:
            try:
                ib.disconnect()
                logger.info("IBKR disconnected.")
            except Exception:
                pass
