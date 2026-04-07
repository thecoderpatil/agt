"""
tests/property/strategies.py — Hypothesis strategies for Walker property tests.

All strategies generate valid TradeEvent sequences that satisfy walker
preconditions (single ticker, single household, chronologically sorted).
Zero I/O — pure in-memory event generation.
"""
from __future__ import annotations

import itertools
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hypothesis import strategies as st
from agt_equities.walker import TradeEvent

# ---------------------------------------------------------------------------
# Domain constants (small sets for fast shrinking)
# ---------------------------------------------------------------------------

TICKERS = ['TEST', 'AAPL', 'MSFT']
EXCLUDED_TICKERS = ['SPX', 'VIX', 'NDX']
ACCOUNTS_YASH = ['U21971297', 'U22076329']       # both Yash_Household
ACCOUNT_VIKRAM = 'U22388499'                       # Vikram_Household
HOUSEHOLD_YASH = 'Yash_Household'
HOUSEHOLD_VIKRAM = 'Vikram_Household'
UNKNOWN_ACCOUNT = 'U99999999'

# 31 days in Jan 2026
DATES = [f'2026010{d}' if d < 10 else f'202601{d}' for d in range(1, 32)]
STRIKES = [float(s) for s in range(50, 505, 5)]
EXPIRIES = ['20260110', '20260117', '20260124', '20260131']
RIGHTS = ['P', 'C']

# Counter for unique transaction IDs
_tid_counter = itertools.count(1_000_000)


def _next_tid() -> str:
    return str(next(_tid_counter))


# ---------------------------------------------------------------------------
# Atomic event builders
# ---------------------------------------------------------------------------

def _make_event(**overrides) -> TradeEvent:
    """Build a TradeEvent with sensible defaults. Mirrors test_walker.py helper."""
    defaults = dict(
        source='FLEX_TRADE',
        account_id='U22076329',
        household_id=HOUSEHOLD_YASH,
        ticker='TEST',
        trade_date='20260101',
        date_time='20260101;100000',
        ib_order_id=None,
        transaction_id=_next_tid(),
        asset_category='OPT',
        right='P',
        strike=100.0,
        expiry='20260110',
        buy_sell='SELL',
        open_close='O',
        quantity=1.0,
        trade_price=1.50,
        net_cash=149.35,
        fifo_pnl_realized=0.0,
        transaction_type='ExchTrade',
        notes='',
        currency='USD',
        raw={},
    )
    defaults.update(overrides)
    return TradeEvent(**defaults)


# ---------------------------------------------------------------------------
# Composite strategies
# ---------------------------------------------------------------------------

@st.composite
def ticker_st(draw):
    """Draw a non-excluded ticker."""
    return draw(st.sampled_from(TICKERS))


@st.composite
def account_st(draw):
    """Draw a Yash_Household account."""
    return draw(st.sampled_from(ACCOUNTS_YASH))


@st.composite
def date_index_st(draw, min_idx=0, max_idx=30):
    """Draw a date index into DATES, returns (index, date_str)."""
    idx = draw(st.integers(min_value=min_idx, max_value=max_idx))
    return idx, DATES[idx]


@st.composite
def option_params_st(draw):
    """Draw common option parameters: right, strike, expiry."""
    right = draw(st.sampled_from(RIGHTS))
    strike = draw(st.sampled_from(STRIKES))
    expiry = draw(st.sampled_from(EXPIRIES))
    return right, strike, expiry


@st.composite
def csp_open_event_st(draw, ticker='TEST', account=None, date_idx=None):
    """Generate a CSP_OPEN event (ExchTrade OPT SELL O right=P)."""
    if account is None:
        account = draw(account_st())
    if date_idx is None:
        date_idx, date_str = draw(date_index_st())
    else:
        date_str = DATES[date_idx]
    strike = draw(st.sampled_from(STRIKES))
    expiry = draw(st.sampled_from(EXPIRIES))
    qty = draw(st.integers(min_value=1, max_value=5))
    price = draw(st.floats(min_value=0.10, max_value=20.0, allow_nan=False, allow_infinity=False))
    return date_idx, _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};100000',
        buy_sell='SELL', open_close='O', right='P', strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=price,
        net_cash=price * qty * 100, asset_category='OPT',
        transaction_type='ExchTrade',
    )


