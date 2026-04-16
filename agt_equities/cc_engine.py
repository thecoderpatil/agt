"""
agt_equities/cc_engine.py -- Pure-function CC strike picker.

Extracts the covered-call strike-selection logic from telegram_bot.py::_walk_cc_chain
into a deterministic, testable pure function following the roll_engine.py pattern.

CANONICAL SPEC (Yash, 2026-04-15):
  "Start at paper basis, round up. If that strike provides between 30 and 130%
   annualized ROI, done. If above 130, move up in strike till below 130.
   If below 30%, move on."

  - Per-account, never household-blended.
  - Paper basis = initial_basis (what you paid), NOT adjusted_basis.

Pure function. No I/O. Deterministic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs -- frozen dataclasses, caller-constructed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainStrike:
    """Single call option quote from the chain."""
    strike: float
    bid: float
    ask: float
    delta: float | None = None


@dataclass(frozen=True)
class CCPickerInput:
    """Everything the strike picker needs. No IB, no DB."""
    ticker: str
    account_id: str
    paper_basis: float          # initial_basis, never adjusted
    spot: float
    dte: int
    expiry: str                 # ISO date string
    chain: tuple[ChainStrike, ...]
    min_ann: float = 30.0       # floor annualized %
    max_ann: float = 130.0      # ceiling annualized %
    bid_floor: float = 0.03     # skip quotes below this mid


# ---------------------------------------------------------------------------
# Outputs -- discriminated union
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CCWrite:
    """Strike selected -- stage a covered call write."""
    kind: Literal["WRITE"] = "WRITE"
    branch: str = ""            # BASIS_ANCHOR or BASIS_STEP_UP
    ticker: str = ""
    account_id: str = ""
    expiry: str = ""
    dte: int = 0
    strike: float = 0.0
    mid: float = 0.0
    annualized: float = 0.0
    otm_pct: float = 0.0
    walk_away_pnl: float = 0.0
    inception_delta: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class CCStandDown:
    """No viable strike in the 30-130% band."""
    kind: Literal["STAND_DOWN"] = "STAND_DOWN"
    ticker: str = ""
    account_id: str = ""
    best_strike: float = 0.0
    best_annualized: float = 0.0
    floor_pct: float = 30.0
    dte: int = 0
    reason: str = ""


CCResult = Union[CCWrite, CCStandDown]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _mid_price(bid: float, ask: float) -> float:
    """Mid of bid/ask; falls back to bid if ask is zero."""
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 4)
    return bid


def _annualized_roi(mid: float, strike: float, dte: int) -> float:
    """Annualized ROI as percentage: (mid/strike) * (365/dte) * 100."""
    if strike <= 0 or dte <= 0:
        return 0.0
    return (mid / strike) * (365.0 / dte) * 100.0


def _walk_away_pnl_per_share(
    paper_basis: float, strike: float, mid: float,
) -> float:
    """Walk-away P&L if assigned: strike + premium - basis."""
    return strike + mid - paper_basis


# ---------------------------------------------------------------------------
# Core picker -- the only public function
# ---------------------------------------------------------------------------

def pick_cc_strike(inp: CCPickerInput) -> CCResult:
    """
    Pure CC strike picker. Deterministic, no I/O.

    Algorithm:
      1. Filter chain to strikes >= paper_basis (round UP).
      2. Sort ascending.
      3. Walk up:
         - Skip if mid < bid_floor (garbage quote).
         - If annualized > max_ann (130%): step up (too rich).
         - If min_ann <= annualized <= max_ann: BAND HIT -> WRITE.
         - If annualized < min_ann (30%): STAND DOWN.
      4. If walk exhausted without band hit, return best-observed STAND_DOWN.

    Returns CCWrite on band hit, CCStandDown otherwise.
    """
    if inp.dte <= 0:
        return CCStandDown(
            ticker=inp.ticker,
            account_id=inp.account_id,
            dte=inp.dte,
            reason="dte_zero_or_negative",
        )

    if not inp.chain:
        return CCStandDown(
            ticker=inp.ticker,
            account_id=inp.account_id,
            dte=inp.dte,
            reason="empty_chain",
        )

    # Filter to strikes >= paper_basis, sort ascending.
    viable = sorted(
        [s for s in inp.chain if s.strike >= inp.paper_basis],
        key=lambda s: s.strike,
    )

    if not viable:
        return CCStandDown(
            ticker=inp.ticker,
            account_id=inp.account_id,
            dte=inp.dte,
            reason="no_strikes_at_or_above_basis",
        )

    anchor_strike = viable[0].strike
    best_observed: CCStandDown | None = None

    for cs in viable:
        mid = _mid_price(cs.bid, cs.ask)

        if mid < inp.bid_floor:
            # Garbage quote -- keep walking.
            continue

        ann = _annualized_roi(mid, cs.strike, inp.dte)

        # Track best observed for stand-down reporting.
        if best_observed is None or ann > best_observed.best_annualized:
            best_observed = CCStandDown(
                ticker=inp.ticker,
                account_id=inp.account_id,
                best_strike=round(cs.strike, 2),
                best_annualized=round(ann, 2),
                floor_pct=inp.min_ann,
                dte=inp.dte,
                reason=f"best_observed:{cs.strike:.2f}@{ann:.1f}%ann",
            )

        if ann > inp.max_ann:
            # Too rich -- step up.
            continue

        if ann < inp.min_ann:
            # Below floor -- stand down (annualized decays as strikes go up).
            break

        # Band hit: min_ann <= ann <= max_ann.
        branch = "BASIS_ANCHOR" if cs.strike == anchor_strike else "BASIS_STEP_UP"
        wap = _walk_away_pnl_per_share(inp.paper_basis, cs.strike, mid)
        otm_pct = ((cs.strike - inp.spot) / inp.spot * 100.0) if inp.spot > 0 else 0.0

        result = CCWrite(
            branch=branch,
            ticker=inp.ticker,
            account_id=inp.account_id,
            expiry=inp.expiry,
            dte=inp.dte,
            strike=round(cs.strike, 2),
            mid=round(mid, 4),
            annualized=round(ann, 2),
            otm_pct=round(otm_pct, 2),
            walk_away_pnl=round(wap, 2),
            inception_delta=cs.delta,
            reason=f"{branch} strike={cs.strike:.2f} ann={ann:.1f}% mid={mid:.4f}",
        )

        try:
            logger.info(
                "WHEEL-4-DECISION surface=CC_WRITE ticker=%s account=%s "
                "strike=%.2f expiry=%s ann=%.2f otm_pct=%.2f basis=%.2f "
                "spot=%.2f branch=%s",
                inp.ticker, inp.account_id, cs.strike, inp.expiry,
                ann, otm_pct, inp.paper_basis, inp.spot, branch,
            )
        except Exception:
            pass

        return result

    # Fell off the walk without a band hit.
    if best_observed is not None:
        try:
            logger.info(
                "WHEEL-4-DECISION surface=CC_STAND_DOWN ticker=%s account=%s "
                "best_strike=%.2f best_ann=%.2f floor=%.1f basis=%.2f spot=%.2f",
                inp.ticker, inp.account_id,
                best_observed.best_strike, best_observed.best_annualized,
                inp.min_ann, inp.paper_basis, inp.spot,
            )
        except Exception:
            pass
        return best_observed

    return CCStandDown(
        ticker=inp.ticker,
        account_id=inp.account_id,
        dte=inp.dte,
        reason="all_quotes_below_bid_floor",
    )
