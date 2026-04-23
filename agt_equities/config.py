"""Canonical configuration constants for AGT Equities.



HOUSEHOLD_MAP is the single source of truth for account→household routing.

Paper-mode override mutates this dict in place at startup; all consumers

must import from here, never redefine.



Exported:

    PAPER_MODE              — bool, True when AGT_PAPER_MODE is set

    HOUSEHOLD_MAP           — forward map: household → [account_ids]

    ACCOUNT_TO_HOUSEHOLD    — derived inverse: account_id → household

    ACTIVE_ACCOUNTS         — list of all account IDs in HOUSEHOLD_MAP

    MARGIN_ELIGIBLE_ACCOUNTS — household → margin-eligible account IDs

    MARGIN_ACCOUNTS         — frozenset of all margin-eligible account IDs

"""

from __future__ import annotations



import logging

import os

from pathlib import Path

from typing import Dict, List



from dotenv import load_dotenv



# ── Load .env (same path resolution as telegram_bot.py:62-63) ──

_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_env_path = Path(os.environ.get("AGT_ENV_FILE", str(_DEFAULT_ENV_PATH)))

load_dotenv(_env_path, override=False)



# ── MR 4: PAPER_MODE derived from AGT_BROKER_MODE (authoritative), AGT_PAPER_MODE fallback.
_broker_mode_raw = os.environ.get("AGT_BROKER_MODE", "").strip().lower()
if _broker_mode_raw in ("paper", "live"):
    PAPER_MODE: bool = _broker_mode_raw == "paper"
else:
    # Fallback: legacy AGT_PAPER_MODE (migration window)
    PAPER_MODE = os.environ.get("AGT_PAPER_MODE", "").lower() in ("1", "true", "yes")



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



# ── Margin-eligible accounts (Sprint D) ──

# IRA accounts excluded — can't deploy margin or sell naked CSPs.

# Per Rulebook v5 Rule 2. Paper mode treats all accounts as margin-eligible.

_LIVE_MARGIN_ELIGIBLE: Dict[str, List[str]] = {

    "Yash_Household": ["U21971297"],

    "Vikram_Household": ["U22388499"],

}



MARGIN_ELIGIBLE_ACCOUNTS: Dict[str, List[str]] = (

    {hh: list(accts) for hh, accts in HOUSEHOLD_MAP.items()}

    if PAPER_MODE

    else _LIVE_MARGIN_ELIGIBLE

)



MARGIN_ACCOUNTS: frozenset = frozenset(

    acct for accts in MARGIN_ELIGIBLE_ACCOUNTS.values() for acct in accts

)



# Strategy blacklist (wheel strategy ticker exclusions).

# Distinct from trade_repo.EXCLUDED_TICKERS (Walker index filter: SPX/VIX/NDX/RUT/XSP).

EXCLUDED_TICKERS: frozenset[str] = frozenset({"IBKR", "TRAW.CVR", "SPX", "SLS", "GTLB"})

# -- Account display labels (MR 2.5: migrated from telegram_bot.py) --
# Used by position_discovery.py and telegram_bot.py display paths.
# ACCOUNT_NAMES (IB subscription routing) stays in telegram_bot.py.
_LIVE_ACCOUNT_LABELS: dict[str, str] = {
    "U21971297": "Individual",
    "U22076329": "Roth IRA",
    "U22388499": "Vikram",
}

ACCOUNT_LABELS: dict[str, str] = (
    {acct: f"Paper-{hh.replace('_Household', '')}"
     for acct, hh in ACCOUNT_TO_HOUSEHOLD.items()}
    if PAPER_MODE
    else _LIVE_ACCOUNT_LABELS
)


# ============================================================================
# Account display labels — canonical source of truth.
# Consumed by agt_deck.queries, telegram_bot, csp_allocator, digest layer.
# Do not duplicate this map anywhere; import from here.
# ============================================================================
ACCOUNT_ALIAS: dict[str, str] = {
    "U21971297": "Yash Ind",
    "U22076329": "Yash Roth",
    "U22076184": "Yash Trad IRA",  # DORMANT — excluded from CSP entry via CSP_ACTIVE_ACCOUNTS
    "U22388499": "Vikram Ind",
}

# Accounts eligible for new CSP entries. Excludes dormant accounts.
# csp_allocator filters household candidates against this set before
# handing tickets to the approval_gate. Monitoring-only accounts (CC rolls,
# harvests, dormant positions) are NOT in this set.
CSP_ACTIVE_ACCOUNTS: frozenset[str] = frozenset({
    "U21971297",  # Yash Ind
    "U22076329",  # Yash Roth
    "U22388499",  # Vikram Ind
})


def is_csp_active_account(account_id: str, mode: str) -> bool:
    """Return True if this account is eligible for a CSP entry.

    Live mode: account must be in CSP_ACTIVE_ACCOUNTS (excludes dormant).
    Paper mode: all accounts pass through (paper is a test sandbox; dormant
    labeling is a live-capital concept and paper gateway accounts are
    DU*-prefixed, not in the live-only frozenset).
    Unknown mode strings fail closed to live semantics so a typo in
    AGT_BROKER_MODE never opens the dormant gate in production.

    Args:
        account_id: IBKR account number (e.g. "U21971297" or "DU1234567").
        mode: broker mode, "live" or "paper" (typically AGT_BROKER_MODE env).

    Returns:
        True if eligible for CSP entry; False otherwise.
    """
    if mode == "paper":
        return True
    return account_id in CSP_ACTIVE_ACCOUNTS


# ============================================================================
# CSP Digest configuration constants — ADR-CSP_TELEGRAM_DIGEST_v1.
# Flippable to Opus via env override; budget tripwire and timeout per ADR.
# ============================================================================
CSP_DIGEST_LLM_MODEL: str = os.environ.get(
    "AGT_CSP_DIGEST_LLM_MODEL", "claude-sonnet-4-6",
)
CSP_DIGEST_DAILY_BUDGET_USD: float = float(
    os.environ.get("AGT_CSP_DIGEST_DAILY_BUDGET_USD", "5.0"),
)
CSP_DIGEST_TIMEOUT_MINUTES: int = int(
    os.environ.get("AGT_CSP_DIGEST_TIMEOUT_MINUTES", "90"),
)