@st.composite
def cc_open_event_st(draw, ticker='TEST', account=None, date_idx=None):
    """Generate a CC_OPEN event (ExchTrade OPT SELL O right=C)."""
    if account is None:
        account = draw(account_st())
    if date_idx is None:
        date_idx, date_str = draw(date_index_st())
    else:
        date_str = DATES[date_idx]
    strike = draw(st.sampled_from(STRIKES))
    expiry = draw(st.sampled_from(EXPIRIES))
    qty = draw(st.integers(min_value=1, max_value=5))
    price = draw(st.floats(min_value=0.10, max_value=20.0, allow_nan=False, allow_infinity=False))
    return date_idx, _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};100000',
        buy_sell='SELL', open_close='O', right='C', strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=price,
        net_cash=price * qty * 100, asset_category='OPT',
        transaction_type='ExchTrade',
    )


@st.composite
def long_opt_open_event_st(draw, ticker='TEST', account=None, date_idx=None):
    """Generate a LONG_OPT_OPEN event (ExchTrade OPT BUY O)."""
    if account is None:
        account = draw(account_st())
    if date_idx is None:
        date_idx, date_str = draw(date_index_st())
    else:
        date_str = DATES[date_idx]
    right, strike, expiry = draw(option_params_st())
    qty = draw(st.integers(min_value=1, max_value=3))
    price = draw(st.floats(min_value=0.10, max_value=20.0, allow_nan=False, allow_infinity=False))
    return date_idx, _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};100000',
        buy_sell='BUY', open_close='O', right=right, strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=price,
        net_cash=-price * qty * 100, asset_category='OPT',
        transaction_type='ExchTrade',
    )


@st.composite
def stk_buy_event_st(draw, ticker='TEST', account=None, date_idx=None):
    """Generate a STK_BUY_DIRECT event."""
    if account is None:
        account = draw(account_st())
    if date_idx is None:
        date_idx, date_str = draw(date_index_st())
    else:
        date_str = DATES[date_idx]
    qty = draw(st.integers(min_value=1, max_value=200))
    price = draw(st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False))
    return date_idx, _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};110000',
        buy_sell='BUY', open_close=None, right=None, strike=None,
        expiry=None, quantity=float(qty), trade_price=price,
        net_cash=-price * qty, asset_category='STK',
        transaction_type='ExchTrade',
    )


@st.composite
def stk_sell_event_st(draw, ticker='TEST', account=None, date_idx=None):
    """Generate a STK_SELL_DIRECT event."""
    if account is None:
        account = draw(account_st())
    if date_idx is None:
        date_idx, date_str = draw(date_index_st())
    else:
        date_str = DATES[date_idx]
    qty = draw(st.integers(min_value=1, max_value=200))
    price = draw(st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False))
    return date_idx, _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};110000',
        buy_sell='SELL', open_close=None, right=None, strike=None,
        expiry=None, quantity=float(qty), trade_price=price,
        net_cash=price * qty, asset_category='STK',
        transaction_type='ExchTrade',
    )


@st.composite
def assignment_pair_st(draw, ticker='TEST', account=None, date_idx=None):
    """Generate a matched CSP_OPEN → ASSIGN_OPT_LEG + ASSIGN_STK_LEG sequence.

    Returns (events_list, date_indices) where assignment day > open day.
    """
    if account is None:
        account = draw(account_st())
    if date_idx is None:
        open_idx = draw(st.integers(min_value=0, max_value=20))
    else:
        open_idx = date_idx
    assign_idx = draw(st.integers(min_value=open_idx + 1, max_value=min(open_idx + 10, 30)))

    strike = draw(st.sampled_from(STRIKES))
    expiry = draw(st.sampled_from(EXPIRIES))
    qty = draw(st.integers(min_value=1, max_value=3))
    premium = draw(st.floats(min_value=0.50, max_value=15.0, allow_nan=False, allow_infinity=False))

    open_date = DATES[open_idx]
    assign_date = DATES[assign_idx]

    csp_open = _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=open_date, date_time=f'{open_date};100000',
        buy_sell='SELL', open_close='O', right='P', strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=premium,
        net_cash=premium * qty * 100, asset_category='OPT',
        transaction_type='ExchTrade',
    )
    assign_opt = _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=assign_date, date_time=f'{assign_date};162000',
        buy_sell='BUY', open_close='C', right='P', strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=0.0,
        net_cash=0.0, asset_category='OPT',
        transaction_type='BookTrade', notes='A',
    )
    assign_stk = _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=assign_date, date_time=f'{assign_date};162000',
        buy_sell='BUY', open_close=None, right=None, strike=None,
        expiry=None, quantity=float(qty * 100), trade_price=strike,
        net_cash=-strike * qty * 100, asset_category='STK',
        transaction_type='BookTrade', notes='A',
    )
    return [csp_open, assign_opt, assign_stk], [open_idx, assign_idx, assign_idx]


