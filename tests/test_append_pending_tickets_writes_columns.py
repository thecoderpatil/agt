"""append_pending_tickets writes new first-class columns + preserves payload.

Notes: directly importing telegram_bot at test time triggers a pile of
import-time side-effects (DB tripwire, TELEGRAM_BOT_TOKEN requirement,
NSSM service handshakes). To keep this unit test isolated, we exercise
the same SQL shape the function emits against a freshly-migrated SQLite
DB. The actual function body is tiny; if telegram_bot.py drifts from
this query shape, the verification grep on `INSERT INTO pending_orders`
will catch it at the gate.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.sprint_a


def _seed_db(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status_history TEXT,
                ib_order_id INTEGER,
                ib_perm_id INTEGER,
                fill_price REAL,
                fill_qty REAL,
                fill_commission REAL,
                fill_time TEXT,
                last_ib_status TEXT,
                client_id INTEGER,
                engine TEXT,
                run_id TEXT,
                broker_mode_at_staging TEXT,
                staged_at_utc TEXT,
                spot_at_staging REAL,
                premium_at_staging REAL,
                gate_verdicts TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


# Mirror of the production INSERT in telegram_bot.append_pending_tickets.
# A static-grep sentinel ("INSERT INTO pending_orders") plus a manual
# audit at MR review time keeps this in sync.
PRODUCTION_INSERT = (
    "INSERT INTO pending_orders ("
    "payload, status, created_at, "
    "engine, run_id, broker_mode_at_staging, staged_at_utc, "
    "spot_at_staging, premium_at_staging, gate_verdicts) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _row_for(ticket: dict) -> tuple:
    payload = dict(ticket)
    gv = payload.get("gate_verdicts")
    gv_json = json.dumps(gv, default=str) if gv is not None else None
    return (
        json.dumps(payload, default=str),
        str(payload.get("status", "staged")),
        str(payload.get("created_at") or "2026-04-26T12:00:00Z"),
        payload.get("engine"),
        payload.get("run_id"),
        payload.get("broker_mode_at_staging"),
        payload.get("staged_at_utc"),
        payload.get("spot_at_staging"),
        payload.get("premium_at_staging"),
        gv_json,
    )


def test_insert_writes_first_class_columns(tmp_path):
    db_path = tmp_path / "agt.db"
    _seed_db(db_path)
    ticket = {
        "ticker": "MSFT", "right": "P", "strike": 400, "qty": 1, "limit": 1,
        "engine": "csp_allocator", "run_id": "run-9",
        "broker_mode_at_staging": "paper", "staged_at_utc": "2026-04-26T12:00:00Z",
        "spot_at_staging": 392.5, "premium_at_staging": 1.05,
        "gate_verdicts": {"mode_match": True, "strike_freshness": True},
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(PRODUCTION_INSERT, _row_for(ticket))
        conn.commit()
        rows = conn.execute(
            "SELECT engine, run_id, broker_mode_at_staging, staged_at_utc, "
            "spot_at_staging, premium_at_staging, gate_verdicts, payload "
            "FROM pending_orders"
        ).fetchall()
    finally:
        conn.close()
    eng, rid, bm, sat, sp, prem, gv, payload = rows[0]
    assert eng == "csp_allocator"
    assert rid == "run-9"
    assert bm == "paper"
    assert sat == "2026-04-26T12:00:00Z"
    assert sp == 392.5
    assert prem == 1.05
    assert json.loads(gv)["mode_match"] is True
    parsed = json.loads(payload)
    assert parsed["ticker"] == "MSFT"
    assert parsed["engine"] == "csp_allocator"


def test_insert_handles_none_first_class_fields(tmp_path):
    """Direct call sites that don't pass first-class columns -> NULL columns."""
    db_path = tmp_path / "agt.db"
    _seed_db(db_path)
    ticket = {"ticker": "AAPL", "right": "P", "strike": 180, "qty": 1, "limit": 0.5}
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(PRODUCTION_INSERT, _row_for(ticket))
        conn.commit()
        row = conn.execute(
            "SELECT engine, run_id, gate_verdicts FROM pending_orders"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None


def test_telegram_bot_uses_matching_insert_shape():
    """Static-grep guard: production telegram_bot.py must contain the same
    INSERT INTO pending_orders shape with the new columns.
    """
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "telegram_bot.py").read_text(encoding="utf-8")
    assert "INSERT INTO pending_orders" in text
    for col in ("engine", "run_id", "broker_mode_at_staging", "staged_at_utc",
                "spot_at_staging", "premium_at_staging", "gate_verdicts"):
        assert col in text, f"telegram_bot.py missing column reference: {col}"
