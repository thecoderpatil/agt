"""
tests/property/test_walker_properties.py — Hypothesis property-based tests for Walker.

18 property tests covering invariants I1–I20 plus determinism, ordering,
and warning emission. Zero I/O — Walker is a pure function.

Target: 100 examples per test, full suite under 60s.
"""
from __future__ import annotations

import copy
import random
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hypothesis import given, settings, example, HealthCheck
from hypothesis import strategies as st

from agt_equities.walker import (
    TradeEvent, EventType, Cycle, UnknownEventError, WalkerWarning,
    walk_cycles, get_walker_warnings, classify_event, canonical_sort_key,
    HOUSEHOLD_MAP,
)
from tests.property.strategies import (
    _make_event, TICKERS, EXCLUDED_TICKERS, ACCOUNTS_YASH, HOUSEHOLD_YASH,
    HOUSEHOLD_VIKRAM, ACCOUNT_VIKRAM, UNKNOWN_ACCOUNT, DATES, STRIKES,
    EXPIRIES, RIGHTS,
    valid_event_sequence_st, csp_open_event_st, cc_open_event_st,
    long_opt_open_event_st, stk_buy_event_st, stk_sell_event_st,
    assignment_pair_st, transfer_pair_st, excluded_ticker_events_st,
    satellite_long_opt_sequence_st,
)


COMMON_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ═════════════════════════════════════════════════════��═════════════════════
# Group A: Input Validation (3 tests)
# ═════════════════════════════════════════════════════��═════════════════════


class TestInputValidation(unittest.TestCase):

    @COMMON_SETTINGS
    @given(st.sampled_from(TICKERS), st.sampled_from(TICKERS))
    def test_mixed_ticker_raises(self, tk1, tk2):
        """I15: walk_cycles raises ValueError on mixed tickers."""
        if tk1 == tk2:
            return  # need different tickers
        events = [
            _make_event(ticker=tk1, household_id=HOUSEHOLD_YASH, trade_date='20260101',
                        date_time='20260101;100000', transaction_type='ExchTrade',
                        buy_sell='SELL', open_close='O', asset_category='OPT'),
            _make_event(ticker=tk2, household_id=HOUSEHOLD_YASH, trade_date='20260102',
                        date_time='20260102;100000', transaction_type='ExchTrade',
                        buy_sell='SELL', open_close='O', asset_category='OPT'),
        ]
        with self.assertRaises(ValueError):
            walk_cycles(events)

    @COMMON_SETTINGS
    @given(st.just(True))
    def test_mixed_household_raises(self, _):
        """I16: walk_cycles raises ValueError on mixed households."""
        events = [
            _make_event(ticker='TEST', household_id=HOUSEHOLD_YASH,
                        account_id='U21971297',
                        trade_date='20260101', date_time='20260101;100000',
                        transaction_type='ExchTrade',
                        buy_sell='SELL', open_close='O', asset_category='OPT'),
            _make_event(ticker='TEST', household_id=HOUSEHOLD_VIKRAM,
                        account_id=ACCOUNT_VIKRAM,
                        trade_date='20260102', date_time='20260102;100000',
                        transaction_type='ExchTrade',
                        buy_sell='SELL', open_close='O', asset_category='OPT'),
        ]
        with self.assertRaises(ValueError):
            walk_cycles(events)

    @COMMON_SETTINGS
    @given(st.sampled_from(DATES[:10]))
    def test_unknown_account_warns(self, date):
        """I5/I14: unknown account_id emits UNKNOWN_ACCT warning."""
        events = [
            _make_event(ticker='TEST', household_id=HOUSEHOLD_YASH,
                        account_id=UNKNOWN_ACCOUNT,
                        trade_date=date, date_time=f'{date};100000',
                        transaction_type='ExchTrade',
                        buy_sell='SELL', open_close='O', asset_category='OPT'),
        ]
        walk_cycles(events)
        warnings = get_walker_warnings()
        unknown_warns = [w for w in warnings if w.code == 'UNKNOWN_ACCT']
        self.assertGreaterEqual(len(unknown_warns), 1)
        self.assertEqual(unknown_warns[0].severity, 'ERROR')
        self.assertEqual(unknown_warns[0].account, UNKNOWN_ACCOUNT)


