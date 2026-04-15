"""
agt_equities/roll_engine.py â€” V2 Router defensive evaluator (pure function).

Single source of truth for the AGT Wheel defensive surface decision:
HOLD / HARVEST / ROLL / ASSIGN / LIQUIDATE / ALERT for an open short call.

WHEEL-3 (2026-04-15) â€” full implementation grounded in the deep-research
brief "Quantitative Defensive Options Mechanics: Optimizing Sub-Basis
Covered Calls in Recovering Equity Portfolios."

Key design departures from the WHEEL-2 scaffold:
  - Routing trigger is EXTRINSIC VALUE, not delta. Static/dynamic delta
    triggers cause whipsaws; extrinsic â‰¤ $0.10 is the only mathematically
    rigorous predictor of imminent assignment risk.
  - inception_delta becomes vestigial for the defensive trigger. The
    legacy 9 NULL fill_log rows from WHEEL-1b are no longer a problem;
    the evaluator never reads inception_delta for routing.
  - Two top-level regimes via `if spot < adjusted_basis`:
      DEFENSE (sub-basis): never assign, grind premium via short rolls,
              accept low RAY (2%), use velocity-ratio harvest, 4-tier
              upward-strike cascade on defensive rolls.
      OFFENSE (at/above-basis): welcome assignment, demand RAY ≥ 10%,
              check Opportunity Cost Breakeven for legacy deep-ITM
              positions where liquidation is mathematically dominant.
  - LiquidateResult is a new variant emitted ONLY in offense regime when
    BTC(call) + STC(shares) yields net proceeds above adjusted_basis.
    Always carries requires_human_approval=True; WHEEL-4 routes to
    Telegram alert demanding manual confirmation.

Pure function. No I/O. Deterministic. WHEEL-4 wires evaluate() into
telegram_bot.py::_scan_and_stage_defensive_rolls and deletes the inline
State 1/2/3 logic.

Reference: project_wheel_sprint_2026_04_14.md, ADR-005.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs â€” frozen dataclasses, caller-constructed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PortfolioContext:
    """Per-household context the evaluator needs that isn't on the position."""
    household: str
    mode: Literal["PEACETIME", "AMBER", "WARTIME"]
    leverage: float


@dataclass(frozen=True)
class Position:
    """An open short call (CC leg of an active wheel cycle)."""
    ticker: str
    account_id: str
    household: str
    strike: float
    expiry: date
    quantity: int                            # contracts short, positive integer
    cost_basis: Optional[float]              # underlying basis per share (raw); None if unknown
    inception_delta: Optional[float]         # vestigial â€” kept for logging only
    opened_at: date                          # CC open date, for V_r time-elapsed math
    avg_premium_collected: float             # cumulative premium / contracts, per share
    # Ledger-derived bases (caller injects from _load_premium_ledger_snapshot)
    assigned_basis: Optional[float]          # initial basis at assignment
    adjusted_basis: Optional[float]          # basis after all premium reductions
    # Lifecycle metadata for V_r calculation
    initial_credit: float                    # credit collected at CC open, per share
    initial_dte: int                         # DTE at CC open


@dataclass(frozen=True)
class OptionQuote:
    """Single option contract market snapshot."""
    strike: float
    expiry: date
    bid: float
    ask: float
    delta: float
    iv: float


@dataclass(frozen=True)
class MarketSnapshot:
    """Live market data for the underlying + the relevant option chain slice."""
    ticker: str
    spot: float
    iv30: float
    chain: tuple[OptionQuote, ...]           # all candidate roll targets
    current_call: OptionQuote                # the contract currently short
    asof: date
    # R4: ex-dividend context. Both None = no known upcoming ex-div, gate skipped.
    # Populated by caller from Finnhub (see telegram_bot._build_market_snapshot_for_evaluator).
    next_ex_div_date: Optional[date] = None
    next_div_amount: Optional[float] = None


