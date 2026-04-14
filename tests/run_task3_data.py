"""Task 3 data gathering script."""
import sqlite3, sys, os, tempfile, logging
from itertools import groupby
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.WARNING)

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import parse_flex_xml, load_flex_xml_from_file, _upsert_rows
from agt_equities import trade_repo
from agt_equities.walker import (
    walk_cycles, UnknownEventError, classify_event,
    canonical_sort_key, EventType
)

INCEPTION = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_inception.xml')
db_path = os.path.join(tempfile.gettempdir(), 'test_task3.db')
if os.path.exists(db_path):
    os.unlink(db_path)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
register_master_log_tables(conn)
conn.commit()
for sd in parse_flex_xml(load_flex_xml_from_file(INCEPTION)):
    _upsert_rows(conn, sd['table'], sd['rows'], sd['pk_cols'], 'now')
conn.commit()

# ── SECTION 1: ACATS sells ──
print("=" * 80)
print("SECTION 1: ACATS LIQUIDATION SELL EVENTS")
print("=" * 80)

for tk in ['ASML', 'MSTR', 'SMCI', 'SOFI', 'TMC', 'TSM', 'NVDA', 'PLTR']:
    rows = conn.execute(
        "SELECT account_id, trade_date, buy_sell, quantity, trade_price, "
        "net_cash, fifo_pnl_realized, cost "
        "FROM master_log_trades "
        "WHERE (underlying_symbol = ? OR symbol = ?) AND asset_category = 'STK' "
        "ORDER BY date_time LIMIT 3",
        (tk, tk)
    ).fetchall()
    for r in rows:
        print(f"  {tk:6s} acct={r['account_id']} date={r['trade_date']} "
              f"{r['buy_sell']} qty={r['quantity']} price={r['trade_price']} "
              f"net_cash={r['net_cash']} fpnl={r['fifo_pnl_realized']} cost={r['cost']}")

# ── SECTION 2: Paper_basis fallbacks ──
print()
print("=" * 80)
print("SECTION 2: PAPER_BASIS FALLBACK ANALYSIS")
print("=" * 80)

all_events = trade_repo._load_trade_events(conn)

for tk in ['ADBE', 'PYPL']:
    for hh in ['Yash_Household']:
        evs = [e for e in all_events if e.household_id == hh and e.ticker == tk]
        evs_sorted = sorted(evs, key=canonical_sort_key)
        print(f"\n--- {hh}/{tk} ({len(evs)} events) ---")

        for ev in evs_sorted:
            et = classify_event(ev)
            if et != EventType.ASSIGN_STK_LEG or ev.buy_sell != 'BUY':
                continue
            # Find the preceding ASSIGN_OPT_LEG
            strike = expiry = None
            for prev in evs_sorted:
                if (prev.trade_date == ev.trade_date
                        and prev.account_id == ev.account_id
                        and prev.asset_category == 'OPT'
                        and prev.right == 'P'
                        and prev.notes == 'A'
                        and prev.date_time <= ev.date_time):
                    strike = prev.strike
                    expiry = prev.expiry

            if strike is None:
                print(f"  ASSIGN {ev.date_time} acct={ev.account_id} qty={ev.quantity} "
                      f"price={ev.trade_price} -> NO OPT LEG FOUND")
                continue

            # Find the originating CSP_OPEN
            opens = [
                e for e in evs_sorted
                if classify_event(e) == EventType.CSP_OPEN
                and e.account_id == ev.account_id
                and e.strike == strike
                and e.expiry == expiry
            ]

            if opens:
                o = opens[-1]
                pps = o.net_cash / (o.quantity * 100)
                print(f"  ASSIGN {ev.date_time} acct={ev.account_id} qty={ev.quantity} "
                      f"strike={strike} exp={expiry} -> FOUND CSP_OPEN {o.date_time} "
                      f"prem/sh={pps:.4f}")
            else:
                print(f"  ASSIGN {ev.date_time} acct={ev.account_id} qty={ev.quantity} "
                      f"strike={strike} exp={expiry} -> MISSING (needs carry-in)")

# ── PYPL lot analysis ──
print()
print("=" * 80)
print("PYPL LOT ANALYSIS")
print("=" * 80)

