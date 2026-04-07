"""
agt_equities/rule_engine.py — Deterministic rule evaluators for Rules 1–11.

Pure functions. Zero DB, zero network, zero side effects.
Each evaluator takes a PortfolioState snapshot and returns RuleEvaluation(s).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PortfolioState:
    """Immutable snapshot of portfolio state for rule evaluation."""
    household_nlv: dict[str, float]                # {household_id: NLV}
    household_el: dict[str, float | None]          # {household_id: EL or None}
    active_cycles: list                            # list[Cycle] — duck-typed
    spots: dict[str, float]                        # {ticker: spot price}
    betas: dict[str, float]                        # {ticker: beta} — default 1.0
    industries: dict[str, str]                     # {ticker: industry_group}
    sector_overrides: dict[str, str]               # {ticker: overridden_industry}
    vix: float | None
    report_date: str                               # YYYYMMDD of data snapshot


@dataclass
class RuleEvaluation:
    """Result of evaluating a single rule for a household (or portfolio-wide)."""
    rule_id: str                                   # "rule_1", "rule_2", etc.
    rule_name: str                                 # human-readable
    household: str | None                          # None for portfolio-wide
    ticker: str | None                             # None for portfolio-wide rules
    raw_value: float | None                        # current measured value
    status: Literal["GREEN", "AMBER", "RED", "PENDING"]
    message: str
    cure_math: dict = field(default_factory=dict)  # {action, qty, impact}
    detail: dict = field(default_factory=dict)     # extra structured data


# ---------------------------------------------------------------------------
# Pure leverage computation (no hysteresis, no mutation)
# ---------------------------------------------------------------------------

LEVERAGE_LIMIT = 1.50

def compute_leverage_pure(
    active_cycles: list,
    spots: dict[str, float],
    betas: dict[str, float],
    household_nlv: dict[str, float],
    household: str,
) -> float:
    """Pure computation of gross beta-weighted leverage for one household.

    Returns leverage ratio (float). No hysteresis, no module state mutation.
    """
    nlv = household_nlv.get(household, 0)
    if nlv <= 0:
        return 0.0
    total_notional = 0.0
    for c in active_cycles:
        if c.status != 'ACTIVE' or c.shares_held <= 0 or c.household_id != household:
            continue
        spot = spots.get(c.ticker, 0)
        beta = betas.get(c.ticker, 1.0)
        total_notional += c.shares_held * beta * spot
    return total_notional / nlv


# ---------------------------------------------------------------------------
# Rule 1: Concentration (single-name % of household NLV)
# ---------------------------------------------------------------------------

CONCENTRATION_LIMIT = 20.0  # percent
CONCENTRATION_DRIFT = 30.0  # allowed if stock fell 30%+ from basis (Rule 1 exception)

def evaluate_rule_1(ps: PortfolioState, household: str) -> list[RuleEvaluation]:
    """Rule 1: per-ticker concentration as % of household NLV.

    Returns one RuleEvaluation per ticker with shares_held > 0.
    """
    nlv = ps.household_nlv.get(household, 0)
    if nlv <= 0:
        return []
    results = []
    for c in ps.active_cycles:
        if c.status != 'ACTIVE' or c.shares_held <= 0 or c.household_id != household:
            continue
        price = ps.spots.get(c.ticker) or c.paper_basis or 0
        pos_val = c.shares_held * price
        pct = pos_val / nlv * 100
        if pct > CONCENTRATION_LIMIT:
            status = "RED"
            shares_to_sell = 0
            if price > 0:
                target_val = nlv * CONCENTRATION_LIMIT / 100
                excess_val = pos_val - target_val
                shares_to_sell = int(excess_val / price)
            cure = {"action": f"Sell {shares_to_sell} shares of {c.ticker}",
                    "shares_to_sell": shares_to_sell,
                    "current_pct": round(pct, 1),
                    "target_pct": CONCENTRATION_LIMIT}
        else:
            status = "GREEN"
            cure = {}
        results.append(RuleEvaluation(
            rule_id="rule_1", rule_name="Concentration",
            household=household, ticker=c.ticker,
            raw_value=round(pct, 2), status=status,
            message=f"{c.ticker} {pct:.1f}% of NLV (limit {CONCENTRATION_LIMIT}%)",
            cure_math=cure,
            detail={"shares_held": c.shares_held, "spot": price, "pos_val": pos_val, "nlv": nlv},
        ))
    return results


# ---------------------------------------------------------------------------
# Rule 2: VIX-scaled EL deployment governor
# ---------------------------------------------------------------------------

_VIX_EL_TABLE_V9 = [
    (0,   20, 0.80, 0.20),
    (20,  25, 0.70, 0.30),
    (25,  30, 0.60, 0.40),
    (30,  40, 0.50, 0.50),
    (40, 999, 0.40, 0.60),
]

def evaluate_rule_2(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 2: EL deployment governor. Requires EL from el_snapshots."""
    el = ps.household_el.get(household)
    nlv = ps.household_nlv.get(household, 0)
    if el is None or nlv <= 0:
        return RuleEvaluation(
            rule_id="rule_2", rule_name="EL Deployment",
            household=household, ticker=None,
            raw_value=None, status="PENDING",
            message="EL data unavailable — evaluator pending live IBKR feed",
        )
    vix = ps.vix or 0
    required_retain = 0.80
    for lo, hi, retain, _ in _VIX_EL_TABLE_V9:
        if lo <= vix < hi:
            required_retain = retain
            break
    el_pct = el / nlv if nlv > 0 else 0
    if el_pct >= required_retain:
        status = "GREEN"
    else:
        status = "RED"
    return RuleEvaluation(
        rule_id="rule_2", rule_name="EL Deployment",
        household=household, ticker=None,
        raw_value=round(el_pct, 4), status=status,
        message=f"EL {el_pct*100:.1f}% of NLV (VIX {vix:.1f} → retain {required_retain*100:.0f}%)",
        detail={"el": el, "nlv": nlv, "vix": vix, "required_retain": required_retain},
    )


