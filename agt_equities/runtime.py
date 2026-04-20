"""AGT runtime context - RunMode, Protocols, and the RunContext dataclass.

Part of ADR-008 Shadow Scan plumbing (MR 1). Engines will gain a required
``ctx: RunContext`` parameter in MR 2-5; the composition root (scheduler
daemon, Telegram handlers, ``dev_cli.py``, ``scripts/shadow_scan.py``)
picks which sinks to wire in.

See ``docs/adr/ADR-008_SHADOW_SCAN.md`` for the full architecture.

This module is *deliberately* thin: no engine import, no bot import, no
DB import. It is allowed to be imported from invariant checks, the
scheduler, Telegram handlers, and ``scripts/shadow_scan.py`` without
pulling the full stack into a command-line tool.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


# Canonical production DB path. Anything that flows through
# ``scripts/shadow_scan.py`` MUST NOT accept this as a ``db_path`` - see
# ``NO_SHADOW_ON_PROD_DB`` in ``agt_equities/invariants/checks.py`` and the
# runtime assert in the CLI entry point.
PROD_DB_PATH: str = (
    os.environ.get("AGT_DB_PATH")
    or r"C:\AGT_Telegram_Bridge\agt_desk.db"
)


class RunMode(str, Enum):
    """Distinguishes live-mode runs (engines write to SQLite + IB) from
    shadow-mode runs (sinks capture in memory, nothing persists).
    """

    LIVE = "live"
    SHADOW = "shadow"


@runtime_checkable
class OrderSink(Protocol):
    """Contract for any collaborator that receives staged order tickets.

    Implementations: ``SQLiteOrderSink`` (live), ``CollectorOrderSink``
    (shadow). Callers pass batches of ticket dicts; the sink decides
    whether they hit ``pending_orders`` or an in-memory list.
    """

    def stage(
        self,
        tickets: list[dict],
        *,
        engine: str,
        run_id: str,
        meta: dict | None = None,
    ) -> None: ...


@runtime_checkable
class DecisionSink(Protocol):
    """Contract for any collaborator that persists decision artifacts.

    ``_run_cc_logic`` today writes ``cc_cycle_log`` and
    ``bucket3_dynamic_exit_log`` inline. MR 5 extracts those writes to
    this seam so shadow runs can observe decision output without
    mutating the DB. Implementations: ``SQLiteDecisionSink`` (live),
    ``CollectorDecisionSink`` (shadow), ``NullDecisionSink`` (tests).
    """

    def record_cc_cycle(self, entries: list[dict], *, run_id: str) -> None: ...

    def record_dynamic_exit(self, entries: list[dict], *, run_id: str) -> None: ...


@dataclass(frozen=True)
class RunContext:
    """Immutable per-invocation context passed to every engine.

    Engines consult ``ctx.order_sink`` / ``ctx.decision_sink`` instead of
    reaching into module-level staging functions. ``db_path`` is present
    for the (temporary) SQLite-clone safety belt described in ADR-008
    section 3.4; once MR 5 lands the clone becomes optional.

    ``frozen=True`` is load-bearing: it prevents a caller from post-hoc
    mutating the sink pointer after the ctx is in flight, which was the
    concurrent-state-bleed failure mode the env-var approach rejected.
    """

    mode: RunMode
    run_id: str
    order_sink: OrderSink
    decision_sink: DecisionSink
    db_path: str | None = None

    @property
    def is_live(self) -> bool:
        return self.mode is RunMode.LIVE

    @property
    def is_shadow(self) -> bool:
        return self.mode is RunMode.SHADOW


# ---------------------------------------------------------------------------
# SQLite clone - ADR-008 section 3.4 safety belt
# ---------------------------------------------------------------------------

def clone_sqlite_db_with_wal(
    src: str | Path,
    dest_dir: str | Path | None = None,
) -> str:
    """Copy a live SQLite DB to a writable temporary location.

    Uses ``sqlite3.Connection.backup()`` which is WAL-aware: it drains
    the WAL frames into the copy atomically without requiring the source
    to be idle. This is safer than a naive ``shutil.copy`` of
    ``agt_desk.db`` + ``-wal`` + ``-shm`` when the bot is writing.

    Args:
        src: path to the source SQLite database.
        dest_dir: optional parent directory. When ``None`` (default), a
            fresh ``tempfile.mkdtemp()`` is used. Callers are responsible
            for cleaning up the returned directory via
            ``shutil.rmtree(Path(returned_path).parent, ignore_errors=True)``
            after the shadow run completes.

    Returns:
        Absolute path to the cloned DB as a string. The file name matches
        the source file name.

    Raises:
        FileNotFoundError: if ``src`` does not exist.
        sqlite3.Error: if the backup operation fails.
    """
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"source DB does not exist: {src_path}")

    if dest_dir is None:
        dest_dir = Path(tempfile.mkdtemp(prefix="agt_shadow_"))
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / src_path.name

    src_conn = sqlite3.connect(str(src_path))
    try:
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            with dest_conn:
                src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    return str(dest_path)


__all__ = [
    "PROD_DB_PATH",
    "RunMode",
    "OrderSink",
    "DecisionSink",
    "RunContext",
    "clone_sqlite_db_with_wal",
]
