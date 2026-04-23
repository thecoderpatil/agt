"""
AGT Equities — shared SQLite connection module.

Single source of truth for all production connections to agt_desk.db.
Used by:
  - telegram_bot.py (the bot process)
  - agt_deck/main.py (the FastAPI Cure Console process)
  - agt_equities/flex_sync.py (the EOD Flex sync writer)
  - agt_equities/trade_repo.py (the walker read interface)
  - agt_scheduler.py (the future APScheduler daemon, Sprint B)

Connection discipline:
  - busy_timeout=15000ms (15s) on every connection — survives the
    contention window when flex_sync holds the writer lock during EOD.
  - row_factory=sqlite3.Row everywhere — callers expect dict-like access.
  - Read-write connections via get_db_connection().
  - Read-only connections via get_ro_connection() — URI mode with
    PRAGMA query_only=ON for the Cure Console's top-strip reads.
  - All write transactions go through tx_immediate(conn), which issues
    explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK rather than relying on
    Python sqlite3's default DEFERRED behavior. DEFERRED races to upgrade
    from shared-to-reserved on first write, which can produce silent
    rollbacks under contention. IMMEDIATE acquires the reserved lock
    upfront and waits up to busy_timeout for it.
  - One-time PRAGMA setup via init_pragmas(conn), called once on
    bot/scheduler startup, not per-connection. WAL mode and synchronous
    are database-file-level settings and persist across connections.

Migration history:
  - 2026-04-13: created from agt_deck/db.py promotion. Sprint A Phase B.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import os

# Canonical DB path. Resolved at import time from AGT_DB_PATH env var,
# falling back to the __file__-relative path for CI / dev environments
# where AGT_DB_PATH is not set. Production NSSM env sets AGT_DB_PATH,
# so the fallback is never exercised in prod.
# Tripwire fixture monkeypatches this attribute to a sentinel path.
_BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH: Path = Path(
    os.environ.get("AGT_DB_PATH") or str(_BASE_DIR / "agt_desk.db")
)


def _resolve_db_path(override: str | Path | None = None) -> Path:
    """Resolve the canonical DB path.

    Resolution order:
      1. Explicit `override` arg (tests, scripts that know what they want).
      2. Module-level DB_PATH attribute if non-None (tripwire fixture
         and any legacy caller that monkeypatches it).
      3. AGT_DB_PATH env var.

    Returns a Path. Raises RuntimeError only if called with no override
    and DB_PATH is somehow None (should not occur after MR 1).
    """
    if override is not None:
        return Path(override)
    if DB_PATH is not None:
        return Path(DB_PATH)
    env = os.environ.get("AGT_DB_PATH", "").strip()
    if not env:
        raise RuntimeError(
            "AGT_DB_PATH unset, DB_PATH module attribute is None, and no "
            "db_path= argument supplied. This is a boot-contract violation "
            "-- the service should have failed at assert_boot_contract()."
        )
    return Path(env)

# Connection-level lock wait. 15 seconds covers the worst-case Flex sync
# contention window observed in production (two-daemon WAL contention).
_BUSY_TIMEOUT_MS = 15000


def get_db_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a read-write connection to agt_desk.db.

    Args:
        db_path: optional explicit path for tests/scripts. Default None
            routes to the module-level DB_PATH (production). Banked in
            FU-A-04 to enable test fixture DB injection without
            monkeypatching the module attribute. See HANDOFF_ARCHITECT_v23
            and DT ruling Q1 from 2026-04-14.

    Returns:
        sqlite3.Connection with row_factory=Row and busy_timeout set.

    Callers must use closing() (or equivalent) to ensure the connection
    is closed. Write transactions must use tx_immediate(conn) — never
    rely on Python sqlite3's implicit DEFERRED 'with conn:' behavior.
    """
    target_path = _resolve_db_path(db_path)
    conn = sqlite3.connect(str(target_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS};")
    return conn


def get_ro_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a read-only connection to agt_desk.db.

    Used by the Cure Console FastAPI process for top-strip reads where
    write capability would be a footgun. PRAGMA query_only=ON enforces
    read-only at the SQLite level — any attempted write raises
    sqlite3.OperationalError.

    Args:
        db_path: optional explicit path for tests/scripts. Default None
            routes to the module-level DB_PATH (production). Banked in
            FU-A-04 to enable test fixture DB injection without
            monkeypatching the module attribute. See HANDOFF_ARCHITECT_v23
            and DT ruling Q1 from 2026-04-14.

    Returns:
        sqlite3.Connection with row_factory=Row, query_only=ON, and
        busy_timeout set.
    """
    target_path = _resolve_db_path(db_path)
    uri = f"file:{target_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS};")
    return conn


@contextmanager
def tx_immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Context manager for write transactions using BEGIN IMMEDIATE.

    Replaces the implicit 'with conn:' pattern (which uses Python's
    DEFERRED transaction default, racing to upgrade from shared to
    reserved on first write).

    BEGIN IMMEDIATE acquires the reserved lock upfront. Under
    contention, the connection waits up to busy_timeout (15s) for the
    lock rather than failing immediately. On exception, the transaction
    rolls back. On clean exit, it commits.

    Usage:
        with closing(get_db_connection()) as conn:
            with tx_immediate(conn):
                conn.execute("INSERT INTO ...", (...))
                conn.execute("UPDATE ...", (...))

    Raises:
        sqlite3.OperationalError if the lock cannot be acquired within
        busy_timeout, or if the underlying transaction fails.
    """
    try:
        conn.execute("BEGIN IMMEDIATE;")
    except sqlite3.OperationalError:
        # busy_timeout exhausted — propagate so callers can retry or alert
        raise
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.OperationalError:
            pass  # rollback failed; surface the original exception
        raise
    else:
        conn.execute("COMMIT;")


def init_pragmas(conn: sqlite3.Connection) -> None:
    """One-time database-level PRAGMA setup.

    Called once on bot or scheduler startup — NOT per-connection. WAL
    mode, synchronous level, and wal_autocheckpoint are SQLite-database-
    file-level settings and persist in the DB file header across all
    future connections.

    Args:
        conn: An open read-write connection to agt_desk.db.
    """
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA wal_autocheckpoint=4000;")
    # busy_timeout is set per-connection by get_db_connection(),
    # not here — it does not persist in the DB file header.
