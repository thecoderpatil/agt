"""Phase B Foundation migration -- additive schema for ADR-020 contract persistence,
operator intervention ledger, and daemon heartbeat samples. Idempotent.

Run order: backup via SQLite backup API -> integrity_check -> apply DDL ->
integrity_check -> table_info(pending_orders) snapshot.

Usage:
  python scripts/migrate_phase_b_foundation.py --dry-run
  python scripts/migrate_phase_b_foundation.py --apply
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
from pathlib import Path

NEW_COLUMNS: list[tuple[str, str]] = [
    ("engine", "TEXT"),
    ("run_id", "TEXT"),
    ("broker_mode_at_staging", "TEXT"),
    ("staged_at_utc", "TEXT"),
    ("spot_at_staging", "REAL"),
    ("premium_at_staging", "REAL"),
    ("submitted_at_utc", "TEXT"),
    ("spot_at_submission", "REAL"),
    ("limit_price_at_submission", "REAL"),
    ("acked_at_utc", "TEXT"),
    ("gate_verdicts", "TEXT"),
]

PENDING_ORDER_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_po_engine_created ON pending_orders (engine, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_po_run_id ON pending_orders (run_id)",
]

CREATE_OPERATOR_INTERVENTIONS = """
CREATE TABLE IF NOT EXISTS operator_interventions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at_utc  TEXT NOT NULL,
    operator_user_id TEXT,
    kind             TEXT NOT NULL,
    target_table     TEXT,
    target_id        INTEGER,
    before_state     TEXT,
    after_state      TEXT,
    reason           TEXT,
    notes            TEXT
)"""

OPERATOR_INTERVENTIONS_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_oi_occurred ON operator_interventions (occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_oi_kind_occurred ON operator_interventions (kind, occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_oi_target ON operator_interventions (target_table, target_id)",
]

CREATE_DAEMON_HEARTBEAT_SAMPLES = """
CREATE TABLE IF NOT EXISTS daemon_heartbeat_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    daemon_name   TEXT NOT NULL,
    beat_utc      TEXT NOT NULL,
    pid           INTEGER NOT NULL,
    client_id     INTEGER,
    notes         TEXT
)"""

DAEMON_HEARTBEAT_SAMPLES_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_dhs_daemon_beat ON daemon_heartbeat_samples (daemon_name, beat_utc)",
]


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_info(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [
        {"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]}
        for r in conn.execute(f"PRAGMA table_info({table})")
    ]


def _integrity(conn: sqlite3.Connection) -> str:
    return "; ".join(str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall())


def _backup(src_path: Path, dst_path: Path) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def planned_alters(conn: sqlite3.Connection) -> list[str]:
    have = _cols(conn, "pending_orders")
    return [
        f"ALTER TABLE pending_orders ADD COLUMN {n} {t}"
        for n, t in NEW_COLUMNS
        if n not in have
    ]


def planned_creates(conn: sqlite3.Connection) -> list[str]:
    have = _tables(conn)
    out: list[str] = []
    if "operator_interventions" not in have:
        out.append(CREATE_OPERATOR_INTERVENTIONS.strip())
    if "daemon_heartbeat_samples" not in have:
        out.append(CREATE_DAEMON_HEARTBEAT_SAMPLES.strip())
    return out


def planned_indexes() -> list[str]:
    return [*PENDING_ORDER_INDEXES, *OPERATOR_INTERVENTIONS_INDEXES, *DAEMON_HEARTBEAT_SAMPLES_INDEXES]


def apply_all(conn: sqlite3.Connection) -> dict:
    applied: dict[str, list[str]] = {"alters": [], "creates": [], "indexes": []}
    for stmt in planned_alters(conn):
        conn.execute(stmt)
        applied["alters"].append(stmt)
    for stmt in planned_creates(conn):
        conn.execute(stmt)
        applied["creates"].append(stmt.split("(")[0].strip())
    for stmt in planned_indexes():
        conn.execute(stmt)
        applied["indexes"].append(stmt.split(" ON ")[0].split()[-1])
    conn.commit()
    return applied


def resolve_db_path() -> Path:
    if env := os.environ.get("AGT_DB_PATH"):
        return Path(env)
    for default in (Path("C:/AGT_Runtime/state/agt_desk.db"), Path("C:/AGT_Telegram_Bridge/agt_desk.db")):
        if default.exists():
            return default
    raise FileNotFoundError("Could not locate prod DB. Set AGT_DB_PATH.")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--db-path", type=str, default=None)
    p.add_argument("--backup-dir", type=str, default="C:/AGT_Runtime/state/backups")
    args = p.parse_args()
    if not args.dry_run and not args.apply:
        p.error("must specify --dry-run or --apply")

    db_path = Path(args.db_path) if args.db_path else resolve_db_path()
    log: dict = {
        "db_path": str(db_path),
        "started_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
    }
    conn = sqlite3.connect(str(db_path))
    try:
        log["table_info_pending_orders_before"] = _table_info(conn, "pending_orders")
        log["existing_tables_before"] = sorted(_tables(conn))
        if args.dry_run:
            log["planned_alters"] = planned_alters(conn)
            log["planned_creates"] = planned_creates(conn)
            log["planned_indexes"] = planned_indexes()
            print(json.dumps(log, indent=2, default=str))
            return 0
        log["integrity_check_before"] = _integrity(conn)
        if log["integrity_check_before"] != "ok":
            print(json.dumps({**log, "error": "integrity_check failed before migration"}, indent=2))
            return 1
        conn.close()
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dst = Path(args.backup_dir) / f"agt_desk_pre_phase_b_{ts}.db"
        _backup(db_path, backup_dst)
        log["backup_path"] = str(backup_dst)
        conn = sqlite3.connect(str(db_path))
        log["applied"] = apply_all(conn)
        log["integrity_check_after"] = _integrity(conn)
        if log["integrity_check_after"] != "ok":
            print(json.dumps({**log, "error": "integrity_check failed after migration"}, indent=2))
            return 1
        log["table_info_pending_orders_after"] = _table_info(conn, "pending_orders")
        log["existing_tables_after"] = sorted(_tables(conn))
        log["completed_at_utc"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    finally:
        conn.close()
    print(json.dumps(log, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
