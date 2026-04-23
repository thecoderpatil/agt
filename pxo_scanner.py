"""
AGT Equities — PXO Scanner (Engine 1: CSP Entry)
==================================================
Scans approved watchlist tickers for near-term Cash-Secured Put candidates
(3-10 DTE) that meet the Heitkoetter 30%+ annualized yield requirement.

Usage:
    # As a module (called from telegram_bot.py):
    from pxo_scanner import scan_csp_candidates
    results = scan_csp_candidates()

    # Standalone dry-run:
    python pxo_scanner.py
"""

import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date as _date
from pathlib import Path

import pandas as pd
import yfinance as yf

from agt_equities.screener.config import EXCLUDED_SECTORS

logger = logging.getLogger("pxo_scanner")

# ---------------------------------------------------------------------------
# Dynamic universe loader — reads from ticker_universe SQLite table.
# Falls back to a hardcoded minimum set if DB is empty/missing.
# ---------------------------------------------------------------------------
# Sprint 5 MR B (E-M-4): lazy DB-path resolution — no __file__-anchored fallback.
# Imported from agt_equities.db so prod NSSM env is authoritative.
from agt_equities.db import get_db_path as _get_db_path


def _resolve_scanner_db_path() -> Path:
    return _get_db_path()

_FALLBACK_WATCHLIST: list[dict] = [
    {"ticker": "AAPL",  "sector": "Technology Hardware"},
    {"ticker": "MSFT",  "sector": "Software - Infrastructure"},
    {"ticker": "GOOGL", "sector": "Internet Content & Information"},
    {"ticker": "AMZN",  "sector": "Internet Retail"},
    {"ticker": "META",  "sector": "Internet Content & Information"},
    {"ticker": "JPM",   "sector": "Banks - Diversified"},
    {"ticker": "OXY",   "sector": "Oil & Gas E&P"},
    {"ticker": "XOM",   "sector": "Oil & Gas Integrated"},
    {"ticker": "WMT",   "sector": "Discount Stores"},
    {"ticker": "UNH",   "sector": "Healthcare Plans"},
    {"ticker": "QCOM",  "sector": "Semiconductors"},
    {"ticker": "COST",  "sector": "Discount Stores"},
    {"ticker": "MCD",   "sector": "Restaurants"},
    {"ticker": "AXP",   "sector": "Credit Services"},
]