@dataclass(frozen=True)
class ConstraintMatrix:
    """Hard constraints + tunable thresholds. Defaults are the WHEEL-3
    research-recommended values."""
    # Harvest (defense regime)
    harvest_velocity_ratio: float = 1.5
    harvest_min_pnl_pct: float = 0.50
    # Harvest (offense regime)
    # NOTE: Rulebook v10 §Mode-2 specifies 50% profit target. Yash deliberately
    # overrides to 90% (wider theta capture on recovered positions). Do NOT
    # "fix" this back to 0.50 without explicit approval â€” conscious override.
    offense_harvest_pnl: float = 0.90
    # RAY floors (annualized yield)
    defense_ray_floor: float = 0.02          # 2% â€” sub-basis "any premium is gravy"
    offense_ray_floor: float = 0.10          # 10% â€” capital is fungible above basis
    # Defensive roll trigger
    defensive_roll_extrinsic_threshold: float = 0.10
    # Gamma cutoff (binarized assignment risk)
    gamma_cutoff_dte: int = 3
    gamma_cutoff_extrinsic: float = 0.05
    gamma_cutoff_delta: float = 0.40
    # Roll cadence
    standard_dte_min: int = 7
    standard_dte_max: int = 14
    defensive_dte_max: int = 45
    # Cascade tier DTE windows
    tier1_dte_min: int = 7
    tier1_dte_max: int = 14
    tier2_dte_min: int = 14
    tier2_dte_max: int = 21
    tier3_dte_min: int = 21
    tier3_dte_max: int = 45
    tier4_dte_min: int = 7
    tier4_dte_max: int = 14
    # Cascade tier strike steps (number of strikes above current)
    tier1_strike_step: int = 1
    tier2_strike_step: int = 1
    tier3_strike_step: int = 2
    tier4_strike_step: int = 0               # same-strike fallback
    # Net-credit floor per share for cascade acceptance.
    # R2: Mode-1 rulebook specifies "any net credit, even $0.01" â€” sub-basis
    # defense must never refuse a profitable roll on threshold hair-splitting.
    min_credit_per_contract: float = 0.01
    # R1: Offense regime roll floor â€” above basis we have optionality; demand a
    # real $0.20/contract credit before preferring roll over assignment.
    offense_roll_min_credit: float = 0.20
    # Legacy compatibility â€” kept for the gamma cutoff delta floor
    static_delta_fallback: float = 0.40


# ---------------------------------------------------------------------------
# Outputs â€” discriminated union
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HoldResult:
    kind: Literal["HOLD"] = "HOLD"
    reason: str = ""


@dataclass(frozen=True)
class HarvestResult:
    """BTC at low residual value to free theta and reset position."""
    kind: Literal["HARVEST"] = "HARVEST"
    btc_limit: float = 0.0
    pnl_pct: float = 0.0
    velocity_ratio: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class RollResult:
    """Roll to new strike/expiry as a BAG order (BUY current + SELL new)."""
    kind: Literal["ROLL"] = "ROLL"
    new_strike: float = 0.0
    new_expiry: Optional[date] = None
    net_credit_per_contract: float = 0.0
    new_delta: float = 0.0
    cascade_tier: int = 0                    # 1-4, which tier matched
    reason: str = ""


@dataclass(frozen=True)
class AssignResult:
    """Let it assign. Offense regime only; sub-basis never returns this."""
    kind: Literal["ASSIGN"] = "ASSIGN"
    reason: str = ""


@dataclass(frozen=True)
class LiquidateResult:
    """Opportunity Cost Breakeven: BTC(call) + STC(shares) for net gain.
    Always requires human approval â€” never auto-fires."""
    kind: Literal["LIQUIDATE"] = "LIQUIDATE"
    btc_limit: float = 0.0                   # current call ask, per share
    stc_market_ref: float = 0.0              # current spot, per share
    contracts: int = 0
    shares: int = 0
    net_proceeds_per_share: float = 0.0      # spot - btc_limit
    requires_human_approval: bool = True
    reason: str = ""


@dataclass(frozen=True)
class AlertResult:
    """No safe action exists â€” page operator. WHEEL-7 CRITICAL_PAGER."""
    kind: Literal["ALERT"] = "ALERT"
    severity: Literal["WARN", "CRITICAL"] = "WARN"
    reason: str = ""
    context: dict = field(default_factory=dict)


EvalResult = Union[
    HoldResult, HarvestResult, RollResult, AssignResult,
    LiquidateResult, AlertResult,
]