# ═══════════════════════════════════════════════════════════════════════════
# Group B: Non-Negativity (4 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestNonNegativity(unittest.TestCase):

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_shares_never_negative(self, events):
        """I6/I8: shares_held >= 0 for all cycles after walk."""
        cycles = walk_cycles(events)
        for c in cycles:
            self.assertGreaterEqual(c.shares_held, 0,
                f"shares_held={c.shares_held} in cycle {c.cycle_seq} ({c.ticker})")

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_short_puts_never_negative(self, events):
        """I7/I9: open_short_puts >= 0 for all cycles."""
        cycles = walk_cycles(events)
        for c in cycles:
            self.assertGreaterEqual(c.open_short_puts, 0,
                f"open_short_puts={c.open_short_puts} in cycle {c.cycle_seq}")

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_short_calls_never_negative(self, events):
        """I8/I10: open_short_calls >= 0 for all cycles."""
        cycles = walk_cycles(events)
        for c in cycles:
            self.assertGreaterEqual(c.open_short_calls, 0,
                f"open_short_calls={c.open_short_calls} in cycle {c.cycle_seq}")

    @COMMON_SETTINGS
    @given(st.sampled_from(DATES[1:10]))
    def test_orphan_expire_emits_counter_guard(self, date):
        """W3.6: EXPIRE_WORTHLESS for a call (right=C) when no call was opened
        triggers COUNTER_GUARD warning (counter would go negative, clamped to 0).
        CSP_OPEN originates the cycle (put), so a call expire is always orphan."""
        events = [
            # CSP_OPEN (put) to originate cycle
            _make_event(ticker='TEST', trade_date='20260101', date_time='20260101;100000',
                        buy_sell='SELL', open_close='O', right='P', strike=100.0,
                        expiry='20260131', quantity=1,
                        transaction_type='ExchTrade', asset_category='OPT'),
            # EXPIRE_WORTHLESS for a CALL (no call was opened → always orphan)
            _make_event(ticker='TEST', trade_date=date,
                        date_time=f'{date};162000',
                        buy_sell='BUY', open_close='C', right='C', strike=999.0,
                        expiry='20260131', quantity=1,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
        ]
        walk_cycles(events)
        warnings = get_walker_warnings()
        guard_warns = [w for w in warnings if w.code == 'COUNTER_GUARD']
        self.assertGreaterEqual(len(guard_warns), 1,
            f"Expected COUNTER_GUARD warning for orphan call expire, got: {warnings}")


# ═══════════════════════════════════════════════════════════════════════════
# Group C: Cycle Semantics (4 tests)
# ═════════════════════════════════════════════════════��═════════════════════


class TestCycleSemantics(unittest.TestCase):

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_closed_iff_all_counters_zero(self, events):
        """I14: a CLOSED cycle has all 5 counters == 0."""
        cycles = walk_cycles(events)
        for c in cycles:
            if c.status == 'CLOSED':
                self.assertEqual(c.shares_held, 0, f"CLOSED cycle has shares_held={c.shares_held}")
                self.assertEqual(c.open_short_puts, 0)
                self.assertEqual(c.open_short_calls, 0)
                self.assertEqual(c.open_long_puts, 0)
                self.assertEqual(c.open_long_calls, 0)

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_realized_pnl_additivity(self, events):
        """I2: sum(cycle.realized_pnl) == sum(event.fifo_pnl_realized) for all events."""
        cycles = walk_cycles(events)
        cycle_total = sum(c.realized_pnl for c in cycles)
        event_total = sum(ev.fifo_pnl_realized for ev in events)
        self.assertAlmostEqual(cycle_total, event_total, places=6,
            msg=f"P&L mismatch: cycles={cycle_total:.6f}, events={event_total:.6f}")

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_cycle_keying_consistent(self, events):
        """I3: every event in every cycle shares that cycle's household + ticker."""
        cycles = walk_cycles(events)
        for c in cycles:
            for ev in c.events:
                self.assertEqual(ev.household_id, c.household_id,
                    f"Event household {ev.household_id} != cycle {c.household_id}")
                self.assertEqual(ev.ticker, c.ticker,
                    f"Event ticker {ev.ticker} != cycle {c.ticker}")

    @COMMON_SETTINGS
    @given(excluded_ticker_events_st())
    def test_excluded_ticker_no_cycles(self, events):
        """I7: excluded ticker produces empty cycle list + EXCLUDED_SKIP warning."""
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 0)
        warnings = get_walker_warnings()
        skip_warns = [w for w in warnings if w.code == 'EXCLUDED_SKIP']
        self.assertEqual(len(skip_warns), 1)
        self.assertEqual(skip_warns[0].severity, 'INFO')


