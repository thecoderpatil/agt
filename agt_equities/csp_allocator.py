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
import os
from typing import Any, Callable, Protocol, runtime_checkable

from agt_equities.config import (
    ACCOUNT_TO_HOUSEHOLD,
    CSP_ACTIVE_ACCOUNTS,
    HOUSEHOLD_MAP,
    MARGIN_ACCOUNTS,
    VIKRAM_HOUSEHOLD,
    is_csp_active_account,
)
from agt_equities.fa_block_margin import (
    AllocationDigest,
    CSPProposal,
    STATUS_APPROVED,
    allocate_csp,
    format_allocation_digest,
)
from agt_equities.runtime import RunContext
from agt_equities import csp_decisions_repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSPCandidate contract (load-bearing interface seam — DT Q4)
# ---------------------------------------------------------------------------
#
# End-state autonomy vision (2026-04-16, project_end_state_vision.md):
#
#   Paper  = full autonomy. Screener → allocator → staging, no human.
#   Live   = full autonomy EXCEPT CSP selection. Screener emits N
#            candidates → LLM digest + Telegram yes/no → allocator
#            consumes the approved subset. CSP selection is the one
#            wheel decision that never goes human-out-of-the-loop.
#
# The seam that makes both paths executable from one codebase:
#
#   1. `CSPCandidate` — an explicit attribute contract the allocator
#      consumes. `ScanCandidate` (pxo_scanner adapter, scan_bridge.py)
#      and `RAYCandidate` (screener terminal output, screener/types.py)
#      both conform via duck typing. Protocol makes the contract
#      enforceable for the future digest tool's output shape.
#
#   2. `approval_gate` — pluggable Callable[[list], list] on
#      `run_csp_allocator`. Default = identity (paper pass-through).
#      Live wires a Telegram-digest gate that drops non-approved
#      candidates before any household work is done.
#
#   3. `AllocatorResult.candidate_reasoning` — per-candidate
#      observability payload. Shape is stable so the future digest
#      tool can consume allocator output as training signal /
#      retrospective audit surface without internal refactor.
#
# Building the LLM digest tool is a SEPARATE ticket, post-allocator.
# Adding LLM plumbing to this sprint pollutes paper debug signal with
# API failure modes and bloats scope (news feeds, prompt eng, multi-
# candidate Telegram UI). The seam here is what makes that ticket
# additive instead of a rewrite.
# ---------------------------------------------------------------------------


@runtime_checkable
class CSPCandidate(Protocol):
    """Attribute contract the CSP allocator consumes.

    Any object satisfying this Protocol is a valid allocator input.
    `ScanCandidate` (pxo_scanner adapter) and `RAYCandidate` (screener
    terminal output) both conform.

    Required attributes
    -------------------
    ticker : str                 — upper-cased root symbol (e.g. "AAPL")
    strike : float               — put strike
    mid : float                  — mid-market premium per contract
    expiry : str                 — expiry date "YYYY-MM-DD"
    annualized_yield : float     — RAY / annualized ROI, decimal OR
                                   percent per pxo_scanner convention

    Optional attributes (read via getattr with safe defaults)
    ---------------------------------------------------------
    dte : int                    — days to expiry
    sector : str                 — GICS sector / industry group
    delta : float                — abs(delta) of the short put
    otm_pct : float              — strike-to-spot OTM distance
    reasoning : dict             — optional per-candidate reasoning
                                   payload from upstream (e.g. LLM
                                   digest). Copied verbatim into
                                   AllocatorResult.candidate_reasoning.
    """

    ticker: str
    strike: float
    mid: float
    expiry: str
    annualized_yield: float


