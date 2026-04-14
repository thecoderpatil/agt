"""
agt_equities/beta_cache.py — Cached yfinance trailing betas.

Sprint 1F Fix 2: eliminates sync yfinance calls from hot paths.
Betas refreshed daily at 04:00 via scheduled job. Deck + rule engine
read from beta_cache table only.

Schema: CREATE TABLE beta_cache (
    ticker TEXT PRIMARY KEY,
    beta REAL NOT NULL DEFAULT 1.0,
    fetched_ts TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"

_STALE_DAYS = 14  # warn if beta older than this


def _get_conn() -> sqlite3.Connection:
    """FU-B: delegate to shared db module (get_db_connection)."""
    from agt_equities.db import get_db_connection
    return get_db_connection()


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create beta_cache table if missing. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS beta_cache (
            ticker      TEXT PRIMARY KEY,
            beta        REAL NOT NULL DEFAULT 1.0,
            fetched_ts  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def get_beta(ticker: str, conn: sqlite3.Connection | None = None) -> float:
    """Read cached beta for ticker. Returns 1.0 if missing or stale."""
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT beta, fetched_ts FROM beta_cache WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if not row:
            return 1.0
        # Staleness warning (informational, still return cached value)
        try:
            ts = datetime.fromisoformat(row["fetched_ts"].replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - ts).days
            if age_days > _STALE_DAYS:
                logger.warning("beta_cache: %s beta is %d days stale", ticker, age_days)
        except Exception:
            pass
        return row["beta"]
    except Exception as exc:
        logger.warning("get_beta(%s) failed: %s", ticker, exc)
        return 1.0
    finally:
        if own_conn:
            conn.close()


def get_betas(tickers: list[str], conn: sqlite3.Connection | None = None) -> dict[str, float]:
    """Batch read betas. Returns {ticker: beta} with 1.0 default for missing."""
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        result = {}
        for tk in tickers:
            result[tk] = get_beta(tk, conn)
        return result
    finally:
        if own_conn:
            conn.close()


def refresh_beta_cache(tickers: list[str]) -> dict[str, float]:
    """Fetch trailing betas from yfinance and upsert into beta_cache.

    Called by daily scheduled job (04:00 local). Per-ticker try/except
    so one failure doesn't block others.
    """
    import yfinance as yf

    results = {}
    with closing(_get_conn()) as conn:
        ensure_table(conn)
        now_ts = datetime.now(timezone.utc).isoformat()
        for tk in tickers:
            try:
                info = yf.Ticker(tk).info
                beta = float(info.get("beta", 1.0) or 1.0)
                if beta <= 0 or beta > 10:
                    logger.warning("beta_cache: %s returned suspicious beta=%.2f, clamping to 1.0", tk, beta)
                    beta = 1.0
                conn.execute(
                    "INSERT INTO beta_cache (ticker, beta, fetched_ts) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(ticker) DO UPDATE SET beta = excluded.beta, fetched_ts = excluded.fetched_ts",
                    (tk, beta, now_ts),
                )
                results[tk] = beta
            except Exception as exc:
                logger.warning("beta_cache: refresh failed for %s: %s", tk, exc)
                results[tk] = get_beta(tk, conn)  # keep existing cached value
        conn.commit()
    logger.info("beta_cache: refreshed %d/%d tickers", len(results), len(tickers))
    return results
