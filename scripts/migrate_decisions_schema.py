"""Idempotent migration: create `decisions` table + indexes for ADR-012.

Invocation: `python scripts/migrate_decisions_schema.py --db-path <path>`.
Safe to re-run; all DDL uses IF NOT EXISTS.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection

DDL = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id           TEXT PRIMARY KEY,
    engine                TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    decision_timestamp    TIMESTAMP NOT NULL,
    raw_input_hash        TEXT NOT NULL,
    llm_reasoning_text    TEXT,
    llm_confidence_score  REAL,
    llm_rank              INTEGER,
    operator_action       TEXT NOT NULL,
    action_timestamp      TIMESTAMP NOT NULL,
    strike                REAL,
    expiry                DATE,
    contracts             INTEGER,
    premium_collected     REAL,
    realized_pnl          REAL,
    realized_pnl_timestamp TIMESTAMP,
    counterfactual_pnl    REAL,
    counterfactual_basis  TEXT,
    market_state_embedding BLOB,
    operator_credibility_at_decision REAL,
    prompt_version        TEXT NOT NULL,
    notes                 TEXT
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_decisions_engine_ts ON decisions(engine, decision_timestamp DESC);",
    "CREATE INDEX IF NOT EXISTS idx_decisions_ticker_ts ON decisions(ticker, decision_timestamp DESC);",
    "CREATE INDEX IF NOT EXISTS idx_decisions_pending_pnl ON decisions(realized_pnl) WHERE realized_pnl IS NULL;",
]


def run(db_path: str | Path | None = None) -> None:
    with closing(get_db_connection(db_path=db_path)) as conn:
        conn.executescript(DDL)
        for stmt in INDEXES:
            conn.execute(stmt)
        conn.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    run(db_path=args.db_path)
    print("decisions schema migration complete")
