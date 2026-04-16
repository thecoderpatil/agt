"""
agt_equities/roll_engine.py — WHEEL evaluator (empirical rules).

WHEEL-6 rewrite: replaces the WHEEL-3 theoretical evaluator with simple
empirical rules validated against 63 real below-basis CC positions:

  1. Only roll below-paper-basis CCs (strike < paper_basis).
     Above paper basis = let assign — you get back what you paid,
     all collected premiums are pure profit.
  2. Roll trigger: DTE ≤ 3 and ITM, OR extrinsic ≤ $0.10 and ITM.
  3. Roll = +1 strike, +1 week out. Collect credit on each roll,
     grinding the strike up toward paper basis.
  4. Circuit breaker: max 10 rolls.
  5. CSPs are never rolled (handled upstream).
  6. Canonical 80/90 harvest fires first regardless of regime.

Backtest: rolling wins 76%, avg 1.8 rolls to resolve, avg $2.07 cum. debit,
max $4.01. Nearly all +1/+1 rolls are credits.

Paper basis = assigned_basis (what you paid for shares), NOT adjusted_basis
(which has premiums subtracted). Confirmed by Yash 2026-04-15.

Pure function. No I/O. Deterministic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs — frozen dataclasses, caller-constructed
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
    inception_delta: Optional[float]         # vestigial — kept for logging only
    opened_at: date                          # CC open date
    avg_premium_collected: float             # cumulative premium / contracts, per share
    # Ledger-derived bases (caller injects from _load_premium_ledger_snapshot)
    assigned_basis: Optional[float]          # paper basis — what you paid for the shares
    adjusted_basis: Optional[float]          # basis after all premium reductions
    # Lifecycle metadata
    initial_credit: float                    # credit collected at CC open, per share
    initial_dte: int                         # DTE at CC open
    # Roll tracking (caller injects from operational state)
    cumulative_roll_debit: float = 0.0       # sum of debits paid across prior rolls
    roll_count: int = 0                      # number of rolls already executed


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
    # Optional context — kept for future use, not consumed by core logic.
    next_ex_div_date: Optional[date] = None
    next_div_amount: Optional[float] = None
    next_earnings_date: Optional[date] = None


@dataclass(frozen=True)
class ConstraintMatrix:
    """Hard constraints + tunable thresholds.

    WHEEL-6: most WHEEL-3 fields retained for backward compat but unused.
    Only active fields: roll trigger thresholds, harvest rules, max_rolls.
    """
    # --- Active in WHEEL-6 ---
    # Roll triggers
    roll_trigger_dte: int = 3                    # DTE at or below which ITM triggers roll
    roll_trigger_extrinsic: float = 0.10         # extrinsic at or below which ITM triggers roll
    # Max rolls circuit breaker
    max_rolls: int = 10
    # Harvest (canonical 80/90)
    harvest_day1_pct: float = 0.80               # day-1 harvest threshold
    harvest_standard_pct: float = 0.90           # day-2+ harvest threshold
    # Roll candidate search
    roll_dte_min: int = 5                        # min DTE for roll target
    roll_dte_max: int = 14                       # max DTE for roll target
    roll_dte_fallback_min: int = 3               # fallback min DTE
    roll_dte_fallback_max: int = 21              # fallback max DTE

    # --- Legacy WHEEL-3 fields (backward compat, not used in evaluate) ---
    harvest_velocity_ratio: float = 1.5
    harvest_min_pnl_pct: float = 0.50
    offense_harvest_pnl: float = 0.90
    defense_ray_floor: float = 0.02
    offense_ray_floor: float = 0.10
    defensive_roll_extrinsic_threshold: float = 0.10
    gamma_cutoff_dte: int = 3
    gamma_cutoff_extrinsic: float = 0.05
    gamma_cutoff_delta: float = 0.40
    standard_dte_min: int = 7
    standard_dte_max: int = 14
    defensive_dte_max: int = 45
    tier1_dte_min: int = 7
    tier1_dte_max: int = 14
    tier2_dte_min: int = 14
    tier2_dte_max: int = 21
    tier3_dte_min: int = 21
    tier3_dte_max: int = 45
    tier4_dte_min: int = 7
    tier4_dte_max: int = 14
    tier1_strike_step: int = 1
    tier2_strike_step: int = 1
    tier3_strike_step: int = 2
    tier4_strike_step: int = 0
    min_credit_per_contract: float = 0.01
    offense_roll_min_credit: float = 0.20
    earnings_week_block_offense: bool = True
    static_delta_fallback: float = 0.40


# ---------------------------------------------------------------------------
# Outputs — discriminated union
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
    cascade_tier: int = 0                    # always 1 in WHEEL-6
    reason: str = ""


@dataclass(frozen=True)
class AssignResult:
    """Let it assign — strike ≥ paper basis, assignment is profitable."""
    kind: Literal["ASSIGN"] = "ASSIGN"
    reason: str = ""


@dataclass(frozen=True)
class LiquidateResult:
    """Opportunity Cost Breakeven: BTC(call) + STC(shares) for net gain.
    Always requires human approval — never auto-fires."""
    kind: Literal["LIQUIDATE"] = "LIQUIDATE"
    btc_limit: float = 0.0
    stc_market_ref: float = 0.0
    contracts: int = 0
    shares: int = 0
    net_proceeds_per_share: float = 0.0
    requires_human_approval: bool = True
    reason: str = ""


@dataclass(frozen=True)
class AlertResult:
    """No safe action exists — page operator."""
    kind: Literal["ALERT"] = "ALERT"
    severity: Literal["WARN", "CRITICAL"] = "WARN"
    reason: str = ""
    context: dict = field(default_factory=dict)


EvalResult = Union[
    HoldResult, HarvestResult, RollResult, AssignResult,
    LiquidateResult, AlertResult,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dte(asof: date, expiry: date) -> int:
    return (expiry - asof).days


def _extrinsic_value(call: OptionQuote, spot: float) -> float:
    """Time value remaining: ask − max(0, spot − strike)."""
    intrinsic = max(0.0, spot - call.strike)
    return max(0.0, call.ask - intrinsic)


def _paper_basis(pos: Position) -> Optional[float]:
    """Resolve paper basis: what you paid for the shares.

    assigned_basis is canonical (set at share assignment). Falls back to
    cost_basis (raw) if assigned_basis is None.
    """
    if pos.assigned_basis is not None:
        return pos.assigned_basis
    return pos.cost_basis


def _check_harvest(
    pos: Position,
    market: MarketSnapshot,
    constraints: ConstraintMatrix,
) -> Optional[HarvestResult]:
    """Canonical 80/90 harvest. Returns HarvestResult or None.

    Day 1 (days_held ≤ 1): harvest at ≥ 80% profit.
    Day 2+ (days_held ≥ 2): harvest at ≥ 90% profit.
    Never harvests on expiry day (dte ≤ 0) — let it ride.
    Never harvests when initial_credit is zero (no signal).
    """
    if pos.initial_credit <= 0:
        return None

    dte = _dte(market.asof, pos.expiry)
    if dte <= 0:
        return None

    call = market.current_call
    p_pct = (pos.initial_credit - call.ask) / pos.initial_credit
    days_held = (market.asof - pos.opened_at).days

    if days_held <= 1 and p_pct >= constraints.harvest_day1_pct:
        return HarvestResult(
            btc_limit=round(call.ask, 4),
            pnl_pct=round(p_pct, 4),
            reason=f"DAY1_HARVEST days_held={days_held} P_pct={p_pct:.2f}>={constraints.harvest_day1_pct}",
        )

    if days_held >= 2 and p_pct >= constraints.harvest_standard_pct:
        return HarvestResult(
            btc_limit=round(call.ask, 4),
            pnl_pct=round(p_pct, 4),
            reason=f"CANONICAL_90_HARVEST days_held={days_held} P_pct={p_pct:.2f}>={constraints.harvest_standard_pct}",
        )

    return None


def _find_roll_target(
    chain: tuple[OptionQuote, ...],
    current_strike: float,
    current_expiry: date,
    asof: date,
    dte_min: int,
    dte_max: int,
) -> Optional[OptionQuote]:
    """Find roll target: next strike above current, within DTE window.

    Returns the candidate closest to 7 DTE (sweet spot for weekly rolls).
    Accepts any net debit or credit — below-paper-basis defense rolls
    regardless of cost. Returns None if no candidate exists.
    """
    strikes_above = sorted({q.strike for q in chain if q.strike > current_strike})
    if not strikes_above:
        return None
    target_strike = strikes_above[0]  # one strike up

    candidates = [
        q for q in chain
        if q.strike == target_strike
        and q.expiry > current_expiry
        and dte_min <= _dte(asof, q.expiry) <= dte_max
    ]

    if not candidates:
        return None

    # Pick closest to 7 DTE
    return min(candidates, key=lambda q: abs(_dte(asof, q.expiry) - 7))


def _select_roll_candidate(
    chain: tuple[OptionQuote, ...],
    current_strike: float,
    current_expiry: date,
    current_call_ask: float,
    asof: date,
    strike_step: int = 1,
    dte_min: int = 5,
    dte_max: int = 14,
    min_credit: float = -999.0,
) -> Optional[OptionQuote]:
    """Legacy-compatible roll candidate selector.

    Kept for backward compatibility with callers that import it directly.
    Delegates to _find_roll_target for the core search.
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
        and q.expiry > current_expiry
        and dte_min <= _dte(asof, q.expiry) <= dte_max
        and (q.bid - current_call_ask) >= min_credit
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda q: q.bid - current_call_ask)


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

