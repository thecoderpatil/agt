"""
Litestream Restore Drill — verify backup integrity.

Usage: python restore_drill.py [--from-r2]

Without --from-r2: verifies the local .phase1_baseline backup.
With --from-r2: downloads from R2 and verifies (requires litestream CLI).
"""
import os
import sqlite3
import sys
import tempfile
import subprocess
from pathlib import Path

LIVE_DB = Path(__file__).parent / "agt_desk.db"
BASELINE_BACKUP = Path(__file__).parent / "agt_desk.db.phase1_baseline_20260407"
LITESTREAM_CONFIG = Path(__file__).parent / "litestream.yml"

KEY_TABLES = [
    "pending_orders",
    "premium_ledger",
    "ticker_universe",
    "master_log_trades",
    "master_log_sync",
    "master_log_open_positions",
]


def verify_db(db_path: Path, label: str) -> dict:
    """Count rows in key tables and return summary."""
    conn = sqlite3.connect(str(db_path))
    result = {}
    for table in KEY_TABLES:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            result[table] = count
        except Exception:
            result[table] = -1  # table missing
    conn.close()
    return result


def compare(live: dict, backup: dict) -> bool:
    """Compare live and backup row counts. Returns True if matched."""
    all_ok = True
    print(f"\n{'Table':40s} {'Live':>8s} {'Backup':>8s} {'Status':>10s}")
    print("-" * 70)
    for table in KEY_TABLES:
        l = live.get(table, -1)
        b = backup.get(table, -1)
        if l == b:
            status = "MATCH"
        elif b == -1:
            status = "MISSING"
            all_ok = False
        else:
            status = f"DELTA {l - b:+d}"
            # Small deltas OK for live DB that's being written to
            if abs(l - b) > 100:
                all_ok = False

        print(f"  {table:38s} {l:>8d} {b:>8d} {status:>10s}")
    return all_ok


def main():
    from_r2 = "--from-r2" in sys.argv

    print("=" * 70)
    print("LITESTREAM RESTORE DRILL")
    print("=" * 70)

    # Live DB
    if not LIVE_DB.exists():
        print(f"ERROR: Live DB not found: {LIVE_DB}")
        return 1

    live_counts = verify_db(LIVE_DB, "Live")
    print(f"\nLive DB: {LIVE_DB} ({LIVE_DB.stat().st_size:,} bytes)")

    if from_r2:
        # Restore from R2
        temp_db = Path(tempfile.gettempdir()) / "agt_desk_restored.db"
        print(f"\nRestoring from R2 to {temp_db}...")
        try:
            result = subprocess.run(
                ["litestream", "restore", "-config", str(LITESTREAM_CONFIG),
                 "-o", str(temp_db), str(LIVE_DB)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                print(f"ERROR: litestream restore failed:\n{result.stderr}")
                return 1
        except FileNotFoundError:
            print("ERROR: litestream binary not found. Run install_litestream.bat first.")
            return 1

        backup_counts = verify_db(temp_db, "R2 Restore")
        print(f"R2 Restore: {temp_db} ({temp_db.stat().st_size:,} bytes)")
        ok = compare(live_counts, backup_counts)
        temp_db.unlink()

    else:
        # Verify local baseline backup
        if not BASELINE_BACKUP.exists():
            print(f"WARNING: Baseline backup not found: {BASELINE_BACKUP}")
            print("Run with --from-r2 after configuring Litestream, or create a backup first.")
            return 1

        backup_counts = verify_db(BASELINE_BACKUP, "Baseline Backup")
        print(f"Baseline: {BASELINE_BACKUP} ({BASELINE_BACKUP.stat().st_size:,} bytes)")
        ok = compare(live_counts, backup_counts)

    print(f"\n{'=' * 70}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    print(f"{'=' * 70}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
