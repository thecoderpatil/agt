"""Tests for scripts/migrate_phase_b_foundation.py -- round-trip + idempotency.

sprint_a marker.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "migrate_phase_b_foundation.py"


def _seed_minimal_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE pending_orders (
                id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status_history TEXT
            );
            INSERT INTO pending_orders (payload, status, created_at, status_history)
                VALUES ('{}', 'staged', '2026-04-25T12:00:00Z', '[]');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_dry_run_emits_planned_ddl(tmp_path):
    db = tmp_path / "agt.db"
    _seed_minimal_db(db)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--db-path", str(db)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert any("ADD COLUMN engine" in s for s in payload["planned_alters"])
    assert any("operator_interventions" in s for s in payload["planned_creates"])
    assert any("daemon_heartbeat_samples" in s for s in payload["planned_creates"])


def test_apply_round_trip_idempotent(tmp_path):
    db = tmp_path / "agt.db"
    _seed_minimal_db(db)
    backup_dir = tmp_path / "backups"
    cmd = [
        sys.executable, str(SCRIPT), "--apply",
        "--db-path", str(db),
        "--backup-dir", str(backup_dir),
    ]
    r1 = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r1.returncode == 0, r1.stderr
    log1 = json.loads(r1.stdout)
    assert log1["integrity_check_before"] == "ok"
    assert log1["integrity_check_after"] == "ok"
    assert len(log1["applied"]["alters"]) == 11
    assert any("operator_interventions" in c for c in log1["applied"]["creates"])
    # Backup file present
    assert any(p.suffix == ".db" for p in backup_dir.iterdir())

    # Re-run is a no-op (idempotent)
    r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0, r2.stderr
    log2 = json.loads(r2.stdout)
    assert log2["applied"]["alters"] == []
    assert log2["applied"]["creates"] == []


def test_table_info_after_apply(tmp_path):
    db = tmp_path / "agt.db"
    _seed_minimal_db(db)
    backup_dir = tmp_path / "backups"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--apply",
         "--db-path", str(db), "--backup-dir", str(backup_dir)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_orders)")}
    finally:
        conn.close()
    assert {"engine", "run_id", "broker_mode_at_staging", "staged_at_utc",
            "submitted_at_utc", "spot_at_submission", "limit_price_at_submission",
            "acked_at_utc", "gate_verdicts"}.issubset(cols)