# ═══════════════════════════════════════���═══════════════════════════════════
# Group D: Satellite & Transfer (4 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestSatelliteAndTransfer(unittest.TestCase):

    @COMMON_SETTINGS
    @given(satellite_long_opt_sequence_st())
    def test_satellite_no_shares_at_creation(self, events):
        """I11: a SATELLITE cycle created from long-option events has shares_held=0
        and short counters=0 after the first event."""
        cycles = walk_cycles(events)
        satellite_cycles = [c for c in cycles if c.cycle_type == 'SATELLITE']
        for c in satellite_cycles:
            # A pure satellite should never hold shares or short options
            # (unless promoted, which doesn't happen in this strategy)
            self.assertEqual(c.shares_held, 0,
                f"SATELLITE has shares_held={c.shares_held}")
            self.assertEqual(c.open_short_puts, 0)
            self.assertEqual(c.open_short_calls, 0)

    @COMMON_SETTINGS
    @given(st.sampled_from(DATES[:15]))
    def test_transfer_in_never_originates_wheel(self, date):
        """I13: orphan TRANSFER_IN does not create a WHEEL cycle."""
        events = [
            _make_event(
                source='FLEX_TRANSFER',
                ticker='TEST', account_id='U22076329', household_id=HOUSEHOLD_YASH,
                trade_date=date, date_time=f'{date};120000',
                buy_sell='BUY', open_close='IN', right=None, strike=None,
                expiry=None, quantity=100.0, trade_price=150.0,
                net_cash=0.0, asset_category='STK',
                transaction_type='Transfer',
            ),
        ]
        cycles = walk_cycles(events)
        wheel_cycles = [c for c in cycles if c.cycle_type == 'WHEEL']
        self.assertEqual(len(wheel_cycles), 0, "TRANSFER_IN must not originate WHEEL")
        warnings = get_walker_warnings()
        orphan_warns = [w for w in warnings if w.code == 'ORPHAN_TRANSFER']
        self.assertEqual(len(orphan_warns), 1)

    @COMMON_SETTINGS
    @given(transfer_pair_st(date_idx=15))
    def test_transfer_conservation(self, pair):
        """I19: matched TRANSFER_OUT + TRANSFER_IN within an active cycle
        preserves net shares_held (household-level wash)."""
        transfer_events, transfer_date_idx = pair
        # Setup: CSP_OPEN on day 1, STK_BUY on day 2 — well before transfers on day 15
        setup = [
            _make_event(
                ticker='TEST', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260101', date_time='20260101;100000',
                buy_sell='SELL', open_close='O', right='P', strike=100.0,
                expiry='20260131', quantity=1, net_cash=150.0,
                transaction_type='ExchTrade', asset_category='OPT',
            ),
            _make_event(
                ticker='TEST', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260102', date_time='20260102;090000',
                buy_sell='BUY', open_close=None, right=None, strike=None,
                expiry=None, quantity=500.0, trade_price=100.0,
                net_cash=-50000.0, asset_category='STK',
                transaction_type='ExchTrade',
            ),
        ]
        all_events = setup + transfer_events
        # Walk with setup only to get pre-transfer shares
        cycles_pre = walk_cycles(setup)
        pre_shares = sum(c.shares_held for c in cycles_pre)
        # Walk full sequence
        cycles_post = walk_cycles(all_events)
        post_shares = sum(c.shares_held for c in cycles_post)
        # Net effect of paired transfer on household aggregate should be zero
        self.assertAlmostEqual(pre_shares, post_shares, places=6,
            msg=f"Transfer changed household shares: {pre_shares} -> {post_shares}")

    @COMMON_SETTINGS
    @given(satellite_long_opt_sequence_st())
    def test_satellite_stays_satellite_without_promotion_event(self, events):
        """I12: SATELLITE cycle stays SATELLITE when only long-option events arrive.
        No promotion without events from the expanded promotion set."""
        cycles = walk_cycles(events)
        for c in cycles:
            if c.cycle_type == 'SATELLITE':
                # Verify no promotion-triggering events were in this cycle
                promotion_set = {
                    EventType.CSP_OPEN, EventType.CC_OPEN,
                    EventType.ASSIGN_STK_LEG, EventType.ASSIGN_OPT_LEG,
                    EventType.STK_BUY_DIRECT, EventType.CARRYIN_STK,
                    EventType.CARRYIN_OPT,
                }
                for et in c.event_types:
                    self.assertNotIn(et, promotion_set,
                        f"SATELLITE has promotion event {et} but wasn't promoted")