@st.composite
def transfer_pair_st(draw, ticker='TEST', date_idx=None):
    """Generate matched TRANSFER_OUT + TRANSFER_IN (same household, diff accounts)."""
    acct_out, acct_in = 'U21971297', 'U22076329'
    if date_idx is None:
        date_idx = draw(st.integers(min_value=0, max_value=30))
    date_str = DATES[date_idx]
    qty = draw(st.integers(min_value=1, max_value=100))
    price = draw(st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False))

    out_ev = _make_event(
        source='FLEX_TRANSFER',
        ticker=ticker, account_id=acct_out, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};120000',
        buy_sell='SELL', open_close='OUT', right=None, strike=None,
        expiry=None, quantity=float(qty), trade_price=price,
        net_cash=0.0, asset_category='STK',
        transaction_type='Transfer',
    )
    in_ev = _make_event(
        source='FLEX_TRANSFER',
        ticker=ticker, account_id=acct_in, household_id=HOUSEHOLD_YASH,
        trade_date=date_str, date_time=f'{date_str};120001',
        buy_sell='BUY', open_close='IN', right=None, strike=None,
        expiry=None, quantity=float(qty), trade_price=price,
        net_cash=0.0, asset_category='STK',
        transaction_type='Transfer',
    )
    return [out_ev, in_ev], date_idx


@st.composite
def excluded_ticker_events_st(draw):
    """Generate 1-5 events for an excluded ticker."""
    ticker = draw(st.sampled_from(EXCLUDED_TICKERS))
    n = draw(st.integers(min_value=1, max_value=5))
    events = []
    for i in range(n):
        date_str = DATES[min(i, 30)]
        events.append(_make_event(
            ticker=ticker, household_id=HOUSEHOLD_YASH,
            trade_date=date_str, date_time=f'{date_str};100000',
            buy_sell='SELL', open_close='O', right='P',
            transaction_type='ExchTrade', asset_category='OPT',
        ))
    return events


@st.composite
def valid_event_sequence_st(draw, ticker='TEST', min_events=2, max_events=12):
    """Generate a valid event sequence for a single (household, ticker).

    Starts with a cycle-originating event (CSP_OPEN), then adds random
    follow-up events. All share same ticker + household. Sorted by date.
    """
    # Always start with a CSP_OPEN to originate a WHEEL cycle
    open_idx, opener = draw(csp_open_event_st(ticker=ticker, date_idx=0))

    follow_count = draw(st.integers(min_value=min_events - 1, max_value=max_events - 1))
    dated_events = [(0, opener)]

    event_generators = [
        csp_open_event_st(ticker=ticker),
        cc_open_event_st(ticker=ticker),
        long_opt_open_event_st(ticker=ticker),
        stk_buy_event_st(ticker=ticker),
        stk_sell_event_st(ticker=ticker),
    ]

    for _ in range(follow_count):
        gen = draw(st.sampled_from(event_generators))
        idx, ev = draw(gen)
        dated_events.append((idx, ev))

    # Sort by date index, then by canonical_sort_key approximation
    dated_events.sort(key=lambda pair: (pair[0], pair[1].date_time))
    return [ev for _, ev in dated_events]


@st.composite
def satellite_long_opt_sequence_st(draw, ticker='TEST'):
    """Generate a pure long-option sequence that creates a SATELLITE cycle."""
    date_idx = draw(st.integers(min_value=0, max_value=20))
    close_idx = draw(st.integers(min_value=date_idx + 1, max_value=min(date_idx + 10, 30)))
    right, strike, expiry = draw(option_params_st())
    account = draw(account_st())
    qty = draw(st.integers(min_value=1, max_value=3))
    price = draw(st.floats(min_value=0.10, max_value=20.0, allow_nan=False, allow_infinity=False))

    open_ev = _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=DATES[date_idx], date_time=f'{DATES[date_idx]};100000',
        buy_sell='BUY', open_close='O', right=right, strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=price,
        net_cash=-price * qty * 100, asset_category='OPT',
        transaction_type='ExchTrade',
    )
    close_ev = _make_event(
        ticker=ticker, account_id=account, household_id=HOUSEHOLD_YASH,
        trade_date=DATES[close_idx], date_time=f'{DATES[close_idx]};100000',
        buy_sell='SELL', open_close='C', right=right, strike=strike,
        expiry=expiry, quantity=float(qty), trade_price=price * 0.5,
        net_cash=price * 0.5 * qty * 100, asset_category='OPT',
        transaction_type='ExchTrade',
    )
    return [open_ev, close_ev]
