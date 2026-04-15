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


@dataclass(frozen=True)
class ConstraintMatrix:
    """Hard constraints + tunable thresholds. Defaults are the WHEEL-3
    research-recommended values."""
    # Harvest (defense regime)
    harvest_velocity_ratio: float = 1.5
    harvest_min_pnl_pct: float = 0.50
    # Harvest (offense regime)
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
    # Net-credit floor per share for cascade acceptance
    min_credit_per_contract: float = 0.05
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
) -> EvalResult:
    """Walk the 4-tier defensive cascade. Returns RollResult on first match,
    AlertResult(CRITICAL) if all tiers fail."""
    tiers = [
        (1, constraints.tier1_strike_step, constraints.tier1_dte_min, constraints.tier1_dte_max),
        (2, constraints.tier2_strike_step, constraints.tier2_dte_min, constraints.tier2_dte_max),
        (3, constraints.tier3_strike_step, constraints.tier3_dte_min, constraints.tier3_dte_max),
        (4, constraints.tier4_strike_step, constraints.tier4_dte_min, constraints.tier4_dte_max),
    ]
    for tier_n, strike_step, dte_min, dte_max in tiers:
        cand = _select_roll_candidate(
            chain=market.chain,
            current_strike=pos.strike,
            current_call_ask=market.current_call.ask,
            asof=market.asof,
            strike_step=strike_step,
            dte_min=dte_min,
            dte_max=dte_max,
            min_credit=constraints.min_credit_per_contract,
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
        reason=f"{reason_prefix}; cascade exhausted (no tier yielded net credit ≥ {constraints.min_credit_per_contract})",
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

    # 2. Gamma cutoff: defense regime can't let assign â€” must roll if triggered
    if (
        dte <= constraints.gamma_cutoff_dte
        and extrinsic <= constraints.gamma_cutoff_extrinsic
        and delta_abs >= constraints.gamma_cutoff_delta
    ):
        return _try_cascade(
            pos, market, constraints,
            reason_prefix=f"GAMMA_CUTOFF dte={dte}â‰¤{constraints.gamma_cutoff_dte} ext={extrinsic:.2f}â‰¤{constraints.gamma_cutoff_extrinsic} delta={delta_abs:.2f}≥{constraints.gamma_cutoff_delta}",
        )

    # 3. Defensive roll trigger: ITM with extrinsic depleted
    is_itm = market.spot > pos.strike
    if is_itm and extrinsic <= constraints.defensive_roll_extrinsic_threshold:
        return _try_cascade(
            pos, market, constraints,
            reason_prefix=f"DEFEND ITM spot={market.spot:.2f}>strike={pos.strike:.2f} ext={extrinsic:.2f}â‰¤{constraints.defensive_roll_extrinsic_threshold}",
        )

    # 4. Hold
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

    # 2. Gamma cutoff in offense â†’ let it call (assignment is profitable here)
    if (
        dte <= constraints.gamma_cutoff_dte
        and extrinsic <= constraints.gamma_cutoff_extrinsic
        and delta_abs >= constraints.gamma_cutoff_delta
    ):
        return AssignResult(
            reason=f"OFFENSE_LET_IT_CALL dte={dte}â‰¤{constraints.gamma_cutoff_dte} ext={extrinsic:.2f}â‰¤{constraints.gamma_cutoff_extrinsic} delta={delta_abs:.2f}≥{constraints.gamma_cutoff_delta}",
        )

    # 3. 90% profit harvest
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
      3. Defense routing: velocity-ratio harvest â†’ gamma cutoff (forces cascade)
         â†’ defensive roll trigger (cascade) â†’ hold.
      4. Offense routing: opportunity cost breakeven â†’ gamma cutoff (assign)
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
