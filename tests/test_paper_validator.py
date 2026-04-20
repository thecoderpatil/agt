"""Tests for ADR-016 Paper Pipeline First-Fire Validator (P1).

Tests the standalone paper_validator module without requiring a live IB
connection. Covers:
  - ensure_schema idempotency
  - _next_friday date logic
  - _build_synthetic_payload structure
  - approval gate detection (AGT_CSP_REQUIRE_APPROVAL=true scenario)
  - _stage_order writes pending_orders row with notes column
  - run_validator blocked_at='approval_gate' when env var set
  - validator_runs row written on approval_gate block
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> str:
    """Return path to a minimal test DB with pending_orders and related tables."""
    db = str(tmp_path / "test_validator.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload JSON NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            ib_order_id INTEGER,
            ib_perm_id INTEGER,
            status_history TEXT,
            fill_price REAL,
            fill_qty INTEGER,
            fill_commission REAL,
            fill_time TEXT,
            last_ib_status TEXT,
            client_id TEXT DEFAULT 'AGT'
        );
    """)
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEnsureSchema:
    def test_creates_validator_runs_table(self, tmp_path):
        from agt_equities.paper_validator import ensure_schema
        db = _make_db(tmp_path)
        ensure_schema(db)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='validator_runs'"
        ).fetchone()
        conn.close()
        assert row is not None, "validator_runs table should be created"

    def test_adds_notes_column_to_pending_orders(self, tmp_path):
        from agt_equities.paper_validator import ensure_schema
        db = _make_db(tmp_path)
        # notes column absent at start
        conn = sqlite3.connect(db)
        cols_before = [r[1] for r in conn.execute("PRAGMA table_info(pending_orders)").fetchall()]
        conn.close()
        assert "notes" not in cols_before

        ensure_schema(db)

        conn = sqlite3.connect(db)
        cols_after = [r[1] for r in conn.execute("PRAGMA table_info(pending_orders)").fetchall()]
        conn.close()
        assert "notes" in cols_after, "notes column should be added"

    def test_idempotent(self, tmp_path):
        from agt_equities.paper_validator import ensure_schema
        db = _make_db(tmp_path)
        ensure_schema(db)
        # Second call must not raise
        ensure_schema(db)


class TestNextFriday:
    def test_returns_friday(self):
        from agt_equities.paper_validator import _next_friday
        import datetime
        result = _next_friday()
        dt = datetime.date.fromisoformat(result)
        assert dt.weekday() == 4, f"Expected Friday (weekday 4), got {dt.weekday()}"

    def test_always_in_future(self):
        from agt_equities.paper_validator import _next_friday
        from agt_equities.dates import et_today
        import datetime
        result = _next_friday()
        dt = datetime.date.fromisoformat(result)
        assert dt > et_today(), "Next Friday must be in the future"


class TestBuildSyntheticPayload:
    def test_payload_structure(self):
        from agt_equities.paper_validator import _build_synthetic_payload, VALIDATOR_ACCOUNT
        run_id = "abc123"
        payload = _build_synthetic_payload(run_id, 520.0, "2026-04-24")
        assert payload["ticker"] == "SPY"
        assert payload["right"] == "P"
        assert payload["action"] == "SELL"
        assert payload["sec_type"] == "OPT"
        assert payload["quantity"] == 1
        assert payload["account_id"] == VALIDATOR_ACCOUNT
        assert payload["notes"] == f"SYNTHETIC_VALIDATOR_{run_id}"

    def test_strike_is_10pct_otm(self):
        from agt_equities.paper_validator import _build_synthetic_payload
        spot = 500.0
        payload = _build_synthetic_payload("test", spot, "2026-04-24")
        # ~10% OTM = ~450 strike
        assert payload["strike"] <= spot * 0.95, "Strike should be at least 5% OTM"
        assert payload["strike"] >= spot * 0.80, "Strike should not be more than 20% OTM"

    def test_notes_contains_run_id(self):
        from agt_equities.paper_validator import _build_synthetic_payload
        run_id = "deadbeef123"
        payload = _build_synthetic_payload(run_id, 520.0, "2026-04-24")
        assert run_id in payload["notes"]
        assert payload["notes"].startswith("SYNTHETIC_VALIDATOR_")


