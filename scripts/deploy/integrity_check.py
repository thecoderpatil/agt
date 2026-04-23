"""Sprint 5 MR C hotfix: standalone integrity_check probe.

Invoked by scripts/deploy/deploy.ps1 post-service-start. Exits 0 if
`PRAGMA integrity_check` returns 'ok' against the supplied DB path; exits
2 (or the sqlite error code) otherwise. Prints the integrity result so
the deploy log captures it.

Separated from deploy.ps1 into a .py file because PowerShell's inline
Python one-liner parser choked on the combination of single-quoted raw
string + backticks + sys.exit inside the quoted invocation. File-based
invocation sidesteps the PS parser entirely.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <db_path>", file=sys.stderr)
        return 2
    db_path = argv[1]
    if not Path(db_path).exists():
        print("db_missing")
        return 2
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        print(f"operational_error: {exc}")
        return 3
    except sqlite3.DatabaseError as exc:
        print(f"database_error: {exc}")
        return 4
    if row is None:
        print("no_row")
        return 2
    result = str(row[0])
    print(result)
    return 0 if result == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