# ---------------------------------------------------------------------------
# Rule 3: Sector concentration (max 2 names per industry)
# ---------------------------------------------------------------------------

SECTOR_LIMIT = 2

def evaluate_rule_3(ps: PortfolioState, household: str) -> list[RuleEvaluation]:
    """Rule 3: per-industry active ticker count. Uses sector_overrides first."""
    from collections import defaultdict
    industry_tickers = defaultdict(set)
    for c in ps.active_cycles:
        if c.status != 'ACTIVE' or c.shares_held <= 0 or c.household_id != household:
            continue
        # Override first, then industries map, then "Unknown"
        ig = ps.sector_overrides.get(c.ticker) or ps.industries.get(c.ticker, "Unknown")
        industry_tickers[ig].add(c.ticker)

    results = []
    for ig, tickers in sorted(industry_tickers.items()):
        count = len(tickers)
        if count > SECTOR_LIMIT:
            status = "RED"
            cure = {"action": f"Reduce {ig} from {count} to {SECTOR_LIMIT} names",
                    "excess": count - SECTOR_LIMIT,
                    "tickers": sorted(tickers)}
        else:
            status = "GREEN"
            cure = {}
        results.append(RuleEvaluation(
            rule_id="rule_3", rule_name="Sector Concentration",
            household=household, ticker=None,
            raw_value=count, status=status,
            message=f"{ig}: {count} names ({', '.join(sorted(tickers))})"
                    f"{' — limit ' + str(SECTOR_LIMIT) if count > SECTOR_LIMIT else ''}",
            cure_math=cure,
            detail={"industry": ig, "tickers": sorted(tickers)},
        ))
    return results


# ---------------------------------------------------------------------------
# Rule 11: Portfolio Circuit Breaker (leverage)
# ---------------------------------------------------------------------------

