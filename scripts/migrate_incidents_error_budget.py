"""Sprint 6 Mega-MR 4B — incidents error-budget columns migration.

Adds two columns to `incidents` table:

  - `error_budget_tier INTEGER NOT NULL DEFAULT 2`
      ADR-013 dual-ledger canonical tier:
        0 = live-capital (critical impact, burn 100x weight)
        1 = operational   (high/medium impact, burn 10x)
        2 = observability (low/warn impact, burn 1x)
  - `budget_consumed_pct REAL` (nullable)
      Rolling 72h-window budget usage percentage at detection time.
      Nullable because most callers won't compute it synchronously.

Backfills existing rows from the string `severity` column per the
dispatch-specified mapping (extended to cover the actual enum in use
on this DB: critical/high/medium/warn).

NOTE: `severity_tier` already exists on this DB (legacy semantic with
values 1 and 2). This migration does NOT touch it — both columns
coexist. `severity_tier` is considered legacy going forward;
`error_budget_tier` is the ADR-013 canonical.

Usage:
    python scripts/migrate_incidents_error_budget.py --db-path <path>
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from agt_equities.db import get_db_connection

# Dispatch mapping (extended to real severity enum on this DB):
#   critical -> 0 (live-capital)
#   high     -> 1 (operational)
#   medium   -> 2 (observability)
#   warn     -> 2 (observability)
BACKFILL_MAP: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "warn": 2,
}

DEFAULT_ERROR_BUDGET_TIER = 2


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def run(db_path: str | Path | None = None) -> dict[str, int]:
    """Add columns if missing + backfill. Returns counts per action."""
    stats = {
        "added_error_budget_tier": 0,
        "added_budget_consumed_pct": 0,
        "backfilled_rows": 0,
    }
    with closing(get_db_connection(db_path=db_path)) as conn:
        existing = _columns(conn, "incidents")
        if "error_budget_tier" not in existing:
            conn.execute(
                "ALTER TABLE incidents ADD COLUMN error_budget_tier "
                f"INTEGER NOT NULL DEFAULT {DEFAULT_ERROR_BUDGET_TIER}"
            )
            stats["added_error_budget_tier"] = 1
        if "budget_consumed_pct" not in existing:
            conn.execute(
                "ALTER TABLE incidents ADD COLUMN budget_consumed_pct REAL"
            )
            stats["added_budget_consumed_pct"] = 1

        # Backfill: rows whose error_budget_tier is at the schema default
        # (2) but whose string severity implies a lower tier.
        total = 0
        for severity, tier in BACKFILL_MAP.items():
            if tier == DEFAULT_ERROR_BUDGET_TIER:
                continue  # default already correct; don't churn
            cur = conn.execute(
                "UPDATE incidents SET error_budget_tier = ? "
                "WHERE severity = ? AND error_budget_tier = ?",
                (tier, severity, DEFAULT_ERROR_BUDGET_TIER),
            )
            total += cur.rowcount
        stats["backfilled_rows"] = total
        conn.commit()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    stats = run(db_path=args.db_path)
    print(f"incidents error-budget migration: {stats}")
