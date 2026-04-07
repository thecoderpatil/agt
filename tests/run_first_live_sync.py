"""Phase 2.2: First live write to agt_desk.db.

Creates master_log_* tables and syncs live Flex data.
Entire operation wrapped in a single transaction with rollback on error.
Writes ONLY to master_log_* tables — no legacy tables touched.
"""
import sqlite3
import sys
import os
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import (
    pull_flex_xml, parse_flex_xml, _upsert_rows, FLEX_QUERY_ID,
)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'agt_desk.db')


def main():
    print("Phase 2.2: First Live Sync")
    print(f"Target: {os.path.abspath(DB_PATH)}")
    print()

    # Step 1: Create tables (idempotent)
    print("Step 1: Creating master_log_* tables...")
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    register_master_log_tables(conn)
    conn.commit()

    # Verify tables created
    ml_tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'master_log%' "
        "ORDER BY name"
    ).fetchall()
    print(f"  Created {len(ml_tables)} master_log tables:")
    for t in ml_tables:
        print(f"    {t['name']}")

    # Also create inception_carryin, bot_order_log, cc_decision_log
    b3_tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('inception_carryin', 'bot_order_log', 'cc_decision_log') "
        "ORDER BY name"
    ).fetchall()
    print(f"  Bucket 3 tables: {[t['name'] for t in b3_tables]}")

    # Verify no legacy tables were modified
    legacy_check = ['premium_ledger', 'trade_ledger', 'fill_log']
    for lt in legacy_check:
        count = conn.execute(f"SELECT COUNT(*) FROM {lt}").fetchone()[0]
        print(f"  Legacy {lt}: {count} rows (unchanged)")

    # Step 2: Pull live Flex XML
    print("\nStep 2: Pulling live Flex XML from IBKR...")
    try:
        xml_bytes = pull_flex_xml()
    except Exception as exc:
        print(f"  FAILED: {exc}")
        conn.close()
        return 1
    print(f"  Pulled {len(xml_bytes):,} bytes")

    # Step 3: Parse
    print("\nStep 3: Parsing...")
    section_data = parse_flex_xml(xml_bytes)
    print(f"  {len(section_data)} sections parsed")

    # Step 4: Write within single transaction
    print("\nStep 4: Writing to master_log_* tables (single transaction)...")
    now = datetime.now(timezone.utc).isoformat()

    try:
        # Write sync audit row
        cursor = conn.execute(
            "INSERT INTO master_log_sync (started_at, flex_query_id, status) "
            "VALUES (?, ?, 'running')",
            (now, FLEX_QUERY_ID),
        )
        sync_id = cursor.lastrowid

        total_rows = 0
        total_inserted = 0
        per_table = {}

        for sd in section_data:
            table = sd['table']
            rows = sd['rows']
            ins, upd = _upsert_rows(conn, table, rows, sd['pk_cols'], now)
            total_rows += len(rows)
            total_inserted += ins
            per_table[table] = {'rows': len(rows), 'inserted': ins}
            print(f"    {table:45s}: {len(rows):>5d} rows, {ins:>5d} inserted")

        # Update sync audit
        conn.execute(
            "UPDATE master_log_sync SET finished_at=?, sections_processed=?, "
            "rows_received=?, rows_inserted=?, status='success' WHERE sync_id=?",
            (datetime.now(timezone.utc).isoformat(), len(section_data),
             total_rows, total_inserted, sync_id),
        )

        # COMMIT
        conn.commit()
        print(f"\n  COMMITTED: {total_inserted} rows inserted, sync_id={sync_id}")

    except Exception as exc:
        # ROLLBACK
        conn.rollback()
        print(f"\n  ROLLBACK: {exc}")
        traceback.print_exc()
        conn.close()
        return 1

    # Step 5: Post-write verification
    print("\nStep 5: Post-write verification...")
    for table in sorted(per_table.keys()):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        expected = per_table[table]['inserted']
        match = "OK" if count == expected else f"MISMATCH (expected {expected})"
        print(f"    {table:45s}: {count:>5d} rows [{match}]")

    # Verify sync audit
    sync = conn.execute(
        "SELECT * FROM master_log_sync WHERE sync_id=?", (sync_id,)
    ).fetchone()
    print(f"\n  master_log_sync: status={sync['status']}, "
          f"rows_inserted={sync['rows_inserted']}, "
          f"sections={sync['sections_processed']}")

    # Verify legacy tables UNCHANGED
    print("\n  Legacy table integrity check:")
    for lt in legacy_check:
        count = conn.execute(f"SELECT COUNT(*) FROM {lt}").fetchone()[0]
        print(f"    {lt}: {count} rows")

    # Spot-check: UBER open position
    uber = conn.execute(
        "SELECT position, cost_basis_price FROM master_log_open_positions "
        "WHERE symbol='UBER' AND account_id='U22076329'"
    ).fetchone()
    if uber:
        print(f"\n  UBER U22076329: pos={uber['position']} cbp={uber['cost_basis_price']}")

    conn.close()
    print("\nFirst live sync complete.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
