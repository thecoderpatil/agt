"""tests/wheel/test_paper_evaluator.py — WHEEL-6 end-to-end evaluator smoke.

Pulls live SPY spot + a narrow call-chain slice from paper gateway,
constructs a synthetic CC `Position` + `MarketSnapshot` + `PortfolioContext`,
and invokes `roll_engine.evaluate()`. Asserts the returned result is one
of the known EvalResult subclasses.

NOT a correctness test — the unit suite in test_roll_engine.py covers
routing. This test's job is to catch shape drift between the live IB
data we feed the evaluator and the dataclass contracts the evaluator
accepts. A green run here is proof that the wheel pipeline can be
driven from a live paper socket without raising.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isnan

import pytest

pytestmark = pytest.mark.paper


from agt_equities.roll_engine import (
    AlertResult,
    AssignResult,
    ConstraintMatrix,
    HarvestResult,
    HoldResult,
    LiquidateResult,
    MarketSnapshot,
    OptionQuote,
    PortfolioContext,
    Position,
    RollResult,
    evaluate,
)


_RESULT_TYPES = (
    HoldResult, HarvestResult, RollResult,
    AssignResult, LiquidateResult, AlertResult,
)


def _nan_safe(x) -> float:
    """Coerce None / nan to 0.0 so OptionQuote construction never blows up.

    IBKR often serves nan for bid/ask on illiquid strikes or outside RTH.
    The evaluator handles zero-bid as no-quote; nan propagates and
    poisons comparisons.
    """
    if x is None:
        return 0.0
    try:
        return 0.0 if isnan(float(x)) else float(x)
    except (TypeError, ValueError):
        return 0.0


def _build_live_snapshot(ib, loop, asof: date) -> MarketSnapshot:
    """Construct a MarketSnapshot from live paper-gateway SPY data.

    Picks the nearest listed expiry >= 7 DTE and the 10 strikes closest
    to spot on the call side. `current_call` = ATM+2 (synthetic short).
    """
    from ib_async import Option, Stock

    spy = Stock("SPY", "SMART", "USD")
    (spy_q,) = loop.run_until_complete(ib.qualifyContractsAsync(spy))

    tickers = loop.run_until_complete(ib.reqTickersAsync(spy_q))
    if not tickers:
        pytest.skip("reqTickersAsync returned empty for SPY")
    tk = tickers[0]
    spot = tk.marketPrice() or tk.last or tk.close
    if spot is None or isnan(float(spot)) or spot <= 0:
        pytest.skip(f"SPY spot unreadable from paper: ticker={tk!r}")
    spot = float(spot)

    params = loop.run_until_complete(
        ib.reqSecDefOptParamsAsync(spy_q.symbol, "", spy_q.secType, spy_q.conId)
    )
    smart = [p for p in params if p.exchange == "SMART"]
    if not smart:
        pytest.skip("no SMART chain params")
    chain_meta = smart[0]

    target_exp_str = None
    for e in sorted(chain_meta.expirations):
        try:
            ed = datetime.strptime(e, "%Y%m%d").date()
        except ValueError:
            continue
        if (ed - asof).days >= 7:
            target_exp_str = e
            break
    if target_exp_str is None:
        pytest.skip("no expiry >= 7 DTE in chain")
    target_expiry = datetime.strptime(target_exp_str, "%Y%m%d").date()

    strikes = sorted(chain_meta.strikes, key=lambda s: abs(s - spot))[:10]
    contracts = [
        Option("SPY", target_exp_str, k, "C", "SMART",
               tradingClass=chain_meta.tradingClass)
        for k in strikes
    ]
    qualified = loop.run_until_complete(ib.qualifyContractsAsync(*contracts))
    if not qualified:
        pytest.skip("no option contracts qualified")

    quotes = loop.run_until_complete(ib.reqTickersAsync(*qualified))

    chain = []
    for q in quotes:
        c = q.contract
        if c is None or c.strike is None:
            continue
        greeks = getattr(q, "modelGreeks", None)
        chain.append(
            OptionQuote(
                strike=float(c.strike),
                expiry=target_expiry,
                bid=_nan_safe(q.bid),
                ask=_nan_safe(q.ask),
                delta=_nan_safe(getattr(greeks, "delta", None) if greeks else None),
                iv=_nan_safe(getattr(greeks, "impliedVol", None) if greeks else None),
            )
        )
    if not chain:
        pytest.skip("chain quote extraction yielded 0 OptionQuotes")

    chain_sorted = sorted(chain, key=lambda q: abs(q.strike - (spot + 2)))
    current_call = chain_sorted[0]

    return MarketSnapshot(
        ticker="SPY",
        spot=spot,
        iv30=_nan_safe(chain[0].iv),
        chain=tuple(chain),
        current_call=current_call,
        asof=asof,
    )


def _build_position(snap: MarketSnapshot, *, basis: float,
                    opened_days_ago: int = 3) -> Position:
    """Synthetic Position shorting snap.current_call."""
    opened = snap.asof - timedelta(days=opened_days_ago)
    return Position(
        ticker=snap.ticker,
        account_id="U99999999",
        household="PaperHarness_Household",
        strike=snap.current_call.strike,
        expiry=snap.current_call.expiry,
        quantity=1,
        cost_basis=basis,
        inception_delta=None,
        opened_at=opened,
        avg_premium_collected=1.50,
        assigned_basis=basis,
        adjusted_basis=basis,
        initial_credit=1.50,
        initial_dte=(snap.current_call.expiry - opened).days,
    )


def test_build_snapshot_shape(ib_paper, event_loop) -> None:
    """MarketSnapshot built from live SPY data has evaluator-valid shape."""
    asof = date.today()
    snap = _build_live_snapshot(ib_paper, event_loop, asof)
    assert snap.spot > 0
    assert len(snap.chain) > 0
    assert snap.current_call.strike > 0
    assert snap.asof == asof


def test_evaluate_defense_on_live_spy(ib_paper, event_loop) -> None:
    """Defense regime (basis > spot): evaluator returns a valid EvalResult."""
    asof = date.today()
    snap = _build_live_snapshot(ib_paper, event_loop, asof)
    pos = _build_position(snap, basis=snap.spot + 20.0)
    ctx = PortfolioContext(
        household="PaperHarness_Household", mode="WARTIME", leverage=1.71,
    )
    result = evaluate(pos, snap, ctx, ConstraintMatrix())
    assert isinstance(result, _RESULT_TYPES), (
        f"evaluate returned {type(result).__name__}, not a known EvalResult"
    )


def test_evaluate_offense_on_live_spy(ib_paper, event_loop) -> None:
    """Offense regime (basis < spot): evaluator returns a valid EvalResult."""
    asof = date.today()
    snap = _build_live_snapshot(ib_paper, event_loop, asof)
    pos = _build_position(snap, basis=max(1.0, snap.spot - 20.0))
    ctx = PortfolioContext(
        household="PaperHarness_Household", mode="PEACETIME", leverage=1.20,
    )
    result = evaluate(pos, snap, ctx, ConstraintMatrix())
    assert isinstance(result, _RESULT_TYPES), (
        f"evaluate returned {type(result).__name__}, not a known EvalResult"
    )
