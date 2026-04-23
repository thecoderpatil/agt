"""Idempotent migration: create news_cache table + indexes.

Per ADR-CSP_NEWS_OVERLAY_v1. Called once from agt_scheduler startup
and from test fixtures. Pure CREATE IF NOT EXISTS — safe to re-run.

Usage:
    python scripts/migrate_news_cache.py [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_cache (
    cache_key       TEXT    PRIMARY KEY,
    source          TEXT    NOT NULL,
    ticker          TEXT,
    lookback_hours  INTEGER NOT NULL,
    items_json      TEXT    NOT NULL,
    fetched_at_utc  TEXT    NOT NULL,
    ttl_seconds     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_cache_ticker_src
    ON news_cache(ticker, source);

CREATE INDEX IF NOT EXISTS idx_news_cache_fetched
    ON news_cache(fetched_at_utc);
"""


def migrate(db_path: str) -> None:
    """Create news_cache table + indexes. Idempotent."""
    with closing(sqlite3.connect(db_path, timeout=10.0)) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="C:/AGT_Runtime/state/agt_desk.db",
        help="SQLite DB path",
    )
    args = parser.parse_args(argv)
    migrate(args.db)
    print(f"news_cache migrated against {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
