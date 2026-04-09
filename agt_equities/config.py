"""Canonical configuration constants for AGT Equities.

HOUSEHOLD_MAP is the single source of truth for account→household routing.
Paper-mode override mutates this dict in place at startup; all consumers
must import from here, never redefine.

Exported:
    PAPER_MODE              — bool, True when AGT_PAPER_MODE is set
    HOUSEHOLD_MAP           — forward map: household → [account_ids]
    ACCOUNT_TO_HOUSEHOLD    — derived inverse: account_id → household
    ACTIVE_ACCOUNTS         — list of all account IDs in HOUSEHOLD_MAP
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

# ── Load .env (same path resolution as telegram_bot.py:62-63) ──
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path, override=True)

# ── Sprint 1C: Paper mode flag (mirrored from telegram_bot.py:76) ──
PAPER_MODE: bool = os.environ.get("AGT_PAPER_MODE", "").lower() in ("1", "true", "yes")

# ── Live household map (forward: household → [account_ids]) ──
_LIVE_HOUSEHOLD_MAP: Dict[str, List[str]] = {
    # U22076184 (Trad IRA) dormant — retained for Walker historical reconstruction
    "Yash_Household": ["U21971297", "U22076329", "U22076184"],
    "Vikram_Household": ["U22388499"],
}

# ── Paper account IDs from env (mirrored from telegram_bot.py:89-101) ──
# Format: AGT_PAPER_ACCOUNTS="DU123:Yash_Household,DU456:Vikram_Household"
_PAPER_HOUSEHOLD_MAP: Dict[str, List[str]] = {}
if PAPER_MODE:
    _raw_paper = os.environ.get("AGT_PAPER_ACCOUNTS", "")
    for _pair in _raw_paper.split(","):
        if ":" in _pair:
            _acct, _hh = _pair.strip().split(":", 1)
            _PAPER_HOUSEHOLD_MAP.setdefault(_hh.strip(), []).append(_acct.strip())
    if not _PAPER_HOUSEHOLD_MAP:
        logging.getLogger(__name__).error(
            "PAPER_MODE active but AGT_PAPER_ACCOUNTS empty or malformed — "
            "desk cannot route orders"
        )

# ── Canonical exports ──
HOUSEHOLD_MAP: Dict[str, List[str]] = (
    _PAPER_HOUSEHOLD_MAP if (PAPER_MODE and _PAPER_HOUSEHOLD_MAP) else _LIVE_HOUSEHOLD_MAP
)

ACCOUNT_TO_HOUSEHOLD: Dict[str, str] = {
    acct: hh for hh, accts in HOUSEHOLD_MAP.items() for acct in accts
}

ACTIVE_ACCOUNTS: List[str] = list(ACCOUNT_TO_HOUSEHOLD)
