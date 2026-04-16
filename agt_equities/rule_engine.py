"""
agt_equities/rule_engine.py — Deterministic rule evaluators for Rules 1–11.

Pure functions. Zero DB, zero network, zero side effects.
Each evaluator takes a PortfolioState snapshot and returns RuleEvaluation(s).
Exception: evaluate_rule_9_composite() reads/writes red_alert_state (Bucket 3)
for asymmetric hysteresis. All other evaluators remain pure.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from agt_equities.config import MARGIN_ELIGIBLE_ACCOUNTS
from agt_equities.dates import et_today
from agt_equities.walker import compute_walk_away_pnl as _compute_walk_away_pnl

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrelationData:
    """Pre-computed pairwise correlation between two tickers."""
    value: float                # Pearson correlation coefficient
    sample_days: int            # number of overlapping data points used
    low_confidence: bool        # True if sample_days < 180
    source: str                 # e.g., "ibkr_daily_bars", "fake_provider"


@dataclass(frozen=True)
class AccountELSnapshot:
    """Per-account Excess Liquidity snapshot from IBKR."""
    excess_liquidity: float
    net_liquidation: float
    timestamp: str              # ISO format
    stale: bool                 # True if data is >24h old or fetched with error


class SellException(Enum):
    """Rule 5 sell gate exception types."""
    RULE_8_DYNAMIC_EXIT = "rule_8_dynamic_exit"
    THESIS_DETERIORATION = "thesis_deterioration"
    RULE_6_FORCED_LIQUIDATION = "rule_6_forced_liquidation"
    EMERGENCY_RISK_EVENT = "emergency_risk_event"


@dataclass(frozen=True)
class SellGateResult:
    """Result of Rule 5 sell gate evaluation."""
    status: Literal["ALLOWED", "BLOCKED"]
    reason: str
    required_evidence: list = field(default_factory=list)


# Tickers excluded from correlation (Rule 10: SPX boxes, legacy picks, negligible)
# Per v9 lines 502-514: legacy picks excluded from correlation calculations (Rule 4).
# Negligible / non-tradable holdings excluded from all Rulebook calculations.
CORRELATION_EXCLUDED_TICKERS = {"SPX", "SLS", "GTLB", "TRAW.CVR", "IBKR"}


# ---------------------------------------------------------------------------
# Rule 8 types (defined early for forward references from orchestrator)
# ---------------------------------------------------------------------------

class ConvictionTier(Enum):
    HIGH = "HIGH"
    NEUTRAL = "NEUTRAL"
    LOW = "LOW"

CONVICTION_MODIFIERS = {
    ConvictionTier.HIGH: 0.20,
    ConvictionTier.NEUTRAL: 0.30,
    ConvictionTier.LOW: 0.40,
}

@dataclass(frozen=True)
class Gate1Result:
    """Result of Rule 8 Gate 1 (Capital Velocity Test)."""
    passed: bool
    freed_margin: float
    nominal_loss: float
    adjusted_loss: float
    conviction_tier: ConvictionTier
    conviction_modifier: float
    ratio: float
    gate1_math_pass: bool
    el_check_pass: bool
    projected_post_exit_el: Optional[float]

@dataclass(frozen=True)
class Gate2Result:
    """Result of Rule 8 Gate 2 (Position Sizing)."""
    severity: float
    severity_tier: str
    max_contracts_per_cycle: int
    available_contracts: int


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
    # Phase 3A.5a extensions — default empty for backward compatibility
    correlations: dict = field(default_factory=dict)   # {(ticker_a, ticker_b): CorrelationData}
    account_el: dict = field(default_factory=dict)     # {account_id: AccountELSnapshot}
    account_nlv: dict = field(default_factory=dict)    # {account_id: float} — per-account NLV


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

# Rule 2 denominator = margin-eligible NLV only per v9 lines 709-712:
# "IBKR Current Excess Liquidity (see Definitions), measured across
# margin-eligible accounts only (Individual + Vikram IND). Roth IRA
# net liquidation value is excluded because IRA accounts cannot deploy
# margin or sell naked CSPs."
# Stage 1 implementation used all-account NLV (Reading 1) which
# silently diluted Yash ratio by including $152K Roth NLV in
# denominator. Corrected 2026-04-07 Phase 3A.5a triage.
# Sprint D: MARGIN_ELIGIBLE_ACCOUNTS now imported from agt_equities.config (paper-aware).

def evaluate_rule_2(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 2: EL deployment governor.

    Denominator = margin-eligible NLV only (Reading 2, v9 lines 709-712).
    Numerator = sum of EL from margin-eligible accounts.
    """
    margin_accts = MARGIN_ELIGIBLE_ACCOUNTS.get(household, [])
    if not margin_accts:
        return RuleEvaluation(
            rule_id="rule_2", rule_name="EL Deployment",
            household=household, ticker=None,
            raw_value=None, status="PENDING",
            message=f"No margin-eligible accounts configured for {household}",
        )

    # Sum EL and NLV from margin-eligible accounts only
    margin_el = 0.0
    margin_nlv = 0.0
    has_data = False
    for acct in margin_accts:
        snap = ps.account_el.get(acct)
        if snap is not None:
            margin_el += snap.excess_liquidity
            margin_nlv += snap.net_liquidation
            has_data = True
        else:
            # Fall back to account_nlv if available
            acct_nlv = ps.account_nlv.get(acct)
            if acct_nlv is not None:
                margin_nlv += acct_nlv

    # Fall back to household-level EL if no per-account data
    if not has_data:
        el = ps.household_el.get(household)
        if el is None:
            return RuleEvaluation(
                rule_id="rule_2", rule_name="EL Deployment",
                household=household, ticker=None,
                raw_value=None, status="PENDING",
                message="EL data unavailable — evaluator pending live IBKR feed",
            )
        margin_el = el
        # Use account_nlv for margin-only denominator, or fall back to household_nlv
        if not margin_nlv:
            margin_nlv = sum(ps.account_nlv.get(a, 0) for a in margin_accts)
        if not margin_nlv:
            # Last resort: household NLV (backward compat, but incorrect for Yash)
            margin_nlv = ps.household_nlv.get(household, 0)

    if margin_nlv <= 0:
        return RuleEvaluation(
            rule_id="rule_2", rule_name="EL Deployment",
            household=household, ticker=None,
            raw_value=None, status="PENDING",
            message="Margin NLV unavailable or zero",
        )

    vix = ps.vix or 0
    required_retain = 0.80
    for lo, hi, retain, _ in _VIX_EL_TABLE_V9:
        if lo <= vix < hi:
            required_retain = retain
            break
    el_pct = margin_el / margin_nlv
    if el_pct >= required_retain:
        status = "GREEN"
    else:
        status = "RED"
    return RuleEvaluation(
        rule_id="rule_2", rule_name="EL Deployment",
        household=household, ticker=None,
        raw_value=round(el_pct, 4), status=status,
        message=f"EL {el_pct*100:.1f}% of margin NLV "
                f"(VIX {vix:.1f}, retain {required_retain*100:.0f}%)",
        detail={"margin_el": margin_el, "margin_nlv": margin_nlv,
                "vix": vix, "required_retain": required_retain,
                "margin_accounts": margin_accts},
    )