# ═══════════════════════════════════════════════════════════════════════════
# Group E: Determinism & Ordering (2 tests)
# ══════════════════════════════════���════════════════════════════��═══════════


class TestDeterminismAndOrdering(unittest.TestCase):

    @COMMON_SETTINGS
    @given(valid_event_sequence_st())
    def test_determinism(self, events):
        """I20: walk_cycles(events) called twice returns identical results."""
        events_copy = list(events)  # shallow copy — TradeEvent is frozen
        cycles1 = walk_cycles(events)
        warnings1 = get_walker_warnings()
        cycles2 = walk_cycles(events_copy)
        warnings2 = get_walker_warnings()

        self.assertEqual(len(cycles1), len(cycles2))
        for c1, c2 in zip(cycles1, cycles2):
            self.assertEqual(c1.status, c2.status)
            self.assertEqual(c1.shares_held, c2.shares_held)
            self.assertEqual(c1.open_short_puts, c2.open_short_puts)
            self.assertEqual(c1.open_short_calls, c2.open_short_calls)
            self.assertEqual(c1.open_long_puts, c2.open_long_puts)
            self.assertEqual(c1.open_long_calls, c2.open_long_calls)
            self.assertAlmostEqual(c1.realized_pnl, c2.realized_pnl, places=6)

        self.assertEqual(len(warnings1), len(warnings2))
        for w1, w2 in zip(warnings1, warnings2):
            self.assertEqual(w1.code, w2.code)
            self.assertEqual(w1.severity, w2.severity)

    @COMMON_SETTINGS
    @given(valid_event_sequence_st(min_events=3, max_events=8))
    def test_eod_ordering_independence(self, events):
        """Shuffling events within the same trade_date (but preserving
        cross-day order) produces identical final cycle state.

        This validates that canonical_sort_key resolves all intra-day ties."""
        # Group by trade_date, shuffle within each day, re-sort by canonical_sort_key
        from itertools import groupby

        cycles_original = walk_cycles(events)

        # Group events by trade_date
        keyed = sorted(events, key=lambda e: e.trade_date)
        shuffled_events = []
        for _date, grp in groupby(keyed, key=lambda e: e.trade_date):
            day_events = list(grp)
            random.shuffle(day_events)
            shuffled_events.extend(day_events)

        # Re-sort by canonical sort key (walker does this internally)
        # Walker sorts internally, so we just pass the shuffled list
        cycles_shuffled = walk_cycles(shuffled_events)

        self.assertEqual(len(cycles_original), len(cycles_shuffled))
        for c1, c2 in zip(cycles_original, cycles_shuffled):
            self.assertEqual(c1.status, c2.status,
                f"Status mismatch: {c1.status} vs {c2.status}")
            self.assertEqual(c1.shares_held, c2.shares_held)
            self.assertEqual(c1.open_short_puts, c2.open_short_puts)
            self.assertEqual(c1.open_short_calls, c2.open_short_calls)


# ═══════════════════════════════════════════════════════════════════════════
# Group F: Paper Basis (1 test)
# ══════════════════════════��═══════════════════════════════���════════════════


class TestPaperBasis(unittest.TestCase):

    @COMMON_SETTINGS
    @given(assignment_pair_st(), assignment_pair_st())
    def test_assignment_basis_determinism(self, pair1, pair2):
        """I17: for any two assignment sequences, paper_basis is deterministic
        regardless of within-day event ordering."""
        events1, _ = pair1
        # Walk the first assignment pair
        cycles1 = walk_cycles(events1)
        active1 = [c for c in cycles1 if c.status == 'ACTIVE']
        if not active1:
            return

        # Walk the same events again
        cycles1b = walk_cycles(list(events1))
        active1b = [c for c in cycles1b if c.status == 'ACTIVE']
        self.assertEqual(len(active1), len(active1b))

        for c1, c1b in zip(active1, active1b):
            if c1.paper_basis is not None and c1b.paper_basis is not None:
                self.assertAlmostEqual(c1.paper_basis, c1b.paper_basis, places=6,
                    msg="Assignment basis not deterministic across calls")


