"""
scripts/migrate_operator_interventions_kind_check.py

Adds DB-level CHECK(kind IN (...)) to operator_interventions.kind via
SQLite table-recreate pattern. Application-level VALID_KINDS frozenset
already enforces this -- migration makes the invariant durable in the schema.

Flags:
  --dry-run  print planned DDL + row count, no writes
  --apply    run VACUUM INTO backup, pre-check, recreate, post-check, log snapshot

Idempotent: detects existing CHECK constraint via sqlite_master DDL scan,
exits 0 with SKIP message on re-run.

Mirrors migrate_phase_b_foundation.py discipline per MR !268.
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from agt_equities.db import get_db_connection, tx_immediate

VALID_KINDS = (
    "reject", "reject_rem", "approve", "recover_transmitting",
    "flex_manual_reconcile", "halt", "direct_sql", "manual_terminal",
    "restart_during_market",
)

NEW_DDL = """CREATE TABLE operator_interventions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at_utc  TEXT NOT NULL,
    operator_user_id TEXT,
    kind             TEXT NOT NULL CHECK(kind IN ({placeholders})),
    target_table     TEXT,
    target_id        INTEGER,
    before_state     TEXT,
    after_state      TEXT,
    reason           TEXT,
    notes            TEXT
)""".format(placeholders=",".join(f"'{k}'" for k in VALID_KINDS))


def _already_has_check(conn) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operator_interventions'"
    ).fetchone()
    return bool(row and row[0] and "CHECK" in row[0])


def _integrity_check(conn) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _vacuum_backup(db_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / "backups" / f"agt_desk_{ts}_pre_kind_check.db"
    backup_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"VACUUM INTO '{backup_path}'")
    conn.close()
    return backup_path


def run(db_path=None, dry_run=False):
    from agt_equities.db import get_db_path
    resolved = Path(str(db_path)) if db_path else get_db_path()

    with closing(get_db_connection(resolved)) as conn:
        if _already_has_check(conn):
            print("SKIP: CHECK constraint already present in operator_interventions")
            return

        row_count = conn.execute("SELECT COUNT(*) FROM operator_interventions").fetchone()[0]
        print(f"operator_interventions: {row_count} rows to migrate")
        print("Planned DDL:")
        print(NEW_DDL)

        if dry_run:
            print(f"DRY-RUN: {row_count} rows would be migrated. No writes performed.")
            return

    # --- APPLY ---
    print("Step 1: VACUUM INTO backup...")
    backup = _vacuum_backup(resolved)
    print(f"  backup: {backup}")

    with closing(get_db_connection(resolved)) as conn:
        print("Step 2: Pre-migration integrity_check...")
        pre = _integrity_check(conn)
        print(f"  result: {pre}")
        if pre != "ok":
            print(f"ABORT: integrity_check pre-migration = {pre!r}", file=sys.stderr)
            sys.exit(1)

        print("Step 3: Applying table-recreate migration...")
        with tx_immediate(conn):
            conn.execute(NEW_DDL.replace("operator_interventions", "operator_interventions_new"))
            conn.execute(
                "INSERT INTO operator_interventions_new "
                "SELECT id,occurred_at_utc,operator_user_id,kind,target_table,"
                "target_id,before_state,after_state,reason,notes "
                "FROM operator_interventions"
            )
            conn.execute("DROP TABLE operator_interventions")
            conn.execute("ALTER TABLE operator_interventions_new RENAME TO operator_interventions")
            conn.execute("CREATE INDEX idx_oi_occurred ON operator_interventions(occurred_at_utc)")
            conn.execute("CREATE INDEX idx_oi_kind_occurred ON operator_interventions(kind, occurred_at_utc)")
            conn.execute("CREATE INDEX idx_oi_target ON operator_interventions(target_table, target_id)")
        print(f"  migrated {row_count} rows")

        print("Step 4: Post-migration integrity_check...")
        post = _integrity_check(conn)
        print(f"  result: {post}")
        if post != "ok":
            print(f"ABORT: integrity_check post-migration = {post!r}", file=sys.stderr)
            sys.exit(1)

        ddl_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_interventions'"
        ).fetchone()[0]
        table_info = conn.execute("PRAGMA table_info(operator_interventions)").fetchall()

    snapshot = {
        "migrated_at_utc": datetime.now(timezone.utc).isoformat(),
        "row_count": row_count,
        "backup": str(backup),
        "integrity_pre": pre,
        "integrity_post": post,
        "ddl_after": ddl_after,
        "table_info": [list(r) for r in table_info],
    }
    snap_path = Path("reports") / "mr_sprint13_post_migration_table_info.json"
    snap_path.parent.mkdir(exist_ok=True)
    snap_path.write_text(json.dumps(snapshot, indent=2))
    print(f"Snapshot written: {snap_path}")
    print("DONE: CHECK constraint added to operator_interventions")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    if not args.dry_run and not args.apply:
        ap.error("specify --dry-run or --apply")
    run(db_path=args.db_path, dry_run=args.dry_run)