def _load_scan_universe() -> list[dict]:
    """Load CSP scan candidates from ticker_universe table."""
    try:
        from contextlib import closing
        with closing(sqlite3.connect(str(_resolve_scanner_db_path()), timeout=10.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT ticker, gics_industry_group AS sector
                   FROM ticker_universe
                   WHERE gics_industry_group IS NOT NULL
                     AND gics_industry_group != ''
                   ORDER BY ticker"""
            ).fetchall()
        if rows:
            # C3.6/C1 hard-exclude: drop any industry_group in EXCLUDED_SECTORS.
            # Case-insensitive. Airlines/Biotechnology/Pharmaceuticals (quality)
            # + REITs/MLPs/BDCs/Trusts/SPACs (structural non-C-corp buckets)
            # must never enter the CSP candidate pool. Prior leak path: MRNA +
            # 12 tickers reached the 2026-04-17 09:35 scan candidate list.
            excl = {s.lower() for s in EXCLUDED_SECTORS}
            return [
                {"ticker": r["ticker"], "sector": r["sector"]}
                for r in rows
                if (r["sector"] or "").strip().lower() not in excl
            ]
    except Exception:
        pass
    return _FALLBACK_WATCHLIST


def _prefilter_by_volatility(
    watchlist: list[dict],
    max_tickers: int,
) -> list[dict]:
    """
    Fast pre-filter: rank tickers by 5-day price change magnitude.
    Higher absolute move = richer IV = better CSP premium.
    """
    try:
        tickers = [e["ticker"] for e in watchlist]
        tickers_str = " ".join(tickers)
        data = yf.download(tickers_str, period="5d", progress=False, threads=True)
        close = data.get("Close")
        if close is None or close.empty:
            return watchlist[:max_tickers]

        moves = {}
        for tkr in tickers:
            try:
                series = close if len(tickers) == 1 else close[tkr]
                if len(series) >= 2 and pd.notna(series.iloc[-1]) and pd.notna(series.iloc[0]):
                    pct_move = abs((series.iloc[-1] - series.iloc[0]) / series.iloc[0])
                    moves[tkr] = pct_move
            except (KeyError, IndexError):
                pass

        sorted_tickers = sorted(moves, key=moves.get, reverse=True)[:max_tickers]
        ticker_set = set(sorted_tickers)
        return [e for e in watchlist if e["ticker"] in ticker_set]

    except Exception as exc:
        logger.warning("Pre-filter failed: %s — using first %d tickers", exc, max_tickers)
        return watchlist[:max_tickers]

# ---------------------------------------------------------------------------
# Heitkoetter filter constants
# ---------------------------------------------------------------------------
MIN_ANNUALIZED_ROI = 30.0    # 30% floor (Rulebook v5, Rule 7)
MAX_ANNUALIZED_ROI = 130.0   # 130% ceiling — too close to ATM
MIN_PREMIUM = 0.10           # $0.10/contract minimum
MIN_DTE = 3                  # near-term weeklies
MAX_DTE = 10                 # 3-10 DTE window
MIN_OTM_PCT = 3.0            # strike must be >= 3% below current price


def _best_premium(row: pd.Series) -> float | None:
    """Extract the best usable premium from a chain row."""
    for col in ("lastPrice", "bid", "ask"):
        val = row.get(col)
        if val is not None and pd.notna(val) and float(val) > 0:
            if col == "ask":
                bid = row.get("bid")
                if bid is not None and pd.notna(bid) and float(bid) > 0:
                    return round((float(bid) + float(val)) / 2, 2)
            return round(float(val), 2)
    return None


def scan_single_ticker(
    ticker: str,
    sector: str,
) -> list[dict]:
    """Scan one ticker for CSP candidates within the DTE window."""
    results: list[dict] = []
    try:
        yf_ticker = yf.Ticker(ticker)
        current_price = None

        # Try fast_info first, fall back to history
        try:
            fast = yf_ticker.fast_info
            current_price = float(
                getattr(fast, "last_price", 0)
                or getattr(fast, "previous_close", 0)
            )
        except Exception:
            pass

        if not current_price or current_price <= 0:
            hist = yf_ticker.history(period="2d")
            if hist.empty:
                return []
            current_price = float(hist["Close"].iloc[-1])

        if current_price <= 0:
            return []

        raw_expirations = list(yf_ticker.options or [])
        today = _date.today()

        # Filter to DTE window
        valid_expirations: list[tuple[str, int]] = []
        for exp_str in raw_expirations:
            try:
                exp_date = _date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if MIN_DTE <= dte <= MAX_DTE:
                valid_expirations.append((exp_str, dte))

        for exp_str, dte in valid_expirations:
            try:
                chain = yf_ticker.option_chain(exp_str)
                puts = chain.puts
            except Exception:
                continue

            if puts is None or not isinstance(puts, pd.DataFrame) or puts.empty:
                continue

            puts = puts.copy()
            puts["strike"] = pd.to_numeric(puts["strike"], errors="coerce")
            puts = puts.dropna(subset=["strike"])
            if puts.empty:
                continue

            for _, row in puts.iterrows():
                try:
                    strike = float(row["strike"])

                    # Must be OTM (below current price)
                    if strike >= current_price:
                        continue

                    # Minimum OTM distance check
                    otm_pct = ((current_price - strike) / current_price) * 100
                    if otm_pct < MIN_OTM_PCT:
                        continue

                    premium = _best_premium(row)
                    if premium is None or premium < MIN_PREMIUM:
                        continue

                    # Annualized ROI = (premium / strike) * (365 / dte)
                    ann_roi = (premium / strike) * (365 / dte) * 100

                    if ann_roi < MIN_ANNUALIZED_ROI:
                        continue
                    if ann_roi > MAX_ANNUALIZED_ROI:
                        continue

                    # Capital required = strike * 100 (cash-secured)
                    capital_required = strike * 100

                    # Delta (informational, not a gate)
                    raw_delta = row.get("delta", 0)
                    delta = abs(float(raw_delta)) if pd.notna(raw_delta) else 0.0

                    # Volume / OI (informational)
                    raw_vol = row.get("volume", 0)
                    raw_oi = row.get("openInterest", 0)
                    volume = int(raw_vol) if pd.notna(raw_vol) else 0
                    open_interest = int(raw_oi) if pd.notna(raw_oi) else 0

                    results.append({
                        "ticker": ticker,
                        "sector": sector,
                        "current_price": round(current_price, 2),
                        "strike": round(strike, 2),
                        "expiry": exp_str,
                        "dte": dte,
                        "premium": premium,
                        "ann_roi": round(ann_roi, 2),
                        "otm_pct": round(otm_pct, 2),
                        "delta": round(delta, 4),
                        "volume": volume,
                        "open_interest": open_interest,
                        "capital_required": round(capital_required, 2),
                    })
                except (ValueError, TypeError, KeyError) as row_exc:
                    logger.debug("Skipping bad row in %s %s: %s", ticker, exp_str, row_exc)
                    continue

    except Exception as exc:
        logger.warning("Scanner failed for %s: %s", ticker, exc)

    return results


def scan_csp_candidates(
    watchlist: list[dict] | None = None,
    top_n: int = 5,
    max_scan: int = 50,
) -> list[dict]:
    """
    Scan candidates and return the top N sorted by annualized ROI.
    If the universe exceeds max_scan, pre-filter to the most volatile names.
    """
    if watchlist is None:
        watchlist = _load_scan_universe()

    if len(watchlist) > max_scan:
        watchlist = _prefilter_by_volatility(watchlist, max_scan)

    all_candidates: list[dict] = []

    for entry in watchlist:
        ticker = entry["ticker"]
        sector = entry.get("sector", "Unknown")
        logger.info("Scanning %s...", ticker)
        hits = scan_single_ticker(ticker, sector)
        all_candidates.extend(hits)

    # Sort by annualized ROI descending, take top N
    all_candidates.sort(key=lambda x: x["ann_roi"], reverse=True)
    top = all_candidates[:top_n]

    # ── Append latest headline for each unique ticker in the top list ──
    headline_cache: dict[str, str] = {}
    for candidate in top:
        tkr = candidate["ticker"]
        if tkr not in headline_cache:
            headline_cache[tkr] = _fetch_latest_headline(tkr)
        candidate["headline"] = headline_cache[tkr]

    return top


_NEWS_TIMEOUT_SECONDS = 3


def _fetch_latest_headline(ticker: str) -> str:
    """Fetch the most recent news headline with a strict 3-second timeout."""
    def _inner() -> str:
        news = yf.Ticker(ticker).news
        if news and isinstance(news, list) and len(news) > 0:
            first = news[0]
            # yfinance >=0.2.28 nests articles under a "content" key
            if isinstance(first, dict) and "content" in first:
                first = first["content"]
            title = first.get("title", "")
            if title:
                return str(title)
        return "(no recent headline)"

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_inner)
            return future.result(timeout=_NEWS_TIMEOUT_SECONDS)
    except FuturesTimeout:
        logger.warning("News fetch timed out for %s (>%ds)", ticker, _NEWS_TIMEOUT_SECONDS)
        return "(headline unavailable — timeout)"
    except Exception as exc:
        logger.warning("News fetch failed for %s: %s", ticker, exc)
        return "(headline unavailable)"


# ---------------------------------------------------------------------------
# Standalone dry-run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from agt_equities.boot import assert_boot_contract
    assert_boot_contract()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    watchlist = _load_scan_universe()

    print("=" * 72)
    print("  AGT EQUITIES — PXO SCANNER (Engine 1: CSP Entry)")
    print(f"  Watchlist: {len(watchlist)} tickers")
    print(f"  DTE Window: {MIN_DTE}-{MAX_DTE} days")
    print(f"  Yield Floor: {MIN_ANNUALIZED_ROI}% annualized")
    print("=" * 72)

    candidates = scan_csp_candidates(watchlist=watchlist, top_n=10)

    if not candidates:
        print("\n  No candidates found meeting Heitkoetter criteria.")
    else:
        print(f"\n  Found {len(candidates)} candidates:\n")
        print(f"  {'Ticker':<8} {'Strike':>8} {'Exp':>12} {'DTE':>4} "
              f"{'Prem':>7} {'AnnROI':>8} {'OTM%':>6} {'CapReq':>10}")
        print("  " + "-" * 72)
        for c in candidates:
            print(f"  {c['ticker']:<8} ${c['strike']:>7.2f} "
                  f"{c['expiry']:>12} {c['dte']:>4} "
                  f"${c['premium']:>6.2f} {c['ann_roi']:>7.2f}% "
                  f"{c['otm_pct']:>5.1f}% ${c['capital_required']:>9,.2f}")

    print()
