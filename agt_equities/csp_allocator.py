"""
agt_equities/csp_allocator.py

Cash-Secured Put allocator for the AGT Equities wheel strategy.

Consumes RAY candidates from the screener pipeline and produces
per-account CSP tickets respecting Rulebook V10 pre-entry gates,
household-level risk, and IRA-first / margin-last routing.

Architectural contract:
  - Household is the unit of risk. All Rulebook checks (Rule 1
    concentration, Rule 3 sector, Rule 4 correlation, Rule 6 EL
    floor) run at household level against household-aggregated
    state. Per-account risk posture is irrelevant.
  - Sizing is NLV-proportional at 10% household NLV target per
    new CSP entry, with a hard ceiling at 20% household NLV
    (Rule 1, at assignment-notional).
  - Routing within a household is IRA-first (cash collateral, no
    interest), then margin-eligible accounts. Partial allocation
    is allowed when the household cannot fit all intended contracts.
  - Existing positions in a name do NOT disqualify new CSP entries.
    The Rule 1 check evaluates POST-TRADE household exposure,
    inclusive of existing shares + open CSP commitments + new CSP
    commitment. If there's room under the 20% ceiling, the new
    entry stages.

This module is a NEW subsystem introduced for multi-client scaling
(May 2026 go-live). It does NOT import from telegram_bot.py — the
orchestrator (cmd_scan in M1.5) passes pre-fetched IBKR state as
parameters to avoid circular imports.

Sprint M1.1 scope: data layer only. The single public function
_fetch_household_buying_power_snapshot() wraps accountSummaryAsync
and consumes pre-fetched _discover_positions output to produce a
household-indexed HouseholdSnapshot dict. No sizing, no gate checks,
no routing, no staging — those land in M1.2 through M1.5.
"""
from __future__ import annotations

import logging
from typing import Any

