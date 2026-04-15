"""
agt_equities.fa_block_margin — Sprint B5 FA-block margin pre-stage library.

Purpose
-------
IBKR rejects the *entire* FA parent block if any child account lacks
margin (Blind Spot #1 — FA Block Margin Contagion). This module performs
local margin math BEFORE the parent block is staged so that underfunded
accounts are dropped ahead of `placeOrder`, preserving the survivors.

DT Q4 ruling invariants encoded here:
  * NLV source = `el_snapshots` (latest row per account).
  * Available capital = IBKR `excess_liquidity` (exposed via the
    `v_available_nlv` view). NOT a re-derived CSP-collateral sum — IBKR's
    portfolio-margin engine already nets box spreads, credit spreads, CC
    assigned-share coverage, and cross-position offsets. Re-deriving in
    SQL double-counts everything (e.g. synthetic financing boxes that
    show up as 13× short SPX puts but are margin-flat in IBKR's view).
  * Traversal = NLV-descending (largest accounts first — maximizes
    coverage when aggregate capital is tight).
  * Partial allocation — if an account only covers N of its pro-rata share,
    allocate N and keep going rather than dropping it entirely.
  * Per-household mode gate evaluated INSIDE the loop, not at entry.
    Act 60 principal accounts can be WARTIME while advisory clients are
    PEACETIME; one WARTIME account must not veto the whole block.
  * Aggregate digest failure surfacing — single structured digest
    per allocation summarizing approved + dropped accounts with reasons
    (caller renders + delivers; this module only returns the digest).

Scope (this MR)
---------------
Pure library + tests. No wiring into `_place_single_order` /
`_stage_csp_fa_block` yet; that's the follow-on MR (B5.b) after paper-run
review. No writes to `pending_order_children.margin_check_*` columns yet
(B5.b territory).

This module does ZERO I/O at import (no telegram/ib_async imports). The
only runtime dependency is `agt_equities.db.get_ro_connection` for the
view query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Mode string constants — match agt_equities.mode_engine vocabulary.
MODE_PEACETIME = "PEACETIME"
MODE_AMBER = "AMBER"
MODE_WARTIME = "WARTIME"

# Margin-check outcome codes persisted to pending_order_children by B5.b
# and rendered by format_allocation_digest today.
STATUS_APPROVED = "approved"
STATUS_INSUFFICIENT_NLV = "insufficient_nlv"
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
        Logical household the proposal belongs to (display only — real
        mode gating lives in mode_gate_accounts per-account).
    ticker : str
    strike : float
    contracts_requested : int
        Total contracts across all accounts; per-account share is
        `contracts_requested // len(mode_gate_accounts)` rounded down,
        with remainder distributed to the NLV-largest account.
    expiry : str
        ISO date or YYYYMMDD. Display only at this layer.
    mode_gate_accounts : Mapping[str, str]
        account_id -> mode (one of MODE_*). Per-account mode lookup is
        the caller's responsibility — allocator treats this dict as
        ground truth.
    """

    household_id: str
    ticker: str
    strike: float
    contracts_requested: int
    expiry: str
    mode_gate_accounts: Mapping[str, str]


@dataclass(frozen=True)
class AccountAllocation:
    """Per-account outcome after margin + mode checks."""

    account_id: str
    contracts_allocated: int
    margin_check_status: str  # one of STATUS_*
    margin_check_reason: str
    available_nlv: float | None  # None if no v_available_nlv row


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
# Core allocator
# ---------------------------------------------------------------------------