# ---------------------------------------------------------------------------
# Helper functions (pure, no logging in hot path)
# ---------------------------------------------------------------------------

def _extrinsic_value(call: OptionQuote, spot: float) -> float:
    """Time value remaining on a call: ask âˆ’ max(0, spot âˆ’ strike)."""
    intrinsic = max(0.0, spot - call.strike)
    return max(0.0, call.ask - intrinsic)


def _ray(premium: float, strike: float, dte: int) -> float:
    """Annualized yield: (premium/strike) Ã— (365/dte). Returns 0 on bad inputs."""
    if strike <= 0 or dte <= 0:
        return 0.0
    return (premium / strike) * (365.0 / dte)


def _velocity_ratio(pos: Position, market: MarketSnapshot) -> tuple[float, float]:
    """Returns (V_r, P_pct). V_r = P_pct / T_pct.

    P_pct = (initial_credit âˆ’ current_ask) / initial_credit
    T_pct = days_elapsed / initial_dte

    V_r = inf when T_pct == 0 and P_pct > 0 (instant decay edge case).
    Both 0 when initial_credit â‰¤ 0 (no harvest signal possible).
    """
    if pos.initial_credit <= 0:
        return 0.0, 0.0
    p_pct = (pos.initial_credit - market.current_call.ask) / pos.initial_credit
    if pos.initial_dte <= 0:
        return (float("inf") if p_pct > 0 else 0.0), p_pct
    days_elapsed = (market.asof - pos.opened_at).days
    if days_elapsed <= 0:
        return (float("inf") if p_pct > 0 else 0.0), p_pct
    t_pct = days_elapsed / pos.initial_dte
    return p_pct / t_pct, p_pct


def _dte(asof: date, expiry: date) -> int:
    return (expiry - asof).days


def _select_roll_candidate(
    chain: tuple[OptionQuote, ...],
    current_strike: float,
    current_expiry: date,
    current_call_ask: float,
    asof: date,
    strike_step: int,
    dte_min: int,
    dte_max: int,
    min_credit: float,
) -> Optional[OptionQuote]:
    """Find best (max net credit) roll candidate matching strike-step + DTE window.

    strike_step: 0 = same strike, 1 = next strike above, 2 = two strikes above.
    Returns None if no candidate clears the net-credit floor.
    """
    if strike_step == 0:
        target_strike = current_strike
    else:
        strikes_above = sorted({q.strike for q in chain if q.strike > current_strike})
        if len(strikes_above) < strike_step:
            return None
        target_strike = strikes_above[strike_step - 1]

    candidates = [
        q for q in chain
        if q.strike == target_strike
        and q.expiry > current_expiry           # R5: never "roll" to same/earlier expiry
        and dte_min <= _dte(asof, q.expiry) <= dte_max
        and (q.bid - current_call_ask) >= min_credit
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda q: q.bid - current_call_ask)


def _try_cascade(
    pos: Position,
    market: MarketSnapshot,
    constraints: ConstraintMatrix,
    reason_prefix: str,
    min_credit_override: Optional[float] = None,
) -> EvalResult:
    """Walk the 4-tier defensive cascade. Returns RollResult on first match,
    AlertResult(CRITICAL) if all tiers fail."""
    tiers = [
        (1, constraints.tier1_strike_step, constraints.tier1_dte_min, constraints.tier1_dte_max),
        (2, constraints.tier2_strike_step, constraints.tier2_dte_min, constraints.tier2_dte_max),
        (3, constraints.tier3_strike_step, constraints.tier3_dte_min, constraints.tier3_dte_max),
        (4, constraints.tier4_strike_step, constraints.tier4_dte_min, constraints.tier4_dte_max),
    ]
    min_credit = (
        min_credit_override
        if min_credit_override is not None
        else constraints.min_credit_per_contract
    )
    for tier_n, strike_step, dte_min, dte_max in tiers:
        cand = _select_roll_candidate(
            chain=market.chain,
            current_strike=pos.strike,
            current_expiry=pos.expiry,
            current_call_ask=market.current_call.ask,
            asof=market.asof,
            strike_step=strike_step,
            dte_min=dte_min,
            dte_max=dte_max,
            min_credit=min_credit,
        )
        if cand is not None:
            return RollResult(
                new_strike=cand.strike,
                new_expiry=cand.expiry,
                net_credit_per_contract=round(cand.bid - market.current_call.ask, 4),
                new_delta=cand.delta,
                cascade_tier=tier_n,
                reason=f"{reason_prefix}; tier-{tier_n} match strike={cand.strike} dte={_dte(market.asof, cand.expiry)}",
            )
    return AlertResult(
        severity="CRITICAL",
        reason=f"{reason_prefix}; cascade exhausted (no tier yielded net credit ≥ {min_credit})",
        context={
            "ticker": pos.ticker,
            "account_id": pos.account_id,
            "current_strike": pos.strike,
            "current_expiry": pos.expiry.isoformat(),
            "spot": market.spot,
            "current_ask": market.current_call.ask,
        },
    )


