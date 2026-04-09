"""Execution kill-switch. Default: disabled.

Three independent disables, OR logic:
  1. Env var AGT_EXECUTION_ENABLED != "true" (deploy-time default)
  2. In-process _HALTED flag in telegram_bot (runtime /halt)
  3. execution_state DB row with disabled=1 (persistent /halt)

Any one disables. All three must allow for execution to proceed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Same DB path resolution as trade_repo.py:22
_DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"


class ExecutionDisabledError(RuntimeError):
    """Raised when any execution gate blocks order placement."""
    pass


def _env_enabled() -> bool:
    """Check AGT_EXECUTION_ENABLED env var. Default false (safe)."""
    return os.getenv("AGT_EXECUTION_ENABLED", "false").strip().lower() == "true"


def _db_enabled() -> bool:
    """Check execution_state DB row. No row or disabled=0 means enabled."""
    try:
        with sqlite3.connect(str(_DB_PATH), timeout=2.0) as conn:
            row = conn.execute(
                "SELECT disabled FROM execution_state WHERE id=1"
            ).fetchone()
            if row is None:
                return True  # no row = not disabled (fresh install pre-schema)
            return row[0] == 0
    except sqlite3.OperationalError:
        return True  # table missing = not disabled (fresh install)


def assert_execution_enabled(in_process_halted: bool = False) -> None:
    """Raises ExecutionDisabledError if any disable gate is active.

    Args:
        in_process_halted: pass telegram_bot._HALTED at the call site.

    Three gates, OR logic — any one blocks:
      1. AGT_EXECUTION_ENABLED env var != "true"
      2. in_process_halted == True (/halt active)
      3. execution_state DB row disabled == 1
    """
    if not _env_enabled():
        raise ExecutionDisabledError(
            "AGT_EXECUTION_ENABLED env var is not 'true'. "
            "Deploy-time kill-switch active."
        )
    if in_process_halted:
        raise ExecutionDisabledError(
            "/halt is active in-process. Restart or /resume CONFIRM to clear."
        )
    if not _db_enabled():
        raise ExecutionDisabledError(
            "execution_state DB row marks execution disabled. "
            "/resume CONFIRM to clear."
        )
