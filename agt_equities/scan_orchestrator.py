"""agt_equities.scan_orchestrator — unified CSP scan entry point.

Collapses cmd_scan (/scan) + _scheduled_csp_scan (09:35 ET autonomous) +
dev_cli scan-daily onto one two-phase pipeline:

    Phase 1 (async):  build_scan_inputs(trigger, ctx) -> ScanInputs
        Pulls watchlist + screener output + IBKR snapshots + bridge-2 extras.
        Pure gather, no allocation, no approval.

    Approval boundary: if approval_policy.needs_csp_approval(ctx) is True,
        route the screener-sorted candidate universe to Telegram digest,
        await operator response as frozenset[int] of indices.

    Phase 2 (sync):   allocate_candidates(approved, inputs, ctx) -> AllocatorResult
        Runs the existing run_csp_allocator with approval_gate=None
        (approval already applied upstream) on the approved subset.

Codex design ruling 2026-04-20 (Option 1 — orchestrator between phases):
    The allocator stays approval-unaware. Approval mutates the candidate
    universe before allocation, not during it. Ordering is preserved by
    filtering the screener-sorted universe through frozenset[int] indices.

Policy invariants (do not change without ADR update):
    - broker_mode="paper"  -> auto-approve all candidates
    - broker_mode="live" + engine="csp"  -> Telegram digest + operator yes/no
    - Any other engine on live -> auto-approve (not gated here)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from agt_equities import approval_policy
from agt_equities.csp_allocator import (
    _fetch_household_buying_power_snapshot,
    run_csp_allocator,
)
from agt_equities.scan_bridge import (
    adapt_scanner_candidates,
    build_watchlist_sector_map,
    make_bridge2_extras_provider,
)
from agt_equities.scan_extras import (
    build_correlation_pairs,
    fetch_earnings_map,
)
from agt_equities.sinks import (
    CollectorOrderSink,
    NullDecisionSink,
    SQLiteOrderSink,
)

if TYPE_CHECKING:
    from agt_equities.runtime import RunContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trigger enum
# ---------------------------------------------------------------------------

class ScanTrigger(str, Enum):
    """Where the scan was initiated. Used for logging + metrics, not behavior.

    Behavior differentiation is via ctx.broker_mode (paper/live) and
    ctx.order_sink (SQLiteOrderSink vs CollectorOrderSink). Trigger is a
    trace-ID prefix and digest header string.

    Members: ScanTrigger.ADHOC (/scan cmd), ScanTrigger.DAILY_AUTO (09:35 ET
    PTB job), ScanTrigger.DRYRUN (dev_cli / tests with CollectorOrderSink).
    """
    ADHOC = "adhoc"           # /scan command
    DAILY_AUTO = "daily_auto" # 09:35 ET PTB JobQueue
    DRYRUN = "dryrun"         # dev_cli / tests — use CollectorOrderSink


# ---------------------------------------------------------------------------
# Result dataclasses (both frozen — runtime immutability)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanInputs:
    """Phase-1 output. Frozen dataclass — consumers may not mutate."""
    trigger: ScanTrigger
    candidates: tuple                  # tuple[ScanCandidate, ...]; screener-sorted
    snapshots: dict[str, dict]         # household_id -> snapshot dict
    vix: float
    extras_provider: Callable[[dict, Any], dict]
    watchlist: list[dict]              # raw from _load_scan_universe (audit trail)
    run_id: str


@dataclass(frozen=True)
class ScanResult:
    """Orchestrator output — consumers render or drain from this."""
    inputs: ScanInputs
    allocation: Any                    # csp_allocator.AllocatorResult
    approved_count: int                # how many of inputs.candidates cleared approval
    approval_applied: bool             # True if live-CSP dispatcher was invoked


# ---------------------------------------------------------------------------
# Phase 1 — build_scan_inputs
# ---------------------------------------------------------------------------

async def build_scan_inputs(
    trigger: ScanTrigger,
    ctx: "RunContext",
    *,
    ib_conn: Any,
    margin_stats: dict | None = None,
) -> ScanInputs:
    """Gather everything the allocator needs. No allocation, no approval.

    Caller supplies ib_conn (already-connected ib_async client) and optional
    margin_stats. We don't own IB connection lifecycle here — caller does.

    Returns ScanInputs with empty candidates tuple if screener produces
    nothing or adapter drops everything. Caller checks len(inputs.candidates).
    """
    from agt_equities import position_discovery
    from pxo_scanner import _load_scan_universe, scan_csp_candidates

    run_id = ctx.run_id or uuid.uuid4().hex

    watchlist = await asyncio.to_thread(_load_scan_universe)
    rows = await asyncio.to_thread(scan_csp_candidates, watchlist, 10, 50)
    if not rows:
        logger.info("scan_orchestrator: trigger=%s — 0 screener rows", trigger.value)
        return ScanInputs(
            trigger=trigger,
            candidates=(),
            snapshots={},
            vix=0.0,
            extras_provider=lambda *_a, **_k: {},
            watchlist=watchlist,
            run_id=run_id,
        )

    candidates_list = adapt_scanner_candidates(rows)
    if not candidates_list:
        logger.info(
            "scan_orchestrator: trigger=%s — 0 candidates survived adapter",
            trigger.value,
        )
        return ScanInputs(
            trigger=trigger,
            candidates=(),
            snapshots={},
            vix=0.0,
            extras_provider=lambda *_a, **_k: {},
            watchlist=watchlist,
            run_id=run_id,
        )

    candidates = tuple(candidates_list)

    # VIX fetch (yfinance, 20.0 fallback — preserves prior behavior)
    def _fetch_vix() -> float:
        try:
            import yfinance as yf  # local import — avoids module-level yf binding
            hist = yf.Ticker("^VIX").history(period="1d")
            if len(hist) and "Close" in hist.columns:
                return float(hist["Close"].iloc[-1])
        except Exception as exc:
            logger.warning("scan_orchestrator: VIX fetch failed: %s", exc)
        return 20.0

    vix = await asyncio.to_thread(_fetch_vix)

    # Household snapshots
    mstats = margin_stats if margin_stats is not None else {}
    disco = await position_discovery.discover_positions(ib_conn, mstats, None)
    if disco.get("error"):
        logger.warning(
            "scan_orchestrator: discover_positions warning: %s", disco["error"]
        )

    snapshots = await _fetch_household_buying_power_snapshot(ib_conn, disco)

    # Bridge-2 extras (earnings + correlations)
    candidate_tickers = [c.ticker for c in candidates]
    all_holding_tickers: set[str] = set()
    for _hh_snap in snapshots.values():
        all_holding_tickers.update(_hh_snap.get("existing_positions", {}).keys())
        all_holding_tickers.update(_hh_snap.get("existing_csps", {}).keys())

    earnings_map = await asyncio.to_thread(fetch_earnings_map, candidate_tickers)
    correlation_pairs = await asyncio.to_thread(
        build_correlation_pairs,
        candidate_tickers,
        sorted(all_holding_tickers),
    )

    sector_map = build_watchlist_sector_map(watchlist)
    extras_provider = make_bridge2_extras_provider(
        sector_map, earnings_map, correlation_pairs,
    )

    return ScanInputs(
        trigger=trigger,
        candidates=candidates,
        snapshots=snapshots,
        vix=vix,
        extras_provider=extras_provider,
        watchlist=watchlist,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Phase 2 — allocate_candidates
# ---------------------------------------------------------------------------

def allocate_candidates(
    approved: tuple,
    inputs: ScanInputs,
    ctx: "RunContext",
):
    """Run the allocator on the approved subset. Synchronous.

    Caller guarantees `approved` is a screener-sort-preserved tuple
    (filter of inputs.candidates, never a reordering). approval_gate is
    NOT passed — approval already applied upstream, allocator stays
    approval-unaware per Codex Option 1 ruling.
    """
    if not approved:
        # Return empty-shaped result so caller can uniformly render
        from agt_equities.csp_allocator import AllocatorResult
        return AllocatorResult(
            staged=[],
            skipped=[],
            errors=[],
            digest_lines=[],
            candidate_reasoning=[],
        )

    return run_csp_allocator(
        ray_candidates=list(approved),
        snapshots=inputs.snapshots,
        vix=inputs.vix,
        extras_provider=inputs.extras_provider,
        ctx=ctx,
        approval_gate=None,  # upstream orchestrator handled approval
    )


# ---------------------------------------------------------------------------
# run_csp_scan — orchestrator
# ---------------------------------------------------------------------------

ApprovalDispatcher = Callable[
    [tuple, "RunContext"],
    Awaitable[frozenset[int]],
]


async def run_csp_scan(
    trigger: ScanTrigger,
    ctx: "RunContext",
    *,
    ib_conn: Any,
    margin_stats: dict | None = None,
    approval_dispatcher: ApprovalDispatcher | None = None,
) -> ScanResult:
    """Two-phase CSP scan with approval boundary.

    approval_dispatcher is required when approval_policy.needs_csp_approval(ctx)
    is True. If None AND approval is required, raises ValueError (fail-closed:
    better to halt than to ship un-approved live orders).

    Paper and non-CSP engines skip the dispatcher and auto-approve the full
    candidate universe.
    """
    inputs = await build_scan_inputs(
        trigger, ctx, ib_conn=ib_conn, margin_stats=margin_stats,
    )

    if not inputs.candidates:
        # Empty universe short-circuit — skip allocator, return empty result
        result = allocate_candidates((), inputs, ctx)
        return ScanResult(
            inputs=inputs,
            allocation=result,
            approved_count=0,
            approval_applied=False,
        )

    approval_applied = False
    if approval_policy.needs_csp_approval(ctx):
        if approval_dispatcher is None:
            raise ValueError(
                "run_csp_scan: needs_csp_approval(ctx) is True but no "
                "approval_dispatcher provided — refusing to stage live "
                "CSP orders without operator review."
            )
        approval_applied = True
        logger.info(
            "scan_orchestrator: trigger=%s — dispatching %d candidates for approval",
            trigger.value, len(inputs.candidates),
        )
        approved_indices: frozenset[int] = await approval_dispatcher(
            inputs.candidates, ctx,
        )
        approved = tuple(
            c for i, c in enumerate(inputs.candidates)
            if i in approved_indices
        )
        logger.info(
            "scan_orchestrator: trigger=%s — operator approved %d/%d",
            trigger.value, len(approved), len(inputs.candidates),
        )
    else:
        approved = inputs.candidates  # paper / non-CSP: auto-approve

    result = allocate_candidates(approved, inputs, ctx)
    return ScanResult(
        inputs=inputs,
        allocation=result,
        approved_count=len(approved),
        approval_applied=approval_applied,
    )


__all__ = [
    "ScanTrigger",
    "ScanInputs",
    "ScanResult",
    "build_scan_inputs",
    "allocate_candidates",
    "run_csp_scan",
    "ApprovalDispatcher",
]