# ---------------------------------------------------------------------------
# Regime routing
# ---------------------------------------------------------------------------

def _evaluate_defense(
    pos: Position,
    market: MarketSnapshot,
    constraints: ConstraintMatrix,
) -> EvalResult:
    """Sub-basis defense: never assign, grind premium, defend ITM via cascade."""
    call = market.current_call
    extrinsic = _extrinsic_value(call, market.spot)
    dte = _dte(market.asof, pos.expiry)
    delta_abs = abs(call.delta)

    is_itm = market.spot > pos.strike

    # 1. Velocity-ratio harvest gate (rapid IV crush / fast move)
    v_r, p_pct = _velocity_ratio(pos, market)
    if (
        v_r >= constraints.harvest_velocity_ratio
        and p_pct >= constraints.harvest_min_pnl_pct
    ):
        return HarvestResult(
            btc_limit=round(call.ask, 4),
            pnl_pct=round(p_pct, 4),
            velocity_ratio=round(v_r, 4) if v_r != float("inf") else v_r,
            reason=f"V_r={v_r:.2f}≥{constraints.harvest_velocity_ratio} AND P_pct={p_pct:.2f}≥{constraints.harvest_min_pnl_pct}",
        )

    # 2. R4: Ex-dividend assignment risk. If ITM and an ex-div falls inside
    # the position's remaining DTE, early-assignment arbitrage fires when
    # call extrinsic < expected dividend payout. Roll preemptively.
    if (
        is_itm
        and market.next_ex_div_date is not None
        and market.next_div_amount is not None
        and market.asof <= market.next_ex_div_date <= pos.expiry
        and extrinsic < market.next_div_amount
    ):
        return _try_cascade(
            pos, market, constraints,
            reason_prefix=(
                f"EX_DIV_RISK spot={market.spot:.2f}>strike={pos.strike:.2f} "
                f"ex_div={market.next_ex_div_date.isoformat()} "
                f"div={market.next_div_amount:.2f}>ext={extrinsic:.2f}"
            ),
        )

    # 3. R8: Short-DTE ITM trigger (replaces R1 gamma cutoff). When dte is
    # inside gamma_cutoff_dte AND we're ITM, extrinsic will compress to zero
    # regardless of current delta; defense must force the cascade at any
    # positive credit. The prior extrinsic+delta trigger missed PYPL-style
    # 2-DTE ITM positions where ask still held $0.10-$0.30 of time value.
    if is_itm and dte <= constraints.gamma_cutoff_dte:
        return _try_cascade(
            pos, market, constraints,
            reason_prefix=(
                f"SHORT_DTE_ITM dte={dte}≤{constraints.gamma_cutoff_dte} "
                f"spot={market.spot:.2f}>strike={pos.strike:.2f} ext={extrinsic:.2f}"
            ),
        )

    # 4. R7: Extrinsic-depleted ITM (kept â€” orthogonal to R8). Catches the
    # mid-DTE case: 10-30 DTE, ITM, extrinsic has collapsed because spot is
    # deep past strike. Different signature than R8 (which fires on clock
    # regardless of extrinsic); this fires on extrinsic regardless of clock.
    if is_itm and extrinsic <= constraints.defensive_roll_extrinsic_threshold:
        return _try_cascade(
            pos, market, constraints,
            reason_prefix=f"DEFEND ITM spot={market.spot:.2f}>strike={pos.strike:.2f} ext={extrinsic:.2f}≤{constraints.defensive_roll_extrinsic_threshold}",
        )

    # 5. Hold
    return HoldResult(
        reason=f"DEFENSE_HOLD spot={market.spot:.2f} strike={pos.strike:.2f} ext={extrinsic:.2f} dte={dte} V_r={v_r:.2f} P_pct={p_pct:.2f}",
    )