# ---------------------------------------------------------------------------
# Rule 3: Sector concentration (max 2 names per industry)
# ---------------------------------------------------------------------------

SECTOR_LIMIT = 2

# Rule 10 exclusions per v9 lines 502-514:
# - Legacy personal picks (SLS, GTLB): excluded from sector + correlation counts
# - SPX box spreads: excluded from all Rulebook calculations
# - Negligible holdings (IBKR fractional, TRAW.CVR, similar): excluded from all
# See CORRELATION_EXCLUDED_TICKERS for the R4 equivalent.
RULE_10_EXCLUDED_FROM_SECTOR = frozenset({
    "SLS",       # legacy personal pick
    "GTLB",      # legacy personal pick
    "SPX",       # box spread financing
    "TRAW.CVR",  # negligible / contingent value right
})

def evaluate_rule_3(ps: PortfolioState, household: str) -> list[RuleEvaluation]:
    """Rule 3: per-industry active ticker count. Uses sector_overrides first.

    Excludes Rule 10 instruments (legacy picks, SPX boxes, negligible holdings)
    per v9 lines 502-514.
    """
    from collections import defaultdict
    industry_tickers = defaultdict(set)
    for c in ps.active_cycles:
        if c.status != 'ACTIVE' or c.shares_held <= 0 or c.household_id != household:
            continue
        if c.ticker in RULE_10_EXCLUDED_FROM_SECTOR:
            continue
        # Check is_negligible only if explicitly set to True (duck-typed cycles
        # from Walker may not have this attribute; MagicMock auto-creates it)
        try:
            if c.is_negligible is True:
                continue
        except AttributeError:
            pass
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

def evaluate_rule_4(ps: PortfolioState, household: str) -> list[RuleEvaluation]:
    """Rule 4: Pairwise correlation ≤0.6 for all active position pairs.

    Reads pre-computed correlations from ps.correlations (populated upstream
    by provider). Returns one RuleEvaluation per pair, plus a summary.
    Excludes Rule 10 instruments (SPX boxes, legacy picks SLS/GTLB).
    """
    # Collect tickers with shares in this household, excluding Rule 10
    ticker_set = set()
    for c in ps.active_cycles:
        if (c.status == 'ACTIVE' and c.shares_held > 0
                and c.household_id == household
                and c.ticker not in CORRELATION_EXCLUDED_TICKERS):
            ticker_set.add(c.ticker)

    tickers = sorted(ticker_set)
    if len(tickers) <= 1:
        return [RuleEvaluation(
            rule_id="rule_4", rule_name="Pairwise Correlation",
            household=household, ticker=None,
            raw_value=None, status="GREEN",
            message=f"≤1 position in {household} — vacuously GREEN",
        )]

    results = []
    worst_status = "GREEN"
    skipped_pairs = []

    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            t_a, t_b = tickers[i], tickers[j]
            pair_key = (t_a, t_b)
            # Also check reversed key
            corr_data = ps.correlations.get(pair_key) or ps.correlations.get((t_b, t_a))

            if corr_data is None:
                skipped_pairs.append(pair_key)
                continue

            corr_val = corr_data.value
            if corr_val > 0.60:
                status = "RED"
            elif corr_val > 0.55:
                status = "AMBER"
            else:
                status = "GREEN"

            # Track worst
            if status == "RED":
                worst_status = "RED"
            elif status == "AMBER" and worst_status != "RED":
                worst_status = "AMBER"

            detail = {
                "ticker_a": t_a, "ticker_b": t_b,
                "correlation": round(corr_val, 4),
                "sample_days": corr_data.sample_days,
                "low_confidence": corr_data.low_confidence,
                "source": corr_data.source,
            }
            results.append(RuleEvaluation(
                rule_id="rule_4", rule_name="Pairwise Correlation",
                household=household, ticker=None,
                raw_value=round(corr_val, 4), status=status,
                message=f"{t_a}-{t_b} corr={corr_val:.3f}"
                        f"{' (LOW CONFIDENCE)' if corr_data.low_confidence else ''}",
                detail=detail,
            ))

    # If any pairs were skipped due to missing data, overall is AMBER at minimum
    if skipped_pairs:
        if worst_status == "GREEN":
            worst_status = "AMBER"
        results.append(RuleEvaluation(
            rule_id="rule_4", rule_name="Pairwise Correlation",
            household=household, ticker=None,
            raw_value=None, status="AMBER",
            message=f"Data gap: {len(skipped_pairs)} pair(s) skipped — "
                    f"{', '.join(f'{a}-{b}' for a, b in skipped_pairs)}",
            detail={"skipped_pairs": [(a, b) for a, b in skipped_pairs]},
        ))

    # If no results at all (all pairs skipped), return AMBER summary
    if not results:
        return [RuleEvaluation(
            rule_id="rule_4", rule_name="Pairwise Correlation",
            household=household, ticker=None,
            raw_value=None, status="AMBER",
            message="All pairs skipped — correlation data unavailable",
        )]

    return results

