"""One-shot migration: create csp_decisions table if not exists.

Safe to run repeatedly. Prod migration step runs this once post-merge.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Path shim so `python scripts/migrate_csp_decisions_table.py` works from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agt_equities.csp_decisions_repo import ensure_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Create csp_decisions table.")
    parser.add_argument("--db-path", type=str, default=None,
                        help="Override agt_desk.db path (default: prod).")
    args = parser.parse_args()
    ensure_schema(db_path=args.db_path)
    print("csp_decisions schema ensured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