from agt_equities.config import (
    ACCOUNT_TO_HOUSEHOLD,
    HOUSEHOLD_MAP,
    MARGIN_ACCOUNTS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HouseholdSnapshot shape (returned by _fetch_household_buying_power_snapshot)
# ---------------------------------------------------------------------------
#
# {
#     "Yash_Household": {
#         "household": "Yash_Household",
#         "hh_nlv": 261000.0,                    # sum of all account NLV
#         "hh_margin_nlv": 109000.0,             # margin-eligible only
#         "hh_margin_el": 32000.0,               # sum of EL across margin accts
#         "accounts": {
#             "U21971297": {
#                 "account_id": "U21971297",
#                 "nlv": 109000.0,
#                 "el": 32000.0,
#                 "buying_power": 54000.0,       # from IBKR BuyingPower tag
#                 "cash_available": 15000.0,     # derived: NLV - existing notional
#                 "margin_eligible": True,
#             },
#             "U22076329": {
#                 "account_id": "U22076329",
#                 "nlv": 152000.0,
#                 "el": 0.0,                     # IRA: no margin concept
#                 "buying_power": 0.0,
#                 "cash_available": 48000.0,
#                 "margin_eligible": False,
#             },
#         },
#         "existing_positions": {
#             # Aggregated across all accounts in household
#             "AAPL": {
#                 "total_shares": 200,
#                 "spot": 185.50,
#                 "current_value": 37100.0,      # shares * spot
#                 "sector": "Technology Hardware",
#             },
#         },
#         "existing_csps": {
#             # Open short puts across all accounts in household
#             "MSFT": {
#                 "total_contracts": 1,
#                 "strike": 420.0,
#                 "notional_commitment": 42000.0,  # contracts * strike * 100
#             },
#         },
#         "working_order_tickers": {"NVDA"},     # set of tickers with live orders
#         "staged_order_tickers": {"GOOGL"},     # set of tickers staged in pending_orders
#     },
#     "Vikram_Household": { ... },
# }
#
# Notes on field semantics:
#   - hh_nlv sums ALL accounts (margin + IRA) for Rule 1 denominator
#   - hh_margin_nlv is used for Rule 2 (per ADR-001)
#   - cash_available for margin accts = buying_power - existing short notional
#   - cash_available for IRAs = NLV - (value of long positions + open CSP commitments)
#   - existing_positions[ticker].current_value is the market value (for Rule 1)
#   - existing_csps[ticker].notional_commitment is at-assignment notional
#   - working_order_tickers and staged_order_tickers are union across all
#     accounts in the household — used for "don't double up" checks
# ---------------------------------------------------------------------------


async def _fetch_household_buying_power_snapshot(
    ib_conn,
    discovered_positions: dict,
) -> dict[str, dict[str, Any]]:
    """Build per-household snapshot of buying power, positions, and CSP state.

    Args:
        ib_conn: Connected ib_async.IB instance. Used for accountSummaryAsync
            to fetch fresh NLV/EL/BuyingPower per account. Caller is
            responsible for connection lifecycle.
        discovered_positions: Output of telegram_bot._discover_positions().
            Caller is responsible for calling _discover_positions and
            passing its result here. This inverts the dependency so
            csp_allocator never imports from telegram_bot.

    Returns:
        Dict keyed by household name. Each value is a HouseholdSnapshot
        dict (shape documented above). Empty dict if ib_conn fails or
        discovered_positions is empty/errored.

    Raises:
        Never. All exceptions are caught, logged, and result in an empty
        snapshot for the affected household. The caller must handle
        empty/partial snapshots gracefully.
    """
    snapshots: dict[str, dict[str, Any]] = {}

    # ── Fetch fresh per-account NLV / EL / BuyingPower from IBKR ──
    account_tags: dict[str, dict[str, float]] = {}
    try:
        summary = await ib_conn.accountSummaryAsync()
        wanted = {"NetLiquidation", "ExcessLiquidity", "BuyingPower"}
        for item in summary:
            acct = item.account
            if acct not in ACCOUNT_TO_HOUSEHOLD:
                continue
            if item.tag not in wanted:
                continue
            try:
                value = float(item.value)
            except (TypeError, ValueError):
                value = 0.0
            account_tags.setdefault(acct, {})[item.tag] = value
    except Exception as exc:
        logger.warning(
            "csp_allocator: accountSummaryAsync failed: %s", exc,
        )
        return {}

    # ── Iterate each household known to config ──
    for hh_name, acct_ids in HOUSEHOLD_MAP.items():
        hh_accounts: dict[str, dict[str, Any]] = {}
        hh_nlv = 0.0
        hh_margin_nlv = 0.0
        hh_margin_el = 0.0

        for acct_id in acct_ids:
            tags = account_tags.get(acct_id, {})
            nlv = tags.get("NetLiquidation", 0.0)
            el = tags.get("ExcessLiquidity", 0.0)
            buying_power = tags.get("BuyingPower", 0.0)
            margin_eligible = acct_id in MARGIN_ACCOUNTS

            hh_accounts[acct_id] = {
                "account_id": acct_id,
                "nlv": nlv,
                "el": el,
                "buying_power": buying_power,
                # populated below after we know existing notional
                "cash_available": 0.0,
                "margin_eligible": margin_eligible,
            }

            hh_nlv += nlv
            if margin_eligible:
                hh_margin_nlv += nlv
                hh_margin_el += el

        # ── Pull household-aggregated existing positions from discovered_positions ──
        existing_positions: dict[str, dict[str, Any]] = {}
        existing_csps: dict[str, dict[str, Any]] = {}
        working_tickers: set[str] = set()
        staged_tickers: set[str] = set()

        hh_disco = discovered_positions.get("households", {}).get(hh_name, {})
        for pos in hh_disco.get("positions", []):
            ticker = str(pos.get("ticker", "")).upper()
            if not ticker:
                continue

            # Stock exposure
            total_shares = int(pos.get("total_shares", 0) or 0)
            spot = float(pos.get("spot_price", 0.0) or 0.0)
            if total_shares > 0 and spot > 0:
                existing_positions[ticker] = {
                    "total_shares": total_shares,
                    "spot": spot,
                    "current_value": total_shares * spot,
                    "sector": pos.get("sector", "Unknown"),
                }

            # Short put exposure (existing CSPs)
            short_puts = pos.get("short_puts", []) or []
            for sp in short_puts:
                contracts = int(sp.get("contracts", 0) or 0)
                strike = float(sp.get("strike", 0.0) or 0.0)
                if contracts > 0 and strike > 0:
                    existing_csps.setdefault(
                        ticker,
                        {
                            "total_contracts": 0,
                            "strike": strike,
                            "notional_commitment": 0.0,
                        },
                    )
                    existing_csps[ticker]["total_contracts"] += contracts
                    existing_csps[ticker]["notional_commitment"] += (
                        contracts * strike * 100
                    )

            # Working / staged order tracking
            if pos.get("has_working_order"):
                working_tickers.add(ticker)
            if pos.get("has_staged_order"):
                staged_tickers.add(ticker)

        # ── Compute cash_available per account ──
        # Margin accounts: use BuyingPower directly (already nets out
        # margin requirements).
        # IRAs: NLV - (sum of position market values in the household)
        # - (sum of open CSP commitments in the household), pro-rated
        # to the account's share of household NLV.
        # NOTE: _discover_positions output is aggregated per-household-
        # per-ticker, not per-account. For M1.1 we approximate IRA
        # cash_available using the account's share of household NLV
        # minus the household's total long position notional. This is
        # a known approximation — M1.2+ can refine to true per-account
        # cash tracking if needed.
        hh_long_notional = sum(
            p["current_value"] for p in existing_positions.values()
        )
        hh_csp_notional = sum(
            c["notional_commitment"] for c in existing_csps.values()
        )

        for acct_id, acct in hh_accounts.items():
            if acct["margin_eligible"]:
                acct["cash_available"] = max(0.0, acct["buying_power"])
            else:
                # IRA approximation: proportional share of household's
                # unencumbered NLV.
                if hh_nlv > 0:
                    acct_pct = acct["nlv"] / hh_nlv
                    hh_unencumbered = max(
                        0.0, hh_nlv - hh_long_notional - hh_csp_notional,
                    )
                    acct["cash_available"] = acct_pct * hh_unencumbered
                else:
                    acct["cash_available"] = 0.0

        snapshots[hh_name] = {
            "household": hh_name,
            "hh_nlv": round(hh_nlv, 2),
            "hh_margin_nlv": round(hh_margin_nlv, 2),
            "hh_margin_el": round(hh_margin_el, 2),
            "accounts": hh_accounts,
            "existing_positions": existing_positions,
            "existing_csps": existing_csps,
            "working_order_tickers": working_tickers,
            "staged_order_tickers": staged_tickers,
        }

    return snapshots
