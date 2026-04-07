"""
jobs/eod_macro_sync.py — Daily IV30 snapshot for IV rank computation.

Standalone cron job. NOT inside flex_sync.py. NOT imported by telegram_bot.py.
Failure must NOT block flex_sync or any other job.

Schedule: daily at 5:00 PM AST (after market close, before midnight).
Configure via Windows Task Scheduler separately from flex_sync.

Reads ticker list from ticker_universe table (active Wheel names).
Fetches IV30 via IBKRPriceVolatilityProvider.
Inserts row per ticker per day into bucket3_macro_iv_history.
Idempotent (PRIMARY KEY conflict = update).
Retention: deletes rows > 400 days old at end of run.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(__file__).resolve().parent.parent / "logs" / "eod_macro_sync.log",
            mode="a",
        ),
    ],
)
logger = logging.getLogger("eod_macro_sync")

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"


def load_active_tickers(conn: sqlite3.Connection) -> list[str]:
    """Load tickers with active Walker cycles from master_log_open_positions."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM master_log_open_positions "
            "WHERE asset_category = 'STK' AND position > 0 "
            "ORDER BY symbol"
        ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.error("Failed to load active tickers: %s", exc)
        return []


def main():
    logger.info("EOD macro sync starting")

    # Ensure logs directory exists
    (Path(__file__).resolve().parent.parent / "logs").mkdir(exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")

    tickers = load_active_tickers(conn)
    if not tickers:
        logger.warning("No active tickers found. Exiting.")
        conn.close()
        return

    logger.info("Processing %d tickers", len(tickers))

    # Build provider (requires TWS connection)
    try:
        from ib_async import IB
        ib = IB()
        ib.connect(
            os.environ.get("IB_HOST", "127.0.0.1"),
            int(os.environ.get("IB_PORT", "4001")),
            clientId=int(os.environ.get("IB_EOD_CLIENT_ID", "90")),
        )
        ib.reqMarketDataType(3)  # delayed

        from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider
        provider = IBKRPriceVolatilityProvider(ib, market_data_mode="delayed")
    except Exception as exc:
        logger.error("Failed to connect to IBKR: %s", exc)
        conn.close()
        return

    success_count = 0
    failure_count = 0
    today = date.today().isoformat()

    for ticker in tickers:
        try:
            vol = provider.get_volatility_surface(ticker)
            if vol and vol.iv_30:
                conn.execute(
                    "INSERT INTO bucket3_macro_iv_history "
                    "(ticker, trade_date, iv_30, sample_source) "
                    "VALUES (?, ?, ?, 'eod_macro_sync') "
                    "ON CONFLICT(ticker, trade_date) DO UPDATE SET "
                    "iv_30 = excluded.iv_30, created_at = CURRENT_TIMESTAMP",
                    (ticker, today, vol.iv_30),
                )
                success_count += 1
                logger.info("  %s: IV30=%.4f", ticker, vol.iv_30)
            else:
                failure_count += 1
                logger.warning("  %s: no vol data returned", ticker)
        except Exception as exc:
            failure_count += 1
            logger.warning("  %s: failed: %s", ticker, exc)

    # Retention purge (keep 400 days)
    try:
        cutoff = (date.today() - timedelta(days=400)).isoformat()
        deleted = conn.execute(
            "DELETE FROM bucket3_macro_iv_history WHERE trade_date < ?",
            (cutoff,),
        ).rowcount
        if deleted:
            logger.info("Purged %d rows older than %s", deleted, cutoff)
    except Exception as exc:
        logger.error("Retention purge failed: %s", exc)

    # Disconnect
    try:
        ib.disconnect()
    except Exception:
        pass

    conn.close()
    logger.info(
        "EOD macro sync complete: %d success, %d failed out of %d tickers",
        success_count, failure_count, len(tickers),
    )


if __name__ == "__main__":
    main()
