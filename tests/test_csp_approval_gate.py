"""Tests for agt_equities.csp_approval_gate — ADR-010 Phase 1.

Sprint A marker required; all tests use an in-memory DB to avoid prod DB.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(ticker="AAPL", strike=150.0, expiry="2026-05-16",
                    mid=1.50, ann_yield=0.18, household_id="Yash_Household"):
    return SimpleNamespace(
        ticker=ticker, strike=strike, expiry=expiry,
        mid=mid, annualized_yield=ann_yield, household_id=household_id,
    )


@contextmanager
def _in_mem_db():
    """In-memory SQLite connection, auto-closes."""
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


def _setup_table(conn):
    from agt_equities.csp_approval_gate import _ensure_table
    _ensure_table(conn)


# ---------------------------------------------------------------------------
# DB primitive tests
# ---------------------------------------------------------------------------

class TestEnsureTable:
    def test_creates_table_and_indexes(self):
        with _in_mem_db() as conn:
            _setup_table(conn)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            indexes = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
        assert "csp_pending_approval" in tables
        assert "idx_csp_pending_approval_status" in indexes
        assert "idx_csp_pending_approval_telegram_msg" in indexes

    def test_idempotent(self):
        """Calling _ensure_table twice must not raise."""
        with _in_mem_db() as conn:
            _setup_table(conn)
            _setup_table(conn)


class TestInsertAndPoll:
    def test_insert_returns_id_and_status_pending(self):
        from agt_equities.csp_approval_gate import _insert_pending_row, _poll_row_status
        now = datetime.now(timezone.utc)
        timeout = now + timedelta(minutes=30)
        with _in_mem_db() as conn:
            _setup_table(conn)
            row_id = _insert_pending_row(conn, "run-1", "[]", now, timeout)
            row = _poll_row_status(conn, row_id)
        assert row is not None
        assert row["status"] == "pending"
        assert row["approved_indices_json"] is None

    def test_poll_nonexistent_returns_none(self):
        from agt_equities.csp_approval_gate import _poll_row_status
        with _in_mem_db() as conn:
            _setup_table(conn)
            assert _poll_row_status(conn, 9999) is None

    def test_timeout_row_flips_status(self):
        from agt_equities.csp_approval_gate import (
            _insert_pending_row, _poll_row_status, _timeout_row,
        )
        now = datetime.now(timezone.utc)
        timeout = now + timedelta(minutes=30)
        with _in_mem_db() as conn:
            _setup_table(conn)
            row_id = _insert_pending_row(conn, "run-2", "[]", now, timeout)
            _timeout_row(conn, row_id)
            row = _poll_row_status(conn, row_id)
        assert row["status"] == "timeout"

    def test_timeout_row_is_cas_safe(self):
        """_timeout_row WHERE status='pending' — already-resolved row unchanged."""
        from agt_equities.csp_approval_gate import (
            _insert_pending_row, _poll_row_status, _timeout_row,
        )
        now = datetime.now(timezone.utc)
        timeout = now + timedelta(minutes=30)
        with _in_mem_db() as conn:
            _setup_table(conn)
            row_id = _insert_pending_row(conn, "run-3", "[]", now, timeout)
            conn.execute(
                "UPDATE csp_pending_approval SET status='approved' WHERE id=?",
                (row_id,),
            )
            conn.commit()
            _timeout_row(conn, row_id)  # should be a no-op
            row = _poll_row_status(conn, row_id)
        assert row["status"] == "approved"


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_digest_text_contains_ticker_and_row_id(self):
        from agt_equities.csp_approval_gate import _build_digest_text
        c = _make_candidate(ticker="MSFT", strike=380.0)
        text = _build_digest_text([c], row_id=42)
        assert "MSFT" in text
        assert "row #42" in text
        assert "380" in text

    def test_keyboard_structure(self):
        from agt_equities.csp_approval_gate import _build_keyboard
        candidates = [_make_candidate("AAPL"), _make_candidate("GOOG")]
        kb = _build_keyboard(candidates, row_id=7)
        # 2 candidate rows + 1 submit row
        assert len(kb) == 3
        # Each candidate row has 2 buttons (approve + skip)
        assert len(kb[0]) == 2
        assert kb[0][0]["callback_data"] == "csp_approve:7:0"
        assert kb[0][1]["callback_data"] == "csp_skip:7:0"
        # Submit row
        assert kb[2][0]["callback_data"] == "csp_submit:7"


# ---------------------------------------------------------------------------
# Gate function integration tests (mocked DB + Telegram)
# ---------------------------------------------------------------------------

class TestTelegramApprovalGate:
    """Mock time.sleep, DB connection, and Telegram send."""

    def _make_mock_db(self, poll_responses):
        """Build a mock get_db_connection context manager sequence."""
        call_count = {"n": 0}

        @contextmanager
        def _fake_conn(db_path=None):
            conn = MagicMock()
            n = call_count["n"]
            call_count["n"] += 1
            if hasattr(poll_responses, "__getitem__") and n < len(poll_responses):
                conn.execute.return_value.fetchone.return_value = poll_responses[n]
            yield conn

        return _fake_conn

    def test_empty_candidates_returns_empty(self):
        from agt_equities.csp_approval_gate import telegram_approval_gate
        result = telegram_approval_gate([])
        assert result == []

    def test_approved_path_returns_subset(self):
        """Row transitions to 'approved' with indices [0] after 1 poll."""
        from agt_equities.csp_approval_gate import telegram_approval_gate

        candidates = [_make_candidate("AAPL"), _make_candidate("GOOG")]
        now = datetime.now(timezone.utc)
        timeout = now + timedelta(minutes=30)

        with _in_mem_db() as real_conn:
            from agt_equities.csp_approval_gate import _ensure_table, _insert_pending_row
            _ensure_table(real_conn)
            row_id = _insert_pending_row(
                real_conn, "run-x", "[]", now, timeout
            )
            # Pre-resolve the row as approved with index 0
            real_conn.execute(
                "UPDATE csp_pending_approval "
                "SET status='approved', approved_indices_json='[0]' WHERE id=?",
                (row_id,),
            )
            real_conn.commit()

            @contextmanager
            def _fake_conn(db_path=None):
                yield real_conn

            with (
                patch("agt_equities.csp_approval_gate.get_db_connection", _fake_conn),
                patch("agt_equities.csp_approval_gate._insert_pending_row", return_value=row_id),
                patch("agt_equities.csp_approval_gate._send_approval_digest", return_value=None),
                patch("agt_equities.csp_approval_gate.time.sleep"),
            ):
                result = telegram_approval_gate(candidates)

        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    def test_timeout_path_returns_empty(self):
        """Row stays 'pending' past timeout_at — gate returns []."""
        from agt_equities.csp_approval_gate import telegram_approval_gate

        candidates = [_make_candidate()]
        # timeout_at in the past
        now = datetime.now(timezone.utc)

        with _in_mem_db() as real_conn:
            from agt_equities.csp_approval_gate import _ensure_table, _insert_pending_row
            _ensure_table(real_conn)
            past_timeout = now - timedelta(minutes=1)
            row_id = _insert_pending_row(
                real_conn, "run-y", "[]",
                now - timedelta(minutes=31), past_timeout,
            )
            # MR !207 (E-H-3 fix): _insert_pending_row no longer calls
            # conn.commit() internally — caller wraps in tx_immediate.
            # Tests that bypass the wrapping helper must commit explicitly
            # so the production tx_immediate(BEGIN IMMEDIATE) doesn't fail
            # with "cannot start a transaction within a transaction".
            real_conn.commit()

            @contextmanager
            def _fake_conn(db_path=None):
                yield real_conn

            with (
                patch("agt_equities.csp_approval_gate.get_db_connection", _fake_conn),
                patch("agt_equities.csp_approval_gate._send_approval_digest", return_value=None),
                patch("agt_equities.csp_approval_gate.time.sleep"),
                patch("agt_equities.csp_approval_gate.datetime") as mock_dt,
            ):
                mock_dt.now.return_value = now  # now > past_timeout
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                result = telegram_approval_gate(candidates, timeout_minutes=0)

        assert result == []

    def test_rejected_path_returns_empty(self):
        """Row status='rejected' -> gate returns []."""
        from agt_equities.csp_approval_gate import telegram_approval_gate

        candidates = [_make_candidate()]
        now = datetime.now(timezone.utc)
        timeout = now + timedelta(minutes=30)

        with _in_mem_db() as real_conn:
            from agt_equities.csp_approval_gate import _ensure_table, _insert_pending_row
            _ensure_table(real_conn)
            row_id = _insert_pending_row(real_conn, "run-z", "[]", now, timeout)
            real_conn.execute(
                "UPDATE csp_pending_approval SET status='rejected' WHERE id=?",
                (row_id,),
            )
            real_conn.commit()

            @contextmanager
            def _fake_conn(db_path=None):
                yield real_conn

            with (
                patch("agt_equities.csp_approval_gate.get_db_connection", _fake_conn),
                patch("agt_equities.csp_approval_gate._insert_pending_row", return_value=row_id),
                patch("agt_equities.csp_approval_gate._send_approval_digest", return_value=None),
                patch("agt_equities.csp_approval_gate.time.sleep"),
            ):
                result = telegram_approval_gate(candidates)

        assert result == []

    def test_out_of_bounds_index_dropped(self):
        """approved_indices_json with OOB index — those indices are silently dropped."""
        from agt_equities.csp_approval_gate import telegram_approval_gate

        candidates = [_make_candidate("AAPL")]  # only index 0 is valid
        now = datetime.now(timezone.utc)
        timeout = now + timedelta(minutes=30)

        with _in_mem_db() as real_conn:
            from agt_equities.csp_approval_gate import _ensure_table, _insert_pending_row
            _ensure_table(real_conn)
            row_id = _insert_pending_row(real_conn, "run-oob", "[]", now, timeout)
            real_conn.execute(
                "UPDATE csp_pending_approval "
                "SET status='approved', approved_indices_json='[0, 5, 99]' WHERE id=?",
                (row_id,),
            )
            real_conn.commit()

            @contextmanager
            def _fake_conn(db_path=None):
                yield real_conn

            with (
                patch("agt_equities.csp_approval_gate.get_db_connection", _fake_conn),
                patch("agt_equities.csp_approval_gate._insert_pending_row", return_value=row_id),
                patch("agt_equities.csp_approval_gate._send_approval_digest", return_value=None),
                patch("agt_equities.csp_approval_gate.time.sleep"),
            ):
                result = telegram_approval_gate(candidates)

        # Only index 0 is valid; 5 and 99 are dropped
        assert len(result) == 1
        assert result[0].ticker == "AAPL"


# ---------------------------------------------------------------------------
# WARTIME pre-check via run_csp_allocator
# ---------------------------------------------------------------------------

class TestWartimePreCheck:
    """Verify Q5 fix: approval_gate not called when all accounts WARTIME."""

    def _make_wartime_snapshots(self):
        return {
            "Yash_Household": {
                "accounts": {
                    "U12345": {"mode": "WARTIME", "margin_eligible": True},
                    "U12346": {"mode": "WARTIME", "margin_eligible": False},
                }
            }
        }

    def _make_mixed_snapshots(self):
        return {
            "Yash_Household": {
                "accounts": {
                    "U12345": {"mode": "WARTIME", "margin_eligible": True},
                    "U12346": {"mode": "PEACETIME", "margin_eligible": True},
                }
            }
        }

    def test_all_wartime_gate_not_called(self):
        from agt_equities.csp_allocator import run_csp_allocator
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import CollectorOrderSink, NullDecisionSink

        gate_called = []

        def spy_gate(candidates):
            gate_called.append(len(candidates))
            return candidates

        ctx = RunContext(
            mode=RunMode.SHADOW,
            run_id="test-wartime",
            order_sink=CollectorOrderSink(),
            decision_sink=NullDecisionSink(),
        
            broker_mode="paper",
            engine="csp",
        )
        candidates = [_make_candidate()]
        snapshots = self._make_wartime_snapshots()

        result = run_csp_allocator(
            ray_candidates=candidates,
            snapshots=snapshots,
            vix=20.0,
            extras_provider=lambda hh, c: {},
            ctx=ctx,
            approval_gate=spy_gate,
        )

        assert gate_called == [], "approval_gate must NOT be called when all accounts WARTIME"

    def test_mixed_mode_gate_is_called(self):
        from agt_equities.csp_allocator import run_csp_allocator
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import CollectorOrderSink, NullDecisionSink

        gate_called = []

        def spy_gate(candidates):
            gate_called.append(len(candidates))
            return []  # reject all — avoids complex downstream setup

        ctx = RunContext(
            mode=RunMode.SHADOW,
            run_id="test-mixed",
            order_sink=CollectorOrderSink(),
            decision_sink=NullDecisionSink(),
        
            broker_mode="paper",
            engine="csp",
        )
        candidates = [_make_candidate()]
        snapshots = self._make_mixed_snapshots()

        run_csp_allocator(
            ray_candidates=candidates,
            snapshots=snapshots,
            vix=20.0,
            extras_provider=lambda hh, c: {},
            ctx=ctx,
            approval_gate=spy_gate,
        )

        assert len(gate_called) == 1, "approval_gate MUST be called when some accounts non-WARTIME"