pypl_evs = [e for e in all_events if e.household_id == 'Yash_Household' and e.ticker == 'PYPL']
pypl_sorted = sorted(pypl_evs, key=canonical_sort_key)
assign_stk = [e for e in pypl_sorted
               if classify_event(e) == EventType.ASSIGN_STK_LEG and e.buy_sell == 'BUY']
print(f"PYPL Yash assignments: {len(assign_stk)} events, "
      f"total qty={sum(e.quantity for e in assign_stk)}")
for e in assign_stk:
    print(f"  {e.date_time} acct={e.account_id} qty={e.quantity} price={e.trade_price}")

pypl_pos = conn.execute(
    "SELECT account_id, position, cost_basis_price "
    "FROM master_log_open_positions WHERE symbol='PYPL' AND asset_category='STK'"
).fetchall()
print(f"\nIBKR PYPL open positions:")
for r in pypl_pos:
    print(f"  {r['account_id']}: pos={r['position']} cbp={r['cost_basis_price']}")

# Check Trad IRA PYPL trades
pypl_trad = conn.execute(
    "SELECT date_time, transaction_type, asset_category, buy_sell, quantity, "
    "trade_price, notes, symbol "
    "FROM master_log_trades WHERE account_id='U22076184' "
    "AND (underlying_symbol='PYPL' OR symbol='PYPL') ORDER BY date_time"
).fetchall()
print(f"\nPYPL in U22076184 (Trad IRA): {len(pypl_trad)} trades")
for r in pypl_trad:
    print(f"  {r['date_time']} {r['transaction_type']} {r['asset_category']} "
          f"{r['buy_sell']} qty={r['quantity']} price={r['trade_price']} "
          f"notes={r['notes']} sym={r['symbol']}")

# ── U22388499 NAV component sum ──
print()
print("=" * 80)
print("U22388499 NAV COMPONENT SUM")
print("=" * 80)

nav = conn.execute(
    "SELECT * FROM master_log_change_in_nav WHERE account_id='U22388499'"
).fetchone()

def f(field):
    return float(nav[field] or 0)

ibkr_delta = f('ending_value') - f('starting_value')
components = [
    ('realized', f('realized')),
    ('change_in_unrealized', f('change_in_unrealized')),
    ('cost_adjustments', f('cost_adjustments')),
    ('transferred_pnl_adjustments', f('transferred_pnl_adjustments')),
    ('deposits_withdrawals', f('deposits_withdrawals')),
    ('internal_cash_transfers', f('internal_cash_transfers')),
    ('asset_transfers', f('asset_transfers')),
    ('dividends', f('dividends')),
    ('withholding_tax', f('withholding_tax')),
    ('change_in_dividend_accruals', f('change_in_dividend_accruals')),
    ('interest', f('interest')),
    ('change_in_interest_accruals', f('change_in_interest_accruals')),
    ('broker_fees', f('broker_fees')),
    ('change_in_broker_fee_accruals', f('change_in_broker_fee_accruals')),
    ('other_fees', f('other_fees')),
    ('other_income', f('other_income')),
    ('commissions', f('commissions')),
    ('other', f('other')),
    ('mtm', f('mtm')),
    ('corporate_action_proceeds', f('corporate_action_proceeds')),
]
comp_sum = sum(v for _, v in components)
residual = ibkr_delta - comp_sum

print(f"starting_value: {f('starting_value'):.5f}")
print(f"ending_value:   {f('ending_value'):.5f}")
print(f"ibkr_delta:     {ibkr_delta:.5f}")
print(f"component_sum:  {comp_sum:.5f}")
print(f"RESIDUAL:       {residual:.5f}")
print()
for name, val in components:
    if val != 0:
        print(f"  {name:40s} = {val:>14.5f}")

# ── IBKR costBasisPrice for deriving missing premiums ──
print()
print("=" * 80)
print("IBKR costBasisPrice FOR CARRY-IN PREMIUM DERIVATION")
print("=" * 80)

for tk in ['ADBE', 'PYPL']:
    rows = conn.execute(
        "SELECT account_id, position, cost_basis_price "
        "FROM master_log_open_positions "
        "WHERE symbol = ? AND asset_category = 'STK'",
        (tk,)
    ).fetchall()
    for r in rows:
        print(f"  {tk} {r['account_id']}: pos={r['position']} cbp={r['cost_basis_price']}")

conn.close()
os.unlink(db_path)