# Default approval gate for paper / full-autonomy mode: identity pass-through.
# Live CSP path will inject a Telegram-digest approval gate here.
def _default_approval_gate(
    candidates: list[CSPCandidate],
) -> list[CSPCandidate]:
    """Identity approval gate — paper-mode default.

    Returns the full candidate list unchanged. Live mode will inject
    a Telegram LLM digest gate that:
      1. Renders a ranked digest of the top N candidates to Yash
      2. Awaits yes/no per candidate
      3. Returns only the approved subset
    """
    return list(candidates)


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

        # MR !108: direct pending_orders guard against dead-flag reliance
        # pos.get('has_staged_order') is a test-fixture-only flag.
        # _discover_positions never sets it in production, so Rule 7
        # would fire on an always-empty set. Query pending_orders directly
        # for ACTIVE CSP put orders on accounts in this household and fold
        # their tickers into staged_tickers alongside the legacy flag path.
        #
        # Status filter: staged/processing/sent/transmitting/partially_filled.
        # Terminal (filled/cancelled/rejected/superseded/failed) excluded.
        try:
            import json as _json
            from agt_equities.db import get_ro_connection as _get_ro
            _ACTIVE_CSP_STATUSES = (
                "staged", "processing", "sent",
                "transmitting", "partially_filled",
            )
            _acct_set = set(acct_ids)
            _ph = ",".join("?" * len(_ACTIVE_CSP_STATUSES))
            _conn = _get_ro()
            try:
                _rows = _conn.execute(
                    f"SELECT payload, status FROM pending_orders "
                    f"WHERE status IN ({_ph})",
                    _ACTIVE_CSP_STATUSES,
                ).fetchall()
            finally:
                _conn.close()
            for _row in _rows:
                try:
                    _p = _json.loads(_row["payload"] or "{}")
                except Exception:
                    continue
                if (_p.get("account_id") or _p.get("account")) not in _acct_set:
                    continue
                # CSP put: right='P' AND action='SELL'
                if str(_p.get("right", "")).upper() != "P":
                    continue
                if str(_p.get("action", "")).upper() != "SELL":
                    continue
                _tkr = str(_p.get("ticker") or _p.get("symbol") or "").upper()
                if not _tkr:
                    continue
                staged_tickers.add(_tkr)
        except Exception as _dedup_exc:
            # Fail-closed: log and keep whatever the legacy flag-union
            # built. A DB read hiccup must not widen the dedup gate.
            logger.warning(
                "csp_allocator: pending_orders dedup query failed: %s",
                _dedup_exc,
            )

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
    """Route n_contracts across household accounts via fa_block_margin.

    DEPRECATED shim as of Sprint B5.b — delegates to
    `agt_equities.fa_block_margin.allocate_csp`, the canonical per-account
    router. Preserves the original ticket-dict return shape so callers that
    haven't migrated to AllocationDigest consumption keep working.

    Use `allocate_csp` directly for new call sites. This shim will be
    removed in M1.5 when cmd_scan wiring collapses the indirection.
    """
    if n_contracts < 1:
        return []
    if candidate.strike * 100 <= 0:
        return []

    digest = _build_and_allocate(n_contracts, hh_snapshot, candidate)
    return _tickets_from_digest(digest, hh_snapshot, candidate)



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



