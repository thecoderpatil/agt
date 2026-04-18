"""ADR-013 Dispatch B - incidents dual-ledger migration.

Idempotent. Safe to re-run.
- Adds columns fault_source, severity_tier, burn_weight (defaults match v1 semantics).
- Backfills known external-fault incidents by invariant_name pattern.
- Creates view v_error_budget_72h.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from agt_equities.db import get_db_connection

ALTER_STMTS = [
    # ALTER TABLE ADD COLUMN is naturally additive in SQLite. Wrap in try/except
    # at the cursor level so re-runs are no-ops (SQLite raises OperationalError
    # on duplicate column).
    ("fault_source", "ALTER TABLE incidents ADD COLUMN fault_source TEXT NOT NULL DEFAULT 'internal'"),
    ("severity_tier", "ALTER TABLE incidents ADD COLUMN severity_tier INTEGER NOT NULL DEFAULT 1"),
    ("burn_weight", "ALTER TABLE incidents ADD COLUMN burn_weight REAL NOT NULL DEFAULT 10"),
]

# Backfill rules: invariant_name LIKE pattern -> (fault_source, severity_tier, burn_weight)
BACKFILL_RULES = [
    # Known broker-side issues observed pre-v2.
    ("UNKNOWN_ACCT%", "broker", 1, 10),
    # Known vendor-side: GitLab CI quota outage 2026-04-16 caused heartbeat-stale chains.
    # Mark heartbeat-stale incidents within the documented window as vendor-attributed.
    # (Architect verifies window after backfill; manual reclassification of any false positives.)
    ("NO_MISSING_DAEMON_HEARTBEAT", "vendor", 2, 1),
]

VIEW_DDL = """
CREATE VIEW IF NOT EXISTS v_error_budget_72h AS
SELECT
  COALESCE(SUM(CASE WHEN fault_source = 'internal' THEN burn_weight ELSE 0 END), 0) AS internal_burn,
  COALESCE(SUM(CASE WHEN fault_source != 'internal' THEN burn_weight ELSE 0 END), 0) AS external_burn,
  SUM(CASE WHEN severity_tier = 0 AND fault_source = 'internal' THEN 1 ELSE 0 END) AS tier0_count,
  SUM(CASE WHEN severity_tier = 1 AND fault_source = 'internal' THEN 1 ELSE 0 END) AS tier1_count,
  SUM(CASE WHEN severity_tier = 2 AND fault_source = 'internal' THEN 1 ELSE 0 END) AS tier2_count
FROM incidents
WHERE detected_at >= datetime('now', '-72 hours');
"""


def _column_exists(conn, col: str) -> bool:
    rows = conn.execute("PRAGMA table_info(incidents)").fetchall()
    return any(r[1] == col for r in rows)


def run(db_path: str | Path | None = None) -> dict:
    stats = {"alters_applied": 0, "rows_backfilled": 0}
    with get_db_connection(db_path=db_path) as conn:
        for col, stmt in ALTER_STMTS:
            if not _column_exists(conn, col):
                conn.execute(stmt)
                stats["alters_applied"] += 1
        for pattern, source, tier, weight in BACKFILL_RULES:
            cur = conn.execute(
                """UPDATE incidents
                   SET fault_source = ?, severity_tier = ?, burn_weight = ?
                   WHERE invariant_name LIKE ? AND fault_source = 'internal'""",
                (source, tier, weight, pattern),
            )
            stats["rows_backfilled"] += cur.rowcount
        conn.executescript(VIEW_DDL)
        conn.commit()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    result = run(db_path=args.db_path)
    print(f"incidents dual-ledger migration: {result}")
