"""
agt_equities/roll_engine.py â€” V2 Router defensive evaluator (pure function).

Single source of truth for the Heitkoetter Wheel defensive surface decision:
HOLD vs HARVEST vs ROLL vs ASSIGN vs ALERT for an open covered call.

WHEEL Sprint scope (2026-04-14):
  - Pure function `evaluate(pos, market, ctx) -> EvalResult`
  - Zero I/O. No DB, no ib_async, no Telegram. Caller supplies all inputs.
  - Discriminated union return; caller dispatches on .kind.
  - Replaces the inline State 1 / State 2 / State 3 logic currently
    embedded in telegram_bot.py::_scan_and_stage_defensive_rolls.

Sprint phasing:
  - WHEEL-2 (this commit): scaffolding only. evaluate() returns HoldResult.
  - WHEEL-3: implements Let-It-Call / Sub-Basis Strut / State-1 ASSIGN /
             State-2 HARVEST / State-3 DEFEND routing, including the
             static-Î”0.40 fallback for legacy positions where
             inception_delta is None (the 9 NULL fill_log rows from WHEEL-1b).
  - WHEEL-4: wires evaluate() into _scan_and_stage_defensive_rolls and
             deletes the inline branches.

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
    leverage: float                          # current household leverage multiple
    ray_floor: float = 0.10                  # 10% RAY floor â€” never lower without DT ruling


@dataclass(frozen=True)
class Position:
    """An open short call (CC leg of an active wheel cycle)."""
    ticker: str
    account_id: str
    household: str
    strike: float
    expiry: date
    quantity: int                            # contracts short, positive integer
    cost_basis: float                        # underlying basis per share
    inception_delta: Optional[float]         # None for legacy pre-Sprint-1.6 positions
    opened_at: date                          # CC open date, for DTE math
    avg_premium_collected: float             # cumulative premium / contracts, per share


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
    iv30: float                              # underlying 30-day IV, for chain slicing
    chain: tuple[OptionQuote, ...]           # candidate roll targets, any expiry/strike
    current_call: OptionQuote                # the contract currently short
    asof: date


@dataclass(frozen=True)
class ConstraintMatrix:
    """Hard constraints the evaluator must respect (mode/RAY/DTE rails)."""
    min_dte: int = 7
    max_dte: int = 60
    min_otm_pct: float = 0.0                 # 0.0 = ATM allowed; State-3 rolls can go ITM
    max_delta_for_roll: float = 0.45         # don't roll into anything deeper than this
    min_credit_per_contract: float = 0.05    # reject sub-nickel rolls
    static_delta_fallback: float = 0.40      # used when position.inception_delta is None


# ---------------------------------------------------------------------------
# Outputs â€” discriminated union
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HoldResult:
    """No action. Position is within tolerance."""
    kind: Literal["HOLD"] = "HOLD"
    reason: str = ""


@dataclass(frozen=True)
class HarvestResult:
    """Buy-to-close at low residual value (State-2). Frees capital for redeploy."""
    kind: Literal["HARVEST"] = "HARVEST"
    btc_limit: float = 0.0
    residual_value_pct: float = 0.0          # residual / max_premium, e.g. 0.18 = 82% harvested
    reason: str = ""


@dataclass(frozen=True)
class RollResult:
    """Roll to new strike/expiry (State-3 defensive)."""
    kind: Literal["ROLL"] = "ROLL"
    new_strike: float = 0.0
    new_expiry: Optional[date] = None
    net_credit_per_contract: float = 0.0
    new_delta: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class AssignResult:
    """Let it assign. State-1 path: ITM on expiry day with no viable defensive roll."""
    kind: Literal["ASSIGN"] = "ASSIGN"
    reason: str = ""


@dataclass(frozen=True)
class AlertResult:
    """No safe action exists â€” page operator. Used by WHEEL-7 CRITICAL_PAGER path."""
    kind: Literal["ALERT"] = "ALERT"
    severity: Literal["WARN", "CRITICAL"] = "WARN"
    reason: str = ""
    context: dict = field(default_factory=dict)


EvalResult = Union[HoldResult, HarvestResult, RollResult, AssignResult, AlertResult]


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

    WHEEL-2 scaffolding: returns HoldResult unconditionally. WHEEL-3 implements
    the actual routing tree:

        if expiry == today and spot >= strike:
            -> Let-It-Call (HoldResult, reason="LET_IT_CALL")
        elif spot < cost_basis * (1 - SUB_BASIS_BUFFER):
            -> Sub-Basis Strut (HoldResult, reason="SUB_BASIS_STRUT")
        elif current_call.delta < HARVEST_DELTA_THRESHOLD:
            -> HarvestResult (State-2)
        elif current_call.delta > defense_trigger_delta(pos, constraints):
            -> RollResult (State-3) or AssignResult if no viable roll
        else:
            -> HoldResult

    `defense_trigger_delta` uses pos.inception_delta when available, falling
    back to constraints.static_delta_fallback (=0.40) for legacy positions
    where inception_delta is None (the 9 pre-Sprint-1.6 fill_log rows).
    """
    try:
        # WHEEL-3 will fill this in. Until then, every call is a HOLD â€”
        # safe default, the inline router in telegram_bot.py is still authoritative
        # because WHEEL-4 hasn't cut over yet.
        return HoldResult(reason="WHEEL-2 scaffold; evaluator not yet implemented")
    except Exception as exc:  # pragma: no cover â€” defensive, evaluator is pure
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
