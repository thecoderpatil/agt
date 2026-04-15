"""
agt_equities/roll_engine.py — V2 Router defensive evaluator (pure function).

Single source of truth for the AGT Wheel defensive surface decision:
HOLD / HARVEST / ROLL / ASSIGN / LIQUIDATE / ALERT for an open short call.

WHEEL-3 (2026-04-15) — full implementation grounded in the deep-research
brief "Quantitative Defensive Options Mechanics: Optimizing Sub-Basis
Covered Calls in Recovering Equity Portfolios."

Key design departures from the WHEEL-2 scaffold:
  - Routing trigger is EXTRINSIC VALUE, not delta. Static/dynamic delta
    triggers cause whipsaws; extrinsic ≤ $0.10 is the only mathematically
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
    defense_ray_floor: float = 0.02          # 2% — sub-basis "any premium is gravy"
    offense_ray_floor: float = 0.10          # 10% — capital is fungible above basis
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
    # Legacy compatibility — kept for the gamma cutoff delta floor
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
    Always requires human approval — never auto-fires."""
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
    """No safe action exists — page operator. WHEEL-7 CRITICAL_PAGER."""
    kind: Literal["ALERT"] = "ALERT"
  