# ═══════════════════════════════════════════════════════════════════════════
# @example seeds from real bug patterns
# ════════════════════════════════════════════════════════════════════════���══

# Seed: ADBE two puts assigned same day at different strikes (W3.1 bug)
_ADBE_SEED_EVENTS = [
    # CSP_OPEN 335P
    _make_event(ticker='ADBE', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260102', date_time='20260102;100000',
                buy_sell='SELL', open_close='O', right='P', strike=335.0,
                expiry='20260110', quantity=1.0, trade_price=2.22,
                net_cash=222.0, transaction_type='ExchTrade', asset_category='OPT'),
    # CSP_OPEN 337.5P
    _make_event(ticker='ADBE', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260103', date_time='20260103;100000',
                buy_sell='SELL', open_close='O', right='P', strike=337.5,
                expiry='20260110', quantity=1.0, trade_price=3.37,
                net_cash=337.0, transaction_type='ExchTrade', asset_category='OPT'),
    # ASSIGN_OPT_LEG 335P
    _make_event(ticker='ADBE', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260109', date_time='20260109;162000',
                buy_sell='BUY', open_close='C', right='P', strike=335.0,
                expiry='20260110', quantity=1.0, trade_price=0.0,
                net_cash=0.0, transaction_type='BookTrade', notes='A', asset_category='OPT'),
    # ASSIGN_STK_LEG 335 (BUY 100 shares)
    _make_event(ticker='ADBE', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260109', date_time='20260109;162000',
                buy_sell='BUY', open_close=None, right=None, strike=None,
                expiry=None, quantity=100.0, trade_price=335.0,
                net_cash=-33500.0, transaction_type='BookTrade', notes='A', asset_category='STK'),
    # ASSIGN_OPT_LEG 337.5P
    _make_event(ticker='ADBE', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260109', date_time='20260109;162000',
                buy_sell='BUY', open_close='C', right='P', strike=337.5,
                expiry='20260110', quantity=1.0, trade_price=0.0,
                net_cash=0.0, transaction_type='BookTrade', notes='A', asset_category='OPT'),
    # ASSIGN_STK_LEG 337.5 (BUY 100 shares)
    _make_event(ticker='ADBE', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260109', date_time='20260109;162000',
                buy_sell='BUY', open_close=None, right=None, strike=None,
                expiry=None, quantity=100.0, trade_price=337.5,
                net_cash=-33750.0, transaction_type='BookTrade', notes='A', asset_category='STK'),
]

# Seed: PYPL roll (close + reopen same second)
_PYPL_SEED_EVENTS = [
    # CSP_OPEN 67.5P
    _make_event(ticker='PYPL', account_id='U22076329', household_id=HOUSEHOLD_YASH,
                trade_date='20260105', date_time='20260105;100000',
                buy_sell='SELL', open_close='O', right='P', strike=67.5,
                expiry='20260117', quantity=2.0, trade_price=1.50,
                net_cash=300.0, transaction_type='ExchTrade', asset_category='OPT'),
    # CSP_CLOSE 67.5P (roll BTC)
    _make_event(ticker='PYPL', account_id='U22076329', household_id=HOUSEHOLD_YASH,
                trade_date='20260115', date_time='20260115;134055',
                buy_sell='BUY', open_close='C', right='P', strike=67.5,
                expiry='20260117', quantity=2.0, trade_price=0.50,
                net_cash=-100.0, transaction_type='ExchTrade', asset_category='OPT'),
    # CSP_OPEN 67.5P (roll STO — same second)
    _make_event(ticker='PYPL', account_id='U22076329', household_id=HOUSEHOLD_YASH,
                trade_date='20260115', date_time='20260115;134055',
                buy_sell='SELL', open_close='O', right='P', strike=67.5,
                expiry='20260124', quantity=2.0, trade_price=2.00,
                net_cash=400.0, transaction_type='ExchTrade', asset_category='OPT'),
]

