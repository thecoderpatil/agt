"""ADR-012 decisions-table repo. Three ingest points; all pass-through to SQLite."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_decision(
    *,
    decision_id: str,
    engine: str,
    ticker: str,
    raw_input_hash: str,
    operator_action: str,
    prompt_version: str,
    llm_reasoning_text: str | None = None,
    llm_confidence_score: float | None = None,
    llm_rank: int | None = None,
    strike: float | None = None,
    expiry: str | None = None,
    contracts: int | None = None,
    premium_collected: float | None = None,
    market_state_embedding: bytes | None = None,
    operator_credibility_at_decision: float | None = None,
    notes: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Insert a new decision row. Idempotent on decision_id."""
    now = _utcnow_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO decisions (
                decision_id, engine, ticker, decision_timestamp, raw_input_hash,
                llm_reasoning_text, llm_confidence_score, llm_rank,
                operator_action, action_timestamp,
                strike, expiry, contracts, premium_collected,
                market_state_embedding, operator_credibility_at_decision,
                prompt_version, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id, engine, ticker, now, raw_input_hash,
                llm_reasoning_text, llm_confidence_score, llm_rank,
                operator_action, now,
                strike, expiry, contracts, premium_collected,
                market_state_embedding, operator_credibility_at_decision,
                prompt_version, notes,
            ),
        )
        conn.commit()


def record_operator_action(
    *,
    decision_id: str,
    operator_action: str,
    db_path: str | Path | None = None,
) -> None:
    """Update operator_action + action_timestamp on an existing row."""
    now = _utcnow_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        conn.execute(
            "UPDATE decisions SET operator_action = ?, action_timestamp = ? WHERE decision_id = ?",
            (operator_action, now, decision_id),
        )
        conn.commit()


def settle_realized_pnl(
    *,
    decision_id: str,
    realized_pnl: float,
    db_path: str | Path | None = None,
) -> None:
    """Record the natural-close P&L on a decision."""
    now = _utcnow_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        conn.execute(
            "UPDATE decisions SET realized_pnl = ?, realized_pnl_timestamp = ? WHERE decision_id = ?",
            (realized_pnl, now, decision_id),
        )
        conn.commit()