def _evaluate_offense(
    pos: Position,
    market: MarketSnapshot,
    constraints: ConstraintMatrix,
) -> EvalResult:
    """At/above-basis: welcome assignment, check Opportunity Cost Breakeven
    for legacy deep-ITM holdovers, harvest at 90% pnl, otherwise hold."""
    call = market.current_call
    extrinsic = _extrinsic_value(call, market.spot)
    dte = _dte(market.asof, pos.expiry)
    delta_abs = abs(call.delta)

    # 1. Opportunity Cost Breakeven (paired liquidation, requires human approval)
    # Trigger: spot recovered above adjusted_basis AND buyback cost still leaves
    # net proceeds above basis. Means we've been forever-rolling a deep-ITM
    # legacy short call past the point where holding is mathematically dominant.
    basis = pos.adjusted_basis if pos.adjusted_basis is not None else pos.cost_basis
    net_proceeds_per_share = market.spot - call.ask
    if (
        basis is not None
        and market.spot > basis
        and net_proceeds_per_share > basis
        and market.spot > pos.strike       # only if call is ITM (otherwise just hold/assign normally)
    ):
        return LiquidateResult(
            btc_limit=round(call.ask, 4),
            stc_market_ref=round(market.spot, 4),
            contracts=pos.quantity,
            shares=pos.quantity * 100,
            net_proceeds_per_share=round(net_proceeds_per_share, 4),
            requires_human_approval=True,
            reason=f"OPPORTUNITY_COST_BREAKEVEN spot={market.spot:.2f}>basis={basis:.2f} net_per_share={net_proceeds_per_share:.2f}>basis",
        )

    is_itm = market.spot > pos.strike

    # 2. R4: Ex-dividend gate. Offense welcomes assignment, but if we're sitting
    # on a deep-ITM call across an ex-div where extrinsic < dividend, the
    # counterparty will call early to capture the div. Try to roll for
    # offense_roll_min_credit ($0.20); if cascade exhausted, ASSIGN is fine
    # (offense regime â€” above basis, assignment is profitable anyway).
    if (
        is_itm
        and market.next_ex_div_date is not None
        and market.next_div_amount is not None
        and market.asof <= market.next_ex_div_date <= pos.expiry
        and extrinsic < market.next_div_amount
    ):
        cascade = _try_cascade(
            pos, market, constraints,
            reason_prefix=(
                f"OFFENSE_EX_DIV spot={market.spot:.2f}>strike={pos.strike:.2f} "
                f"ex_div={market.next_ex_div_date.isoformat()} "
                f"div={market.next_div_amount:.2f}>ext={extrinsic:.2f}"
            ),
            min_credit_override=constraints.offense_roll_min_credit,
        )
        if isinstance(cascade, RollResult):
            return cascade
        # Cascade exhausted â€” in offense, early-assign is profitable, take it.
        return AssignResult(
            reason=(
                f"OFFENSE_EX_DIV_ASSIGN cascade_failed_at_0.20 "
                f"ex_div={market.next_ex_div_date.isoformat()} "
                f"div={market.next_div_amount:.2f}>ext={extrinsic:.2f}"
            ),
        )

    # 3. R1 + R8: Short-DTE ITM. Replaces the prior gamma-cutoff trigger.
    # When dte inside gamma_cutoff_dte AND ITM, try one roll at
    # offense_roll_min_credit ($0.20) before defaulting to ASSIGN. This lets
    # offense capture a final short-cycle credit if the chain cooperates
    # while still preferring assignment over a marginal/unprofitable roll.
    if is_itm and dte <= constraints.gamma_cutoff_dte:
        cascade = _try_cascade(
            pos, market, constraints,
            reason_prefix=(
                f"OFFENSE_SHORT_DTE_ITM dte={dte}≤{constraints.gamma_cutoff_dte} "
                f"spot={market.spot:.2f}>strike={pos.strike:.2f} ext={extrinsic:.2f}"
            ),
            min_credit_override=constraints.offense_roll_min_credit,
        )
        if isinstance(cascade, RollResult):
            return cascade
        return AssignResult(
            reason=(
                f"OFFENSE_LET_IT_CALL dte={dte}≤{constraints.gamma_cutoff_dte} "
                f"ext={extrinsic:.2f} (cascade failed at â‰¥${constraints.offense_roll_min_credit:.2f})"
            ),
        )

    # 4. 90% profit harvest (see R3 note on offense_harvest_pnl).
    _v_r, p_pct = _velocity_ratio(pos, market)
    if p_pct >= constraints.offense_harvest_pnl:
        return HarvestResult(
            btc_limit=round(call.ask, 4),
            pnl_pct=round(p_pct, 4),
            velocity_ratio=0.0,              # not used in offense
            reason=f"OFFENSE_HARVEST P_pct={p_pct:.2f}≥{constraints.offense_harvest_pnl}",
        )

    # 4. Hold
    return HoldResult(
        reason=f"OFFENSE_HOLD spot={market.spot:.2f} basis={basis} ext={extrinsic:.2f} dte={dte} P_pct={p_pct:.2f}",
    )