def evaluate_rule_11(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 11: gross beta-weighted leverage vs 1.50x limit."""
    leverage = compute_leverage_pure(
        ps.active_cycles, ps.spots, ps.betas, ps.household_nlv, household
    )
    if leverage >= LEVERAGE_LIMIT:
        status = "RED"
    elif leverage >= 1.30:
        status = "AMBER"
    else:
        status = "GREEN"

    nlv = ps.household_nlv.get(household, 0)
    excess = leverage - LEVERAGE_LIMIT if leverage > LEVERAGE_LIMIT else 0
    notional_to_reduce = excess * nlv if nlv > 0 else 0

    cure = {}
    if excess > 0:
        cure = {"action": f"Reduce notional by ${notional_to_reduce:,.0f}",
                "excess_leverage": round(excess, 4),
                "notional_to_reduce": round(notional_to_reduce, 2)}

    return RuleEvaluation(
        rule_id="rule_11", rule_name="Leverage Circuit Breaker",
        household=household, ticker=None,
        raw_value=round(leverage, 4), status=status,
        message=f"Leverage {leverage:.2f}x (limit {LEVERAGE_LIMIT}x)",
        cure_math=cure,
        detail={"leverage": leverage, "limit": LEVERAGE_LIMIT, "nlv": nlv},
    )


# ---------------------------------------------------------------------------
# Stub evaluators for rules not yet implemented
# ---------------------------------------------------------------------------

def _stub_rule(rule_id: str, rule_name: str, household: str, reason: str) -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=rule_id, rule_name=rule_name,
        household=household, ticker=None,
        raw_value=None, status="PENDING",
        message=f"{rule_name}: evaluator pending — {reason}",
    )

def evaluate_rule_4(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 4: Pairwise correlation ≤0.6. NOT IMPLEMENTED."""
    return _stub_rule("rule_4", "Pairwise Correlation", household,
                       "requires 6-month price history")

def evaluate_rule_5(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 5: Capital velocity > breakeven. NOT IMPLEMENTED."""
    return _stub_rule("rule_5", "Capital Velocity", household,
                       "requires per-cycle annualized return calc")

def evaluate_rule_6(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 6: Vikram IND EL ≥20% NLV."""
    if household != "Vikram_Household":
        return _stub_rule("rule_6", "Vikram EL Floor", household,
                          "only applies to Vikram_Household")
    el = ps.household_el.get(household)
    if el is None:
        return _stub_rule("rule_6", "Vikram EL Floor", household,
                          "EL data unavailable — pending live IBKR feed")
    nlv = ps.household_nlv.get(household, 0)
    el_pct = (el / nlv * 100) if nlv > 0 else 0
    if el_pct >= 20:
        status = "GREEN"
    elif el_pct >= 10:
        status = "AMBER"
    else:
        status = "RED"
    return RuleEvaluation(
        rule_id="rule_6", rule_name="Vikram EL Floor",
        household=household, ticker=None,
        raw_value=round(el_pct, 2), status=status,
        message=f"Vikram EL {el_pct:.1f}% of NLV (floor 20%)",
        detail={"el": el, "nlv": nlv, "el_pct": el_pct},
    )

def evaluate_rule_7(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 7: CC/CSP procedure. Procedural — not evaluable."""
    return _stub_rule("rule_7", "CC/CSP Procedure", household,
                       "procedural rule, not a compliance metric")

def evaluate_rule_8(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 8: Dynamic Exit Matrix. NOT IMPLEMENTED as evaluator."""
    return _stub_rule("rule_8", "Dynamic Exit", household,
                       "per-cycle decision tool, not portfolio-level compliance")

def evaluate_rule_9(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 9: Red Alert (2+ simultaneous breaches). NOT IMPLEMENTED."""
    return _stub_rule("rule_9", "Red Alert", household,
                       "meta-rule over R1-R8, pending foundation rules")

def evaluate_rule_10(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 10: Exclusions. Config rule — not evaluable."""
    return _stub_rule("rule_10", "Exclusions", household,
                       "config rule, handled by Walker EXCLUDED_TICKERS")


# ---------------------------------------------------------------------------
# Evaluate all rules for a household
# ---------------------------------------------------------------------------

def evaluate_all(ps: PortfolioState, household: str) -> list[RuleEvaluation]:
    """Run all 11 rule evaluators for one household. Returns flat list."""
    results = []
    results.extend(evaluate_rule_1(ps, household))
    results.append(evaluate_rule_2(ps, household))
    results.extend(evaluate_rule_3(ps, household))
    results.append(evaluate_rule_4(ps, household))
    results.append(evaluate_rule_5(ps, household))
    results.append(evaluate_rule_6(ps, household))
    results.append(evaluate_rule_7(ps, household))
    results.append(evaluate_rule_8(ps, household))
    results.append(evaluate_rule_9(ps, household))
    results.append(evaluate_rule_10(ps, household))
    results.append(evaluate_rule_11(ps, household))
    return results