class TestStageOrder:
    def test_writes_row_with_notes(self, tmp_path):
        from agt_equities.paper_validator import ensure_schema, _stage_order
        db = _make_db(tmp_path)
        ensure_schema(db)

        payload = {
            "ticker": "SPY",
            "strike": 468.0,
            "expiry": "2026-04-24",
            "quantity": 1,
            "action": "SELL",
            "right": "P",
            "sec_type": "OPT",
            "account_id": "DUP751003",
            "limit_price": 0.05,
            "notes": "SYNTHETIC_VALIDATOR_test123",
        }
        order_id = _stage_order(db, payload)
        assert isinstance(order_id, int) and order_id > 0

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT status, notes, payload FROM pending_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "staged"
        assert row[1] == "SYNTHETIC_VALIDATOR_test123"
        loaded = json.loads(row[2])
        assert loaded["ticker"] == "SPY"


class TestApprovalGateDetection:
    def test_run_validator_blocks_on_approval_required(self, tmp_path, monkeypatch):
        """When AGT_CSP_REQUIRE_APPROVAL=true, validator must block at approval_gate."""
        from agt_equities import paper_validator
        db = _make_db(tmp_path)
        paper_validator.ensure_schema(db)

        monkeypatch.setenv("AGT_CSP_REQUIRE_APPROVAL", "true")

        result = paper_validator.run_validator(trigger="on_demand", db_path=db)

        assert result["success"] is False, "Should fail when approval required"
        assert result["blocked_at"] == "approval_gate"
        assert result["blocked_reason"] == "AGT_CSP_REQUIRE_APPROVAL_TRUE"

    def test_run_validator_blocks_records_db_row(self, tmp_path, monkeypatch):
        """validator_runs row must be written even on approval_gate block."""
        from agt_equities import paper_validator
        db = _make_db(tmp_path)
        paper_validator.ensure_schema(db)

        monkeypatch.setenv("AGT_CSP_REQUIRE_APPROVAL", "true")

        result = paper_validator.run_validator(trigger="on_demand", db_path=db)

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT success, stage_reached, blocked_at, blocked_reason "
            "FROM validator_runs WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
        conn.close()

        assert row is not None, "validator_runs row should be written"
        assert row[0] == 0, "success should be 0"
        assert row[2] == "approval_gate"
        assert row[3] == "AGT_CSP_REQUIRE_APPROVAL_TRUE"

    def test_run_validator_passes_with_approval_false(self, tmp_path, monkeypatch):
        """When AGT_CSP_REQUIRE_APPROVAL=false, should not block at approval_gate."""
        from agt_equities import paper_validator
        db = _make_db(tmp_path)
        paper_validator.ensure_schema(db)

        monkeypatch.setenv("AGT_CSP_REQUIRE_APPROVAL", "false")
        # Patch _circuit_breaker_check and IB to stop before real IB connect
        monkeypatch.setattr(
            paper_validator, "_circuit_breaker_check", lambda: (False, "TEST_STOP_EARLY")
        )

        result = paper_validator.run_validator(trigger="on_demand", db_path=db)
        # Should NOT block at approval_gate — must get at least to circuit_breaker stage
        assert result.get("blocked_at") != "approval_gate"


class TestValidatorRunsSchema:
    def test_validator_runs_columns(self, tmp_path):
        from agt_equities.paper_validator import ensure_schema
        db = _make_db(tmp_path)
        ensure_schema(db)
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(validator_runs)").fetchall()]
        conn.close()
        required = [
            "run_id", "started_at_utc", "completed_at_utc", "trigger",
            "success", "stage_reached", "blocked_at", "blocked_reason",
            "pending_order_id", "ib_order_id", "cleanup_status", "evidence_json",
        ]
        for col in required:
            assert col in cols, f"Missing column: {col}"
