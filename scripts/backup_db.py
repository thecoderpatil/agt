"""
scripts/backup_db.py -- WAL-safe SQLite online backup via stdlib sqlite3.

Usage: python backup_db.py <src.db> <dst.db>

Uses sqlite3.Connection.backup(), which is the canonical Python API for
SQLite online backup. Safe against concurrent readers/writers in WAL mode.
No external binary dependency.
"""
import os
import sqlite3
import sys


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <src.db> <dst.db>", file=sys.stderr)
        sys.exit(1)

    src, dst = sys.argv[1], sys.argv[2]

    if not os.path.exists(src):
        print(f"ERROR: source DB not found: {src}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()

    size_mb = os.path.getsize(dst) / (1024 * 1024)
    print(f"Backup complete: {dst} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
