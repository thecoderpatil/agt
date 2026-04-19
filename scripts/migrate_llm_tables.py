"""Idempotent migration for ADR-010 §5.3–§5.5 tables.

Run standalone:
    python scripts/migrate_llm_tables.py
    python scripts/migrate_llm_tables.py --db-path /tmp/custom.db

cached_client.CachedAnthropicClient.__init__ calls _ensure_schema()
which imports and reuses the same DDL — this script is for operator
sanity (pre-seeding on a fresh DB) and CI smoke verification.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agt_equities.cached_client import _ensure_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="ADR-010 LLM tables migration")
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to sqlite DB (default: agt_equities.db.DB_PATH)",
    )
    args = parser.parse_args()
    _ensure_schema(args.db_path)
    target = args.db_path or "<module DB_PATH>"
    print(f"migrate_llm_tables: OK (target={target})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