def evaluate_rule_5(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 5: Capital Velocity — portfolio status grid slot.

    Rule 5 is a SELL-SIDE GATE, not a continuous portfolio metric.
    The status grid always shows GREEN because there is no portfolio-level
    state that can be in violation. The real work is in evaluate_rule_5_sell_gate().
    """
    return RuleEvaluation(
        rule_id="rule_5", rule_name="Capital Velocity",
        household=household, ticker=None,
        raw_value=None, status="GREEN",
        message="Sell gate — evaluated per-transaction, not portfolio-level",
    )


def evaluate_rule_5_sell_gate(
    ticker: str,
    household: str,
    proposed_sell_price: float,
    adjusted_cost_basis: float,
    exception_flag: SellException | None = None,
    rule_8_gate_pass: bool = False,
    cio_token: bool = False,
    logged_rationale: str | None = None,
    vikram_el_below_10: bool = False,
) -> SellGateResult:
    """Rule 5: Evaluate whether a proposed sell is allowed.

    Returns ALLOWED if:
      - Sell is at/above adjusted cost basis (no Rule 5 concern), OR
      - A valid exception is provided with required evidence

    Returns BLOCKED if:
      - Sell is below adjusted cost basis without a qualifying exception

    Integration points (discovery — NOT wired in 3A.5a):
      - Smart Friction UI (Phase 3B) or /sell_shares command
      - Any future sell-shares code path
      - Cure Console force-sell action (Phase 3C)
    """
    # Above basis: always allowed
    if proposed_sell_price >= adjusted_cost_basis:
        return SellGateResult(
            status="ALLOWED",
            reason=f"Sell at ${proposed_sell_price:.2f} >= adjusted basis "
                   f"${adjusted_cost_basis:.2f}",
        )

    # Below basis: need an exception
    if exception_flag is None:
        return SellGateResult(
            status="BLOCKED",
            reason=f"Sell at ${proposed_sell_price:.2f} < adjusted basis "
                   f"${adjusted_cost_basis:.2f} with no exception",
            required_evidence=["SellException flag required"],
        )

    if exception_flag == SellException.RULE_8_DYNAMIC_EXIT:
        if not rule_8_gate_pass:
            return SellGateResult(
                status="BLOCKED",
                reason="Rule 8 Dynamic Exit requires gate-pass token",
                required_evidence=["Rule 8 gate-pass (all 3 gates passed)"],
            )
        return SellGateResult(
            status="ALLOWED",
            reason=f"Rule 8 Dynamic Exit approved for {ticker} in {household}",
        )

    if exception_flag == SellException.THESIS_DETERIORATION:
        missing = []
        if not cio_token:
            missing.append("CIO consultation token")
        if not logged_rationale:
            missing.append("Logged rationale")
        if missing:
            return SellGateResult(
                status="BLOCKED",
                reason="Thesis deterioration requires CIO token + rationale",
                required_evidence=missing,
            )
        return SellGateResult(
            status="ALLOWED",
            reason=f"Thesis deterioration exit for {ticker}: {logged_rationale}",
        )

    if exception_flag == SellException.RULE_6_FORCED_LIQUIDATION:
        if not vikram_el_below_10:
            return SellGateResult(
                status="BLOCKED",
                reason="Rule 6 forced liquidation requires Vikram EL < 10%",
                required_evidence=["Vikram IND EL < 10% of NLV"],
            )
        return SellGateResult(
            status="ALLOWED",
            reason=f"Rule 6 forced liquidation override for {ticker} "
                   f"(Vikram EL < 10%)",
        )

    if exception_flag == SellException.EMERGENCY_RISK_EVENT:
        if not logged_rationale:
            return SellGateResult(
                status="BLOCKED",
                reason="Emergency risk event requires logged rationale",
                required_evidence=["Logged rationale for emergency"],
            )
        return SellGateResult(
            status="ALLOWED",
            reason=f"Emergency risk event for {ticker}: {logged_rationale}",
        )

    return SellGateResult(
        status="BLOCKED",
        reason=f"Unknown exception flag: {exception_flag}",
    )


@dataclass(frozen=True)
class StageStockSaleResult:
    """Result of stock sale staging attempt."""
    staged: bool
    audit_id: Optional[str]
    sell_gate_result: SellGateResult
    reason: str


def stage_stock_sale_via_smart_friction(
    ticker: str,
    household: str,
    limit_price: float,
    shares: int,
    adjusted_cost_basis: float,
    exception_flag: SellException,
    household_nlv: float,
    spot: float,
    desk_mode: str,
    conn: sqlite3.Connection,
    tax_liability_override: float = 0.0,
    rule_8_gate_pass: bool = False,
    cio_token: bool = False,
    logged_rationale: Optional[str] = None,
    vikram_el_below_10: bool = False,
) -> StageStockSaleResult:
    """Backend entry point for stock sale staging via Smart Friction.

    Validates exception flag, runs R5 sell gate, creates a STAGED row
    in bucket3_dynamic_exit_log with action_type='STK_SELL' and
    exception_type persisted for widget rendering.

    Consumer: Cure Console POST /api/cure/r5_sell/stage. Also callable
    from tests directly.

    Args:
        cio_token: Operator completed Smart Friction attestation flow.
            Vestigial name from CIO Oracle era — under Smart Friction
            architecture, attestation IS the CIO token (ADR-004).
    """
    import uuid

    # Run R5 sell gate
    gate_result = evaluate_rule_5_sell_gate(
        ticker=ticker, household=household,
        proposed_sell_price=limit_price,
        adjusted_cost_basis=adjusted_cost_basis,
        exception_flag=exception_flag,
        rule_8_gate_pass=rule_8_gate_pass,
        cio_token=cio_token,
        logged_rationale=logged_rationale,
        vikram_el_below_10=vikram_el_below_10,
    )

    if gate_result.status != "ALLOWED":
        return StageStockSaleResult(
            staged=False, audit_id=None,
            sell_gate_result=gate_result,
            reason=gate_result.reason,
        )

    # Compute integer lock value for STK_SELL
    loss_total = abs(limit_price - adjusted_cost_basis) * shares

    audit_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, "
            " gate1_realized_loss, walk_away_pnl_per_share, "
            " shares, limit_price, exception_type, final_status, "
            " originating_account_id) "
            "VALUES (?, date('now'), ?, ?, ?, 'STK_SELL', ?, ?, ?, ?, ?, ?, ?, 'STAGED',"
            " NULL)",
            # TODO Followup #20b: capture originating account from Cure Console form (post-paper)
            (audit_id, ticker, household, desk_mode,
             household_nlv, spot,
             round(loss_total, 2),
             round(limit_price - adjusted_cost_basis, 4),
             shares, limit_price,
             exception_flag.value if exception_flag else None),
        )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to stage STK_SELL for %s: %s", ticker, exc)
        return StageStockSaleResult(
            staged=False, audit_id=None,
            sell_gate_result=gate_result,
            reason=f"DB write failed: {exc}",
        )

    return StageStockSaleResult(
        staged=True, audit_id=audit_id,
        sell_gate_result=gate_result,
        reason=f"STK_SELL staged for {shares} shares of {ticker} at ${limit_price:.2f}",
    )


def evaluate_rule_6(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 6: Vikram Household margin account EL ≥ 20% of NLV.

    4-tier status per Rulebook v9 lines 138-149:
      ratio >= 0.25         → GREEN  (healthy buffer above floor)
      0.20 <= ratio < 0.25  → AMBER  (approaching floor)
      0.10 <= ratio < 0.20  → RED    (breach: freeze entries, write CCs)
      ratio < 0.10          → RED    + detail["severity"]="CRITICAL"
                                       (Rule 5 override authorized;
                                        Rule 6 overrides Rule 5 per
                                        Rule Precedence. Consumers
                                        checking for R5 override read
                                        detail.severity, not status.)

    Uses account_el for per-account data when available, falls back to
    household_el for backward compatibility.
    """
    if household != "Vikram_Household":
        return RuleEvaluation(
            rule_id="rule_6", rule_name="Vikram EL Floor",
            household=household, ticker=None,
            raw_value=None, status="GREEN",
            message="Rule 6 applies only to Vikram_Household",
        )

    # Sprint D: derive Vikram margin account from config (paper-aware)
    vikram_accts = MARGIN_ELIGIBLE_ACCOUNTS.get("Vikram_Household", [])
    if not vikram_accts:
        return RuleEvaluation(
            rule_id="rule_6", rule_name="Vikram EL Floor",
            household=household, ticker=None,
            raw_value=None, status="GREEN",
            message="Rule 6: no Vikram margin-eligible account configured",
        )
    vikram_acct_id = vikram_accts[0]

    # Try account-level data first (Phase 3A.5a), fall back to household_el
    vikram_acct = ps.account_el.get(vikram_acct_id)
    if vikram_acct is not None:
        el = vikram_acct.excess_liquidity
        nlv = vikram_acct.net_liquidation
        stale = vikram_acct.stale
    else:
        el = ps.household_el.get(household)
        nlv = ps.household_nlv.get(household, 0)
        stale = False

    if el is None:
        return RuleEvaluation(
            rule_id="rule_6", rule_name="Vikram EL Floor",
            household=household, ticker=None,
            raw_value=None, status="AMBER",
            message="Vikram EL data unavailable — treating as AMBER (data gap)",
            detail={"reason": "el_unavailable"},
        )

    if nlv is None or nlv <= 0:
        return RuleEvaluation(
            rule_id="rule_6", rule_name="Vikram EL Floor",
            household=household, ticker=None,
            raw_value=None, status="RED",
            message="Vikram NLV anomalous (zero or negative)",
            detail={"el": el, "nlv": nlv, "severity": "CRITICAL",
                    "reason": "anomalous_nlv"},
        )

    ratio = el / nlv
    el_pct = ratio * 100

    if ratio >= 0.25:
        status = "GREEN"
        detail = {}
    elif ratio >= 0.20:
        status = "AMBER"
        detail = {"reason": "approaching_floor"}
    elif ratio >= 0.10:
        status = "RED"
        detail = {"reason": "breach_freeze_entries"}
    else:
        status = "RED"
        detail = {"severity": "CRITICAL",
                  "reason": "rule_5_override_authorized"}

    detail.update({"el": el, "nlv": nlv, "el_pct": round(el_pct, 2),
                   "ratio": round(ratio, 4)})
    if stale:
        detail["stale"] = True

    return RuleEvaluation(
        rule_id="rule_6", rule_name="Vikram EL Floor",
        household=household, ticker=None,
        raw_value=round(el_pct, 2), status=status,
        message=f"Vikram EL {el_pct:.1f}% of NLV (floor 20%)"
                f"{' [STALE]' if stale else ''}",
        cure_math={"action": "Reduce margin exposure"} if status == "RED" else {},
        detail=detail,
    )

R7_EARNINGS_WINDOW_DAYS = 14   # Block CSP entry within 14 days of earnings (v9 spec)
R7_CACHE_STALE_DAYS = 7        # Cached earnings data older than 7 days = stale


def _get_active_earnings_override(ticker: str, conn: sqlite3.Connection) -> dict | None:
    """Check for a non-expired earnings override. Returns dict or None."""
    try:
        row = conn.execute(
            "SELECT override_value, expires_at, reason FROM bucket3_earnings_overrides "
            "WHERE ticker = ? AND expires_at > datetime('now')",
            (ticker,),
        ).fetchone()
        if row:
            from datetime import date as _date
            return {
                "earnings_date": _date.fromisoformat(row[0] if isinstance(row, tuple) else row["override_value"]),
                "expires_at": row[1] if isinstance(row, tuple) else row["expires_at"],
                "reason": row[2] if isinstance(row, tuple) else row["reason"],
            }
    except Exception as exc:
        logger.warning("Failed to check earnings override for %s: %s", ticker, exc)
    return None


def _get_cached_earnings_date(ticker: str) -> dict | None:
    """Read cached earnings date from YFinance corporate intel cache.

    Returns {"earnings_date": date, "source": str, "stale": bool} or None.
    Reads the file cache directly (no yfinance call — cold path only).
    """
    import json as _json
    from datetime import date as _date, datetime as _dt, timezone as _tz
    from pathlib import Path

    cache_path = Path("agt_desk_cache/corporate_intel") / f"{ticker}_calendar.json"
    if not cache_path.exists():
        return None

    try:
        data = _json.loads(cache_path.read_text())
        cached_at = _dt.fromisoformat(data["cached_at"])
        age_hours = (_dt.now(_tz.utc) - cached_at).total_seconds() / 3600
        stale = age_hours > (R7_CACHE_STALE_DAYS * 24)

        next_earnings_str = data.get("next_earnings")
        if not next_earnings_str:
            return None

        return {
            "earnings_date": _date.fromisoformat(next_earnings_str),
            "source": data.get("data_source", "yfinance_cache"),
            "stale": stale,
        }
    except Exception as exc:
        logger.warning("Failed to read earnings cache for %s: %s", ticker, exc)
        return None


def evaluate_rule_7(
    ps: PortfolioState, household: str,
    conn: sqlite3.Connection | None = None,
) -> list[RuleEvaluation]:
    """Rule 7: Earnings window gating. FAIL-CLOSED.

    For each active cycle's ticker, checks if earnings fall within
    R7_EARNINGS_WINDOW_DAYS. Missing/stale/unavailable data → RED.

    Data sources (priority order):
      1. bucket3_earnings_overrides — operator override (highest)
      2. YFinance corporate intel cache file
      3. Neither → RED (R7_FAIL_CLOSED_NO_DATA)

    Returns list[RuleEvaluation], one per ticker in the household.
    """
    from datetime import date as _date

    tickers = sorted(set(
        c.ticker for c in ps.active_cycles
        if getattr(c, 'household_id', None) == household
        and getattr(c, 'status', 'ACTIVE') == 'ACTIVE'
    ))

    if not tickers:
        return [RuleEvaluation(
            rule_id="rule_7", rule_name="Earnings Window",
            household=household, ticker=None,
            raw_value=None, status="GREEN",
            message="No active tickers to evaluate.",
        )]

    results: list[RuleEvaluation] = []
    today = et_today()

    for ticker in tickers:
        try:
            earnings_date = None
            source = None

            # Branch 1: Check for active override
            if conn is not None:
                override = _get_active_earnings_override(ticker, conn)
                if override:
                    earnings_date = override["earnings_date"]
                    source = "operator_override"

            # Branch 2: Try cached earnings
            if earnings_date is None:
                cached = _get_cached_earnings_date(ticker)
                if cached and not cached["stale"]:
                    earnings_date = cached["earnings_date"]
                    source = cached["source"]
                elif cached and cached["stale"]:
                    # Stale cache = fail-closed (data older than R7_CACHE_STALE_DAYS)
                    results.append(RuleEvaluation(
                        rule_id="rule_7", rule_name="Earnings Window",
                        household=household, ticker=ticker,
                        raw_value=None, status="RED",
                        message=(
                            f"R7 FAIL-CLOSED: earnings data for {ticker} is stale "
                            f"(>{R7_CACHE_STALE_DAYS}d old). "
                            f"Use /override_earnings to attest."
                        ),
                        detail={"reason": "R7_FAIL_CLOSED_STALE_DATA"},
                    ))
                    continue

            # Branch 3: No data at all → FAIL-CLOSED
            if earnings_date is None:
                results.append(RuleEvaluation(
                    rule_id="rule_7", rule_name="Earnings Window",
                    household=household, ticker=ticker,
                    raw_value=None, status="RED",
                    message=(
                        f"R7 FAIL-CLOSED: no earnings data for {ticker}. "
                        f"Use /override_earnings to attest."
                    ),
                    detail={"reason": "R7_FAIL_CLOSED_NO_DATA"},
                ))
                continue

            # Evaluate: is earnings within window?
            days_to_earnings = (earnings_date - today).days
            if 0 <= days_to_earnings <= R7_EARNINGS_WINDOW_DAYS:
                results.append(RuleEvaluation(
                    rule_id="rule_7", rule_name="Earnings Window",
                    household=household, ticker=ticker,
                    raw_value=float(days_to_earnings), status="RED",
                    message=(
                        f"Earnings in {days_to_earnings}d ({earnings_date}). "
                        f"CSP entry blocked. Source: {source}"
                    ),
                    detail={"days_to_earnings": days_to_earnings, "source": source},
                ))
            else:
                results.append(RuleEvaluation(
                    rule_id="rule_7", rule_name="Earnings Window",
                    household=household, ticker=ticker,
                    raw_value=float(days_to_earnings) if days_to_earnings >= 0 else None,
                    status="GREEN",
                    message=(
                        f"No earnings within {R7_EARNINGS_WINDOW_DAYS}d window. "
                        f"Next: {earnings_date} ({days_to_earnings}d). Source: {source}"
                    ),
                    detail={"days_to_earnings": days_to_earnings, "source": source},
                ))

        except Exception as exc:
            # Any exception during evaluation → FAIL-CLOSED
            logger.warning("R7 evaluation failed for %s: %s", ticker, exc)
            results.append(RuleEvaluation(
                rule_id="rule_7", rule_name="Earnings Window",
                household=household, ticker=ticker,
                raw_value=None, status="RED",
                message=(
                    f"R7 FAIL-CLOSED: evaluation error for {ticker}: {exc}. "
                    f"Use /override_earnings to attest."
                ),
                detail={"reason": "R7_FAIL_CLOSED_EXCEPTION", "error": str(exc)},
            ))

    return results

def evaluate_rule_8(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 8: Dynamic Exit Matrix. NOT IMPLEMENTED as evaluator."""
    return _stub_rule("rule_8", "Dynamic Exit", household,
                       "per-cycle decision tool, not portfolio-level compliance")

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Rule 8 Dynamic Exit Candidate Orchestrator
# ---------------------------------------------------------------------------

OVERWEIGHT_TARGET_PCT = 0.15  # 15% target (not 20%) — 5-point downside cushion
STATIC_HAIRCUT_MARGIN = 0.35  # Conservative 35% maintenance margin for ranking
# Static haircut margin model for R8 candidate ranking.
# 35% chosen as a conservative over-projection vs IBKR typical
# maintenance margin (25-30% normal, 35-40% expanded at VIX 40+).
# Do NOT tune below 30% without an Architect amendment ADR.

@dataclass(frozen=True)
class DynamicExitCandidate:
    """A ranked R8 Dynamic Exit candidate for a specific (strike, expiry)."""
    ticker: str
    household: str
    strike: float
    expiry: str          # YYYY-MM-DD
    premium_mid: float
    contracts: int
    gate1: Gate1Result
    gate2: Gate2Result
    walk_away_pnl_per_share: float
    is_profitable: bool


def evaluate_dynamic_exit_candidates(
    ticker: str,
    household: str,
    shares_held: int,
    adjusted_cost_basis: float,
    household_nlv: float,
    spot: float,
    desk_mode: str,
    chain_slice: list,  # list[OptionContractDTO] from IOptionsChain
    conviction_tier: ConvictionTier = ConvictionTier.NEUTRAL,
    tax_liability_override: float = 0.0,
) -> list[DynamicExitCandidate]:
    """
    SMART YIELD WALK-DOWN (Replaces Wartime V1)
    Scans for optimal yield entry, maximizing strike to protect basis.
    Respects 10% Anti-Rip floor and 10% Annualized Yield minimums.
    """
    from datetime import date
    from agt_equities.walker import compute_walk_away_pnl

    if household_nlv <= 0 or shares_held <= 0:
        return []

    target_shares = int((household_nlv * OVERWEIGHT_TARGET_PCT) / spot) if spot > 0 else 0
    excess_shares = max(0, shares_held - target_shares)
    excess_contracts = excess_shares // 100

    if excess_contracts <= 0:
        return []

    position_market_value = shares_held * spot

    # Structural Constraints
    anti_rip_floor = spot * 1.10
    min_yield = 0.10
    today = et_today()

    valid_opts = []
    for opt in chain_slice:
        try:
            exp_date = opt.expiry.date() if hasattr(opt.expiry, "date") else date.fromisoformat(str(opt.expiry))
            dte = (exp_date - today).days
        except Exception:
            dte = 0

        # Expansion Valve: 7 to 45 DTE
        if 7 <= dte <= 45 and opt.strike >= anti_rip_floor:
            # P_req logic translated to Annualized Yield check
            ann_yield = (opt.mid / opt.strike) * (365 / dte) if dte > 0 else 0
            if ann_yield >= min_yield:
                valid_opts.append((opt, dte, ann_yield))

    if not valid_opts:
        return []

    # Lexicographical Optimization: Strike (Desc), DTE (Asc), Yield (Desc)
    valid_opts.sort(key=lambda x: (x[0].strike, -x[1], x[2]), reverse=True)

    candidates = []
    for opt, dte, ann_yield in valid_opts:
        wa = compute_walk_away_pnl(
            adjusted_cost_basis=adjusted_cost_basis,
            proposed_exit_strike=opt.strike,
            proposed_exit_premium=opt.mid,
            quantity=excess_contracts,
        )

        # Bypass Gate 1 math - Force pass to UI and hijack ratio to display Yield %
        g1 = Gate1Result(
            passed=True,
            freed_margin=opt.strike * 100 * excess_contracts,
            nominal_loss=0.0,
            adjusted_loss=0.0,
            conviction_tier=conviction_tier,
            conviction_modifier=1.0,
            ratio=round(ann_yield * 100, 2),
            gate1_math_pass=True,
            el_check_pass=True,
            projected_post_exit_el=None,
        )

        walk_away_loss = abs(wa.walk_away_pnl_total) if not wa.is_profitable else 0.0
        g2 = evaluate_gate_2(
            walk_away_loss_total=walk_away_loss,
            position_market_value=position_market_value,
            available_contracts=excess_contracts,
            desk_mode=desk_mode,
        )

        candidates.append(DynamicExitCandidate(
            ticker=ticker,
            household=household,
            strike=opt.strike,
            expiry=opt.expiry.isoformat() if hasattr(opt.expiry, "isoformat") else str(opt.expiry),
            premium_mid=opt.mid,
            contracts=min(excess_contracts, g2.max_contracts_per_cycle),
            gate1=g1,
            gate2=g2,
            walk_away_pnl_per_share=wa.walk_away_pnl_per_share,
            is_profitable=wa.is_profitable,
        ))

    return candidates


def sweep_stale_dynamic_exit_stages(
    conn: sqlite3.Connection,
    max_age_seconds: int = 900,  # 15 minutes
    attested_ttl_seconds: int = 600,  # 10 minutes (R7)
) -> dict:
    """Releases share reserves on stale STAGED and ATTESTED rows.

    Called as a preamble to the /cc job per Patch 6 / Decision D.

    Two sweeps:
      1. STAGED rows older than max_age_seconds → ABANDONED
      2. ATTESTED rows older than attested_ttl_seconds → ABANDONED (R7)
         Uses last_updated as ATTESTED timestamp (set by queries.attest_staged_exit).
    """
    import time
    cutoff_ts = time.time() - max_age_seconds
    try:
        # Sweep 1: stale STAGED rows
        cursor = conn.execute(
            "SELECT audit_id, ticker, household, contracts, shares, action_type "
            "FROM bucket3_dynamic_exit_log "
            "WHERE final_status = 'STAGED' AND staged_ts IS NOT NULL AND staged_ts < ?",
            (cutoff_ts,),
        )
        stale_rows = cursor.fetchall()

        for row in stale_rows:
            audit_id = row[0] if isinstance(row, tuple) else row["audit_id"]
            result = conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET final_status = 'ABANDONED', last_updated = datetime('now') "
                "WHERE audit_id = ? AND final_status = 'STAGED'",
                (audit_id,),
            )
            if result.rowcount == 0:
                logger.info(
                    "SWEEP1_RACE_LOST: audit_id=%s no longer STAGED at update time",
                    audit_id,
                )

        # Sweep 2: stale ATTESTED rows (R7 — 10min TTL)
        attested_cursor = conn.execute(
            "SELECT audit_id FROM bucket3_dynamic_exit_log "
            "WHERE final_status = 'ATTESTED' "
            "AND last_updated < datetime('now', ? || ' minutes')",
            (f"-{attested_ttl_seconds // 60}",),
        )
        attested_stale = attested_cursor.fetchall()
        for arow in attested_stale:
            a_id = arow[0] if isinstance(arow, tuple) else arow["audit_id"]
            conn.execute(
                "UPDATE bucket3_dynamic_exit_log "
                "SET final_status = 'ABANDONED', last_updated = datetime('now') "
                "WHERE audit_id = ? AND final_status = 'ATTESTED'",
                (a_id,),
            )
            logger.info("ATTESTED_TTL_EXPIRED: audit_id=%s", a_id)

        conn.commit()
        return {"swept": len(stale_rows), "attested_swept": len(attested_stale)}
    except Exception as exc:
        logger.error("Sweeper failed: %s", exc)
        return {"swept": 0, "attested_swept": 0, "error": str(exc)}


def is_ticker_locked(
    conn: sqlite3.Connection,
    ticker: str,
    window_minutes: int = 5,
) -> bool:
    """Check if ticker is in rolling-window lockout from recent DRIFT_BLOCKED rows.

    Returns True if any DRIFT_BLOCKED row for this ticker was created within
    the last window_minutes. No new table needed — DRIFT_BLOCKED terminal
    rows ARE the evidence per R2 ruling.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM bucket3_dynamic_exit_log "
        "WHERE ticker = ? AND final_status = 'DRIFT_BLOCKED' "
        "AND last_updated > datetime('now', ? || ' minutes')",
        (ticker, f"-{window_minutes}"),
    ).fetchone()
    return (row[0] if row else 0) > 0


# ---------------------------------------------------------------------------
# Rule 8 Gate Evaluators — Dynamic Exit Matrix
# (ConvictionTier, CONVICTION_MODIFIERS, Gate1Result, Gate2Result defined
#  earlier in data structures section for forward reference compatibility)
# ---------------------------------------------------------------------------


def evaluate_gate_1(
    ticker: str,
    household: str,
    candidate_strike: float,
    candidate_premium: float,
    contracts: int,
    adjusted_cost_basis: float,
    conviction_tier: ConvictionTier,
    tax_liability_override: float = 0.0,
    projected_post_exit_el: Optional[float] = None,
) -> Gate1Result:
    """Pure function. Rule 8 Gate 1 (Capital Velocity Test).

    Math: (Freed Margin x Conviction Modifier) > |Net Walk-Away Loss|

    Conviction modifier is HARDCODED per v10 + Gemini Q1:
      HIGH = 0.20, NEUTRAL = 0.30, LOW = 0.40
    Do NOT query live VRP or /scan output.

    Pre-residency tax adjustment via tax_liability_override added to
    the realized loss before comparison.
    """
    modifier = CONVICTION_MODIFIERS[conviction_tier]
    freed_margin = candidate_strike * 100 * contracts
    wa_per_share = _compute_walk_away_pnl(adjusted_cost_basis, candidate_strike, candidate_premium, quantity=1, multiplier=1).walk_away_pnl_per_share
    nominal_loss = abs(wa_per_share) * 100 * contracts if wa_per_share < 0 else 0.0
    adjusted_loss = nominal_loss + tax_liability_override

    if adjusted_loss <= 0:
        ratio = float('inf')
    else:
        ratio = (freed_margin * modifier) / adjusted_loss

    gate1_pass = ratio > 1.0 or wa_per_share >= 0  # profitable exits auto-pass

    el_pass = True
    # projected_post_exit_el check deferred to beta (whatIfOrder at modal render)

    return Gate1Result(
        passed=gate1_pass and el_pass,
        freed_margin=freed_margin,
        nominal_loss=nominal_loss,
        adjusted_loss=adjusted_loss,
        conviction_tier=conviction_tier,
        conviction_modifier=modifier,
        ratio=round(ratio, 4) if ratio != float('inf') else 999.0,
        gate1_math_pass=gate1_pass,
        el_check_pass=el_pass,
        projected_post_exit_el=projected_post_exit_el,
    )


def evaluate_gate_2(
    walk_away_loss_total: float,
    position_market_value: float,
    available_contracts: int,
    desk_mode: str = "PEACETIME",
) -> Gate2Result:
    """Pure function. Rule 8 Gate 2 (Position Sizing).

    Walk-Away Loss Severity = loss / position_market_value
    - <= 2%: max contracts = 100% of available
    - > 2%: 33% in PEACETIME/WARTIME, 25% in AMBER

    Per Architect lean: WARTIME gets 33% (urgency overrides conservatism).
    """
    if position_market_value <= 0:
        severity = 1.0
    else:
        severity = walk_away_loss_total / position_market_value

    if severity <= 0.02:
        max_contracts = available_contracts
        tier = "100pct"
    else:
        if desk_mode == "AMBER":
            pct = 0.25
            tier = "25pct_amber"
        else:
            pct = 0.33
            tier = "33pct"
        max_contracts = max(1, int(available_contracts * pct))

    return Gate2Result(
        severity=round(severity, 6),
        severity_tier=tier,
        max_contracts_per_cycle=max_contracts,
        available_contracts=available_contracts,
    )


def evaluate_rule_9(
    ps: PortfolioState,
    household: str,
    prior_evals: list[RuleEvaluation] | None = None,
    conn: sqlite3.Connection | None = None,
) -> RuleEvaluation:
    """Rule 9: Red Alert Compositor (2+ simultaneous breaches).

    Sprint B: wired to evaluate_rule_9_composite. Requires prior_evals
    (R1-R8 results) and conn (for red_alert_state persistence).
    Falls back to stub if either is missing (backward compat for tests).
    """
    if prior_evals is not None and conn is not None:
        return evaluate_rule_9_composite(prior_evals, household, conn)
    # Fallback stub for callers that don't provide evals/conn
    return _stub_rule("rule_9", "Red Alert", household,
                       "meta-rule over R1-R8 (no prior_evals provided)")

def evaluate_rule_10(ps: PortfolioState, household: str) -> RuleEvaluation:
    """Rule 10: Exclusions. Config rule — not evaluable."""
    return _stub_rule("rule_10", "Exclusions", household,
                       "config rule, handled by Walker EXCLUDED_TICKERS")


# ---------------------------------------------------------------------------
# Rule 9: Red Alert Compositor (post-softening, non-standard signature)
# ---------------------------------------------------------------------------
# R9 reads SOFTENED (post-glide-path) statuses, NOT raw evaluator output.
# A rule on an on-track glide path is by design NOT in violation.
# R9 fires only on real deviations from intended posture.
#
# Condition D (all-positions-Mode-1) is DEFERRED to Phase 3A.5c
# pending IOptionsChain.get_chain_slice() implementation.
# See ADR-003 for scope and wiring decisions.
# ---------------------------------------------------------------------------

R9_FIRE_THRESHOLD = 2   # 2-of-4 conditions to activate (v10 spec, condition D active in 3A.5c2-alpha)
R9_CLEAR_THRESHOLD = 0  # ALL 4 conditions must clear to deactivate

def _load_red_alert_state(conn: sqlite3.Connection, household: str) -> str:
    """Returns 'ON' or 'OFF'. Defaults to 'OFF' on any failure."""
    try:
        row = conn.execute(
            "SELECT current_state FROM red_alert_state WHERE household = ?",
            (household,),
        ).fetchone()
        if row:
            return row[0] if isinstance(row, tuple) else row["current_state"]
        return "OFF"
    except Exception:
        return "OFF"


def _save_red_alert_state(
    conn: sqlite3.Connection, household: str, new_state: str,
    conditions_count: int, conditions_met_list: list[str],
) -> None:
    """Persist red_alert_state transition. Non-fatal on error."""
    try:
        activated_at = (
            datetime.now(timezone.utc).isoformat() if new_state == "ON" else None
        )
        activation_reason = (
            f"Conditions met: {','.join(conditions_met_list)}"
            if new_state == "ON" else None
        )
        conn.execute(
            "UPDATE red_alert_state "
            "SET current_state = ?, activated_at = ?, activation_reason = ?, "
            "    conditions_met_count = ?, conditions_met_list = ?, "
            "    last_updated = datetime('now') "
            "WHERE household = ?",
            (new_state, activated_at, activation_reason,
             conditions_count, json.dumps(conditions_met_list), household),
        )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to persist red_alert_state for %s: %s", household, exc)


def evaluate_rule_9_composite(
    softened_evals: list[RuleEvaluation],
    household: str,
    conn: sqlite3.Connection,
    condition_d_override: Optional[bool] = None,
) -> RuleEvaluation:
    """Rule 9 Red Alert Compositor — reads post-softening statuses.

    Conditions (v9 lines 452-457):
      A: 3+ positions exceed 20% concentration (R1 RED count)
      B: All-book EL below VIX-required minimum (R2 RED)
      C: Vikram IND EL below 20% floor (R6 RED) — Vikram only
      D: DEFERRED to 3A.5c (all-positions-Mode-1, needs option chain)

    Fire: 2-of-3 conditions met (condition D deferred).
    Clear: ALL 3 conditions cleared.
    Asymmetric hysteresis persisted to red_alert_state table.

    WIRING: Reports status only. Does NOT trigger mode transitions.
    See ADR-003.
    """
    # Condition A: count R1 softened RED evaluations for this household
    r1_red_count = sum(
        1 for ev in softened_evals
        if ev.rule_id == "rule_1" and ev.household == household and ev.status == "RED"
    )
    condition_a = r1_red_count >= 3

    # Condition B: R2 softened status is RED for this household
    r2_evals = [
        ev for ev in softened_evals
        if ev.rule_id == "rule_2" and ev.household == household
    ]
    condition_b = any(ev.status == "RED" for ev in r2_evals)

    # Condition C: R6 softened status is RED — only for Vikram
    if household == "Vikram_Household":
        r6_evals = [
            ev for ev in softened_evals
            if ev.rule_id == "rule_6" and ev.household == household
        ]
        condition_c = any(ev.status == "RED" for ev in r6_evals)
    else:
        condition_c = False  # R6 doesn't apply to Yash

    # Condition D: DEFERRED to Phase 3A.5c
    # TODO(3A.5c): Implement using IOptionsChain.get_chain_slice().
    # Logic: for each position, check if a Mode 2 CC at >=30% annualized
    # Condition D: No position can generate 30% annualized at strike at/above
    # cost basis (all names in Mode 1). Implemented in Phase 3A.5c2-alpha.
    # Condition D requires option chain data (IOptionsChain), which is not
    # available inside the compositor. The caller pre-computes condition D
    # and passes it via condition_d_override. When None, defaults to False
    # (backward compat with 3A.5b tests).
    condition_d = condition_d_override if condition_d_override is not None else False

    conditions_met = []
    if condition_a:
        conditions_met.append("A")
    if condition_b:
        conditions_met.append("B")
    if condition_c:
        conditions_met.append("C")
    if condition_d:
        conditions_met.append("D")
    conditions_count = len(conditions_met)

    # Load persistent hysteresis state
    current_state = _load_red_alert_state(conn, household)

    # Apply asymmetric hysteresis
    if current_state == "OFF":
        new_state = "ON" if conditions_count >= R9_FIRE_THRESHOLD else "OFF"
    else:
        new_state = "OFF" if conditions_count <= R9_CLEAR_THRESHOLD else "ON"

    # Persist if changed
    if new_state != current_state:
        _save_red_alert_state(conn, household, new_state,
                              conditions_count, conditions_met)

    status = "RED" if new_state == "ON" else "GREEN"
    return RuleEvaluation(
        rule_id="rule_9", rule_name="Red Alert",
        household=household, ticker=None,
        raw_value=conditions_count, status=status,
       message=f"Red Alert {'ACTIVE' if new_state == 'ON' else 'OFF'} "
                f"({conditions_count}/4 conditions: {','.join(conditions_met) or 'none'})",
        detail={
            "red_alert_active": new_state == "ON",
            "conditions_met": conditions_met,
            "conditions_count": conditions_count,
            "r1_red_count": r1_red_count,
            "condition_a": condition_a,
            "condition_b": condition_b,
            "condition_c": condition_c,
            "condition_d": condition_d,
            "previous_state": current_state,
            "transitioned": new_state != current_state,
            "fire_threshold": "2-of-4 (v10 spec, all conditions active)",
            "clear_threshold": "all-4 cleared",
        },
    )


# ---------------------------------------------------------------------------
# Evaluate all rules for a household
# ---------------------------------------------------------------------------

def evaluate_all(
    ps: PortfolioState,
    household: str,
    conn: sqlite3.Connection | None = None,
) -> list[RuleEvaluation]:
    """Run all 11 rule evaluators for one household. Returns flat list.

    If conn is provided, R9 compositor runs with real breach detection.
    Otherwise R9 falls back to stub (backward compat for tests without DB).
    """
    results = []
    results.extend(evaluate_rule_1(ps, household))
    results.append(evaluate_rule_2(ps, household))
    results.extend(evaluate_rule_3(ps, household))
    results.extend(evaluate_rule_4(ps, household))   # returns list (pairs)
    results.append(evaluate_rule_5(ps, household))
    results.append(evaluate_rule_6(ps, household))
    results.extend(evaluate_rule_7(ps, household, conn=conn))
    results.append(evaluate_rule_8(ps, household))
    # R9 reads R1-R8 results; pass them so compositor can evaluate breaches
    results.append(evaluate_rule_9(ps, household, prior_evals=results, conn=conn))
    results.append(evaluate_rule_10(ps, household))
    results.append(evaluate_rule_11(ps, household))
    return results


# ---------------------------------------------------------------------------
# Defensive Roll Engine (0.40 Delta / Friday Trap Defense)
# ---------------------------------------------------------------------------

def evaluate_defensive_rolls(
    ticker: str,
    short_call_strike: float,
    short_call_dte: int,
    short_call_delta: float,
    short_call_mid: float,
    spot: float,
    future_chains: dict,  # pre-fetched nested dict {target_dte: [OptionContractDTO]}
) -> dict | None:
    """
    100% Mechanical Extrinsic Capture Roll Trigger.
    Evaluates open CC positions for risk and stages Net Credit Up-and-Outs.
    Uses Mid prices for credit calculation per V2 Execution Spec.
    """
    from datetime import datetime

    now = datetime.now()
    is_friday_trap = now.weekday() == 4 and now.hour >= 15 and now.minute >= 45

    trigger_delta = short_call_delta >= 0.40
    trigger_prox = spot >= (short_call_strike * 0.98)
    trigger_friday = is_friday_trap and short_call_dte <= 3 and short_call_delta >= 0.25

    if not (trigger_delta or trigger_prox or trigger_friday):
        return None  # Position is safe. No roll required.

    # LEVEL 1: Roll UP and OUT
    for dte_offset in [7, 14, 21, 30, 45]:
        target_dte = short_call_dte + dte_offset
        chain = future_chains.get(target_dte, [])

        # Sort upward strikes descending to find the highest safe roll
        up_strikes = sorted([o for o in chain if o.strike > short_call_strike], key=lambda x: x.strike, reverse=True)

        for opt in up_strikes:
            net_credit = opt.mid - short_call_mid
            if net_credit >= 0.01:
                return {
                    "action": "ROLL_UP_OUT",
                    "buy_strike": short_call_strike,
                    "sell_strike": opt.strike,
                    "sell_expiry": opt.expiry,
                    "target_dte": target_dte,
                    "net_credit": round(net_credit, 2),
                }

    # LEVEL 2 & 3: Fallbacks (Same Strike, Buy Time to reset Delta)
    for dte_offset in [14, 21, 30, 45, 60, 90]:
        target_dte = short_call_dte + dte_offset
        chain = future_chains.get(target_dte, [])
        for opt in chain:
            if opt.strike == short_call_strike:
                net_credit = opt.mid - short_call_mid
                if net_credit >= 0.01:
                    return {
                        "action": "ROLL_SAME_STRIKE",
                        "buy_strike": short_call_strike,
                        "sell_strike": opt.strike,
                        "sell_expiry": opt.expiry,
                        "target_dte": target_dte,
                        "net_credit": round(net_credit, 2),
                    }

    return {"action": "CRITICAL_ALERT_NO_ROLL_AVAILABLE"}
