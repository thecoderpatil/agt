"""
tests/test_walker.py — Walker unit tests derived from real IBKR data.

Test scenarios from REFACTOR_SPEC_v3.md section 5.
Expected values come from actual portfolio data pulled 2026-04-06.
"""
import unittest
import sys
import os
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.walker import (
    TradeEvent, EventType, Cycle, UnknownEventError, WalkerWarning,
    classify_event, walk_cycles, canonical_sort_key, get_walker_warnings,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_sample.xml')
INCEPTION_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_inception.xml')

# Account → household mapping
HOUSEHOLD_MAP = {
    'U21971297': 'Yash_Household',
    'U22076184': 'Yash_Household',
    'U22076329': 'Yash_Household',
    'U22388499': 'Vikram_Household',
}


def _make_event(**overrides) -> TradeEvent:
    """Helper to create a TradeEvent with sensible defaults."""
    defaults = dict(
        source='FLEX_TRADE',
        account_id='U22076329',
        household_id='Yash_Household',
        ticker='TEST',
        trade_date='20260101',
        date_time='20260101;100000',
        ib_order_id=None,
        transaction_id=None,
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


def _parse_float(val: str) -> float:
    """Parse a numeric string, treating empty as 0."""
    if not val or val == '':
        return 0.0
    return float(val)


def _parse_int_or_none(val: str):
    """Parse an int string, treating empty as None."""
    if not val or val == '':
        return None
    return int(val)


def _load_trades_from_fixture(account_id: str, ticker: str) -> list[TradeEvent]:
    """Load all trades for a given account+ticker from the XML fixture."""
    tree = ET.parse(FIXTURE_PATH)
    root = tree.getroot()
    household = HOUSEHOLD_MAP[account_id]
    events = []
    for fs in root.findall('.//FlexStatement'):
        if fs.attrib.get('accountId') != account_id:
            continue
        for t in fs.findall('Trades/Trade'):
            us = t.attrib.get('underlyingSymbol', '')
            sym = t.attrib.get('symbol', '')
            if us != ticker and sym != ticker:
                continue
            raw = dict(t.attrib)
            ev = TradeEvent(
                source='FLEX_TRADE',
                account_id=account_id,
                household_id=household,
                ticker=ticker,
                trade_date=raw.get('tradeDate', ''),
                date_time=raw.get('dateTime', ''),
                ib_order_id=_parse_int_or_none(raw.get('ibOrderID', '')),
                transaction_id=raw.get('transactionID', ''),
                asset_category=raw.get('assetCategory', ''),
                right=raw.get('putCall', '') or None,
                strike=_parse_float(raw.get('strike', '')) or None,
                expiry=raw.get('expiry', '') or None,
                buy_sell=raw.get('buySell', ''),
                open_close=raw.get('openCloseIndicator', '') or None,
                quantity=abs(_parse_float(raw.get('quantity', '0'))),
                trade_price=_parse_float(raw.get('tradePrice', '0')),
                net_cash=_parse_float(raw.get('netCash', '0')),
                fifo_pnl_realized=_parse_float(raw.get('fifoPnlRealized', '0')),
                transaction_type=raw.get('transactionType', ''),
                notes=raw.get('notes', ''),
                currency=raw.get('currency', 'USD'),
                raw=raw,
            )
            events.append(ev)
    return events


class TestWalker(unittest.TestCase):
    """Walker unit tests. Tests 1-8 from real data, 9-10 synthetic."""

    def test_uber_premium_only_cycle(self):
        """UBER U22076329 events 1-5: 3 CSP opens, 3 BTC closes within 4 days.
        Expected: 1 cycle, status=CLOSED, duration=4 days, shares_held=0,
        premium_total approx $125, realized_pnl approx $125."""
        all_events = _load_trades_from_fixture('U22076329', 'UBER')
        # Events 4-8 (0-indexed from fixture output): the 3 CSP opens and BTC closes
        # Event indices in fixture: [4]=STO 79P, [5]=BTC 79P, [6]=STO 80P×2,
        #   [7]=BTC 80P×1, [8]=BTC 80P×1
        events = [e for e in all_events if e.expiry == '20260109']
        self.assertEqual(len(events), 5)
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.status, 'CLOSED')
        self.assertEqual(c.shares_held, 0)
        self.assertAlmostEqual(c.premium_total, 124.67763, places=2)
        self.assertAlmostEqual(c.realized_pnl, 124.67763, places=2)

    def test_uber_clean_expiration_cycle(self):
        """UBER U22076329 events 6-7: 1 CSP open, 1 Ep close at expiry.
        Expected: 1 cycle, status=CLOSED, shares_held=0,
        premium_total approx $54, realized_pnl approx $54."""
        all_events = _load_trades_from_fixture('U22076329', 'UBER')
        # STO 83P 260116 + Ep 83P 260116
        events = [e for e in all_events if e.expiry == '20260116' and e.right == 'P']
        self.assertEqual(len(events), 2)
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.status, 'CLOSED')
        self.assertEqual(c.shares_held, 0)
        self.assertAlmostEqual(c.premium_total, 53.94796, places=2)
        self.assertAlmostEqual(c.realized_pnl, 53.94796, places=2)

    def test_uber_deep_multi_assignment_cycle(self):
        """UBER U22076329 events 8-24: 4 CSPs opened, 3 assigned (300 shares),
        1 expired, then multiple CC attempts.
        Expected: 1 ACTIVE cycle, shares_held=300, open_short_options=0,
        paper_basis approx $74.67, premium_total approx $406,
        adjusted_basis approx $73.32."""
        all_events = _load_trades_from_fixture('U22076329', 'UBER')
        # All events AFTER the 260109 and 260116P cycles: from event index 10 onward
        # This includes: 78P×4 STO+Ep, 83P STO+A+STK, 70P STO+A+STK, 71P STO+A+STK,
        # 82C×3 STO+Ep, 76C×3 STO+BTC
        events_260109 = {e.transaction_id for e in all_events if e.expiry == '20260109'}
        events_260116_p = {e.transaction_id for e in all_events
                          if e.expiry == '20260116' and e.right == 'P'}
        events = [e for e in all_events
                  if e.transaction_id not in events_260109
                  and e.transaction_id not in events_260116_p]
        cycles = walk_cycles(events)
        # Should be 1 active cycle (the deep assignment cycle)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        c = active[0]
        self.assertEqual(c.shares_held, 300)
        self.assertEqual(c.open_short_options, 0)
        # paper_basis is IRS-adjusted: strike minus assigned-put premium
        # IBKR costBasisPrice = 73.994, Walker paper_basis should be within $0.10
        self.assertAlmostEqual(c.paper_basis, 74.04, delta=0.10)
        self.assertAlmostEqual(c.premium_total, 406.47, delta=5.0)
        self.assertIsNotNone(c.adjusted_basis)
        # Strategy basis = paper_basis - (premium / shares)
        self.assertAlmostEqual(c.adjusted_basis, 72.69, delta=0.15)

    def test_meta_called_away_with_carryin(self):
        """META U22388499 events 1-9: assignment at $657.5 from carry-in put,
        then 3 CC cycles, final assignment called-away at $692.5.
        Expected: 1 CLOSED cycle, shares_held=0, premium_total approx $2098."""
        all_events = _load_trades_from_fixture('U22388499', 'META')
        # META has a carry-in for the 657.5P that was assigned on 260102.
        # The assignment OPT leg (event 3) is the BookTrade A on 260102P00657500.
        # We need a carry-in event for the short put that existed before 260102.
        carryin = TradeEvent(
            source='INCEPTION_CARRYIN',
            account_id='U22388499',
            household_id='Vikram_Household',
            ticker='META',
            trade_date='20251231',
            date_time='20251231;235959',
            ib_order_id=None,
            transaction_id='CARRYIN_META_657P',
            asset_category='OPT',
            right='P',
            strike=657.5,
            expiry='20260102',
            buy_sell='SELL',
            open_close='O',
            quantity=1.0,
            trade_price=0.0,
            net_cash=0.0,
            fifo_pnl_realized=0.0,
            transaction_type='InceptionCarryin',
            notes='',
            currency='USD',
            raw={},
        )
        events = [carryin] + all_events
        cycles = walk_cycles(events)
        closed = [c for c in cycles if c.status == 'CLOSED']
        self.assertTrue(len(closed) >= 1)
        # The full cycle: carry-in → assignment → 3 CC rounds → called away
        c = closed[-1]
        self.assertEqual(c.shares_held, 0)
        # Premium from CC trades (net_cash on OPT events)
        opt_premium = sum(e.net_cash for e, et in zip(c.events, c.event_types)
                         if e.asset_category == 'OPT')
        self.assertAlmostEqual(opt_premium, 2098.92, delta=10.0)

    def test_adbe_long_put_hedge_round_trip(self):
        """ADBE U21971297 events 27-34: 8 events in 3 minutes — 2 long puts
        opened, 2 short puts opened, all 4 closed.
        Expected: premium_total net approx -$21 from these events."""
        all_events = _load_trades_from_fixture('U21971297', 'ADBE')
        # Events 28-35 in the fixture (0-indexed): the 302.5P and 305P round trips
        # on 2026-01-28. Filter by trade_date=20260128.
        day_events = [e for e in all_events if e.trade_date == '20260128']
        self.assertEqual(len(day_events), 8)
        # These happen within an existing ADBE cycle. We need the full event
        # stream to test them properly. Let's run the full ADBE walker and
        # check the premium contribution of just these 8 events.
        cycles = walk_cycles(all_events)
        # Find the events in the active cycle
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertTrue(len(active) >= 1)
        # Sum net_cash for 20260128 events (the hedge round trip)
        hedge_premium = sum(e.net_cash for e in day_events)
        self.assertAlmostEqual(hedge_premium, -21.51, delta=2.0)

    def test_adbe_manual_roll_different_ib_order_id(self):
        """ADBE U21971297 events 17-18: BTC 312.5P 260116 at 13:40:55
        (ibOrderID=4782044916), STO 312.5P 260123 at same second
        (ibOrderID=4782046202).
        Expected: both events in same cycle, no fragmentation."""
        all_events = _load_trades_from_fixture('U21971297', 'ADBE')
        # Find the two roll events
        btc = [e for e in all_events
               if e.ib_order_id == 4782044916 and e.strike == 312.5]
        sto = [e for e in all_events
               if e.ib_order_id == 4782046202 and e.strike == 312.5]
        self.assertEqual(len(btc), 1)
        self.assertEqual(len(sto), 1)
        # Both should be in the same cycle
        cycles = walk_cycles(all_events)
        for c in cycles:
            btc_in = btc[0] in c.events
            sto_in = sto[0] in c.events
            if btc_in:
                self.assertTrue(sto_in,
                    "BTC and STO of same-second roll must be in the same cycle")
                break
        else:
            self.fail("Could not find the BTC event in any cycle")

    def test_uber_eod_assignment_cluster(self):
        """UBER U22076329 events on 2026-01-30: STK BUY (A), Ep on 78P×4,
        A on 83P — all at 20260130;162000.
        Expected: canonical sort puts OPT close legs before STK legs.
        After processing 20260130: shares_held=100 (from 83P assignment),
        open_short_options=0 (78P×4 expired + 83P assigned)."""
        all_events = _load_trades_from_fixture('U22076329', 'UBER')
        # Build a subset: the cycle that starts with 78P×4 and 83P on 260130
        # plus the assignment cluster. This is part of the deep cycle (test 3),
        # but we can test the EOD state on 260130 specifically.
        # Take all events from the deep cycle up through 260130.
        events_260109 = {e.transaction_id for e in all_events if e.expiry == '20260109'}
        events_260116_p = {e.transaction_id for e in all_events
                          if e.expiry == '20260116' and e.right == 'P'}
        deep_events = [e for e in all_events
                       if e.transaction_id not in events_260109
                       and e.transaction_id not in events_260116_p]
        # Filter to only events up through 20260130
        up_to_0130 = [e for e in deep_events if e.trade_date <= '20260130']
        cycles = walk_cycles(up_to_0130)
        # The 260130 cluster: after Ep(78P×4) + A(83P) + STK BUY
        # shares_held should be 100 (from the 83P assignment)
        # open_short_options should be 0 (78P×4 expired, 83P assigned)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        c = active[0]
        self.assertEqual(c.shares_held, 100)
        self.assertEqual(c.open_short_options, 0)

    def test_strategy_basis_vs_ibkr_tax_basis_uber(self):
        """UBER U22076329 Cycle 3: Walker adjusted_basis approx $73.32,
        IBKR costBasisPrice approx $73.99, delta = $0.67/share.
        Assertion: abs(delta) < $10.0."""
        all_events = _load_trades_from_fixture('U22076329', 'UBER')
        events_260109 = {e.transaction_id for e in all_events if e.expiry == '20260109'}
        events_260116_p = {e.transaction_id for e in all_events
                          if e.expiry == '20260116' and e.right == 'P'}
        deep_events = [e for e in all_events
                       if e.transaction_id not in events_260109
                       and e.transaction_id not in events_260116_p]
        cycles = walk_cycles(deep_events)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        c = active[0]
        self.assertIsNotNone(c.adjusted_basis)
        # Strategy basis = paper_basis - (premium / shares)
        self.assertAlmostEqual(c.adjusted_basis, 72.69, delta=0.5)
        # paper_basis (IRS-adjusted) should match IBKR costBasisPrice within $0.10
        ibkr_cost_basis_price = 73.994
        self.assertIsNotNone(c.paper_basis)
        paper_delta = abs(c.paper_basis - ibkr_cost_basis_price)
        self.assertLess(paper_delta, 0.10,
            f"paper_basis vs IBKR costBasisPrice: {paper_delta:.4f} exceeds $0.10")

    def test_unknown_book_trade_notes_fails_closed(self):
        """Synthetic: BookTrade with notes='X' must raise UnknownEventError."""
        ev = _make_event(
            ticker='FAKE',
            transaction_type='BookTrade',
            notes='X',
            asset_category='OPT',
        )
        with self.assertRaises(UnknownEventError) as ctx:
            classify_event(ev)
        self.assertIn('X', str(ctx.exception))

    def test_non_usd_event_fails_closed(self):
        """Synthetic: event with currency='CAD' must raise UnknownEventError."""
        ev = _make_event(ticker='FAKE', currency='CAD')
        with self.assertRaises(UnknownEventError) as ctx:
            classify_event(ev)
        self.assertIn('CAD', str(ctx.exception))


    def test_crm_no_premature_closure_on_cc_assignment(self):
        """CRM Yash_Household: CC assignment should not cause premature cycle
        closure when CSPs are still open. Regression test for the
        open_short_puts/calls split.

        Scenario from real data (events 53-65 in CRM trace):
        - CC assignment closes 2+1 calls (osp_calls goes 0→-1 before split fix)
        - Meanwhile 4 CSPs are opened, 3 closed → 1 put still live
        - Old single-counter: osp hits 0 at EOD 20251211, cycle closes,
          then the surviving put's Ep on 20251212 becomes orphaned
        - With split counters: open_short_puts=1 at EOD 20251211,
          cycle stays open, Ep processes normally"""
        if not os.path.exists(INCEPTION_FIXTURE_PATH):
            self.skipTest("Inception fixture not available")

        # Load CRM events for U21971297 only (the same-account case)
        # from the inception fixture — events around the 20251204-20251212 window
        tree = ET.parse(INCEPTION_FIXTURE_PATH)
        root = tree.getroot()
        events = []
        for fs in root.findall('.//FlexStatement'):
            if fs.attrib.get('accountId') != 'U21971297':
                continue
            for t in fs.findall('Trades/Trade'):
                us = t.attrib.get('underlyingSymbol', '')
                sym = t.attrib.get('symbol', '')
                if us != 'CRM' and sym != 'CRM':
                    continue
                raw = dict(t.attrib)
                ev = TradeEvent(
                    source='FLEX_TRADE',
                    account_id='U21971297',
                    household_id='Yash_Household',
                    ticker='CRM',
                    trade_date=raw.get('tradeDate', ''),
                    date_time=raw.get('dateTime', ''),
                    ib_order_id=int(raw['ibOrderID']) if raw.get('ibOrderID') else None,
                    transaction_id=raw.get('transactionID', ''),
                    asset_category=raw.get('assetCategory', ''),
                    right=raw.get('putCall', '') or None,
                    strike=float(raw['strike']) if raw.get('strike') else None,
                    expiry=raw.get('expiry', '') or None,
                    buy_sell=raw.get('buySell', ''),
                    open_close=raw.get('openCloseIndicator', '') or None,
                    quantity=abs(float(raw.get('quantity', '0'))),
                    trade_price=float(raw.get('tradePrice', '0')),
                    net_cash=float(raw.get('netCash', '0')),
                    fifo_pnl_realized=float(raw.get('fifoPnlRealized', '0')),
                    transaction_type=raw.get('transactionType', ''),
                    notes=raw.get('notes', ''),
                    currency=raw.get('currency', 'USD'),
                    raw=raw,
                )
                events.append(ev)

        self.assertGreater(len(events), 30, "Expected 30+ CRM events in U21971297")

        # The Walker should NOT freeze — all events should be processable
        # without raising UnknownEventError
        cycles = walk_cycles(events)

        # Find the cycle that contains the 20251208 CSP opens
        target_cycle = None
        for c in cycles:
            dates = {ev.trade_date for ev in c.events}
            if '20251208' in dates and '20251212' in dates:
                target_cycle = c
                break

        self.assertIsNotNone(target_cycle,
            "Expected a cycle spanning 20251208-20251212 (the CC assignment + CSP window)")

        # The expire_worthless on 20251212 must be IN the cycle, not orphaned
        ep_events = [
            ev for ev in target_cycle.events
            if ev.trade_date == '20251212' and ev.notes == 'Ep'
        ]
        self.assertGreater(len(ep_events), 0,
            "The 252.5P expiry on 20251212 should be inside the cycle, not orphaned")


    def test_adbe_per_account_paper_basis(self):
        """ADBE Yash_Household: per-account paper_basis matches IBKR costBasisPrice.

        U21971297 holds 400 ADBE shares across 4 assignments.
        U22076329 holds 100 ADBE shares from 1 assignment.
        Walker paper_basis_for_account should match IBKR costBasisPrice
        within $0.10/share for each account independently."""
        if not os.path.exists(INCEPTION_FIXTURE_PATH):
            self.skipTest("Inception fixture not available")

        # Load all ADBE events from inception fixture, ALL Yash accounts
        tree = ET.parse(INCEPTION_FIXTURE_PATH)
        root = tree.getroot()
        events = []
        yash_accts = {'U21971297', 'U22076329', 'U22076184'}
        for fs in root.findall('.//FlexStatement'):
            acct = fs.attrib.get('accountId', '')
            if acct not in yash_accts:
                continue
            for t in fs.findall('Trades/Trade'):
                us = t.attrib.get('underlyingSymbol', '')
                sym = t.attrib.get('symbol', '')
                if us != 'ADBE' and sym != 'ADBE':
                    continue
                raw = dict(t.attrib)
                ev = TradeEvent(
                    source='FLEX_TRADE',
                    account_id=acct,
                    household_id='Yash_Household',
                    ticker='ADBE',
                    trade_date=raw.get('tradeDate', ''),
                    date_time=raw.get('dateTime', ''),
                    ib_order_id=int(raw['ibOrderID']) if raw.get('ibOrderID') else None,
                    transaction_id=raw.get('transactionID', ''),
                    asset_category=raw.get('assetCategory', ''),
                    right=raw.get('putCall', '') or None,
                    strike=float(raw['strike']) if raw.get('strike') else None,
                    expiry=raw.get('expiry', '') or None,
                    buy_sell=raw.get('buySell', ''),
                    open_close=raw.get('openCloseIndicator', '') or None,
                    quantity=abs(float(raw.get('quantity', '0'))),
                    trade_price=float(raw.get('tradePrice', '0')),
                    net_cash=float(raw.get('netCash', '0')),
                    fifo_pnl_realized=float(raw.get('fifoPnlRealized', '0')),
                    transaction_type=raw.get('transactionType', ''),
                    notes=raw.get('notes', ''),
                    currency=raw.get('currency', 'USD'),
                    raw=raw,
                )
                events.append(ev)

        cycles = walk_cycles(events)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        c = active[0]

        # Per-account verification against IBKR costBasisPrice
        # U22076329: 100 shares, IBKR cbp = 347.673169
        u329_basis = c.paper_basis_for_account('U22076329')
        self.assertIsNotNone(u329_basis)
        self.assertAlmostEqual(u329_basis, 347.6732, delta=0.10)

        # U21971297: 400 shares, IBKR cbp = 329.108183
        # Known residual ~$0.29/share from IRS wash-sale adjustments
        # that IBKR applies but Walker does not track
        u297_basis = c.paper_basis_for_account('U21971297')
        self.assertIsNotNone(u297_basis)
        self.assertAlmostEqual(u297_basis, 329.1082, delta=0.10)


    def test_long_put_expiry_does_not_decrement_short_counter(self):
        """Synthetic: a long put opened and expired should NOT decrement
        open_short_puts. Regression test for the long/short expiry bug."""
        events = [
            # CSP open to originate the cycle
            _make_event(ticker='TEST', date_time='20260101;100000', trade_date='20260101',
                        buy_sell='SELL', open_close='O', right='P', strike=100.0,
                        expiry='20260110', quantity=1, net_cash=150.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            # Long put open (hedge)
            _make_event(ticker='TEST', date_time='20260102;100000', trade_date='20260102',
                        buy_sell='BUY', open_close='O', right='P', strike=95.0,
                        expiry='20260110', quantity=1, net_cash=-100.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            # Long put expires worthless
            _make_event(ticker='TEST', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='C', right='P', strike=95.0,
                        expiry='20260110', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
            # Short put expires worthless
            _make_event(ticker='TEST', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='C', right='P', strike=100.0,
                        expiry='20260110', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
        ]
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.status, 'CLOSED')
        self.assertEqual(c.open_short_puts, 0)  # not -1

    def test_crm_full_household_no_freeze(self):
        """CRM Yash_Household full 87-event stream should not freeze.
        Regression test for the long-option expiry bug discovered in
        the CRM household trace."""
        if not os.path.exists(INCEPTION_FIXTURE_PATH):
            self.skipTest("Inception fixture not available")

        tree = ET.parse(INCEPTION_FIXTURE_PATH)
        root = tree.getroot()
        yash_accts = {'U21971297', 'U22076329', 'U22076184'}
        events = []
        for fs in root.findall('.//FlexStatement'):
            acct = fs.attrib.get('accountId', '')
            if acct not in yash_accts:
                continue
            for t in fs.findall('Trades/Trade'):
                us = t.attrib.get('underlyingSymbol', '')
                sym = t.attrib.get('symbol', '')
                if us != 'CRM' and sym != 'CRM':
                    continue
                raw = dict(t.attrib)
                ev = TradeEvent(
                    source='FLEX_TRADE', account_id=acct,
                    household_id='Yash_Household', ticker='CRM',
                    trade_date=raw.get('tradeDate', ''),
                    date_time=raw.get('dateTime', ''),
                    ib_order_id=int(raw['ibOrderID']) if raw.get('ibOrderID') else None,
                    transaction_id=raw.get('transactionID', ''),
                    asset_category=raw.get('assetCategory', ''),
                    right=raw.get('putCall', '') or None,
                    strike=float(raw['strike']) if raw.get('strike') else None,
                    expiry=raw.get('expiry', '') or None,
                    buy_sell=raw.get('buySell', ''),
                    open_close=raw.get('openCloseIndicator', '') or None,
                    quantity=abs(float(raw.get('quantity', '0'))),
                    trade_price=float(raw.get('tradePrice', '0')),
                    net_cash=float(raw.get('netCash', '0')),
                    fifo_pnl_realized=float(raw.get('fifoPnlRealized', '0')),
                    transaction_type=raw.get('transactionType', ''),
                    notes=raw.get('notes', ''),
                    currency=raw.get('currency', 'USD'),
                    raw=raw,
                )
                events.append(ev)

        self.assertGreater(len(events), 80)
        # Must not raise UnknownEventError
        cycles = walk_cycles(events)
        self.assertGreater(len(cycles), 0)


    def test_adbe_two_puts_assigned_same_day_correct_strike_match(self):
        """ADBE U21971297 20260109: 335P and 337.5P assigned same day.
        Each stock leg must match its own put by strike price.

        Before fix: both stock legs matched 337.5P (last scanned).
        After fix: stock@335 matches 335P, stock@337.5 matches 337.5P.

        335P CSP premium = $2.22/sh → IRS basis = $332.78
        337.5P CSP premium = $3.37/sh → IRS basis = $334.13"""
        if not os.path.exists(INCEPTION_FIXTURE_PATH):
            self.skipTest("Inception fixture not available")

        tree = ET.parse(INCEPTION_FIXTURE_PATH)
        root = tree.getroot()
        yash_accts = {'U21971297', 'U22076329', 'U22076184'}
        events = []
        for fs in root.findall('.//FlexStatement'):
            acct = fs.attrib.get('accountId', '')
            if acct not in yash_accts:
                continue
            for t in fs.findall('Trades/Trade'):
                us = t.attrib.get('underlyingSymbol', '')
                sym = t.attrib.get('symbol', '')
                if us != 'ADBE' and sym != 'ADBE':
                    continue
                raw = dict(t.attrib)
                ev = TradeEvent(
                    source='FLEX_TRADE', account_id=acct,
                    household_id='Yash_Household', ticker='ADBE',
                    trade_date=raw.get('tradeDate', ''),
                    date_time=raw.get('dateTime', ''),
                    ib_order_id=int(raw['ibOrderID']) if raw.get('ibOrderID') else None,
                    transaction_id=raw.get('transactionID', ''),
                    asset_category=raw.get('assetCategory', ''),
                    right=raw.get('putCall', '') or None,
                    strike=float(raw['strike']) if raw.get('strike') else None,
                    expiry=raw.get('expiry', '') or None,
                    buy_sell=raw.get('buySell', ''),
                    open_close=raw.get('openCloseIndicator', '') or None,
                    quantity=abs(float(raw.get('quantity', '0'))),
                    trade_price=float(raw.get('tradePrice', '0')),
                    net_cash=float(raw.get('netCash', '0')),
                    fifo_pnl_realized=float(raw.get('fifoPnlRealized', '0')),
                    transaction_type=raw.get('transactionType', ''),
                    notes=raw.get('notes', ''),
                    currency=raw.get('currency', 'USD'),
                    raw=raw,
                )
                events.append(ev)

        cycles = walk_cycles(events)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        c = active[0]

        # U21971297 per-account basis should match IBKR $329.108 within $0.10
        u297 = c.paper_basis_for_account('U21971297')
        self.assertIsNotNone(u297)
        self.assertAlmostEqual(u297, 329.1082, delta=0.10,
            msg=f"U21971297 paper_basis {u297:.4f} vs IBKR 329.1082")


    def test_satellite_long_opt_no_prior_csp(self):
        """Synthetic: LONG_OPT_OPEN with no prior CSP creates a SATELLITE cycle.
        Wheel cycles are unaffected."""
        events = [
            _make_event(ticker='SPEC', date_time='20260101;100000', trade_date='20260101',
                        buy_sell='BUY', open_close='O', right='C', strike=100.0,
                        expiry='20260110', quantity=1, net_cash=-500.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            _make_event(ticker='SPEC', date_time='20260105;100000', trade_date='20260105',
                        buy_sell='SELL', open_close='C', right='C', strike=100.0,
                        expiry='20260110', quantity=1, net_cash=700.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
        ]
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.cycle_type, 'SATELLITE')
        self.assertEqual(c.status, 'CLOSED')
        # Premium total = net_cash of open (-500) + close (+700) = 200
        self.assertAlmostEqual(c.premium_total, 200.0, delta=1.0)
        self.assertEqual(c.shares_held, 0)

    def test_satellite_then_wheel_cycle(self):
        """Synthetic: SATELLITE cycle closes, then CSP_OPEN starts a new WHEEL."""
        events = [
            # Satellite: long call
            _make_event(ticker='MIX', date_time='20260101;100000', trade_date='20260101',
                        buy_sell='BUY', open_close='O', right='C', strike=100.0,
                        expiry='20260103', quantity=1, net_cash=-200.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            _make_event(ticker='MIX', date_time='20260103;162000', trade_date='20260103',
                        buy_sell='BUY', open_close='C', right='C', strike=100.0,
                        expiry='20260103', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
            # Wheel: CSP
            _make_event(ticker='MIX', date_time='20260105;100000', trade_date='20260105',
                        buy_sell='SELL', open_close='O', right='P', strike=95.0,
                        expiry='20260110', quantity=1, net_cash=150.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            _make_event(ticker='MIX', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='C', right='P', strike=95.0,
                        expiry='20260110', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
        ]
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0].cycle_type, 'SATELLITE')
        self.assertEqual(cycles[0].status, 'CLOSED')
        self.assertEqual(cycles[1].cycle_type, 'WHEEL')
        self.assertEqual(cycles[1].status, 'CLOSED')

    def test_nflx_full_stream_satellite_only(self):
        """NFLX Yash_Household: 3 events, all long calls. Should produce
        1 SATELLITE cycle, 0 WHEEL cycles."""
        if not os.path.exists(INCEPTION_FIXTURE_PATH):
            self.skipTest("Inception fixture not available")

        tree = ET.parse(INCEPTION_FIXTURE_PATH)
        root = tree.getroot()
        events = []
        for fs in root.findall('.//FlexStatement'):
            acct = fs.attrib.get('accountId', '')
            if acct != 'U22076329':
                continue
            for t in fs.findall('Trades/Trade'):
                us = t.attrib.get('underlyingSymbol', '')
                if us != 'NFLX':
                    continue
                raw = dict(t.attrib)
                ev = TradeEvent(
                    source='FLEX_TRADE', account_id=acct,
                    household_id='Yash_Household', ticker='NFLX',
                    trade_date=raw.get('tradeDate', ''),
                    date_time=raw.get('dateTime', ''),
                    ib_order_id=int(raw['ibOrderID']) if raw.get('ibOrderID') else None,
                    transaction_id=raw.get('transactionID', ''),
                    asset_category=raw.get('assetCategory', ''),
                    right=raw.get('putCall', '') or None,
                    strike=float(raw['strike']) if raw.get('strike') else None,
                    expiry=raw.get('expiry', '') or None,
                    buy_sell=raw.get('buySell', ''),
                    open_close=raw.get('openCloseIndicator', '') or None,
                    quantity=abs(float(raw.get('quantity', '0'))),
                    trade_price=float(raw.get('tradePrice', '0')),
                    net_cash=float(raw.get('netCash', '0')),
                    fifo_pnl_realized=float(raw.get('fifoPnlRealized', '0')),
                    transaction_type=raw.get('transactionType', ''),
                    notes=raw.get('notes', ''),
                    currency=raw.get('currency', 'USD'),
                    raw=raw,
                )
                events.append(ev)

        self.assertEqual(len(events), 3)
        cycles = walk_cycles(events)
        self.assertGreater(len(cycles), 0)
        wheel = [c for c in cycles if c.cycle_type == 'WHEEL']
        satellite = [c for c in cycles if c.cycle_type == 'SATELLITE']
        self.assertEqual(len(wheel), 0)
        self.assertGreater(len(satellite), 0)
        self.assertEqual(satellite[0].status, 'CLOSED')


    def test_intra_household_opt_transfer_no_osp_change(self):
        """OPT transfer within household: TRANSFER_OUT does NOT decrement
        open_short_puts/calls (household-level cycle is unchanged)."""
        events = [
            # CSP open in account A
            _make_event(ticker='TEST', account_id='U22076184', household_id='Yash_Household',
                        date_time='20251001;100000', trade_date='20251001',
                        buy_sell='SELL', open_close='O', right='P', strike=100.0,
                        expiry='20251010', quantity=1, net_cash=150.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            # Transfer OUT from account A (to account B within household)
            _make_event(ticker='TEST', account_id='U22076184', household_id='Yash_Household',
                        source='FLEX_TRANSFER',
                        date_time='20251005;120000', trade_date='20251005',
                        buy_sell='SELL', open_close='OUT', right='P', strike=100.0,
                        expiry='20251010', quantity=1, net_cash=0.0,
                        transaction_type='Transfer', asset_category='OPT'),
            # Expires in account B
            _make_event(ticker='TEST', account_id='U22076329', household_id='Yash_Household',
                        date_time='20251010;162000', trade_date='20251010',
                        buy_sell='BUY', open_close='C', right='P', strike=100.0,
                        expiry='20251010', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
        ]
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertEqual(c.status, 'CLOSED')
        # osp should be 0 (not -1): TRANSFER_OUT doesn't touch option counters
        self.assertEqual(c.open_short_puts, 0)
        self.assertAlmostEqual(c.premium_total, 150.0, delta=1.0)

    def test_cash_only_transfer_ignored(self):
        """Cash-only transfer (qty=0) should not crash or create events."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from agt_equities import trade_repo
        trade_repo.DB_PATH = r'C:\AGT_Telegram_Bridge\agt_desk.db'
        import sqlite3
        conn = sqlite3.connect(trade_repo.DB_PATH)
        conn.row_factory = sqlite3.Row
        events = trade_repo._load_transfer_events(conn)
        conn.close()
        # Should have exactly 2 events (ADBE and QCOM, not the cash ones)
        self.assertEqual(len(events), 2)
        for ev in events:
            self.assertGreater(ev.quantity, 0)

    def test_cross_household_transfer_guard(self):
        """Cross-household transfer should raise UnknownEventError if the
        Walker encounters a transfer event with mismatched household."""
        # This tests that TRANSFER_OUT/IN events are properly classified
        # and that the Walker doesn't crash on them
        from agt_equities.walker import classify_event, EventType
        ev = _make_event(
            ticker='TEST', source='FLEX_TRANSFER',
            open_close='OUT', asset_category='OPT',
        )
        et = classify_event(ev)
        self.assertEqual(et, EventType.TRANSFER_OUT)

        ev_in = _make_event(
            ticker='TEST', source='FLEX_TRANSFER',
            open_close='IN', asset_category='STK',
        )
        et_in = classify_event(ev_in)
        self.assertEqual(et_in, EventType.TRANSFER_IN)


    def test_corp_action_forward_split(self):
        """Forward 4:1 split: 100 shares at $400 → 400 shares at $100."""
        events = [
            _make_event(ticker='SPLIT', date_time='20260101;100000', trade_date='20260101',
                        buy_sell='SELL', open_close='O', right='P', strike=400.0,
                        expiry='20260110', quantity=1, net_cash=500.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            _make_event(ticker='SPLIT', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='C', right='P', strike=400.0,
                        expiry='20260110', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='A', asset_category='OPT'),
            _make_event(ticker='SPLIT', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='O', strike=400.0,
                        quantity=100, net_cash=-40000.0, trade_price=400.0,
                        transaction_type='BookTrade', notes='A', asset_category='STK'),
            # Forward split: receive 300 additional shares (4:1 = +300 on 100 base)
            _make_event(ticker='SPLIT', source='FLEX_CORP_ACTION',
                        date_time='20260115;090000', trade_date='20260115',
                        buy_sell='BUY', open_close='O',
                        quantity=300, net_cash=0.0, trade_price=0.0,
                        transaction_type='CorpAction', notes='',
                        asset_category='STK',
                        raw={'type': 'FS'}),
        ]
        cycles = walk_cycles(events)
        active = [c for c in cycles if c.status == 'ACTIVE']
        self.assertEqual(len(active), 1)
        c = active[0]
        self.assertEqual(c.shares_held, 400)
        # Basis should be $100/share (total cost $40,000 / 400 shares)
        self.assertAlmostEqual(c.paper_basis, 100.0, delta=5.0)

    def test_corp_action_cash_merger(self):
        """Cash merger: position closed at merger price."""
        events = [
            _make_event(ticker='MERGER', date_time='20260101;100000', trade_date='20260101',
                        buy_sell='SELL', open_close='O', right='P', strike=50.0,
                        expiry='20260110', quantity=1, net_cash=200.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            _make_event(ticker='MERGER', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='C', right='P', strike=50.0,
                        expiry='20260110', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='A', asset_category='OPT'),
            _make_event(ticker='MERGER', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='O', strike=50.0,
                        quantity=100, net_cash=-5000.0, trade_price=50.0,
                        transaction_type='BookTrade', notes='A', asset_category='STK'),
            # Cash merger at $60/share
            _make_event(ticker='MERGER', source='FLEX_CORP_ACTION',
                        date_time='20260120;090000', trade_date='20260120',
                        buy_sell='SELL', open_close='C',
                        quantity=0, net_cash=0.0, trade_price=0.0,
                        transaction_type='CorpAction', notes='',
                        asset_category='STK',
                        raw={'type': 'CM', 'proceeds': 6000.0}),
        ]
        cycles = walk_cycles(events)
        # Merger should close the cycle (shares_held → 0)
        closed = [c for c in cycles if c.status == 'CLOSED']
        self.assertGreater(len(closed), 0)
        c = closed[-1]
        self.assertEqual(c.shares_held, 0)

    def test_corp_action_symbol_change_no_crash(self):
        """Symbol/CUSIP change: no economic effect, no crash."""
        events = [
            _make_event(ticker='OLDTK', date_time='20260101;100000', trade_date='20260101',
                        buy_sell='SELL', open_close='O', right='P', strike=100.0,
                        expiry='20260110', quantity=1, net_cash=150.0,
                        transaction_type='ExchTrade', asset_category='OPT'),
            # Symbol change event
            _make_event(ticker='OLDTK', source='FLEX_CORP_ACTION',
                        date_time='20260105;090000', trade_date='20260105',
                        buy_sell='BUY', open_close='O',
                        quantity=0, net_cash=0.0, trade_price=0.0,
                        transaction_type='CorpAction', notes='',
                        asset_category='STK',
                        raw={'type': 'TC'}),
            # Expiry after symbol change
            _make_event(ticker='OLDTK', date_time='20260110;162000', trade_date='20260110',
                        buy_sell='BUY', open_close='C', right='P', strike=100.0,
                        expiry='20260110', quantity=1, net_cash=0.0,
                        transaction_type='BookTrade', notes='Ep', asset_category='OPT'),
        ]
        cycles = walk_cycles(events)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].status, 'CLOSED')


# ---------------------------------------------------------------------------
# W3.6: Walker Warnings Tests
# ---------------------------------------------------------------------------


class TestWalkerWarningDataclass(unittest.TestCase):
    """W3.6 test (a): WalkerWarning dataclass shape."""

    def test_dataclass_fields(self):
        w = WalkerWarning(
            code="TEST_CODE", severity="WARN", ticker="AAPL",
            household="Yash_Household", account="U21971297",
            message="test message", context={"key": "val"},
        )
        self.assertEqual(w.code, "TEST_CODE")
        self.assertEqual(w.severity, "WARN")
        self.assertEqual(w.ticker, "AAPL")
        self.assertEqual(w.household, "Yash_Household")
        self.assertEqual(w.account, "U21971297")
        self.assertEqual(w.message, "test message")
        self.assertEqual(w.context, {"key": "val"})

    def test_dataclass_frozen(self):
        w = WalkerWarning(code="X", severity="INFO", ticker=None,
                          household=None, account=None, message="m")
        with self.assertRaises(AttributeError):
            w.code = "Y"

    def test_default_context(self):
        w = WalkerWarning(code="X", severity="INFO", ticker=None,
                          household=None, account=None, message="m")
        self.assertEqual(w.context, {})


class TestStaleWarningsAccumulation(unittest.TestCase):
    """W3.6 test (b): stale-warnings regression — warnings from multiple
    walk_cycles() calls must be captured by caller, not lost."""

    def test_multi_group_accumulation(self):
        """Simulate /reconcile loop: two ticker groups, each producing warnings.
        Verify that calling get_walker_warnings() after each walk captures all."""
        # Group 1: SPX (excluded) — produces EXCLUDED_SKIP warning
        spx_events = [
            _make_event(ticker='SPX', household_id='Yash_Household',
                        trade_date='20260101', date_time='20260101;100000',
                        transaction_id='spx_1', asset_category='OPT',
                        buy_sell='SELL', open_close='O',
                        transaction_type='ExchTrade'),
        ]
        # Group 2: VIX (excluded) — produces EXCLUDED_SKIP warning
        vix_events = [
            _make_event(ticker='VIX', household_id='Yash_Household',
                        trade_date='20260102', date_time='20260102;100000',
                        transaction_id='vix_1', asset_category='OPT',
                        buy_sell='SELL', open_close='O',
                        transaction_type='ExchTrade'),
        ]

        accumulated = []
        walk_cycles(spx_events)
        accumulated.extend(get_walker_warnings())

        walk_cycles(vix_events)
        accumulated.extend(get_walker_warnings())

        # Without accumulation fix, only the last group's warnings would survive
        self.assertEqual(len(accumulated), 2)
        self.assertEqual(accumulated[0].code, "EXCLUDED_SKIP")
        self.assertEqual(accumulated[0].ticker, "SPX")
        self.assertEqual(accumulated[1].code, "EXCLUDED_SKIP")
        self.assertEqual(accumulated[1].ticker, "VIX")

        # Verify get_walker_warnings() alone only returns LAST group's warnings
        last_only = get_walker_warnings()
        self.assertEqual(len(last_only), 1)
        self.assertEqual(last_only[0].ticker, "VIX")


class TestExcludedSkipWarning(unittest.TestCase):
    """W3.6 test (f): EXCLUDED_SKIP emission for excluded tickers."""

    def test_excluded_ticker_emits_warning(self):
        events = [
            _make_event(ticker='SPX', household_id='Yash_Household',
                        trade_date='20260101', date_time='20260101;100000',
                        transaction_id='spx_1', asset_category='OPT',
                        buy_sell='SELL', open_close='O',
                        transaction_type='ExchTrade'),
            _make_event(ticker='SPX', household_id='Yash_Household',
                        trade_date='20260102', date_time='20260102;100000',
                        transaction_id='spx_2', asset_category='OPT',
                        buy_sell='BUY', open_close='C',
                        transaction_type='ExchTrade'),
        ]
        cycles = walk_cycles(events)
        warnings = get_walker_warnings()
        self.assertEqual(len(cycles), 0)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "EXCLUDED_SKIP")
        self.assertEqual(warnings[0].severity, "INFO")
        self.assertEqual(warnings[0].ticker, "SPX")
        self.assertIn("2 events", warnings[0].message)

    def test_non_excluded_ticker_no_skip_warning(self):
        """Regular ticker should not emit EXCLUDED_SKIP."""
        events = [
            _make_event(ticker='AAPL', household_id='Yash_Household',
                        trade_date='20260101', date_time='20260101;100000',
                        transaction_id='aapl_1'),
        ]
        walk_cycles(events)
        warnings = get_walker_warnings()
        skip_warnings = [w for w in warnings if w.code == "EXCLUDED_SKIP"]
        self.assertEqual(len(skip_warnings), 0)


class TestWalkerWarningsLogRoundtrip(unittest.TestCase):
    """W3.6 test (c): walker_warnings_log write/read roundtrip."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE walker_warnings_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_id         TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                code            TEXT NOT NULL,
                severity        TEXT NOT NULL,
                ticker          TEXT,
                household       TEXT,
                account         TEXT,
                message         TEXT NOT NULL,
                context_json    TEXT
            )
        """)

    def tearDown(self):
        self.conn.close()

    def test_write_and_read(self):
        import json
        w = WalkerWarning(
            code="COUNTER_GUARD", severity="WARN", ticker="AAPL",
            household="Yash_Household", account="U21971297",
            message="test warning", context={"counter": "shares_held"},
        )
        self.conn.execute(
            "INSERT INTO walker_warnings_log "
            "(sync_id, code, severity, ticker, household, account, message, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("42", w.code, w.severity, w.ticker, w.household, w.account,
             w.message, json.dumps(w.context)),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT * FROM walker_warnings_log WHERE sync_id = '42'"
        ).fetchone()
        self.assertEqual(row['code'], "COUNTER_GUARD")
        self.assertEqual(row['severity'], "WARN")
        self.assertEqual(row['ticker'], "AAPL")
        self.assertEqual(row['household'], "Yash_Household")
        self.assertEqual(json.loads(row['context_json']), {"counter": "shares_held"})

    def test_severity_aggregation_query(self):
        """Test the exact query used by build_top_strip()."""
        import json
        for code, sev in [("A", "INFO"), ("B", "WARN"), ("C", "ERROR")]:
            self.conn.execute(
                "INSERT INTO walker_warnings_log "
                "(sync_id, code, severity, message) VALUES (?, ?, ?, ?)",
                ("99", code, sev, f"msg_{code}"),
            )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT COUNT(*) as cnt, "
            "MAX(CASE severity WHEN 'ERROR' THEN 3 WHEN 'WARN' THEN 2 WHEN 'INFO' THEN 1 ELSE 0 END) as worst "
            "FROM walker_warnings_log "
            "WHERE sync_id = (SELECT MAX(sync_id) FROM walker_warnings_log)"
        ).fetchone()
        self.assertEqual(row['cnt'], 3)
        self.assertEqual(row['worst'], 3)  # ERROR = 3

    def test_empty_table_returns_zero(self):
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt, "
            "MAX(CASE severity WHEN 'ERROR' THEN 3 WHEN 'WARN' THEN 2 WHEN 'INFO' THEN 1 ELSE 0 END) as worst "
            "FROM walker_warnings_log "
            "WHERE sync_id = (SELECT MAX(sync_id) FROM walker_warnings_log)"
        ).fetchone()
        self.assertEqual(row['cnt'], 0)


class TestDeckBadgeSeverityColor(unittest.TestCase):
    """W3.6 test (e): deck badge severity color mapping logic."""

    def _badge_color(self, count, worst_severity):
        """Replicate the Jinja2 badge color logic in Python."""
        if count is None:
            return "text-slate-500"  # dash
        if worst_severity == "ERROR":
            return "text-rose-400"
        elif worst_severity == "WARN":
            return "text-amber-400"
        elif count > 0:
            return "text-slate-300"
        else:
            return "text-emerald-400"

    def test_zero_warnings(self):
        self.assertEqual(self._badge_color(0, None), "text-emerald-400")

    def test_info_only(self):
        self.assertEqual(self._badge_color(2, "INFO"), "text-slate-300")

    def test_warn_severity(self):
        self.assertEqual(self._badge_color(3, "WARN"), "text-amber-400")

    def test_error_severity(self):
        self.assertEqual(self._badge_color(1, "ERROR"), "text-rose-400")

    def test_none_count(self):
        self.assertEqual(self._badge_color(None, None), "text-slate-500")


if __name__ == '__main__':
    unittest.main()
