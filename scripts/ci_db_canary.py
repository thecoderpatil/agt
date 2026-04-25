"""Phase A piece 4 — CI DB writability canary.

Proves the CI test DB (AGT_DB_PATH) is writable before pytest starts.
Writes a probe row to a dedicated canary table; rows accumulate (small,
auditable, append-only by design — no row removal in this script).

If the test DB is unwritable — misconfiguration, wrong path, ACL error —
CI fails fast here with a clear message rather than mid-test with a
confusing sqlite3 error.

Sentinel: CI DB writability canary.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_CANARY_TABLE = "_ci_canary"
_CANARY_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_CANARY_TABLE} "
    "(probe_at_utc TEXT NOT NULL, runner_id TEXT NOT NULL, job_id TEXT NOT NULL)"
)


def write_canary_probe() -> None:
    """Insert one probe row into the CI canary table, proving write access."""
    db_path = os.environ.get("AGT_DB_PATH")
    if not db_path:
        _fail("AGT_DB_PATH unset — cannot determine CI test DB path")
    p = Path(db_path)
    if not p.parent.exists():
        _fail(f"parent dir {p.parent} does not exist — CI test DB path misconfigured")
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute(_CANARY_DDL)
            conn.execute(
                f"INSERT INTO {_CANARY_TABLE} VALUES (?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    os.environ.get("CI_RUNNER_ID", "unknown"),
                    os.environ.get("CI_JOB_ID", "unknown"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, PermissionError) as exc:
        _fail(str(exc))


def _fail(msg: str) -> None:
    print(f"CANARY FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    write_canary_probe()
    return 0


if __name__ == "__main__":
    sys.exit(main())
