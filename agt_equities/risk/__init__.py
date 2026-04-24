"""Sprint 6 Mega-MR 5 — ADR-011 §4 pre-gateway risk layer package.

All canonical risk constants and functions are defined here so that
`from agt_equities.risk import LEVERAGE_LIMIT` (and friends) work
correctly when agt_equities/risk is a package rather than a module.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Rule 2: VIX â†’ EL retain % / max deploy %
# v8 table (current production):
_VIX_EL_TABLE_V8 = [
    (0,   15, 0.80),
    (15,  20, 0.80),
    (20,  25, 0.85),
    (25,  30, 0.90),
    (30,  35, 0.95),
    (35, 999, 1.00),
]

# v9 table (PROPOSED â€” awaiting Yash promotion):
# Cap max deployment at 60% regardless of VIX.
# The last 40% of EL is reserved as a survival bunker against IBKR
# maintenance margin expansion during tail events. No VIX level
# unlocks the bunker.
_VIX_EL_TABLE_V9 = [
    # (vix_lo, vix_hi, retain_pct, max_deploy_pct)
    (0,   20, 0.80, 0.20),
    (20,  25, 0.70, 0.30),
    (25,  30, 0.60, 0.40),
    (30,  40, 0.50, 0.50),
    (40, 999, 0.40, 0.60),
]

# Active table â€” promoted to V9 per audit-recommended survival bunker (2026-04-07)
RULEBOOK_VERSION = 'v9'
_VIX_EL_TABLE = _VIX_EL_TABLE_V9


def vix_required_el_pct(vix: float) -> float:
    """Rule 2: VIX â†’ required EL retain % of NLV."""
    for entry in _VIX_EL_TABLE:
        lo, hi, retain = entry[0], entry[1], entry[2]
        if lo <= vix < hi:
            return retain
    return 1.0  # conservative fallback


def concentration_check(
    cycles: list, household_nlv: dict[str, float],
    spots: dict[str, float] | None = None,
) -> tuple[str, float, str]:
    """Rule 1: largest position as % of household NLV.
    Returns (ticker, pct, household_short_name).
    Uses spot price if available, falls back to paper_basis."""
    spots = spots or {}
    worst_ticker = ""
    worst_pct = 0.0
    worst_hh = ""
    for c in cycles:
        if c.status != 'ACTIVE' or c.shares_held <= 0:
            continue
        hh = c.household_id
        nlv = household_nlv.get(hh, 0)
        if nlv <= 0:
            continue
        price = spots.get(c.ticker) or c.paper_basis or 0
        pos_val = c.shares_held * price
        pct = pos_val / nlv * 100
        if pct > worst_pct:
            worst_pct = pct
            worst_ticker = c.ticker
            worst_hh = hh.replace("_Household", "")
    return worst_ticker, worst_pct, worst_hh


# Rule 11: Portfolio Circuit Breaker
LEVERAGE_LIMIT = 1.50
LEVERAGE_RELEASE = 1.40  # hysteresis â€” must drop below this to release

# Module-level state for hysteresis
_leverage_breached: dict[str, bool] = {}  # {household: True/False}


def gross_beta_leverage(
    cycles: list, spots: dict[str, float], betas: dict[str, float],
    household_nlv: dict[str, float],
) -> dict[str, tuple[float, str]]:
    """Rule 11: compute beta-weighted leverage per household.

    Returns {household_short_name: (leverage_ratio, status)}.
    Status: 'OK', 'AMBER', 'BREACHED'.
    """
    result = {}
    for hh, nlv in household_nlv.items():
        if nlv <= 0:
            continue
        total_beta_notional = 0.0
        for c in cycles:
            if c.status != 'ACTIVE' or c.shares_held <= 0 or c.household_id != hh:
                continue
            spot = spots.get(c.ticker, 0)
            beta = betas.get(c.ticker, 1.0)
            total_beta_notional += c.shares_held * beta * spot

        leverage = total_beta_notional / nlv
        hh_short = hh.replace("_Household", "")

        # Hysteresis: once breached, stays breached until below release threshold
        was_breached = _leverage_breached.get(hh, False)
        if leverage >= LEVERAGE_LIMIT:
            _leverage_breached[hh] = True
            status = 'BREACHED'
        elif was_breached and leverage >= LEVERAGE_RELEASE:
            status = 'BREACHED'  # still in breach zone (hysteresis)
        elif leverage >= 1.30:
            _leverage_breached[hh] = False
            status = 'AMBER'
        else:
            _leverage_breached[hh] = False
            status = 'OK'

        result[hh_short] = (leverage, status)
    return result


def sector_violations(
    cycles: list, industry_map: dict[str, str]
) -> list[tuple[str, list[str]]]:
    """Rule 3: industries with >2 active tickers."""
    from collections import defaultdict
    industry_tickers = defaultdict(set)
    for c in cycles:
        if c.status != 'ACTIVE' or c.shares_held <= 0:
            continue
        ig = industry_map.get(c.ticker, "Unknown")
        industry_tickers[ig].add(c.ticker)
    return [
        (ig, sorted(tickers))
        for ig, tickers in sorted(industry_tickers.items())
        if len(tickers) > 2
    ]


# ---------------------------------------------------------------------------
# Dynamic Exit Threshold (W3.8, per Codex Problem 1)
# ---------------------------------------------------------------------------

def dynamic_exit_threshold(
    redeploy_yield: float,
    wait_months: int,
    cc_yield: float = 0.05,
    recovery_prob: float = 1.0,
) -> float:
    """Compute the maximum drawdown at which exiting + redeploying beats waiting.

    Formula: d*(Y,W,y_cc,q) = (a^W - b^W) / (a^W - b^W + q)
    where:
      a = 1 + Y/12    (monthly redeploy return)
      b = 1 + y_cc/12 (monthly CC yield on frozen position)
      q = recovery_prob (probability position recovers to basis)
      Y = redeploy_yield (annualized yield on redeployed capital)
      W = wait_months (time horizon for recovery)

    Returns a fraction (0-1) representing the drawdown threshold.
    If current drawdown > threshold: exit and redeploy.
    If current drawdown < threshold: freeze and harvest CCs.

    Three regimes:
      1. d* < 5%:  position is close to basis, freeze + CC harvest
      2. 5% < d* < 25%: exit decision depends on redeploy opportunity
      3. d* > 25%: strong exit signal â€” deep drawdown, high opportunity cost
    """
    if wait_months <= 0:
        return 0.0
    if redeploy_yield <= 0:
        return 0.0

    a = 1.0 + redeploy_yield / 12.0
    b = 1.0 + cc_yield / 12.0
    q = max(0.01, min(1.0, recovery_prob))

    a_w = a ** wait_months
    b_w = b ** wait_months

    numerator = a_w - b_w
    denominator = numerator + q

    if denominator <= 0:
        return 0.0

    return numerator / denominator