def _contracts_affordable(available_nlv: float, strike: float) -> int:
    """Floor(available / (strike * 100)). Non-negative.

    Strike * 100 is the cash-collateral requirement per contract for
    an equity CSP. Non-standard multipliers (index options, futures)
    are out of scope for v1 — the Wheel strategy runs on equity
    underliers only.
    """
    if strike <= 0:
        return 0
    per_contract = strike * 100.0
    if available_nlv <= 0:
        return 0
    return int(available_nlv // per_contract)


def _fetch_available_nlv(
    account_ids: list[str],
    *,
    db_path: str | Path | None = None,
) -> dict[str, float]:
    """Query v_available_nlv for the given account_ids.

    Returns {account_id: available_nlv}. Accounts with no matching row
    (no recent el_snapshot) are absent from the result — the allocator
    treats absence as STATUS_NO_SNAPSHOT.

    Defensive against: view not yet created (OperationalError), empty
    account_ids list (returns {} without hitting DB), connection failure
    (re-raises — caller decides halt vs proceed-with-zero).
    """
    if not account_ids:
        return {}

    # Late import so this module is safe to import in test contexts
    # that don't have a DB stack wired up yet.
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
        # Support both sqlite3.Row and plain tuples.
        try:
            acct = row["account_id"]
            nlv = row["available_nlv"]
        except (IndexError, TypeError):
            acct, nlv = row[0], row[1]
        if nlv is not None:
            out[acct] = float(nlv)
    return out


def allocate_csp(
    proposal: CSPProposal,
    *,
    db_path: str | Path | None = None,
    available_nlv_override: Mapping[str, float] | None = None,
) -> AllocationDigest:
    """Allocate a CSP proposal across the accounts in its mode-gate map.

    Parameters
    ----------
    proposal : CSPProposal
    db_path : optional, passed to shared RO connection.
    available_nlv_override : optional, bypasses the v_available_nlv query
        entirely. Used in tests and by B5.b when the caller already has
        NLV in hand from a prior lookup.

    Returns
    -------
    AllocationDigest

    Behavior
    --------
    1. Fetch available_nlv per account (view lookup unless override).
    2. Sort accounts NLV-descending. Accounts with no v_available_nlv
       row sort last (treated as 0.0 for ordering, flagged NO_SNAPSHOT).
    3. Compute per-account pro-rata share. Remainder (from integer
       division) goes to the NLV-largest account.
    4. Per-account loop:
         a. If mode == WARTIME → 0 contracts, STATUS_MODE_BLOCKED.
         b. Else if no NLV snapshot → 0 contracts, STATUS_NO_SNAPSHOT.
         c. Else compute affordable contracts at strike. If affordable
            >= pro-rata share → STATUS_APPROVED, allocate pro-rata.
            If affordable > 0 but < pro-rata → STATUS_INSUFFICIENT_NLV
            with partial allocation. If affordable == 0 →
            STATUS_INSUFFICIENT_NLV with 0 contracts.
    5. Build digest; `dropped_accounts` = every non-APPROVED account.
    """
    accounts = list(proposal.mode_gate_accounts.keys())

    if available_nlv_override is not None:
        nlv_map = {a: float(v) for a, v in available_nlv_override.items()}
    else:
        try:
            nlv_map = _fetch_available_nlv(accounts, db_path=db_path)
        except Exception:
            # If the view lookup itself fails, we cannot make margin
            # decisions. Fail loud — better to halt than to stage a
            # block against stale/empty data.
            raise

    # NLV-descending sort; no-snapshot accounts sort last with NLV = -inf
    # sentinel so they're traversed after every funded account.
    def _sort_key(acct: str) -> float:
        return nlv_map.get(acct, float("-inf"))

    sorted_accounts = sorted(accounts, key=_sort_key, reverse=True)

    n = len(sorted_accounts)
    if n == 0:
        return AllocationDigest(
            proposal=proposal,
            allocations=(),
            total_contracts_requested=proposal.contracts_requested,
            total_contracts_allocated=0,
            dropped_accounts=(),
        )

    base_share, remainder = divmod(proposal.contracts_requested, n)

    allocations: list[AccountAllocation] = []
    dropped: list[tuple[str, str]] = []
    total_allocated = 0

    for idx, acct in enumerate(sorted_accounts):
        mode = proposal.mode_gate_accounts[acct]
        # Remainder goes to NLV-largest account (index 0 after sort desc).
        pro_rata = base_share + (remainder if idx == 0 else 0)

        # Mode gate — evaluated per-account.
        if mode == MODE_WARTIME:
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=0,
                margin_check_status=STATUS_MODE_BLOCKED,
                margin_check_reason=(
                    f"WARTIME — LLM CSP entry blocked per 3-mode state machine"
                ),
                available_nlv=nlv_map.get(acct),
            )
            allocations.append(alloc)
            dropped.append((acct, alloc.margin_check_reason))
            continue

        # No-snapshot gate.
        if acct not in nlv_map:
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=0,
                margin_check_status=STATUS_NO_SNAPSHOT,
                margin_check_reason=(
                    "no recent el_snapshot in v_available_nlv"
                ),
                available_nlv=None,
            )
            allocations.append(alloc)
            dropped.append((acct, alloc.margin_check_reason))
            continue

        available = nlv_map[acct]
        affordable = _contracts_affordable(available, proposal.strike)

        if affordable >= pro_rata and pro_rata > 0:
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=pro_rata,
                margin_check_status=STATUS_APPROVED,
                margin_check_reason=(
                    f"available=${available:,.0f} "
                    f"required=${pro_rata * proposal.strike * 100:,.0f}"
                ),
                available_nlv=available,
            )
            allocations.append(alloc)
            total_allocated += pro_rata
        elif affordable > 0:
            # Partial allocation — take what the account can cover.
            alloc = AccountAllocation(
                account_id=acct,
                contracts_allocated=affordable,
                margin_check_status=STATUS_INSUFFICIENT_NLV,
                margin_check_reason=(
                    f"partial: available=${available:,.0f} "
                    f"covers {affordable} of {pro_rata} requested "
                    f"(required=${pro_rata * proposal.strike * 100:,.0f})"
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
                    f"available=${available:,.0f} "
                    f"covers 0 of {pro_rata} requested at strike ${proposal.strike:,.2f}"
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

    Shape (one Telegram message per allocation, not N per account):

        CSP Allocator — {household_id}/{ticker} {contracts}x@${strike} {expiry}
        Requested: {requested} | Allocated: {allocated} | Dropped: {dropped_count}
        Approved:
          - {acct}: {contracts}x (available=${nlv})
          ...
        Dropped:
          - {acct}: {reason}
          ...

    Tolerant of: empty allocations, zero approved, zero dropped,
    None available_nlv. Never raises on well-formed digest input.
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
            nlv_str = (
                f"available=${a.available_nlv:,.0f}"
                if a.available_nlv is not None
                else "available=n/a"
            )
            lines.append(
                f"  - {a.account_id}: {a.contracts_allocated}x ({nlv_str})"
            )

    non_approved = [
        a for a in digest.allocations if a.margin_check_status != STATUS_APPROVED
    ]
    if non_approved:
        lines.append("Dropped:")
        for a in non_approved:
            lines.append(f"  - {a.account_id}: {a.margin_check_reason}")

    return "\n".join(lines)
