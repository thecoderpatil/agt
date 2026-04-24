"""Sprint 6 Mega-MR 6 — backlog sweep sentinels.

Scope shipped in this MR (narrower than full dispatch):
- 6A F1-M-1: MAX_ROUNDS Telegram user-facing reply (telegram_bot.py)
- 6D F3-M-2: vrp_veto.py env-var-driven _VRP_DB_PATH resolution
- 6H: scripts/observe_trading_day.py SELECT rowid, * fix

Deliberately DEFERRED to Sprint 7 (per dispatch Reasoning Latitude):
- 6B F1-M-2: Gate-1/2 wiring at parse_and_stage time (invasive, >60 LOC)
- 6C F1-M-3: update_live_order ACTIVE_ACCOUNTS check (requires tool-loop
  structural review; own MR)
- 6E F2-H-2 phase 2: CONTINGENT on bookmark-migration confirmation
- 6F F2-L-1: CSRF double-submit (depends on 6E)
- 6G F2-L-2: .deck_token icacls (Windows-only, deploy hardening)
- 6I /digest_status command (non-trivial telegram_bot register path)

The deferred items are noted in the end-of-sprint rollup for Sprint 7
backlog pickup. Current MR keeps the sweep bounded + shippable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_6a_max_rounds_user_facing_reply_present():
    """F1-M-1: MAX_ROUNDS must now send a Telegram reply, not just log."""
    src = _read(REPO / "telegram_bot.py")
    assert "LLM hit max-rounds" in src, (
        "Sprint 6 6A: telegram_bot.py must send a user-facing Telegram "
        "message when MAX_ROUNDS is hit. Prior version only logged, "
        "leaving the operator with a truncated or silent response."
    )


def test_6d_vrp_veto_env_var_resolution_present():
    """F3-M-2: vrp_veto.py must read AGT_VRP_DB_PATH env first before
    falling back to the __file__-anchored default."""
    src = _read(REPO / "vrp_veto.py")
    assert "AGT_VRP_DB_PATH" in src, (
        "Sprint 6 6D: vrp_veto.py must resolve _VRP_DB_PATH from "
        "AGT_VRP_DB_PATH env var first (mirror MR !221 pattern)."
    )
    assert "_resolve_vrp_db_path" in src, (
        "Sprint 6 6D: vrp_veto.py must define _resolve_vrp_db_path() "
        "as the resolution entry point."
    )


def test_6h_observer_uses_explicit_rowid_select():
    """Observer row[rowid] bug: SELECT * doesn't expose rowid via Row
    factory; fix uses `SELECT rowid, *`."""
    src = _read(REPO / "scripts" / "observe_trading_day.py")
    assert "SELECT rowid, *" in src, (
        "Sprint 6 6H: observe_trading_day.py must use `SELECT rowid, *` "
        "so row[\"rowid\"] succeeds on the Row factory output. The prior "
        "`SELECT *` failed 85% of poll iterations with 'No item with "
        "that key'."
    )
