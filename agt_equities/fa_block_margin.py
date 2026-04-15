"""
agt_equities.fa_block_margin — Sprint B5 CSP Allocator pre-stage library.

Purpose
-------
IBKR rejects the *entire* FA parent block if any child account lacks
margin (Blind Spot #1 — FA Block Margin Contagion). This module performs
local margin math BEFORE the parent block is staged so that underfunded
accounts are dropped ahead of `placeOrder`, preserving the survivors.

B5.b integration
----------------
Replaces M1.x `_csp_route_to_accounts` as the canonical per-account router.
Two-phase allocation:

  Phase 1 — cash (IRA) accounts: greedy fill by cash_available desc.
    IRA-first invariant from M1.x. No margin-view lookup (IRA accounts
    don't have IBKR excess_liquidity semantics). No FA-block contagion
    risk (cash collateral is always isolated per-account).

  Phase 2 — margin accounts: pro-rata on residual contracts, NLV-desc.
    DT Q4 invariant: each margin account gets contracts_requested / n
    base share, remainder to NLV-largest. Per-account WARTIME mode gate
    inline (Act 60 principal vs advisory nuance). Partial allocation
    when affordable < pro_rata (no peer forfeit).

DT Q4 ruling invariants encoded here:
  * NLV source for margin accounts = `el_snapshots` via `v_available_nlv`.
  * Available capital = IBKR `excess_liquidity` (netted by portfolio
    margin — do NOT re-derive in SQL, box spreads double-count).
  * Traversal: cash-first greedy (M1.x), margin pro-rata NLV-desc (DT Q4).
  * Partial allocation — allocate what fits, surface residual in digest.
  * Per-household mode gate evaluated INSIDE the loop, not at entry.
  * Aggregate digest failure surfacing — fold-together approved/dropped.
  * Mode-blocked / no-snapshot accounts do NOT forfeit their share to
    peers (DT Q4 non-redistribution invariant).

Scope (B5.b MR)
---------------
Pure library + tests. No live `accountSummaryAsync` refresh (deferred
to B5.c). No writes to `pending_order_children.margin_check_*` (B5.c).

ZERO I/O at import. Only runtime dep: `agt_equities.db.get_ro_connection`
(lazy-imported inside `_fetch_available_nlv`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Mode string constants — match agt_equities.mode_engine vocabulary.
MODE_PEACETIME = "PEACETIME"
MODE_AMBER = "AMBER"
MODE_WARTIME = "WARTIME"

# Margin-check outcome codes persisted to pending_order_children by B5.c
# and rendered by format_allocation_digest today.
STATUS_APPROVED = "approved"
STATUS_INSUFFICIENT_NLV = "insufficient_nlv"
STATUS_INSUFFICIENT_CASH = "insufficient_cash"
STATUS_MODE_BLOCKED = "mode_blocked"
STATUS_NO_SNAPSHOT = "no_snapshot"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CSPProposal:
    """A single proposed CSP block pending allocation across accounts.

    Fields
    ------
    household_id : str
    ticker : str
    strike : float
    contracts_requested : int
        Total contracts across all accounts in the household.
    expiry : str
        ISO date or YYYYMMDD. Display + ticket construction.
    mode_gate_accounts : Mapping[str, str]
        account_id -> mode (one of MODE_*). Per-account mode lookup.
    margin_eligible : Mapping[str, bool]
        account_id -> True for margin account, False for cash/IRA.
        Absent or empty dict → treat all accounts as margin
        (preserves pre-B5.b behavior for tests and callers).
    limit_price : float
        Option mid for ticket construction downstream (M1.x tickets).
    annualized_yield : float
        For digest rendering context; not used in allocation math.
    """

    household_id: str
    ticker: str
    strike: float
    contracts_requested: int
    expiry: str
    mode_gate_accounts: Mapping[str, str]
    margin_eligible: Mapping[str, bool] = field(default_factory=dict)
    limit_price: float = 0.0
    annualized_yield: float = 0.0


@dataclass(frozen=True)
class AccountAllocation:
    """Per-account outcome after margin + mode checks."""

    account_id: str
    contracts_allocated: int
    margin_check_status: str  # one of STATUS_*
    margin_check_reason: str
    available_nlv: float | None  # None for cash accts or no-snapshot


@dataclass(frozen=True)
class AllocationDigest:
    """Aggregate allocator result for digest surfacing."""

    proposal: CSPProposal
    allocations: tuple[AccountAllocation, ...]
    total_contracts_requested: int
    total_contracts_allocated: int
    dropped_accounts: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # (account_id, reason) — everything not STATUS_APPROVED


# ---------------------------------------------------------------------------
# Core allocator helpers
# ---------------------------------------------------------------------------


def _contracts_affordable(available: float, strike: float) -> int:
    """Floor(available / (strike * 100)). Non-negative.

    Strike * 100 is cash-collateral / margin requirement per contract
    for an equity CSP. Non-standard multipliers (index options, futures)
    are out of scope — Wheel runs on equity underliers only.
    """
    if strike <= 0:
        return 0
    per_contract = strike * 100.0
    if available <= 0:
        return 0
    return int(available // per_contract)


def _fetch_available_nlv(
    account_ids: list[str],
    *,
    db_path: str | Path | None = None,
) -> dict[str, float]:
    """Query v_available_nlv for the given margin account_ids.

    Returns {account_id: available_nlv}. Accounts with no matching row
    (no recent el_snapshot) are absent from the result — allocator
    treats absence as STATUS_NO_SNAPSHOT.

    Empty account_ids → returns {} without hitting DB.
    Connection failure re-raises — caller decides halt vs proceed.
    """
    if not account_ids:
        return {}

    from agt_equities.db import get_ro_connection

    placeholders = ",".join("?" for _ in account_ids)
    sql = (
        "SELECT account_id, available_nlv "
        "FROM v_available_nlv "
        f"WHERE account_id IN ({placeholders})"
    )
    conn = get_ro_connection(db_path=db_path)
    try:
        rows = conn.execute(sql, list(account_ids)).fetchall()
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover — best-effort close
            pass

    out: dict[str, float] = {}
    for row in rows:
        try:
            acct = row["account_id"]
            nlv = row["available_nlv"]
        except (IndexError, TypeError):
            acct, nlv = row[0], row[1]
        if nlv is not None:
            out[acct] = float(nlv)
    return out


def _is_margin(proposal: CSPProposal, acct: str) -> bool:
    """Account is margin-eligible unless explicitly flagged False.

    Empty margin_eligible map → all-margin (pre-B5.b behavior).
    """
    if not proposal.margin_eligible:
        return True
    return bool(proposal.margin_eligible.get(acct, True))


def allocate_csp(
    proposal: CSPProposal,
    *,
    db_path: str | Path | None = None,
    available_nlv_override: Mapping[str, float] | None = None,
    cash_snapshot: Mapping[str, float] | None = None,
) -> AllocationDigest:
    """Allocate a CSP proposal across cash + margin accounts.

    Phase 1 (cash accounts, greedy):
        Sort by cash_available desc. Mode gate inline. Fill greedily.
        WARTIME → STATUS_MODE_BLOCKED. Zero cash → STATUS_INSUFFICIENT_CASH.

    Phase 2 (margin accounts, pro-rata):
        Residual = contracts_requested - cash_allocated.
        base_share, remainder = divmod(residual, n_margin_accounts).
        Sort margin accts NLV-desc (no-snapshot sorts last).
        idx=0 gets base+remainder; others get base.
        Per-account WARTIME → blocked. No view row → no_snapshot.
        affordable >= pro_rata → approved. affordable > 0 → partial
        (STATUS_INSUFFICIENT_NLV, take affordable). affordable == 0 →
        insufficient, take 0.

    Neither phase redistributes a dropped account's share — DT Q4
    non-forfeit invariant (WARTIME/missing peer doesn't enlarge survivors).

    Parameters
    ----------
    proposal : CSPProposal
    db_path : optional, shared RO connection path.
    available_nlv_override : optional, bypasses v_available_nlv lookup.
        Tests and B5.c pre-fetched paths use this.
    cash_snapshot : optional, account_id -> cash_available for cash
        accounts. Absent account → 0 cash. Unused for margin accounts.
    """
    accounts = list(proposal.mode_gate_accounts.keys())

    if not accounts or proposal.contracts_requested <= 0:
        return AllocationDigest(
            proposal=proposal,
            allocations=(),
            total_contracts_requested=proposal.contracts_requested,
            total_contracts_allocated=0,
            dropped_accounts=(),
        )

    cash_map = dict(cash_snapshot) if cash_snapshot else {}
    cash_accounts = [a for a in accounts if not _is_margin(proposal, a)]
    margin_accounts = [a for a in accounts if _is_margin(proposal, a)]

    strike = proposal.strike
    per_contract = strike * 100.0
    allocations: list[AccountAllocation] = []
    dropped: list[tuple[str, str]] = []
    total_allocated = 0
    remaining = proposal.contracts_requested

    # ---------- Phase 1: cash accounts, greedy desc ----------
    cash_accounts.sort(key=lambda a: cash_map.get(a, 0.0), reverse=True)
    for acct in cash_accounts:
        mode = proposal.mode_gate_accounts[acct]
        if mode == MODE_WARTIME:
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=0,
                margin_check_status=STATUS_MODE_BLOCKED,
                margin_check_reason="WARTIME — LLM CSP entry blocked per 3-mode state machine",
                available_nlv=None,
            )
            allocations.append(alloc)
            dropped.append((acct, alloc.margin_check_reason))
            continue

        cash = cash_map.get(acct, 0.0)
        affordable = _contracts_affordable(cash, strike)
        if remaining <= 0:
            # No residual demand — show as dropped-insufficient w/ zero.
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=0,
                margin_check_status=STATUS_INSUFFICIENT_CASH,
                margin_check_reason="no remaining contracts to allocate",
                available_nlv=None,
            )
            allocations.append(alloc)
            # Not "dropped" in the failure sense — don't append to dropped.
            continue

        if affordable <= 0:
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=0,
                margin_check_status=STATUS_INSUFFICIENT_CASH,
                margin_check_reason=(
                    f"cash: available=${cash:,.0f} "
                    f"covers 0 of {remaining} remaining at strike ${strike:,.2f}"
                ),
                available_nlv=None,
            )
            allocations.append(alloc)
            dropped.append((acct, alloc.margin_check_reason))
            continue

        take = min(remaining, affordable)
        alloc = AccountAllocation(
            account_id=acct,
            contracts_allocated=take,
            margin_check_status=STATUS_APPROVED,
            margin_check_reason=(
                f"cash: available=${cash:,.0f} "
                f"covers {take}x (required=${take * per_contract:,.0f})"
            ),
            available_nlv=None,
        )
        allocations.append(alloc)
        total_allocated += take
        remaining -= take

    # ---------- Phase 2: margin accounts, pro-rata on residual ----------
    if margin_accounts and remaining > 0:
        if available_nlv_override is not None:
            nlv_map = {a: float(v) for a, v in available_nlv_override.items()}
        else:
            nlv_map = _fetch_available_nlv(margin_accounts, db_path=db_path)

        def _nlv_key(acct: str) -> float:
            return nlv_map.get(acct, float("-inf"))

        margin_accounts.sort(key=_nlv_key, reverse=True)

        n_margin = len(margin_accounts)
        base_share, remainder = divmod(remaining, n_margin)

        for idx, acct in enumerate(margin_accounts):
            mode = proposal.mode_gate_accounts[acct]
            pro_rata = base_share + (remainder if idx == 0 else 0)

            if mode == MODE_WARTIME:
                alloc = AccountAllocation(
                    account_id=acct,
                    contracts_allocated=0,
                    margin_check_status=STATUS_MODE_BLOCKED,
                    margin_check_reason="WARTIME — LLM CSP entry blocked per 3-mode state machine",
                    available_nlv=nlv_map.get(acct),
                )
                allocations.append(alloc)
                dropped.append((acct, alloc.margin_check_reason))
                continue

            if acct not in nlv_map:
                alloc = AccountAllocation(
                    account_id=acct,
                    contracts_allocated=0,
                    margin_check_status=STATUS_NO_SNAPSHOT,
                    margin_check_reason="no recent el_snapshot in v_available_nlv",
                    available_nlv=None,
                )
                allocations.append(alloc)
                dropped.append((acct, alloc.margin_check_reason))
                continue

            available = nlv_map[acct]
            affordable = _contracts_affordable(available, strike)

            if pro_rata <= 0:
                # Residual insufficient for integer share — mark zero.
                alloc = AccountAllocation(
                    account_id=acct,
                    contracts_allocated=0,
                    margin_check_status=STATUS_INSUFFICIENT_NLV,
                    margin_check_reason=(
                        f"margin: available=${available:,.0f} "
                        f"pro_rata share is 0 (residual {remaining} < n_margin {n_margin})"
                    ),
                    available_nlv=available,
                )
                allocations.append(alloc)
                dropped.append((acct, alloc.margin_check_reason))
                continue

            if affordable >= pro_rata:
                alloc = AccountAllocation(
                    account_id=acct,
                    contracts_allocated=pro_rata,
                    margin_check_status=STATUS_APPROVED,
                    margin_check_reason=(
                        f"margin: available=${available:,.0f} "
                        f"required=${pro_rata * per_contract:,.0f}"
                    ),
                    available_nlv=available,
                )
                allocations.append(alloc)
                total_allocated += pro_rata
            elif affordable > 0:
                # Partial — take what fits.
                alloc = AccountAllocation(
                    account_id=acct,
                    contracts_allocated=affordable,
                    margin_check_status=STATUS_INSUFFICIENT_NLV,
                    margin_check_reason=(
                        f"margin partial: available=${available:,.0f} "
                        f"covers {affordable} of {pro_rata} pro_rata "
                        f"(required=${pro_rata * per_contract:,.0f})"
                    ),
                    available_nlv=available,
                )
                allocations.append(alloc)
                total_allocated += affordable
                dropped.append((acct, alloc.margin_check_reason))
            else:
                alloc = AccountAllocation(
                    account_id=acct,
                    contracts_allocated=0,
                    margin_check_status=STATUS_INSUFFICIENT_NLV,
                    margin_check_reason=(
                        f"margin: available=${available:,.0f} "
                        f"covers 0 of {pro_rata} pro_rata at strike ${strike:,.2f}"
                    ),
                    available_nlv=available,
                )
                allocations.append(alloc)
                dropped.append((acct, alloc.margin_check_reason))

    return AllocationDigest(
        proposal=proposal,
        allocations=tuple(allocations),
        total_contracts_requested=proposal.contracts_requested,
        total_contracts_allocated=total_allocated,
        dropped_accounts=tuple(dropped),
    )


# ---------------------------------------------------------------------------
# Digest rendering (aggregate surfacing — DT Q4 invariant #4)
# ---------------------------------------------------------------------------


def format_allocation_digest(digest: AllocationDigest) -> str:
    """Render a single human-readable digest string.

    Fold-together format — one Approved block (cash + margin mixed,
    per-account reason text distinguishes), one Dropped block.

        CSP Allocator — {household_id}/{ticker} {contracts}x@${strike} {expiry}
        Requested: {requested} | Allocated: {allocated} | Dropped: {dropped_count}
        Approved:
          - {acct}: {contracts}x ({reason})
          ...
        Dropped:
          - {acct}: {reason}
          ...

    Tolerant of empty allocations, zero approved, zero dropped.
    Never raises on well-formed digest input.
    """
    p = digest.proposal
    lines: list[str] = []
    lines.append(
        f"CSP Allocator — {p.household_id}/{p.ticker} "
        f"{p.contracts_requested}x@${p.strike:,.2f} {p.expiry}"
    )
    lines.append(
        f"Requested: {digest.total_contracts_requested} | "
        f"Allocated: {digest.total_contracts_allocated} | "
        f"Dropped: {len(digest.dropped_accounts)}"
    )

    approved = [a for a in digest.allocations if a.margin_check_status == STATUS_APPROVED]
    if approved:
        lines.append("Approved:")
        for a in approved:
            lines.append(
                f"  - {a.account_id}: {a.contracts_allocated}x ({a.margin_check_reason})"
            )

    non_approved = [
        a for a in digest.allocations if a.margin_check_status != STATUS_APPROVED
    ]
    if non_approved:
        lines.append("Dropped:")
        for a in non_approved:
            lines.append(f"  - {a.account_id}: {a.margin_check_reason}")

    return "\n".join(lines)