# ---------------------------------------------------------------------------
# Pure entry point
# ---------------------------------------------------------------------------

def evaluate(
    pos: Position,
    market: MarketSnapshot,
    ctx: PortfolioContext,
    constraints: ConstraintMatrix = ConstraintMatrix(),
) -> EvalResult:
    """
    Decide what to do with an open covered call.

    Pure function. No I/O. Deterministic: same inputs always yield same output.

    Routing:
      1. Defensive try/except wraps the whole evaluator â†’ AlertResult(CRITICAL)
         on any unexpected exception. Live capital is at stake.
      2. Regime gate: spot < adjusted_basis â†’ defense; else offense.
         If adjusted_basis is None, fall back to assigned_basis, then to
         cost_basis. If all three are None, assume defense (safer â€” never
         realize a loss against unknown basis).
      3. Defense routing: velocity-ratio harvest â†’ ex-div gate (R4) â†’
         short-DTE ITM (R8 cascade) â†’ extrinsic-depleted ITM (R7 cascade)
         â†’ hold.
      4. Offense routing: opportunity cost breakeven â†’ ex-div gate (R4
         cascade-then-ASSIGN) â†’ short-DTE ITM (R1+R8 cascade-then-ASSIGN)
         â†’ 90% harvest â†’ hold.

    ctx.mode (PEACETIME/AMBER/WARTIME) is currently informational â€” defense
    runs regardless of household mode. Offense decisions are gated upstream
    in the caller (CSP allocator etc.); the evaluator stays pure.
    """
    try:
        # Sanity: position must have a current_call quote
        if market.current_call is None:
            return AlertResult(
                severity="CRITICAL",
                reason="missing current_call quote",
                context={"ticker": pos.ticker, "account_id": pos.account_id},
            )

        # Resolve basis with fallback chain
        basis_for_regime = (
            pos.adjusted_basis
            if pos.adjusted_basis is not None
            else (pos.assigned_basis if pos.assigned_basis is not None else pos.cost_basis)
        )

        # Default to defense if basis is unknown (safer)
        if basis_for_regime is None or market.spot < basis_for_regime:
            return _evaluate_defense(pos, market, constraints)
        return _evaluate_offense(pos, market, constraints)

    except Exception as exc:
        logger.exception(
            "roll_engine.evaluate raised on %s/%s strike=%s expiry=%s: %s",
            pos.ticker, pos.account_id, pos.strike, pos.expiry, exc,
        )
        return AlertResult(
            severity="CRITICAL",
            reason=f"evaluator exception: {exc!r}",
            context={
                "ticker": pos.ticker,
                "account_id": pos.account_id,
                "strike": pos.strike,
                "expiry": pos.expiry.isoformat() if pos.expiry else None,
            },
        )
