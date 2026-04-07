"""
agt_equities.parity — Sync-time invariant checks.

Verifies cross-section consistency in the master_log mirror.
See REFACTOR_SPEC_v3.md section 6 (parity invariant check).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def verify_option_eae_parity(
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """
    For every row in master_log_option_eae, verify a matching BookTrade
    exists in master_log_trades.

    Match criteria: same account_id, same conid, same trade_date
    (YYYYMMDD portion of date_time), and transaction_type='BookTrade'.

    Returns list of unmatched rows (empty = all matched = good).
    """
    close_conn = False
    if conn is None:
        conn = _get_db()
        close_conn = True

    try:
        unmatched = conn.execute("""
            SELECT eae.*
            FROM master_log_option_eae eae
            WHERE NOT EXISTS (
                SELECT 1 FROM master_log_trades t
                WHERE t.account_id = eae.account_id
                  AND t.conid = eae.conid
                  AND substr(t.date_time, 1, 8) = eae.date
                  AND t.transaction_type = 'BookTrade'
            )
        """).fetchall()

        result = [dict(r) for r in unmatched]

        if result:
            logger.warning(
                "OptionEAE parity check: %d unmatched rows", len(result))
            for row in result[:5]:
                logger.warning(
                    "  Unmatched: account=%s conid=%s date=%s type=%s symbol=%s",
                    row.get('account_id'), row.get('conid'),
                    row.get('date'), row.get('transaction_type'),
                    row.get('symbol'),
                )
        else:
            logger.info("OptionEAE parity check: all %d rows matched",
                        conn.execute("SELECT COUNT(*) FROM master_log_option_eae").fetchone()[0])

        return result

    finally:
        if close_conn:
            conn.close()
