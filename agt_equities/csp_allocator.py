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
from typing import Any, Callable

from agt_equities.config import (
    ACCOUNT_TO_HOUSEHOLD,
    HOUSEHOLD_MAP,
    MARGIN_ACCOUNTS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# M1.2 sizing + routing constants
# ---------------------------------------------------------------------------

# Rule 1 / Rule 2 sizing parameters. See Rulebook V10 lines 67 (Rule 1
# ceiling) and 86 (Rule 2 VIX-scaled deployment governor).

CSP_TARGET_NLV_PCT = 0.10           # Target per-trade sizing as % household NLV
CSP_CEILING_NLV_PCT = 0.20          # Rule 1 hard ceiling (post-assignment)
MAINTENANCE_MARGIN_HAIRCUT = 0.30   # Conservative Reg T + buffer for Rule 2
                                    # post-assignment margin impact estimate

# VIX → EL retention % table (Rule 2). Each row is (lo_inclusive,
# hi_exclusive, retain_pct). "Retain" is the fraction of margin NLV
# that must be HELD BACK from deployment. Deployable fraction is
# therefore (1 - retain_pct).
VIX_RETAIN_TABLE = [
    (0.0,   20.0, 0.80),   # VIX <20  → retain 80%, deploy 20%
    (20.0,  25.0, 0.70),
    (25.0,  30.0, 0.60),
    (30.0,  40.0, 0.50),
    (40.0,  999.0, 0.40),  # VIX ≥40 → retain 40%, deploy 60% (cap)
]


def _vix_retain_pct(vix: float) -> float:
    """Return required EL retention pct for a given VIX level (Rule 2).

    Lookup is inclusive-lo / exclusive-hi. VIX values outside the table's
    coverage (negative or NaN-like) fall through to the safe default
    (80% retention, 20% deployment). Pure function.
    """
    for lo, hi, retain in VIX_RETAIN_TABLE:
        if lo <= vix < hi:
            return retain
    return 0.80  # safe default


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


# ---------------------------------------------------------------------------
# M1.2: pure sizing function
# ---------------------------------------------------------------------------

def _csp_size_household(
    hh_snapshot: dict,
    candidate,         # RAYCandidate: .ticker, .strike, .mid, .expiry, .dte
    vix: float,
) -> int:
    """Compute target contract count for one candidate in one household.

    Returns 0 if the candidate fails Rule 1 or Rule 2 at any contract
    count, or cannot fit even 1 integer contract. Otherwise returns
    the integer contract count closest to 10% household NLV target,
    preferring lower count on tie.

    Rule 1 (hard ceiling, line 67 of Rulebook V10):
      Post-assignment household exposure must stay strictly below
      20% household NLV. Exposure = (existing_shares × spot) +
      (existing_csp_notional) + (new_csp_notional at strike).

    Rule 2 (VIX-scaled deployment governor, line 86):
      Worst-case-at-sizing: assumes all new contracts route to margin
      accounts. New margin impact = strike × 100 × contracts × 0.30
      (conservative haircut). Must fit within VIX-scaled margin
      headroom on the household's margin-eligible accounts.

    Pure function — no IB, no DB, no side effects.
    """
    hh_nlv = hh_snapshot["hh_nlv"]
    if hh_nlv <= 0:
        return 0

    strike = candidate.strike
    collateral_per_contract = strike * 100
    if collateral_per_contract <= 0:
        return 0

    target_dollars = CSP_TARGET_NLV_PCT * hh_nlv
    ceiling_dollars = CSP_CEILING_NLV_PCT * hh_nlv

    # Existing household exposure on this name (Rule 1 baseline)
    ticker = candidate.ticker.upper()
    existing = 0.0
    existing_pos = hh_snapshot.get("existing_positions", {}).get(ticker)
    if existing_pos:
        existing += existing_pos.get("current_value", 0.0)
    existing_csp = hh_snapshot.get("existing_csps", {}).get(ticker)
    if existing_csp:
        existing += existing_csp.get("notional_commitment", 0.0)

    # Rule 2 VIX-scaled margin headroom
    retain_pct = _vix_retain_pct(vix)
    deployable_pct = 1.0 - retain_pct
    hh_margin_nlv = hh_snapshot["hh_margin_nlv"]
    hh_margin_el = hh_snapshot["hh_margin_el"]
    margin_used_pre = max(0.0, hh_margin_nlv - hh_margin_el)
    margin_budget = hh_margin_nlv * deployable_pct
    margin_headroom = max(0.0, margin_budget - margin_used_pre)

    # Candidate integer contract counts: floor and ceil around target
    target_contracts_float = target_dollars / collateral_per_contract
    c_low = int(target_contracts_float)
    c_high = c_low + 1

    def _feasible(c: int) -> bool:
        if c < 1:
            return False
        new_notional = c * collateral_per_contract
        # Rule 1: post-assignment household exposure strict < 20% NLV
        if existing + new_notional >= ceiling_dollars:
            return False
        # Rule 2: worst-case margin impact (assume all margin-routed)
        new_margin_impact = new_notional * MAINTENANCE_MARGIN_HAIRCUT
        if new_margin_impact > margin_headroom:
            return False
        return True

    options = [c for c in (c_low, c_high) if _feasible(c)]
    if not options:
        return 0

    # Pick closest to 10% target dollars, prefer lower count on tie
    return min(
        options,
        key=lambda c: (
            abs(c * collateral_per_contract - target_dollars),
            c,
        ),
    )


# ---------------------------------------------------------------------------
# M1.2: pure routing function
# ---------------------------------------------------------------------------

def _csp_route_to_accounts(
    n_contracts: int,
    hh_snapshot: dict,
    candidate,
) -> list[dict]:
    """Route n_contracts across accounts in a household, IRA-first.

    Ordering: non-margin (IRA) accounts first sorted by cash_available
    desc, then margin-eligible accounts sorted by buying_power desc.
    Greedy fill. Partial allocation allowed — returns whatever fit,
    possibly less than n_contracts. Empty list if nothing fits.

    Each returned ticket is a dict matching the pending_orders payload
    shape used by append_pending_tickets.

    Pure function — no IB, no DB, no side effects.
    """
    if n_contracts < 1:
        return []

    collateral = candidate.strike * 100
    if collateral <= 0:
        return []

    # IRA first (margin_eligible=False), margin last.
    # Within each group, largest capacity first for efficient packing.
    # The sort key returns a (group, neg_capacity) tuple:
    #   group=0 for IRA, group=1 for margin
    #   capacity is cash_available for IRA, buying_power for margin
    ordered = sorted(
        hh_snapshot["accounts"].values(),
        key=lambda a: (
            0 if not a["margin_eligible"] else 1,
            -(
                a["cash_available"]
                if not a["margin_eligible"]
                else a["buying_power"]
            ),
        ),
    )

    remaining = n_contracts
    tickets: list[dict] = []
    for acct in ordered:
        if remaining == 0:
            break
        capacity = (
            acct["cash_available"]
            if not acct["margin_eligible"]
            else acct["buying_power"]
        )
        max_fit = int(capacity // collateral)
        take = min(remaining, max_fit)
        if take < 1:
            continue
        tickets.append({
            "account_id": acct["account_id"],
            "household": hh_snapshot["household"],
            "ticker": candidate.ticker.upper(),
            "action": "SELL",
            "sec_type": "OPT",
            "right": "P",
            "strike": float(candidate.strike),
            "expiry": candidate.expiry.replace("-", ""),  # YYYYMMDD
            "quantity": take,
            "limit_price": float(candidate.mid),
            "annualized_yield": float(candidate.annualized_yield),
            "mode": "CSP_ENTRY",
            "status": "staged",
        })
        remaining -= take

    return tickets



# ---------------------------------------------------------------------------
# M1.3: Rule gate predicate functions + composable registry
# ---------------------------------------------------------------------------
#
# All gate functions share a uniform signature so CSP_GATE_REGISTRY can be
# iterated trivially by the orchestrator (M1.5 cmd_scan) without any
# per-gate dispatch logic.
#
#   gate(hh_snapshot, candidate, new_contracts, vix, extras)
#     -> (passed: bool, reason: str)
#
# `extras` is an orchestrator-populated dict carrying per-run data that
# gates need beyond the snapshot itself — sector_map, correlations,
# delta, days_to_earnings, etc. Gates that need nothing from extras
# simply ignore it. Missing extras data is fail-open per Rule 3 and
# Rule 4 — the orchestrator is responsible for logging data holes.
#
# All gate functions are PURE. No IB calls. No DB writes. No side
# effects. They read from hh_snapshot, candidate, and extras and
# return a tuple.
# ---------------------------------------------------------------------------

CSPGate = Callable[[dict, Any, int, float, dict], tuple[bool, str]]
# (hh_snapshot, candidate, new_contracts, vix, extras) -> (passed, reason)
# extras: dict populated by orchestrator with per-run data gates need
#   beyond the snapshot — sector_map, correlations, etc. Uniform
#   signature keeps CSP_GATE_REGISTRY iteration trivially composable.


def _csp_check_rule_1(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 1: post-assignment household concentration < 20% NLV.

    Existing exposure = (existing_shares × spot) + (open_csp_notional).
    New exposure = strike × 100 × n (full assignment-notional).
    Strict inequality < 20% household NLV.
    """
    hh_nlv = hh["hh_nlv"]
    if hh_nlv <= 0:
        return (False, "zero household NLV")
    ticker = candidate.ticker.upper()
    existing = 0.0
    pos = hh.get("existing_positions", {}).get(ticker)
    if pos:
        existing += pos.get("current_value", 0.0)
    csp = hh.get("existing_csps", {}).get(ticker)
    if csp:
        existing += csp.get("notional_commitment", 0.0)
    new_notional = candidate.strike * 100 * n
    total = existing + new_notional
    ceiling = CSP_CEILING_NLV_PCT * hh_nlv
    if total >= ceiling:
        return (
            False,
            f"rule_1 post-trade exposure ${total:,.0f} "
            f">= 20% ceiling ${ceiling:,.0f}",
        )
    return (True, "")


def _csp_check_rule_2(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 2: VIX-scaled margin headroom (worst-case 30% haircut).

    Assumes all n new contracts route to margin accounts (worst case
    at sizing). Margin impact = strike * 100 * n * 0.30.
    """
    hh_margin_nlv = hh["hh_margin_nlv"]
    if hh_margin_nlv <= 0:
        # No margin-eligible accounts → Rule 2 inapplicable (IRA-only)
        return (True, "")
    retain_pct = _vix_retain_pct(vix)
    deployable_pct = 1.0 - retain_pct
    hh_margin_el = hh["hh_margin_el"]
    margin_used_pre = max(0.0, hh_margin_nlv - hh_margin_el)
    margin_budget = hh_margin_nlv * deployable_pct
    headroom = max(0.0, margin_budget - margin_used_pre)
    new_impact = candidate.strike * 100 * n * MAINTENANCE_MARGIN_HAIRCUT
    if new_impact > headroom:
        return (
            False,
            f"rule_2 margin impact ${new_impact:,.0f} > "
            f"headroom ${headroom:,.0f} (VIX {vix:.1f})",
        )
    return (True, "")


def _csp_check_rule_3(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 3: post-trade household sector count <= 2 per GICS industry group.

    extras['sector_map']: dict[ticker -> industry_group] for existing
      household tickers plus the candidate's own classification.

    Fail-open on missing/unknown classification — the orchestrator is
    responsible for logging data holes separately. This is INTENTIONAL
    per Rulebook guidance: better to miss a reject than block every
    trade on a bad sector feed.
    """
    sector_map = extras.get("sector_map", {})
    candidate_sector = sector_map.get(candidate.ticker.upper())
    if not candidate_sector or candidate_sector == "Unknown":
        return (True, "")  # no classification → fail-open, log elsewhere
    existing_tickers = set(hh.get("existing_positions", {}).keys())
    existing_tickers |= set(hh.get("existing_csps", {}).keys())
    # Don't double-count the candidate itself if already held
    existing_tickers.discard(candidate.ticker.upper())
    same_sector_count = sum(
        1 for t in existing_tickers
        if sector_map.get(t) == candidate_sector
    )
    # Post-trade this candidate becomes the (same_sector_count + 1)-th name
    if same_sector_count + 1 > 2:
        return (
            False,
            f"rule_3 sector '{candidate_sector}' would hold "
            f"{same_sector_count + 1} names (limit 2)",
        )
    return (True, "")


def _csp_check_rule_4(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 4: no existing household position with >0.6 6-mo correlation.

    extras['correlations']: dict[(ticker_a, ticker_b) -> float] for
      candidate vs every existing household ticker. Order-independent
      lookup — check both (a,b) and (b,a).

    Fail-open on missing correlation data — same rationale as Rule 3.
    """
    correlations = extras.get("correlations", {})
    candidate_ticker = candidate.ticker.upper()
    existing_tickers = set(hh.get("existing_positions", {}).keys())
    existing_tickers |= set(hh.get("existing_csps", {}).keys())
    existing_tickers.discard(candidate_ticker)
    for other in existing_tickers:
        corr = (
            correlations.get((candidate_ticker, other))
            or correlations.get((other, candidate_ticker))
        )
        if corr is None:
            continue  # missing data → fail-open, log elsewhere
        if abs(corr) > 0.6:
            return (
                False,
                f"rule_4 correlation with {other} = {corr:.2f} "
                f"exceeds 0.6 limit",
            )
    return (True, "")


def _csp_check_rule_6(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 6: Vikram-household-only EL floor check.

    Hardcoded to Vikram_Household for M1.3 per Rulebook V10 line 169.
    Generalization to 'any margin-eligible household with configured
    floor' is a post-May refactor item.
    """
    if hh["household"] != "Vikram_Household":
        return (True, "")
    hh_margin_nlv = hh["hh_margin_nlv"]
    hh_margin_el = hh["hh_margin_el"]
    if hh_margin_nlv <= 0:
        return (True, "")
    el_pct = hh_margin_el / hh_margin_nlv
    # Rule 6: floor at 20%, freeze entries below
    if el_pct < 0.20:
        return (
            False,
            f"rule_6 Vikram EL {el_pct*100:.1f}% below 20% floor — "
            f"frozen for new entries",
        )
    return (True, "")


def _csp_check_rule_7(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 7 CSP Operating Procedure: delta, earnings, working orders.

    extras['delta']: float (absolute delta of candidate short put)
    extras['days_to_earnings']: int or None

    Rulebook V10 line 291: delta <= 0.25
    Rulebook V10 line 293: no CSP within 7 calendar days of earnings
    Rulebook V10 line 302: no working/staged order on the same ticker
    """
    delta = extras.get("delta")
    if delta is not None and abs(delta) > 0.25:
        return (False, f"rule_7 delta {abs(delta):.2f} > 0.25 limit")
    dte_earnings = extras.get("days_to_earnings")
    if dte_earnings is not None and 0 <= dte_earnings <= 7:
        return (
            False,
            f"rule_7 earnings in {dte_earnings}d (< 7d blackout)",
        )
    ticker = candidate.ticker.upper()
    if ticker in hh.get("working_order_tickers", set()):
        return (False, f"rule_7 working order exists on {ticker}")
    if ticker in hh.get("staged_order_tickers", set()):
        return (False, f"rule_7 staged order exists on {ticker}")
    return (True, "")


CSP_GATE_REGISTRY: list[tuple[str, CSPGate]] = [
    ("rule_1_concentration",   _csp_check_rule_1),
    ("rule_2_el_deployment",   _csp_check_rule_2),
    ("rule_3_sector",          _csp_check_rule_3),
    ("rule_4_correlation",     _csp_check_rule_4),
    ("rule_6_vikram_el_floor", _csp_check_rule_6),
    ("rule_7_csp_procedure",   _csp_check_rule_7),
]



# ---------------------------------------------------------------------------
# M1.4: run_csp_allocator orchestrator + AllocatorResult
# ---------------------------------------------------------------------------
#
# Pure orchestrator. Consumes RAY candidates + household snapshots and
# iterates candidates × households, running CSP_GATE_REGISTRY, sizing,
# and routing. Stages via an INJECTED staging_callback so the module
# stays pure of telegram_bot dependencies. Per-run data (sector_map,
# correlations, delta, etc.) is provided via an injected extras_provider
# for the same reason.
#
# In-memory snapshot mutation between candidates prevents double-booking
# cash when multiple candidates land on the same household.
#
# No IB, no DB, no cmd_scan integration. M1.5 will wire cmd_scan ->
# run_csp_allocator with concrete extras_provider and staging_callback
# implementations.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class AllocatorResult:
    """Result of running the CSP allocator over a set of candidates."""
    staged: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # [{household, ticker, reason}]
    errors: list[dict] = field(default_factory=list)   # [{household, ticker, error}]
    digest_lines: list[str] = field(default_factory=list)

    @property
    def total_staged_contracts(self) -> int:
        return sum(t.get("quantity", 0) for t in self.staged)

    @property
    def total_staged_notional(self) -> float:
        return sum(
            t.get("quantity", 0) * t.get("strike", 0) * 100
            for t in self.staged
        )


def run_csp_allocator(
    ray_candidates: list,
    snapshots: dict[str, dict],
    vix: float,
    extras_provider: Callable[[dict, Any], dict],
    staging_callback: Callable[[list[dict]], None] | None = None,
) -> AllocatorResult:
    """Run the CSP allocator over a set of RAY candidates.

    For each (candidate, household) pair:
      1. Build per-candidate extras dict via extras_provider(hh, candidate).
         The provider supplies sector_map, correlations, delta,
         days_to_earnings for the gates that need them.
      2. Run all gates in CSP_GATE_REGISTRY order. First failure
         short-circuits; the candidate is skipped for that household
         with the failing gate's reason logged.
      3. If all gates pass, size via _csp_size_household. Sub-integer
         at 10% target → skip.
      4. Route via _csp_route_to_accounts. Partial allocation allowed.
         Empty routing result → skip.
      5. Stage tickets via staging_callback if provided (None means
         dry-run: compute everything but do not persist).
      6. Mutate the in-memory snapshot to reduce buying power /
         cash_available on the affected accounts so the next
         candidate sees the reduced capacity.

    Args:
        ray_candidates: Screener output. Each must have .ticker,
            .strike, .mid, .expiry (YYYY-MM-DD), .annualized_yield.
        snapshots: Output of _fetch_household_buying_power_snapshot().
            MUTATED IN PLACE as candidates are allocated — pass a
            copy if the caller wants to preserve the original.
        vix: Current VIX level for Rule 2 scaling.
        extras_provider: Callable that returns the per-(hh, candidate)
            extras dict. Injecting this keeps the orchestrator pure
            of yfinance / DB / correlation-fetch dependencies — the
            caller (cmd_scan in M1.5) supplies a concrete provider.
        staging_callback: Optional. Called with each list of tickets
            that passed gates+sizing+routing. None = dry-run.

    Returns:
        AllocatorResult with staged, skipped, errors, digest_lines.

    Raises:
        Never. All per-candidate errors are caught and logged to
        result.errors so one broken candidate doesn't abort the run.
    """
    result = AllocatorResult()

    for candidate in ray_candidates:
        for hh_name, hh in snapshots.items():
            try:
                _process_one(
                    hh, hh_name, candidate, vix,
                    extras_provider, staging_callback, result,
                )
            except Exception as exc:
                logger.exception(
                    "csp_allocator: unhandled error on %s/%s",
                    hh_name, getattr(candidate, "ticker", "?"),
                )
                result.errors.append({
                    "household": hh_name,
                    "ticker": getattr(candidate, "ticker", "?"),
                    "error": f"{type(exc).__name__}: {exc}",
                })

    result.digest_lines = _format_digest(result)
    return result


def _process_one(
    hh: dict,
    hh_name: str,
    candidate: Any,
    vix: float,
    extras_provider: Callable,
    staging_callback: Callable | None,
    result: AllocatorResult,
) -> None:
    """Process one (household, candidate) pair. Mutates hh + result."""
    # Step 1: build extras
    extras = extras_provider(hh, candidate) or {}

    # Step 2: run gates in registry order, short-circuit on first fail
    # Gate check uses n=1 as a feasibility probe; real sizing is below.
    # This keeps gate semantics "can this household take ANY contracts"
    # rather than "can it take N specific contracts."
    for gate_name, gate_fn in CSP_GATE_REGISTRY:
        passed, reason = gate_fn(hh, candidate, 1, vix, extras)
        if not passed:
            result.skipped.append({
                "household": hh_name,
                "ticker": candidate.ticker,
                "reason": f"{gate_name}: {reason}",
            })
            return

    # Step 3: size at household level
    n_contracts = _csp_size_household(hh, candidate, vix)
    if n_contracts < 1:
        result.skipped.append({
            "household": hh_name,
            "ticker": candidate.ticker,
            "reason": (
                "sizing returned 0 "
                "(sub-integer at 10% target or ceiling breach)"
            ),
        })
        return

    # Step 4: route across accounts (partial allowed)
    tickets = _csp_route_to_accounts(n_contracts, hh, candidate)
    if not tickets:
        result.skipped.append({
            "household": hh_name,
            "ticker": candidate.ticker,
            "reason": "no account has capacity for any integer contract",
        })
        return

    # Step 5: stage (or dry-run)
    if staging_callback is not None:
        try:
            staging_callback(tickets)
        except Exception as exc:
            logger.warning(
                "csp_allocator: staging_callback failed for %s/%s: %s",
                hh_name, candidate.ticker, exc,
            )
            result.errors.append({
                "household": hh_name,
                "ticker": candidate.ticker,
                "error": f"staging failed: {exc}",
            })
            return

    result.staged.extend(tickets)

    # Step 6: mutate snapshot to prevent double-booking
    collateral_per_contract = candidate.strike * 100
    for t in tickets:
        acct_id = t["account_id"]
        acct = hh["accounts"][acct_id]
        consumed = t["quantity"] * collateral_per_contract
        if acct["margin_eligible"]:
            acct["buying_power"] = max(
                0.0, acct["buying_power"] - consumed,
            )
            # Margin used increases → EL drops by haircut amount
            margin_impact = consumed * MAINTENANCE_MARGIN_HAIRCUT
            hh["hh_margin_el"] = max(
                0.0, hh["hh_margin_el"] - margin_impact,
            )
        else:
            acct["cash_available"] = max(
                0.0, acct["cash_available"] - consumed,
            )

    # Also update existing_csps so Rule 1 on NEXT candidate for same
    # ticker sees the freshly-staged commitment
    ticker = candidate.ticker.upper()
    existing = hh.setdefault("existing_csps", {}).setdefault(
        ticker,
        {
            "total_contracts": 0,
            "strike": candidate.strike,
            "notional_commitment": 0.0,
        },
    )
    total_new_contracts = sum(t["quantity"] for t in tickets)
    existing["total_contracts"] += total_new_contracts
    existing["notional_commitment"] += (
        total_new_contracts * collateral_per_contract
    )


def _format_digest(result: AllocatorResult) -> list[str]:
    """Human-readable digest lines for Telegram output."""
    lines: list[str] = []
    if result.staged:
        lines.append(
            f"━━ CSP Allocator — {len(result.staged)} tickets staged ━━"
        )
        by_hh: dict[str, list] = {}
        for t in result.staged:
            by_hh.setdefault(t["household"], []).append(t)
        for hh_name, tickets in by_hh.items():
            short = hh_name.replace("_Household", "")
            lines.append(f"\n[{short}]")
            for t in tickets:
                lines.append(
                    f"  {t['ticker']} ${t['strike']:.0f}P x{t['quantity']} "
                    f"@ ${t['limit_price']:.2f} "
                    f"({t['annualized_yield']:.1f}% ann)"
                )
        lines.append(
            f"\nTotal: {result.total_staged_contracts} contracts, "
            f"${result.total_staged_notional:,.0f} notional"
        )
    if result.skipped:
        lines.append(f"\nSkipped: {len(result.skipped)}")
        # Show first 5 skip reasons, collapse the rest
        for s in result.skipped[:5]:
            short_hh = s["household"].replace("_Household", "")
            lines.append(f"  {s['ticker']} [{short_hh}]: {s['reason']}")
        if len(result.skipped) > 5:
            lines.append(f"  ... and {len(result.skipped) - 5} more")
    if result.errors:
        lines.append(f"\n⚠️ Errors: {len(result.errors)}")
        for e in result.errors[:3]:
            lines.append(f"  {e['ticker']}: {e['error']}")
    if not result.staged and not result.skipped and not result.errors:
        lines.append("CSP Allocator: no candidates processed")
    return lines


# ---------------------------------------------------------------------------
# B5: local margin math -- DB-backed CSP availability check
# ---------------------------------------------------------------------------
#
# local_margin_check() queries v_available_nlv to verify that an account has
# sufficient available_el to absorb new CSP notional (strike * qty * 100).
# This allows pre-stage margin checks without a live IBKR round-trip, using
# the latest el_snapshot minus open commitment notional from
# pending_order_children.
#
# Fail-open contract:
#   - No el_snapshot for account -> (True, "no_snapshot").  Prevents blocking
#     orders when el_snapshots is empty (fresh restart before first poll).
#   - Any exception -> (True, "check_error: ...").  A margin check failure
#     must never abort an order placement.
#
# Kill switch: AGT_B5_LOCAL_MARGIN_CHECK=0 disables.  Only the literal "0"
# disables; any other value keeps default-ON behaviour.

import os as _b5_os
import sqlite3 as _b5_sqlite3


def local_margin_check_enabled() -> bool:
    """Return True iff the B5 local margin check is enabled.

    Reads AGT_B5_LOCAL_MARGIN_CHECK on each call so operators can toggle
    without a process restart. Default ON; only "0" disables.
    """
    return _b5_os.environ.get("AGT_B5_LOCAL_MARGIN_CHECK", "1") != "0"


def local_margin_check(
    conn: _b5_sqlite3.Connection,
    account_id: str,
    notional: float,
) -> tuple[bool, str]:
    """Query v_available_nlv to check if account_id can absorb `notional`.

    Returns (ok: bool, reason: str).
      ok=True  -- available_el >= notional, or no snapshot (fail-open).
      ok=False -- available_el < notional; reason carries the rejection detail.

    Never raises. All exceptions return (True, "check_error: ...").
    """
    try:
        row = conn.execute(
            "SELECT available_el, available_nlv, committed_csp_notional "
            "FROM v_available_nlv WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return (True, "no_snapshot")
        # Support both sqlite3.Row (name-indexed) and plain tuple
        try:
            available_el = float(row["available_el"] or 0.0)
            committed    = float(row["committed_csp_notional"] or 0.0)
        except TypeError:
            available_el = float(row[0] or 0.0)
            committed    = float(row[2] or 0.0)
        if available_el < notional:
            return (
                False,
                f"available_el ${available_el:,.0f} < notional ${notional:,.0f} "
                f"(committed ${committed:,.0f})",
            )
        return (
            True,
            f"available_el ${available_el:,.0f} >= notional ${notional:,.0f}",
        )
    except Exception as exc:
        logger.warning(
            "local_margin_check failed for %s: %s (fail-open)", account_id, exc,
        )
        return (True, f"check_error: {exc}")

