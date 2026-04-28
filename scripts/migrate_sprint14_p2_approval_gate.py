"""
scripts/migrate_sprint14_p2_approval_gate.py

Sprint 14 P2: combined schema migration.

  A. CREATE TABLE csp_ticker_approvals — per-ticker approval gate.
     Idempotent: skipped if table already exists.

  B. Extend operator_interventions.kind CHECK with 4 new CSP approval kinds:
       csp_ticker_approve, csp_ticker_reject,
       csp_timeout_auto_approve, csp_timeout_auto_reject
     Idempotent: skipped if new kinds already in CHECK DDL.

Flags:
  --dry-run  print planned DDL + row counts, no writes
  --apply    VACUUM INTO backup, integrity pre-check, migrate, post-check, snapshot

Mirrors migrate_operator_interventions_kind_check.py discipline.
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
    "csp_ticker_approve", "csp_ticker_reject",
    "csp_timeout_auto_approve", "csp_timeout_auto_reject",
)

CTA_TABLE_DDL = (
    "CREATE TABLE csp_ticker_approvals (\n"
    "    id              INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    run_id          TEXT NOT NULL,\n"
    "    ticker          TEXT NOT NULL,\n"
    "    status          TEXT NOT NULL DEFAULT 'pending'\n"
    "                    CHECK(status IN ('pending','approved','rejected',\n"
    "                                     'timeout_approved','timeout_rejected')),\n"
    "    created_at_utc  TEXT NOT NULL,\n"
    "    resolved_at_utc TEXT,\n"
    "    resolved_by     TEXT,\n"
    "    timeout_at_utc  TEXT NOT NULL\n"
    ")"
)

OI_NEW_DDL = (
    "CREATE TABLE operator_interventions (\n"
    "    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    occurred_at_utc  TEXT NOT NULL,\n"
    "    operator_user_id TEXT,\n"
    "    kind             TEXT NOT NULL CHECK(kind IN ({placeholders})),\n"
    "    target_table     TEXT,\n"
    "    target_id        INTEGER,\n"
    "    before_state     TEXT,\n"
    "    after_state      TEXT,\n"
    "    reason           TEXT,\n"
    "    notes            TEXT\n"
    ")"
).format(placeholders=",".join(f"'{k}'" for k in VALID_KINDS))


def _cta_table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='csp_ticker_approvals'"
    ).fetchone()
    return row is not None


def _oi_has_new_kinds(conn) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operator_interventions'"
    ).fetchone()
    return bool(row and row[0] and "csp_ticker_approve" in row[0])


def _integrity_check(conn) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _vacuum_backup(db_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / "backups" / f"agt_desk_{ts}_pre_p2_approval_gate.db"
    backup_path.parent.mkdir(exist_ok=True)
    raw = sqlite3.connect(str(db_path))
    raw.execute(f"VACUUM INTO '{backup_path}'")
    raw.close()
    return backup_path


def run(db_path=None, dry_run=False):
    from agt_equities.db import get_db_path
    resolved = Path(str(db_path)) if db_path else get_db_path()

    with closing(get_db_connection(resolved)) as conn:
        cta_exists = _cta_table_exists(conn)
        oi_done = _oi_has_new_kinds(conn)
        oi_row_count = conn.execute(
            "SELECT COUNT(*) FROM operator_interventions"
        ).fetchone()[0]

    print(f"csp_ticker_approvals exists: {cta_exists}")
    print(f"operator_interventions new kinds present: {oi_done}")
    print(f"operator_interventions rows: {oi_row_count}")

    if cta_exists and oi_done:
        print("SKIP: both migrations already applied.")
        return

    if dry_run:
        if not cta_exists:
            print("\n[A] Would CREATE TABLE csp_ticker_approvals:")
            print(CTA_TABLE_DDL)
        if not oi_done:
            print("\n[B] Would recreate operator_interventions with extended CHECK:")
            print(OI_NEW_DDL)
        print("\nDRY-RUN: no writes performed.")
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

        print("Step 3: Applying migrations...")
        with tx_immediate(conn):
            if not cta_exists:
                print("  [A] CREATE TABLE csp_ticker_approvals")
                conn.execute(CTA_TABLE_DDL)
                conn.execute(
                    "CREATE UNIQUE INDEX idx_cta_run_ticker "
                    "ON csp_ticker_approvals(run_id, ticker)"
                )
                conn.execute(
                    "CREATE INDEX idx_cta_status_timeout "
                    "ON csp_ticker_approvals(status, timeout_at_utc)"
                )

            if not oi_done:
                print(
                    f"  [B] Recreate operator_interventions "
                    f"with {len(VALID_KINDS)} kinds"
                )
                conn.execute(
                    OI_NEW_DDL.replace(
                        "operator_interventions",
                        "operator_interventions_new",
                    )
                )
                conn.execute(
                    "INSERT INTO operator_interventions_new "
                    "SELECT id,occurred_at_utc,operator_user_id,kind,target_table,"
                    "target_id,before_state,after_state,reason,notes "
                    "FROM operator_interventions"
                )
                conn.execute("DROP TABLE operator_interventions")
                conn.execute(
                    "ALTER TABLE operator_interventions_new "
                    "RENAME TO operator_interventions"
                )
                conn.execute(
                    "CREATE INDEX idx_oi_occurred "
                    "ON operator_interventions(occurred_at_utc)"
                )
                conn.execute(
                    "CREATE INDEX idx_oi_kind_occurred "
                    "ON operator_interventions(kind, occurred_at_utc)"
                )
                conn.execute(
                    "CREATE INDEX idx_oi_target "
                    "ON operator_interventions(target_table, target_id)"
                )

        print("Step 4: Post-migration integrity_check...")
        post = _integrity_check(conn)
        print(f"  result: {post}")
        if post != "ok":
            print(f"ABORT: integrity_check post-migration = {post!r}", file=sys.stderr)
            sys.exit(1)

        cta_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='csp_ticker_approvals'"
        ).fetchone()
        oi_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_interventions'"
        ).fetchone()

    snapshot = {
        "migrated_at_utc": datetime.now(timezone.utc).isoformat(),
        "backup": str(backup),
        "integrity_pre": pre,
        "integrity_post": post,
        "cta_table_created": not cta_exists,
        "oi_check_extended": not oi_done,
        "oi_rows_migrated": oi_row_count if not oi_done else 0,
        "cta_ddl": cta_ddl[0] if cta_ddl else None,
        "oi_ddl": oi_ddl[0] if oi_ddl else None,
    }
    snap_path = Path("reports") / "mr_sprint14_p2_post_migration_table_info.txt"
    snap_path.parent.mkdir(exist_ok=True)
    snap_path.write_text(json.dumps(snapshot, indent=2))
    print(f"Snapshot written: {snap_path}")
    print("DONE.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args()
    if not args.dry_run and not args.apply:
        ap.error("specify --dry-run or --apply")
    run(db_path=args.db_path, dry_run=args.dry_run)
