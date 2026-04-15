"""AGT Equities -- ticker_universe DB refresh from Wikipedia + yfinance.

Extracted from telegram_bot.py for Decoupling Sprint A (A5e) so both the
bot and the scheduler daemon can call refresh_ticker_universe() without
importing the full bot module.

Sources:
  - Wikipedia S&P 500 list
  - Wikipedia NASDAQ-100 list
  - yfinance GICS sector/industry enrichment

Writes to the ``ticker_universe`` table via agt_equities.db shared module.
Returns a dict: {added: int, updated: int, total: int, error: str|None}.
"""
from __future__ import annotations

import logging
import time
from contextlib import closing
from datetime import datetime as _datetime

import pandas as pd
import yfinance as yf

from agt_equities.db import get_db_connection, tx_immediate

logger = logging.getLogger("agt_equities.universe_refresh")


def refresh_ticker_universe(*, db_path=None) -> dict:
    """Refresh ticker_universe from Wikipedia S&P 500 + NASDAQ-100 + yfinance GICS.

    Returns {"added": int, "updated": int, "total": int, "error": str|None}.
    Thread-safe (opens its own connection per call).
    """
    try:
        import requests as _req

        _wiki_session = _req.Session()
        _wiki_session.headers.update(
            {"User-Agent": "AGTEquitiesBot/1.0 (research; contact: admin@agt.pr)"}
        )
        tickers: dict[str, dict] = {}

        # -- S&P 500 from Wikipedia --
        try:
            _sp500_html = _wiki_session.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=15,
            ).text
            from io import StringIO

            sp500_tables = pd.read_html(StringIO(_sp500_html), match="Symbol")
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

        # -- NASDAQ-100 from Wikipedia --
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
                    if any(
                        k in str(col).lower()
                        for k in ("company", "security", "name")
                    ):
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
            return {
                "added": 0,
                "updated": 0,
                "total": 0,
                "error": "Both Wikipedia scrapes failed",
            }

        all_syms = list(tickers.keys())
        added = 0
        updated = 0

        with closing(get_db_connection(db_path=db_path)) as conn:
            with tx_immediate(conn):
                existing = {
                    row["ticker"]
                    for row in conn.execute(
                        "SELECT ticker FROM ticker_universe"
                    ).fetchall()
                }

                CHUNK_SIZE = 20
                now_iso = _datetime.now().isoformat()

                for i in range(0, len(all_syms), CHUNK_SIZE):
                    chunk = all_syms[i : i + CHUNK_SIZE]
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
                                (
                                    entry["company_name"],
                                    gics_sector,
                                    gics_industry_group,
                                    index_str,
                                    now_iso,
                                    sym,
                                ),
                            )
                            updated += 1
                        else:
                            conn.execute(
                                """INSERT INTO ticker_universe
                                       (ticker, company_name, gics_sector,
                                        gics_industry_group, index_membership,
                                        last_updated)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (
                                    sym,
                                    entry["company_name"],
                                    gics_sector,
                                    gics_industry_group,
                                    index_str,
                                    now_iso,
                                ),
                            )
                            added += 1

                    time.sleep(1.0)

        total = added + updated
        logger.info(
            "ticker_universe refresh: %d added, %d updated, %d total",
            added,
            updated,
            total,
        )
        return {"added": added, "updated": updated, "total": total, "error": None}

    except Exception as exc:
        logger.exception("refresh_ticker_universe failed")
        return {"added": 0, "updated": 0, "total": 0, "error": str(exc)}