def _csp_check_vix_acceleration(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """VIX acceleration veto: block all CSP entries when VIX has risen >20% over 3 sessions.

    extras['vix_history']: list of recent VIX closes, newest-first.
      Minimum 4 values needed (today + 3 prior sessions).
      Example: [22.0, 20.5, 19.0, 18.0] → today=22, 3 sessions ago=18.

    Rate of change = (current - 3_sessions_ago) / 3_sessions_ago.
    Threshold: >20% rise → reject ALL CSP entries.

    Fail-open on missing/insufficient VIX history — the orchestrator
    is responsible for logging data holes separately.
    """
    vix_history = extras.get("vix_history")
    if not vix_history or len(vix_history) < 4:
        return (True, "")  # insufficient data → fail-open

    try:
        vix_current = float(vix_history[0])
        vix_3_ago = float(vix_history[3])
    except (TypeError, ValueError, IndexError):
        return (True, "")  # bad data → fail-open

    if vix_3_ago <= 0:
        return (True, "")  # nonsensical baseline → fail-open

    vix_roc = (vix_current - vix_3_ago) / vix_3_ago
    if vix_roc > 0.20:
        return (
            False,
            f"vix_acceleration VIX rose {vix_roc:.1%} over 3 sessions "
            f"({vix_3_ago:.1f}→{vix_current:.1f}), >20% threshold — "
            f"all CSP entries blocked",
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


def _csp_check_rule_3b(hh, candidate, n, vix, extras) -> tuple[bool, str]:
    """Rule 3b: hard-exclude candidates whose sector is in EXCLUDED_SECTORS.

    Airlines/Biotech/Pharma are QUALITY exclusions (C1); REITs/MLPs/BDCs/
    Trusts/SPACs are STRUCTURAL non-C-corp buckets (C3.6). These never
    enter the CSP pool regardless of fundamentals, technicals, or RAY.

    Fails CLOSED on missing sector_map entry -- unlike rule_3 (concentration),
    this is a compliance filter where silent bypass is worse than a false
    reject on a data hole. If a candidate reaches the allocator without a
    sector classification, something upstream is broken and we refuse to
    allocate until it's resolved.
    """
    from agt_equities.screener.config import EXCLUDED_SECTORS
    sector_map = extras.get("sector_map", {}) or {}
    candidate_sector = sector_map.get(candidate.ticker.upper(), "")
    excl = {s.lower() for s in EXCLUDED_SECTORS}
    if (candidate_sector or "").strip().lower() in excl:
        return (
            False,
            f"rule_3b excluded sector '{candidate_sector}' "
            f"(EXCLUDED_SECTORS hard filter)",
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
    if hh["household"] != VIKRAM_HOUSEHOLD:
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
    ("vix_acceleration",       _csp_check_vix_acceleration),
    ("rule_3_sector",           _csp_check_rule_3),
    ("rule_3b_excluded_sector", _csp_check_rule_3b),   # hard sector exclusion
    ("rule_4_correlation",      _csp_check_rule_4),
    ("rule_6_vikram_el_floor", _csp_check_rule_6),
    ("rule_7_csp_procedure",   _csp_check_rule_7),
]


def _candidate_evidence(candidate) -> dict:
    """Snapshot candidate's decision-relevant inputs for audit trail."""
    return {
        "ticker": getattr(candidate, "ticker", None),
        "sector": getattr(candidate, "sector", None),
        "delta": getattr(candidate, "delta", None),
        "ivr": getattr(candidate, "ivr", None),
        "dte": getattr(candidate, "dte", None),
        "strike": getattr(candidate, "strike", None),
        "premium": getattr(candidate, "premium", None),
        "earnings_date": getattr(candidate, "earnings_date", None),
    }


# ---------------------------------------------------------------------------
# M1.4: run_csp_allocator orchestrator + AllocatorResult
# ---------------------------------------------------------------------------
#
# Pure orchestrator. Consumes RAY candidates + household snapshots and
# iterates candidates x households, running CSP_GATE_REGISTRY, sizing,
# and routing. Staging flows through ``ctx.order_sink.stage`` (ADR-008).
# Live callers wire a SQLiteOrderSink whose staging_fn is
# append_pending_tickets; shadow_scan wires a CollectorOrderSink. Per-run
# data (sector_map, correlations, delta, etc.) is provided via an
# injected extras_provider, keeping the module free of
# yfinance/DB/correlation-fetch dependencies.
#
# In-memory snapshot mutation between candidates prevents double-booking
# cash when multiple candidates land on the same household.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class AllocatorResult:
    """Result of running the CSP allocator over a set of candidates.

    Fields
    ------
    staged : list of ticket dicts successfully routed + (optionally) persisted
    skipped : list of {household, ticker, reason} for household-level rejects
              (gate failure, sizing=0, all-account veto). Also carries
              `{household: "(pre-allocation)", ticker, reason}` entries
              for candidates dropped by the approval_gate.
    errors : list of {household, ticker, error} for unhandled exceptions
    digest_lines : human-readable Telegram-output lines
    candidate_reasoning : per-candidate observability payload (LOAD-BEARING
        — the future LLM digest tool consumes this as its input shape).
        One entry per INPUT candidate, independent of approval or gate
        outcome. Shape:
          {
            "ticker": str,
            "strike": float,
            "expiry": str,
            "annualized_yield": float,
            "approval_status": "approved" | "rejected",
            "approval_reason": str,        # "" if approved
            "upstream_reasoning": dict,    # copied from candidate.reasoning
                                           # if present, else {}
            "households": [
              {
                "household": str,
                "outcome": "staged" | "skipped" | "error",
                "tickets": int,            # # of tickets created
                "contracts": int,          # total contracts staged
                "reason": str,             # gate/sizing/routing reason
                                           # ("" on staged)
              }, ...
            ],
          }
    """
    staged: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # [{household, ticker, reason}]
    errors: list[dict] = field(default_factory=list)   # [{household, ticker, error}]
    digest_lines: list[str] = field(default_factory=list)
    candidate_reasoning: list[dict] = field(default_factory=list)

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
    *,
    ctx: RunContext,
    approval_gate: Callable[[list], list] | None = None,
) -> AllocatorResult:
    """Run the CSP allocator over a set of candidates.

    The candidate list first passes through `approval_gate` (default =
    identity pass-through for paper mode). Live mode injects a Telegram
    LLM digest gate so only human-approved candidates reach allocation.

    For each approved (candidate, household) pair:
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
      5. Stage tickets via ctx.order_sink.stage. Live mode wires a
         SQLiteOrderSink that forwards to append_pending_tickets; shadow
         mode wires a CollectorOrderSink that captures in memory with
         zero DB writes. See ADR-008 Shadow Scan.
      6. Mutate the in-memory snapshot to reduce buying power /
         cash_available on the affected accounts so the next
         candidate sees the reduced capacity.

    Every INPUT candidate produces one entry in
    result.candidate_reasoning — independent of approval or gate
    outcome — so the future LLM digest tool has a stable audit
    surface to consume as training signal.

    Args:
        ray_candidates: Screener output. Each must conform to the
            CSPCandidate attribute contract (.ticker, .strike, .mid,
            .expiry YYYY-MM-DD, .annualized_yield).
        snapshots: Output of _fetch_household_buying_power_snapshot().
            MUTATED IN PLACE as candidates are allocated — pass a
            copy if the caller wants to preserve the original.
        vix: Current VIX level for Rule 2 scaling.
        extras_provider: Callable that returns the per-(hh, candidate)
            extras dict. Injecting this keeps the orchestrator pure
            of yfinance / DB / correlation-fetch dependencies — the
            caller (cmd_scan in M1.5) supplies a concrete provider.
        ctx: Keyword-only. RunContext carrying the OrderSink and run_id.
            Every staged ticket batch is forwarded to
            ``ctx.order_sink.stage(tickets, engine='csp_allocator',
            run_id=ctx.run_id, meta=...)``. Live callers wire a
            SQLiteOrderSink; shadow_scan wires a CollectorOrderSink.
        approval_gate: Keyword-only. Optional pluggable gate called
            ONCE on the full candidate list before allocation. Must
            return a list (subset of input). Default = identity
            (paper pass-through). Live mode injects Telegram digest.
            Rejected candidates appear in result.skipped with
            household="(pre-allocation)" and in
            candidate_reasoning with approval_status="rejected".

    Returns:
        AllocatorResult with staged, skipped, errors, digest_lines,
        candidate_reasoning.

    Raises:
        Never. All per-candidate errors are caught and logged to
        result.errors so one broken candidate doesn't abort the run.
        A broken `approval_gate` that raises falls back to identity
        (paper-safe default) and logs the error.
    """
    result = AllocatorResult()

    # Q5 (ADR-010 §3.1 #4): WARTIME pre-check.
    # If ALL household accounts are in WARTIME mode, fa_block_margin will veto
    # every candidate at Step 4 anyway. Suppress the approval gate entirely so
    # the operator is never shown a digest for zero-allocation candidates.
    _any_non_wartime = snapshots and any(
        acct.get("mode", "PEACETIME") != "WARTIME"
        for hh in snapshots.values()
        for acct in hh.get("accounts", {}).values()
    )
    if snapshots and not _any_non_wartime:
        logger.info(
            "csp_allocator: all household accounts in WARTIME — "
            "suppressing approval gate, returning empty result"
        )
        return result

    # ── Stage 0: approval gate ──
    # Called once on the full list. Identity default for paper; live
    # mode wires a Telegram digest gate. A broken gate degrades to
    # identity to preserve paper-mode behavior — we'd rather allocate
    # than silently drop the whole scan on a gate bug.
    gate_fn = approval_gate or _default_approval_gate
    try:
        approved = list(gate_fn(list(ray_candidates)))
    except Exception as exc:
        logger.exception(
            "csp_allocator: approval_gate raised — falling back to identity",
        )
        result.errors.append({
            "household": "(approval_gate)",
            "ticker": "*",
            "error": f"{type(exc).__name__}: {exc}",
        })
        approved = list(ray_candidates)

    # Identity-by-membership: anything in the input that is not
    # approved (by object identity) was rejected by the gate.
    approved_ids = {id(c) for c in approved}

    # ── Initialize per-candidate reasoning skeleton for ALL inputs ──
    reasoning_by_id: dict[int, dict] = {}
    for candidate in ray_candidates:
        entry = _init_reasoning_entry(candidate)
        if id(candidate) in approved_ids:
            entry["approval_status"] = "approved"
            entry["approval_reason"] = ""
        else:
            entry["approval_status"] = "rejected"
            entry["approval_reason"] = "approval_gate rejected"
            # Also surface in skipped for Telegram digest visibility
            result.skipped.append({
                "household": "(pre-allocation)",
                "ticker": getattr(candidate, "ticker", "?"),
                "reason": "approval_gate rejected",
            })
        reasoning_by_id[id(candidate)] = entry
        result.candidate_reasoning.append(entry)

    # ── Stage 1+: approved candidates × households ──
    for candidate in approved:
        reasoning = reasoning_by_id[id(candidate)]
        for hh_name, hh in snapshots.items():
            try:
                _process_one(
                    hh, hh_name, candidate, vix,
                    extras_provider, ctx, result,
                    reasoning=reasoning,
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
                reasoning["households"].append({
                    "household": hh_name,
                    "outcome": "error",
                    "tickets": 0,
                    "contracts": 0,
                    "reason": f"{type(exc).__name__}: {exc}",
                })

    result.digest_lines = _format_digest(result)

    # Sprint 4 MR A: fail-soft persist of latest result for csp_digest_send scheduler job.
    # A persistence error must NEVER block the allocation run itself.
    try:
        persist_latest_result(result, run_id=ctx.run_id)
    except Exception as exc:
        logger.warning(
            "csp_allocator.persist_latest_result failed (non-blocking): %s", exc,
        )
    return result


# ---------------------------------------------------------------------------
# Sprint 4 MR A: csp_allocator_latest persistence for digest scheduler job.
# See ADR-CSP_TELEGRAM_DIGEST_v1 §5 step 2 + scripts/migrate_csp_allocator_latest.py.
# ---------------------------------------------------------------------------


def persist_latest_result(
    result: "AllocatorResult",
    *,
    run_id: str,
    trade_date: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Serialize the most recent AllocatorResult into csp_allocator_latest (singleton id=1).

    Called fail-softly at the end of run_csp_allocator. A persistence error is
    swallowed by the caller so a DB hiccup never blocks an allocation run. The
    scheduler's csp_digest_send job reads this row at 09:37 ET and renders the
    digest from it.

    trade_date defaults to today's UTC date. Shape of the persisted blobs is
    intentionally permissive — the digest formatter reconstructs DigestCandidate
    objects from the staged tickets, treating missing fields as defaults.
    """
    import json as _json
    from datetime import datetime, timezone

    td = trade_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")

    staged_payload = _json.dumps(_sanitize_staged_for_persist(result.staged))
    rejected_payload = _json.dumps(_sanitize_rejected_for_persist(result))

    from agt_equities.db import get_db_connection, tx_immediate
    with get_db_connection(db_path=db_path) as conn:
        with tx_immediate(conn):
            conn.execute(
                """
                INSERT INTO csp_allocator_latest
                    (id, run_id, trade_date, staged_json, rejected_json, created_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    run_id = excluded.run_id,
                    trade_date = excluded.trade_date,
                    staged_json = excluded.staged_json,
                    rejected_json = excluded.rejected_json,
                    created_at = excluded.created_at
                """,
                (run_id, td, staged_payload, rejected_payload, created),
            )


def load_latest_result(
    *, db_path: str | Path | None = None,
) -> dict | None:
    """Read the singleton csp_allocator_latest row.

    Returns a dict {run_id, trade_date, staged, rejected, created_at} on hit,
    or None on miss / malformed row.
    """
    import json as _json
    from agt_equities.db import get_db_connection
    with get_db_connection(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT run_id, trade_date, staged_json, rejected_json, created_at "
            "FROM csp_allocator_latest WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    try:
        run_id, trade_date, staged_json, rejected_json, created_at = row
        return {
            "run_id": run_id,
            "trade_date": trade_date,
            "staged": _json.loads(staged_json) if staged_json else [],
            "rejected": _json.loads(rejected_json) if rejected_json else [],
            "created_at": created_at,
        }
    except Exception as exc:
        logger.warning("csp_allocator.load_latest_result: malformed row: %s", exc)
        return None


def _sanitize_staged_for_persist(tickets: list[dict]) -> list[dict]:
    """Strip non-JSON-serializable fields (AllocationDigest objects) from tickets."""
    out: list[dict] = []
    for t in tickets:
        safe = {k: v for k, v in t.items() if k != "_allocation_digest"}
        out.append(safe)
    return out


def _sanitize_rejected_for_persist(result: "AllocatorResult") -> list[dict]:
    """Collect skipped + errors into a uniform rejected-list shape for the digest."""
    rejected: list[dict] = []
    for s in result.skipped:
        rejected.append({
            "ticker": s.get("ticker"),
            "household": s.get("household"),
            "reason": s.get("reason", ""),
            "kind": "skipped",
        })
    for e in result.errors:
        rejected.append({
            "ticker": e.get("ticker"),
            "household": e.get("household"),
            "reason": e.get("error", ""),
            "kind": "error",
        })
    return rejected


# Path import used by the persist helpers above.
from pathlib import Path  # noqa: E402


def _init_reasoning_entry(candidate: Any) -> dict:
    """Build the per-candidate reasoning skeleton.

    Copies `upstream_reasoning` verbatim from candidate.reasoning if
    present - that's how the future LLM digest tool will thread its
    per-candidate rationale into the allocator audit surface.
    """
    upstream = getattr(candidate, "reasoning", None)
    return {
        "ticker": getattr(candidate, "ticker", "?"),
        "strike": float(getattr(candidate, "strike", 0.0) or 0.0),
        "expiry": str(getattr(candidate, "expiry", "") or ""),
        "annualized_yield": float(getattr(candidate, "annualized_yield", 0.0) or 0.0),
        "approval_status": "",
        "approval_reason": "",
        "upstream_reasoning": dict(upstream) if isinstance(upstream, dict) else {},
        "households": [],
    }


# ---------------------------------------------------------------------------
# B5.b helpers: bridge M1.x snapshot dicts to fa_block_margin.CSPProposal
# ---------------------------------------------------------------------------


def _build_csp_proposal(
    n_contracts: int,
    hh_snapshot: dict,
    candidate,
) -> CSPProposal:
    """Construct a CSPProposal from M1.x snapshot + candidate.

    Inherits mode from each account's snapshot (caller-populated).
    margin_eligible flag comes straight from hh_snapshot.accounts.
    """
    accounts = hh_snapshot.get("accounts", {})
    margin_eligible = {
        acct_id: bool(acct.get("margin_eligible", True))
        for acct_id, acct in accounts.items()
    }
    return CSPProposal(
        household_id=hh_snapshot.get("household", "?"),
        ticker=candidate.ticker.upper(),
        strike=float(candidate.strike),
        contracts_requested=n_contracts,
        expiry=candidate.expiry.replace("-", ""),  # YYYYMMDD
        account_ids=list(accounts.keys()),
        margin_eligible=margin_eligible,
        limit_price=float(getattr(candidate, "mid", 0.0)),
        annualized_yield=float(getattr(candidate, "annualized_yield", 0.0)),
    )


def _build_and_allocate(
    n_contracts: int,
    hh_snapshot: dict,
    candidate,
    *,
    db_path=None,
) -> AllocationDigest:
    """Build CSPProposal + call allocate_csp with cash_snapshot from M1.x.

    Cash accounts get their cash_available from the M1.x snapshot.
    Margin accounts use v_available_nlv view (DT Q4 invariant).
    """
    proposal = _build_csp_proposal(n_contracts, hh_snapshot, candidate)
    cash_snapshot = {
        acct_id: float(acct.get("cash_available", 0.0))
        for acct_id, acct in hh_snapshot.get("accounts", {}).items()
        if not acct.get("margin_eligible", True)
    }
    return allocate_csp(
        proposal,
        db_path=db_path,
        cash_snapshot=cash_snapshot,
    )


def _tickets_from_digest(
    digest: AllocationDigest,
    hh_snapshot: dict,
    candidate,
) -> list[dict]:
    """Convert approved AccountAllocations to M1.x ticket dicts."""
    # Default to empty string (NOT "paper") so an unset AGT_BROKER_MODE
    # falls into is_csp_active_account's unknown-mode-fails-closed branch
    # instead of silently bypassing the dormant filter on live capital.
    # Bug E-H-2 from opus_bug_hunt_overnight.md: the prior "paper" default
    # was introduced in MR !200; on a live system with the env var unset,
    # CSP entries on dormant accounts (e.g., U22076184 Yash Trad IRA) would
    # have routed through the gate.
    broker_mode = os.environ.get("AGT_BROKER_MODE", "")
    tickets: list[dict] = []
    for alloc in digest.allocations:
        if alloc.margin_check_status != STATUS_APPROVED:
            continue
        if alloc.contracts_allocated < 1:
            continue
        if not is_csp_active_account(alloc.account_id, broker_mode):
            logger.info(
                "csp_allocator.skip_inactive account=%s mode=%s reason=not_in_CSP_ACTIVE_ACCOUNTS",
                alloc.account_id,
                broker_mode,
            )
            continue
        tickets.append({
            "account_id": alloc.account_id,
            "household": hh_snapshot.get("household", "?"),
            "ticker": candidate.ticker.upper(),
            "action": "SELL",
            "sec_type": "OPT",
            "right": "P",
            "strike": float(candidate.strike),
            "expiry": candidate.expiry.replace("-", ""),
            "quantity": alloc.contracts_allocated,
            "limit_price": float(getattr(candidate, "bid", candidate.mid)),
            "annualized_yield": float(candidate.annualized_yield),
            "mode": "CSP_ENTRY",
            "status": "staged",
            "delta": float(getattr(candidate, "delta", 0.0) or 0.0),
            "inception_delta": float(getattr(candidate, "delta", 0.0) or 0.0),
            "otm_pct": float(getattr(candidate, "otm_pct", 0.0) or 0.0),
            "spot": float(getattr(candidate, "current_price", 0.0) or 0.0),
        })
    return tickets


def _process_one(
    hh: dict,
    hh_name: str,
    candidate: Any,
    vix: float,
    extras_provider: Callable,
    ctx: RunContext,
    result: AllocatorResult,
    *,
    reasoning: dict | None = None,
) -> None:
    """Process one (household, candidate) pair. Mutates hh + result.

    `reasoning` is the per-candidate dict from
    `result.candidate_reasoning`. When provided, a per-household
    outcome row is appended so the future LLM digest tool can audit
    which households took vs. rejected each candidate and why.
    Passing None preserves backwards compatibility for any legacy
    direct callers.
    """
    def _record(outcome: str, reason: str, tickets: int = 0, contracts: int = 0) -> None:
        if reasoning is not None:
            reasoning["households"].append({
                "household": hh_name,
                "outcome": outcome,
                "tickets": tickets,
                "contracts": contracts,
                "reason": reason,
            })

    # Step 1: build extras
    extras = extras_provider(hh, candidate) or {}

    # Step 2: run gates in registry order, short-circuit on first fail
    # Gate check uses n=1 as a feasibility probe; real sizing is below.
    # This keeps gate semantics "can this household take ANY contracts"
    # rather than "can it take N specific contracts."
    verdicts: list[dict] = []
    for gate_name, gate_fn in CSP_GATE_REGISTRY:
        passed, reason = gate_fn(hh, candidate, 1, vix, extras)
        verdicts.append({"gate": gate_name, "ok": passed, "reason": reason})
        if not passed:
            csp_decisions_repo.record_decision(
                run_id=ctx.run_id,
                household_id=hh_name,
                ticker=candidate.ticker,
                final_outcome=f"rejected_by_{gate_name}",
                gate_verdicts=verdicts,
                evidence_snapshot=_candidate_evidence(candidate),
                n_requested=None,
                n_sized=None,
                db_path=ctx.db_path,
            )
            result.skipped.append({
                "household": hh_name,
                "ticker": candidate.ticker,
                "reason": f"{gate_name}: {reason}",
            })
            _record("skipped", f"{gate_name}: {reason}")
            return

    # Step 3: size at household level
    n_contracts = _csp_size_household(hh, candidate, vix)
    if n_contracts < 1:
        sizing_reason = (
            "sizing returned 0 "
            "(sub-integer at 10% target or ceiling breach)"
        )
        result.skipped.append({
            "household": hh_name,
            "ticker": candidate.ticker,
            "reason": sizing_reason,
        })
        _record("skipped", sizing_reason)
        return

    # Step 4: route via fa_block_margin allocator (B5.b).
    # allocate_csp handles cash/margin split, per-account mode gate,
    # view-backed margin veto. Returns AllocationDigest — ground truth
    # for staging + mutation. Dropped accounts surface in digest, not
    # here (result.skipped is for household-level candidate rejection).
    alloc_digest = _build_and_allocate(n_contracts, hh, candidate)
    tickets = _tickets_from_digest(alloc_digest, hh, candidate)
    if not tickets:
        # All accounts vetoed (mode-blocked, no-snapshot, insufficient).
        # Surface the digest in result.skipped so the outer digest has
        # full context without burying per-account reasons.
        reason = (
            f"allocator vetoed all {len(alloc_digest.allocations)} accounts"
            if alloc_digest.allocations
            else "no accounts in household"
        )
        result.skipped.append({
            "household": hh_name,
            "ticker": candidate.ticker,
            "reason": reason,
            "allocation_digest": alloc_digest,
        })
        _record("skipped", reason)
        return

    # Step 5: stage via ctx.order_sink.
    # Live mode: SQLiteOrderSink.stage forwards ``tickets`` to
    # append_pending_tickets positionally - byte-identical to the prior
    # direct callback path. Shadow mode: CollectorOrderSink.stage buffers
    # ShadowOrder entries in memory and never touches pending_orders.

    # ADR-020 ticket contract — staging evidence written before order_sink.stage()
    # so append_pending_tickets serialization captures it into pending_orders.payload.
    from datetime import datetime, timezone as _tz
    _staged_at_iso = datetime.now(_tz.utc).isoformat()
    for _t in tickets:
        _cp = getattr(candidate, "current_price", None)
        _t["spot_at_staging"] = float(_cp) if _cp else None
        _t["premium_at_staging"] = float(candidate.mid) if candidate.mid else None
        _t["staged_at_utc"] = _staged_at_iso
        _t["broker_mode_at_staging"] = ctx.broker_mode

    try:
        ctx.order_sink.stage(
            tickets,
            engine="csp_allocator",
            run_id=ctx.run_id,
            meta={
                "household": hh_name,
                "ticker": candidate.ticker.upper(),
                "n_contracts": sum(t.get("quantity", 0) for t in tickets),
            },
        )
    except Exception as exc:
        logger.warning(
            "csp_allocator: order_sink.stage failed for %s/%s: %s",
            hh_name, candidate.ticker, exc,
        )
        result.errors.append({
            "household": hh_name,
            "ticker": candidate.ticker,
            "error": f"staging failed: {exc}",
        })
        _record("error", f"staging failed: {exc}")
        return

    # Attach the allocator digest to each ticket for _format_digest.
    for t in tickets:
        t["_allocation_digest"] = alloc_digest
    result.staged.extend(tickets)
    total_contracts = sum(t["quantity"] for t in tickets)
    _record("staged", "", tickets=len(tickets), contracts=total_contracts)

    # Audit trail for staged candidate (fail-open; record_decision swallows
    # sqlite errors at WARNING). Enrich ticket payload with gate_verdicts
    # so pending_orders.payload carries the full decision trace.
    for t in tickets:
        t["gate_verdicts"] = verdicts
    csp_decisions_repo.record_decision(
        run_id=ctx.run_id,
        household_id=hh_name,
        ticker=candidate.ticker,
        final_outcome="staged",
        gate_verdicts=verdicts,
        evidence_snapshot=_candidate_evidence(candidate),
        n_requested=None,
        n_sized=n_contracts,
        db_path=ctx.db_path,
    )

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
        # B5.b: per-candidate allocator detail (deduped by digest identity)
        seen_digests: set[int] = set()
        for t in result.staged:
            d = t.get("_allocation_digest")
            if d is None or id(d) in seen_digests:
                continue
            seen_digests.add(id(d))
            if d.dropped_accounts:
                lines.append("")
                lines.append(format_allocation_digest(d))
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