def evaluate(
    pos: Position,
    market: MarketSnapshot,
    ctx: PortfolioContext,
    constraints: ConstraintMatrix = ConstraintMatrix(),
) -> EvalResult:
    """
    Decide what to do with an open covered call.

    Pure function. No I/O. Deterministic.

    Decision tree:
      1. Safety: missing current_call → ALERT.
      2. Expiry day (DTE ≤ 0) → HOLD (let ride).
      3. Canonical 80/90 harvest → HARVEST if profitable enough.
      4. Strike ≥ paper basis → let assign or hold (you're whole).
      5. Strike < paper basis, ITM, assignment imminent → ROLL +1/+1.
      6. Strike < paper basis, OTM → HOLD (theta working).
      7. Circuit breaker (max rolls) → ALERT operator.
      8. No roll candidate in chain → ALERT operator.

    Paper basis = assigned_basis (what you paid), not adjusted_basis.
    """
    try:
        # 0. Safety: must have a current_call quote
        if market.current_call is None:
            return AlertResult(
                severity="CRITICAL",
                reason="missing current_call quote",
                context={"ticker": pos.ticker, "account_id": pos.account_id},
            )

        call = market.current_call
        dte = _dte(market.asof, pos.expiry)
        is_itm = market.spot > pos.strike
        basis = _paper_basis(pos)

        # 1. Expiry day — let it ride, don't pay spread to close
        if dte <= 0:
            result = HoldResult(
                reason=f"EXPIRY_LET_RIDE dte={dte} spot={market.spot:.2f} strike={pos.strike:.2f}",
            )
            _log_decision(pos, market, basis, result)
            return result

        # 2. Canonical 80/90 harvest — fires first, any regime
        harvest = _check_harvest(pos, market, constraints)
        if harvest is not None:
            _log_decision(pos, market, basis, harvest)
            return harvest

        # 3. Strike ≥ paper basis — you're whole, welcome assignment
        if basis is not None and pos.strike >= basis:
            if is_itm:
                # Opportunity Cost Breakeven: can you BTC + STC for net > basis?
                net_proceeds = market.spot - call.ask
                if net_proceeds > basis:
                    result = LiquidateResult(
                        btc_limit=round(call.ask, 4),
                        stc_market_ref=round(market.spot, 4),
                        contracts=pos.quantity,
                        shares=pos.quantity * 100,
                        net_proceeds_per_share=round(net_proceeds, 4),
                        requires_human_approval=True,
                        reason=(
                            f"OPPORTUNITY_COST spot={market.spot:.2f} "
                            f"net={net_proceeds:.2f}>basis={basis:.2f}"
                        ),
                    )
                    _log_decision(pos, market, basis, result)
                    return result

                result = AssignResult(
                    reason=(
                        f"ABOVE_PAPER_BASIS_LET_ASSIGN "
                        f"strike={pos.strike:.2f}>=basis={basis:.2f} "
                        f"spot={market.spot:.2f}"
                    ),
                )
                _log_decision(pos, market, basis, result)
                return result

            # OTM with strike ≥ basis — hold, theta working, assignment fine if it comes
            result = HoldResult(
                reason=(
                    f"ABOVE_PAPER_BASIS_OTM "
                    f"strike={pos.strike:.2f}>=basis={basis:.2f} "
                    f"spot={market.spot:.2f}"
                ),
            )
            _log_decision(pos, market, basis, result)
            return result

        # --- Below paper basis from here ---
        # Strike < basis (or basis unknown → assume below, safer).
        # Goal: grind strike up to paper basis via +1/+1 rolls.

        # 4. OTM — hold, no assignment risk, theta decaying
        if not is_itm:
            result = HoldResult(
                reason=(
                    f"BELOW_BASIS_OTM spot={market.spot:.2f}<strike={pos.strike:.2f} "
                    f"basis={basis} dte={dte}"
                ),
            )
            _log_decision(pos, market, basis, result)
            return result

        # 5. ITM — check if assignment is imminent
        extrinsic = _extrinsic_value(call, market.spot)
        assignment_imminent = (
            (dte <= constraints.roll_trigger_dte)
            or (extrinsic <= constraints.roll_trigger_extrinsic)
        )

        if not assignment_imminent:
            # ITM but still has time value — hold, not urgent
            result = HoldResult(
                reason=(
                    f"BELOW_BASIS_ITM_NOT_URGENT "
                    f"spot={market.spot:.2f}>strike={pos.strike:.2f} "
                    f"ext={extrinsic:.2f} dte={dte}"
                ),
            )
            _log_decision(pos, market, basis, result)
            return result

        # 6. Circuit breaker: max rolls exceeded
        if pos.roll_count >= constraints.max_rolls:
            result = AlertResult(
                severity="CRITICAL",
                reason=(
                    f"MAX_ROLLS_EXCEEDED roll_count={pos.roll_count}"
                    f">={constraints.max_rolls} "
                    f"strike={pos.strike:.2f} basis={basis}"
                ),
                context={
                    "ticker": pos.ticker,
                    "account_id": pos.account_id,
                    "roll_count": pos.roll_count,
                    "strike": pos.strike,
                    "basis": basis,
                },
            )
            _log_decision(pos, market, basis, result)
            return result

        # 7. Try to roll: +1 strike, +1 week
        candidate = _find_roll_target(
            chain=market.chain,
            current_strike=pos.strike,
            current_expiry=pos.expiry,
            asof=market.asof,
            dte_min=constraints.roll_dte_min,
            dte_max=constraints.roll_dte_max,
        )

        # Fallback: widen DTE window
        if candidate is None:
            candidate = _find_roll_target(
                chain=market.chain,
                current_strike=pos.strike,
                current_expiry=pos.expiry,
                asof=market.asof,
                dte_min=constraints.roll_dte_fallback_min,
                dte_max=constraints.roll_dte_fallback_max,
            )

        if candidate is not None:
            net_credit = round(candidate.bid - call.ask, 4)
            new_dte = _dte(market.asof, candidate.expiry)
            result = RollResult(
                new_strike=candidate.strike,
                new_expiry=candidate.expiry,
                net_credit_per_contract=net_credit,
                new_delta=candidate.delta,
                cascade_tier=1,
                reason=(
                    f"ROLL_UP_OUT strike={pos.strike:.2f}->{candidate.strike:.2f} "
                    f"dte={new_dte} net={net_credit:.4f} "
                    f"basis={basis} rolls={pos.roll_count + 1}"
                ),
            )
            _log_decision(pos, market, basis, result)
            return result

        # 8. No candidate — alert operator
        result = AlertResult(
            severity="CRITICAL",
            reason=(
                f"NO_ROLL_CANDIDATE "
                f"strike={pos.strike:.2f} spot={market.spot:.2f} "
                f"basis={basis} dte={dte}"
            ),
            context={
                "ticker": pos.ticker,
                "account_id": pos.account_id,
                "strike": pos.strike,
                "spot": market.spot,
                "basis": basis,
                "expiry": pos.expiry.isoformat(),
            },
        )
        _log_decision(pos, market, basis, result)
        return result

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


def _log_decision(
    pos: Position,
    market: MarketSnapshot,
    basis: Optional[float],
    result: EvalResult,
) -> None:
    """Best-effort structured logging. Never raises."""
    try:
        logger.info(
            "WHEEL-6 ticker=%s acct=%s strike=%.2f expiry=%s spot=%.2f "
            "basis=%s result=%s reason=%s",
            pos.ticker, pos.account_id, float(pos.strike),
            pos.expiry.isoformat(), float(market.spot),
            basis, result.kind,
            (getattr(result, "reason", "") or "")[:160],
        )
    except Exception:
        pass