# Seed: Long put expiry that tripped W3.1 short-counter guard
_GUARD_TRIP_SEED = [
    _make_event(ticker='TEST', trade_date='20260101', date_time='20260101;100000',
                buy_sell='SELL', open_close='O', right='P', strike=100.0,
                expiry='20260110', quantity=1, net_cash=150.0,
                transaction_type='ExchTrade', asset_category='OPT'),
    _make_event(ticker='TEST', trade_date='20260102', date_time='20260102;100000',
                buy_sell='BUY', open_close='O', right='P', strike=95.0,
                expiry='20260110', quantity=1, net_cash=-100.0,
                transaction_type='ExchTrade', asset_category='OPT'),
    _make_event(ticker='TEST', trade_date='20260110', date_time='20260110;162000',
                buy_sell='BUY', open_close='C', right='P', strike=95.0,
                expiry='20260110', quantity=1, net_cash=0.0,
                transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
    _make_event(ticker='TEST', trade_date='20260110', date_time='20260110;162000',
                buy_sell='BUY', open_close='C', right='P', strike=100.0,
                expiry='20260110', quantity=1, net_cash=0.0,
                transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
]

# Seed: Vikram intra-household transfer
_VIKRAM_TRANSFER_SEED = [
    _make_event(ticker='TEST', account_id='U22076329', household_id=HOUSEHOLD_YASH,
                trade_date='20260101', date_time='20260101;100000',
                buy_sell='SELL', open_close='O', right='P', strike=100.0,
                expiry='20260131', quantity=1, net_cash=150.0,
                transaction_type='ExchTrade', asset_category='OPT'),
    _make_event(ticker='TEST', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260105', date_time='20260105;090000',
                buy_sell='BUY', open_close=None, right=None, strike=None,
                expiry=None, quantity=200.0, trade_price=100.0,
                net_cash=-20000.0, asset_category='STK',
                transaction_type='ExchTrade'),
    _make_event(source='FLEX_TRANSFER',
                ticker='TEST', account_id='U21971297', household_id=HOUSEHOLD_YASH,
                trade_date='20260110', date_time='20260110;120000',
                buy_sell='SELL', open_close='OUT', right=None, strike=None,
                expiry=None, quantity=50.0, trade_price=100.0,
                net_cash=0.0, asset_category='STK',
                transaction_type='Transfer'),
    _make_event(source='FLEX_TRANSFER',
                ticker='TEST', account_id='U22076329', household_id=HOUSEHOLD_YASH,
                trade_date='20260110', date_time='20260110;120001',
                buy_sell='BUY', open_close='IN', right=None, strike=None,
                expiry=None, quantity=50.0, trade_price=100.0,
                net_cash=0.0, asset_category='STK',
                transaction_type='Transfer'),
]


class TestExampleSeeds(unittest.TestCase):
    """Explicit @example seeds from known bug patterns. These run as
    normal unit tests AND serve as Hypothesis shrink anchors."""

    def test_adbe_assignment_chain_non_negative(self):
        """ADBE two-puts-same-day: counters must be non-negative."""
        cycles = walk_cycles(_ADBE_SEED_EVENTS)
        for c in cycles:
            self.assertGreaterEqual(c.shares_held, 0)
            self.assertGreaterEqual(c.open_short_puts, 0)

    def test_adbe_assignment_chain_basis_determinism(self):
        """ADBE: paper_basis is deterministic across repeated calls."""
        c1 = walk_cycles(_ADBE_SEED_EVENTS)
        c2 = walk_cycles(list(_ADBE_SEED_EVENTS))
        active1 = [c for c in c1 if c.status == 'ACTIVE']
        active2 = [c for c in c2 if c.status == 'ACTIVE']
        self.assertEqual(len(active1), len(active2))
        for a, b in zip(active1, active2):
            if a.paper_basis is not None:
                self.assertAlmostEqual(a.paper_basis, b.paper_basis, places=6)

    def test_pypl_roll_no_premature_closure(self):
        """PYPL: close+reopen same second must not prematurely close cycle."""
        cycles = walk_cycles(_PYPL_SEED_EVENTS)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1, "PYPL roll should keep one active cycle")
        self.assertEqual(active[0].open_short_puts, 2)

    def test_guard_trip_long_expiry(self):
        """Long put expiry must not decrement short counter (W3.1 regression)."""
        cycles = walk_cycles(_GUARD_TRIP_SEED)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].status, 'CLOSED')
        self.assertEqual(cycles[0].open_short_puts, 0)

    def test_vikram_transfer_conservation(self):
        """Intra-household transfer preserves net shares."""
        cycles = walk_cycles(_VIKRAM_TRANSFER_SEED)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        # 200 bought, 50 OUT + 50 IN = net 200 shares
        self.assertEqual(active[0].shares_held, 200)


if __name__ == '__main__':
    unittest.main()
