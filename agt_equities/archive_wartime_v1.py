"""
ARCHIVED WARTIME V1 LOGIC (Emergency Kill-Switch)
Archived on: 2026-04-09
Purpose: Maximizes freed margin by finding deep OTM strikes that fill instantly at $0.01.
"""
from dataclasses import dataclass
from typing import Optional

OVERWEIGHT_TARGET_PCT = 0.15
STATIC_HAIRCUT_MARGIN = 0.35

# Note: Types (Gate1Result, Gate2Result, ConvictionTier, DynamicExitCandidate)
# rely on rule_engine.py imports. This is an archive file for reference.

def evaluate_dynamic_exit_candidates(
    ticker: str,
    household: str,
    shares_held: int,
    adjusted_cost_basis: float,
    household_nlv: float,
    spot: float,
    desk_mode: str,
    chain_slice: list,
    conviction_tier,
    tax_liability_override: float = 0.0,
) -> list:
    from agt_equities.walker import compute_walk_away_pnl

    if household_nlv <= 0 or shares_held <= 0:
        return []

    target_shares = int((household_nlv * OVERWEIGHT_TARGET_PCT) / spot) if spot > 0 else 0
    excess_shares = max(0, shares_held - target_shares)
    excess_contracts = excess_shares // 100

    if excess_contracts <= 0:
        return []

    position_market_value = shares_held * spot

    candidates = []
    for opt in chain_slice:
        wa = compute_walk_away_pnl(
            adjusted_cost_basis=adjusted_cost_basis,
            proposed_exit_strike=opt.strike,
            proposed_exit_premium=opt.mid,
            quantity=excess_contracts,
        )

        g1 = evaluate_gate_1(
            ticker=ticker,
            household=household,
            candidate_strike=opt.strike,
            candidate_premium=opt.mid,
            contracts=excess_contracts,
            adjusted_cost_basis=adjusted_cost_basis,
            conviction_tier=conviction_tier,
            tax_liability_override=tax_liability_override,
        )

        if not g1.passed:
            continue

        walk_away_loss = abs(wa.walk_away_pnl_total) if not wa.is_profitable else 0.0
        # g2 = evaluate_gate_2(...) omitted for brevity in archive, see main rule_engine
        pass
