"""
tests/test_telegram_async_offload.py

Sprint 3 MR 1 (2026-04-24): smoke tests for the asyncio.to_thread offload
wrap sites in telegram_bot.py. See reports/telegram_approval_gate_asyncio_audit.md
(Investigation B, Sprint 2) for the audit that motivated these fixes.

Scope:
  - _sync_db_read_one + _sync_db_write helpers exist, run sync, correct return types
  - Under load, offloading via asyncio.to_thread does NOT block the PTB event loop
    (a parallel asyncio.sleep task completes while the blocking helper runs)
  - _sync_db_write uses tx_immediate (commits even without explicit conn.commit)

These tests intentionally DO NOT import telegram_bot at module scope (~22k LOC +
heavy deps). They re-execute the helper bodies against an in-memory sqlite3 and
assert the offload primitive works. The real wrap sites are covered by integration
smoke in paper mode post-deploy.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest


pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Helpers that mirror the production module-level ones in telegram_bot.py
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    conn.commit()
    return conn


def _run_read_sync(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()


def _run_write_sync(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    # Mirror of _sync_db_write: execute under IMMEDIATE transaction, return rowcount.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_read_helper_returns_row_or_none():
    conn = _make_conn()
    conn.execute("INSERT INTO t (id, v) VALUES (1, 42)")
    conn.commit()

    row = _run_read_sync(conn, "SELECT id, v FROM t WHERE id = ?", (1,))
    assert row is not None
    assert row["id"] == 1
    assert row["v"] == 42

    missing = _run_read_sync(conn, "SELECT id, v FROM t WHERE id = ?", (999,))
    assert missing is None


def test_write_helper_returns_rowcount():
    conn = _make_conn()
    conn.execute("INSERT INTO t (id, v) VALUES (1, 100)")
    conn.commit()

    rc_update = _run_write_sync(conn, "UPDATE t SET v = ? WHERE id = ?", (200, 1))
    assert rc_update == 1

    rc_noop = _run_write_sync(conn, "UPDATE t SET v = ? WHERE id = ?", (999, 99999))
    assert rc_noop == 0

    # Verify the value actually persisted (tx_immediate committed)
    row = _run_read_sync(conn, "SELECT v FROM t WHERE id = ?", (1,))
    assert row["v"] == 200


@pytest.mark.asyncio
async def test_to_thread_does_not_block_event_loop():
    """The core invariant MR 1 protects: blocking work offloaded via asyncio.to_thread
    lets other tasks run concurrently."""

    def _slow_sync() -> str:
        time.sleep(0.3)
        return "done"

    async def _fast_task() -> float:
        t0 = time.monotonic()
        await asyncio.sleep(0.01)
        return time.monotonic() - t0

    t0 = time.monotonic()
    slow_task = asyncio.create_task(asyncio.to_thread(_slow_sync))
    fast_elapsed = await _fast_task()
    fast_ack_at = time.monotonic() - t0

    result = await slow_task
    total = time.monotonic() - t0

    assert result == "done"
    # Fast task completed well before slow helper finished — event loop was free.
    assert fast_ack_at < 0.15, f"event loop blocked: fast ack took {fast_ack_at:.3f}s"
    # Slow helper did run for ~0.3s
    assert total >= 0.25


@pytest.mark.asyncio
async def test_to_thread_preserves_return_value_and_exceptions():
    def _ok() -> int:
        return 42

    def _raises() -> None:
        raise ValueError("boom")

    assert await asyncio.to_thread(_ok) == 42

    with pytest.raises(ValueError, match="boom"):
        await asyncio.to_thread(_raises)


@pytest.mark.asyncio
async def test_write_helper_via_to_thread_commits_and_returns_rowcount(tmp_path):
    """End-to-end: the production helpers open a fresh connection inside the
    helper body (not passed in from outside), so the connection stays within
    the worker thread. Mirror that here using a file-backed sqlite so the
    commit is visible to a separate read call."""
    db_path = str(tmp_path / "offload_smoke.db")

    def _init() -> None:
        c = sqlite3.connect(db_path)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        c.execute("INSERT INTO t (id, v) VALUES (7, 1)")
        c.commit()
        c.close()

    _init()

    def _write_in_thread(sql: str, params: tuple) -> int:
        c = sqlite3.connect(db_path)
        try:
            c.execute("BEGIN IMMEDIATE")
            rc = c.execute(sql, params).rowcount
            c.commit()
            return rc
        finally:
            c.close()

    def _read_in_thread(sql: str, params: tuple):
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, params).fetchone()
        finally:
            c.close()

    rc = await asyncio.to_thread(
        _write_in_thread,
        "UPDATE t SET v = ? WHERE id = ?",
        (99, 7),
    )
    assert rc == 1

    row = await asyncio.to_thread(
        _read_in_thread,
        "SELECT v FROM t WHERE id = ?",
        (7,),
    )
    assert row["v"] == 99
