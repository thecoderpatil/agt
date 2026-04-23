"""llm_cost_ledger writes + daily-cost reads.

Per ADR-CSP_TELEGRAM_DIGEST_v1 §"llm_cost_ledger schema". Schema lives
in scripts/migrate_llm_cost_ledger.py (idempotent CREATE IF NOT EXISTS).
This module is the typed write/read API the digest's llm_commentary
calls into.

The trailing-24h `daily_cost_usd` powers the $5/day tripwire pre-send
check (safeguard #7 of 8 from the ADR).
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

CallStatus = Literal["ok", "timeout", "error", "budget_exceeded"]


def record_llm_call(
    db_path: str | Path,
    *,
    timestamp_utc: datetime,
    run_id: str,
    call_site: str,
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    status: CallStatus,
    error_class: str | None = None,
) -> None:
    """Append one row to llm_cost_ledger. Fail-soft on DB error.

    A ledger write failure should never block the digest path — if we
    can't write the audit row, we still want the digest to ship.
    """
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            conn.execute(
                "INSERT INTO llm_cost_ledger ("
                " timestamp_utc, run_id, call_site, model, "
                " input_tokens, cached_input_tokens, output_tokens, "
                " cost_usd, status, error_class"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    timestamp_utc.isoformat(), run_id, call_site, model,
                    int(input_tokens), int(cached_input_tokens),
                    int(output_tokens), float(cost_usd), status, error_class,
                ),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        logger.warning("llm_cost_ledger.record_err err=%s", exc)


def daily_cost_usd(
    db_path: str | Path,
    *,
    call_site: str | None = None,
    now_utc: datetime | None = None,
) -> float:
    """Sum cost_usd over the trailing 24h. Optionally filtered by call_site.

    Returns 0.0 on any DB error (fail-open: a missing table should not
    permanently lock out the LLM path; the operator will see budget
    rows missing and investigate).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = (now_utc - timedelta(hours=24)).isoformat()
    sql = (
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_cost_ledger "
        "WHERE timestamp_utc >= ?"
    )
    params: list = [cutoff]
    if call_site is not None:
        sql += " AND call_site = ?"
        params.append(call_site)
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError as exc:
        logger.warning("llm_cost_ledger.daily_cost_err err=%s", exc)
        return 0.0
    return float(row[0]) if row else 0.0
