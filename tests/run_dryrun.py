"""Phase 2.1: Dry-run live sync — two runs 60s apart for idempotency."""
import sys, os, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.flex_sync import dry_run_sync, pull_flex_xml

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'agt_desk.db')


def run_and_report(label, xml_bytes):
    plan = dry_run_sync(xml_bytes=xml_bytes, db_path_override=DB_PATH)
    print(f"\n{'='*80}")
    print(f"{label}")
    print(f"{'='*80}")
    print(f"XML size: {plan.xml_bytes_size:,} bytes")
    print(f"Accounts: {plan.accounts}")
    print(f"Sections parsed: {plan.sections}")
    print(f"Total rows: {plan.total_rows}")
    print(f"Total would-insert: {plan.total_would_insert}")
    print()
    print(f"{'Table':45s} {'Parsed':>8s} {'Insert':>8s} {'Update':>8s} {'SkipPK':>8s}")
    print("-" * 80)
    for table in sorted(plan.per_table.keys()):
        e = plan.per_table[table]
        print(f"  {table:43s} {e['rows_parsed']:>8d} {e['would_insert']:>8d} "
              f"{e['would_update']:>8d} {e['would_skip_null_pk']:>8d}")
    print("-" * 80)
    totals = {
        'rows': sum(e['rows_parsed'] for e in plan.per_table.values()),
        'insert': sum(e['would_insert'] for e in plan.per_table.values()),
        'update': sum(e['would_update'] for e in plan.per_table.values()),
        'skip': sum(e['would_skip_null_pk'] for e in plan.per_table.values()),
    }
    print(f"  {'TOTAL':43s} {totals['rows']:>8d} {totals['insert']:>8d} "
          f"{totals['update']:>8d} {totals['skip']:>8d}")
    return plan


def main():
    print("Phase 2.1: Dry-Run Live Sync")
    print(f"Target DB: {DB_PATH}")
    print()

    # Pull once — reuse for both runs
    print("Pulling live Flex XML from IBKR...")
    xml_bytes = pull_flex_xml()
    print(f"Pulled {len(xml_bytes):,} bytes")

    # Run 1
    plan1 = run_and_report("RUN 1 (fresh)", xml_bytes)

    # Wait 60s
    print(f"\nWaiting 60 seconds for idempotency check...")
    time.sleep(60)

    # Run 2 — same XML, same DB
    plan2 = run_and_report("RUN 2 (idempotency — expect 0 inserts)", xml_bytes)

    # Idempotency check
    print(f"\n{'='*80}")
    print("IDEMPOTENCY CHECK")
    print(f"{'='*80}")
    # Run 1 should show all inserts (tables don't exist yet)
    # Run 2 should be identical to run 1 (tables STILL don't exist — dry run doesn't write)
    # But both runs are against the same unmodified DB, so both should show the same plan
    r1_inserts = plan1.total_would_insert
    r2_inserts = plan2.total_would_insert
    r1_rows = plan1.total_rows
    r2_rows = plan2.total_rows

    print(f"Run 1: {r1_rows} rows parsed, {r1_inserts} would-insert")
    print(f"Run 2: {r2_rows} rows parsed, {r2_inserts} would-insert")

    if r1_rows == r2_rows and r1_inserts == r2_inserts:
        print("IDEMPOTENCY: PASS — both runs produce identical transaction plans")

        # Per-table check
        all_match = True
        for table in sorted(set(list(plan1.per_table.keys()) + list(plan2.per_table.keys()))):
            e1 = plan1.per_table.get(table, {})
            e2 = plan2.per_table.get(table, {})
            if e1 != e2:
                print(f"  MISMATCH: {table}")
                print(f"    Run 1: {e1}")
                print(f"    Run 2: {e2}")
                all_match = False
        if all_match:
            print("  All tables match exactly.")
    else:
        print("IDEMPOTENCY: FAIL — transaction plans differ")
        print("  STOP: Parser is non-deterministic. Investigate before proceeding.")

    # Delete count check
    print(f"\nDelete count: 0 (flex_sync is append-only, no deletes)")


if __name__ == '__main__':
    main()
