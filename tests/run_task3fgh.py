"""Tasks 3F, 3G, 3H data gathering."""
import sqlite3, sys, os, tempfile, logging, csv
from itertools import groupby
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.ERROR)

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import parse_flex_xml, load_flex_xml_from_file, _upsert_rows
from agt_equities import trade_repo
from agt_equities.walker import (
    walk_cycles, UnknownEventError, classify_event,
    canonical_sort_key, EventType, _new_cycle, _apply_event
)

INCEPTION = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_inception.xml')
db_path = os.path.join(tempfile.gettempdir(), 'test_3fgh.db')
if os.path.exists(db_path):
    os.unlink(db_path)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
register_master_log_tables(conn)
conn.commit()
for sd in parse_flex_xml(load_flex_xml_from_file(INCEPTION)):
    _upsert_rows(conn, sd['table'], sd['rows'], sd['pk_cols'], 'now')
with open(os.path.join(os.path.dirname(__file__), '..', 'data', 'inception_carryin.csv'), 'r') as f:
    reader = csv.DictReader((row for row in f if not row.startswith('#')))
    for row in reader:
        if not row.get('symbol'):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO inception_carryin "
            "(household_id,account_id,asset_class,symbol,conid,right,strike,expiry,"
            "quantity,basis_price,as_of_date,source_broker,reason,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row['household_id'], row['account_id'], row['asset_class'], row['symbol'],
             row.get('conid') or None, row.get('right') or None,
             row.get('strike') or None, row.get('expiry') or None,
             row['quantity'], row.get('basis_price'), row['as_of_date'],
             row.get('source_broker'), row.get('reason'), row.get('notes')))
conn.commit()
trade_repo.DB_PATH = db_path

ACCT_TO_HH = trade_repo.ACCOUNT_TO_HOUSEHOLD

# ══════════════════════════════════════════════════════════════════
# TASK 3F: Derive basis from IBKR
# ══════════════════════════════════════════════════════════════════
print("=" * 80)
print("TASK 3F: DERIVE CARRY-IN BASIS FROM IBKR")
print("=" * 80)

# QCOM — ACATS sell in U21971297
print("\n--- QCOM U21971297 (ACATS pattern) ---")
qcom_sell = conn.execute(
    "SELECT trade_date, buy_sell, quantity, trade_price, net_cash, cost, "
    "fifo_pnl_realized FROM master_log_trades "
    "WHERE account_id='U21971297' AND symbol='QCOM' AND asset_category='STK' "
    "ORDER BY date_time LIMIT 1"
).fetchone()
if qcom_sell:
    cost = float(qcom_sell['cost'] or 0)
    qty = abs(float(qcom_sell['quantity']))
    basis = abs(cost) / qty if qty > 0 else 0
    print(f"  sell: {qcom_sell['trade_date']} qty={qcom_sell['quantity']} "
          f"price={qcom_sell['trade_price']} cost={cost} "
          f"=> basis_per_share={basis:.2f}")

# AMD — U22076329 sells CCs, needs stock position
print("\n--- AMD U22076329 (pre-window stock) ---")
# Check open_positions
amd_pos = conn.execute(
    "SELECT account_id, position, cost_basis_price, symbol "
    "FROM master_log_open_positions "
    "WHERE symbol='AMD' AND asset_category='STK'"
).fetchall()
for r in amd_pos:
    print(f"  open_pos: {r['account_id']} pos={r['position']} cbp={r['cost_basis_price']}")

# Check all AMD STK trades to see if stock was acquired/sold
amd_stk = conn.execute(
    "SELECT account_id, trade_date, buy_sell, quantity, trade_price, cost, notes "
    "FROM master_log_trades "
    "WHERE (underlying_symbol='AMD' OR symbol='AMD') AND asset_category='STK' "
    "ORDER BY date_time"
).fetchall()
print(f"  AMD STK trades: {len(amd_stk)}")
for r in amd_stk:
    print(f"    {r['account_id']} {r['trade_date']} {r['buy_sell']} "
          f"qty={r['quantity']} price={r['trade_price']} cost={r['cost']} "
          f"notes={r['notes']}")

# UBER — U22076329 pre-window stock
print("\n--- UBER U22076329 (pre-window stock) ---")
uber_pos = conn.execute(
    "SELECT account_id, position, cost_basis_price "
    "FROM master_log_open_positions "
    "WHERE symbol='UBER' AND asset_category='STK'"
).fetchall()
for r in uber_pos:
    print(f"  open_pos: {r['account_id']} pos={r['position']} cbp={r['cost_basis_price']}")

uber_stk = conn.execute(
    "SELECT account_id, trade_date, buy_sell, quantity, trade_price, cost, notes "
    "FROM master_log_trades "
    "WHERE (underlying_symbol='UBER' OR symbol='UBER') AND asset_category='STK' "
    "AND account_id='U22076329' ORDER BY date_time"
).fetchall()
print(f"  UBER STK trades in U22076329: {len(uber_stk)}")
for r in uber_stk:
    print(f"    {r['trade_date']} {r['buy_sell']} qty={r['quantity']} "
          f"price={r['trade_price']} cost={r['cost']} notes={r['notes']}")

# AMZN Yash — U21971297 re-acquired stock
print("\n--- AMZN U21971297 (re-acquired stock) ---")
amzn_pos = conn.execute(
    "SELECT account_id, position, cost_basis_price "
    "FROM master_log_open_positions "
    "WHERE symbol='AMZN' AND asset_category='STK'"
).fetchall()
for r in amzn_pos:
    print(f"  open_pos: {r['account_id']} pos={r['position']} cbp={r['cost_basis_price']}")

amzn_stk_yash = conn.execute(
    "SELECT account_id, trade_date, buy_sell, quantity, trade_price, cost, notes "
    "FROM master_log_trades "
    "WHERE (underlying_symbol='AMZN' OR symbol='AMZN') AND asset_category='STK' "
    "AND account_id IN ('U21971297','U22076329','U22076184') ORDER BY date_time"
).fetchall()
print(f"  AMZN STK trades in Yash: {len(amzn_stk_yash)}")
for r in amzn_stk_yash:
    print(f"    {r['account_id']} {r['trade_date']} {r['buy_sell']} "
          f"qty={r['quantity']} price={r['trade_price']} cost={r['cost']} "
          f"notes={r['notes']}")

# AMZN Vikram — U22388499 pre-window stock
print("\n--- AMZN U22388499 (Vikram pre-window stock) ---")
amzn_stk_vik = conn.execute(
    "SELECT account_id, trade_date, buy_sell, quantity, trade_price, cost, notes "
    "FROM master_log_trades "
    "WHERE (underlying_symbol='AMZN' OR symbol='AMZN') AND asset_category='STK' "
    "AND account_id='U22388499' ORDER BY date_time"
).fetchall()
print(f"  AMZN STK trades in Vikram: {len(amzn_stk_vik)}")
for r in amzn_stk_vik:
    print(f"    {r['trade_date']} {r['buy_sell']} qty={r['quantity']} "
          f"price={r['trade_price']} cost={r['cost']} notes={r['notes']}")

# ══════════════════════════════════════════════════════════════════
# TASK 3G: NFLX / PLTR classification
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("TASK 3G: NFLX / PLTR CLASSIFICATION")
print("=" * 80)

for tk, acct_filter in [('NFLX', 'U22076329'), ('PLTR', 'U21971297')]:
    print(f"\n--- {tk} {acct_filter} ---")

    # Open positions
    pos = conn.execute(
        "SELECT account_id, asset_category, position, cost_basis_price, symbol, "
        "put_call, strike, expiry "
        "FROM master_log_open_positions "
        "WHERE (symbol LIKE ? OR underlying_symbol=?) AND account_id=?",
        (tk + '%', tk, acct_filter)
    ).fetchall()
    print(f"  Open positions: {len(pos)}")
    for r in pos:
        print(f"    {r['account_id']} {r['asset_category']} pos={r['position']} "
              f"cbp={r['cost_basis_price']} sym={r['symbol']} "
              f"pc={r['put_call']} strike={r['strike']} exp={r['expiry']}")

    # All events in 365d window
    all_ev = trade_repo._load_trade_events(conn)
    hh = ACCT_TO_HH[acct_filter]
    tk_evs = [e for e in all_ev if e.ticker == tk and e.household_id == hh]
    tk_sorted = sorted(tk_evs, key=canonical_sort_key)
    print(f"  Events in window: {len(tk_sorted)}")
    for ev in tk_sorted:
        et = classify_event(ev).value
        print(f"    {ev.date_time} {et:20s} {ev.asset_category} {ev.buy_sell} "
              f"qty={ev.quantity} strike={ev.strike} right={ev.right} "
              f"exp={ev.expiry} acct={ev.account_id}")

# ══════════════════════════════════════════════════════════════════
# TASK 3H: CRM FULL HOUSEHOLD TRACE
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("TASK 3H: CRM FULL HOUSEHOLD TRACE (87 events)")
print("=" * 80)

all_ev = trade_repo._load_trade_events(conn)
ci_ev = trade_repo._load_carryin_events(conn)
crm_evs = [e for e in (ci_ev + all_ev) if e.household_id == 'Yash_Household' and e.ticker == 'CRM']
crm_sorted = sorted(crm_evs, key=canonical_sort_key)
print(f"CRM Yash: {len(crm_sorted)} events, accounts={sorted(set(e.account_id for e in crm_sorted))}")

print(f"\n{'idx':>4s} {'date':>15s} {'acct':>12s} {'event_type':>22s} {'qty':>5s} "
      f"{'strike':>8s} {'expiry':>10s} {'rt':>2s} | {'shr':>5s} {'p':>3s} {'c':>3s} {'state':>6s}")
print("-" * 115)

current = None
cycles = []
seq = 0
prev_td = None

for i, ev in enumerate(crm_sorted):
    et = classify_event(ev)

    if prev_td is not None and ev.trade_date != prev_td:
        if (current is not None
                and current.shares_held == 0
                and current.open_short_puts == 0
                and current.open_short_calls == 0):
            current.status = 'CLOSED'
            current.closed_at = prev_td
            cycles.append(current)
            print(f"     {'--- EOD CLOSURE on ' + prev_td + ' ---':>60s} | "
                  f"{current.shares_held:>5.0f} {current.open_short_puts:>3d} "
                  f"{current.open_short_calls:>3d} CLOSED")
            current = None

    if current is None:
        if et == EventType.CSP_OPEN:
            seq += 1
            current = _new_cycle('Yash_Household', 'CRM', seq, ev.trade_date)
        elif et in (EventType.CARRYIN_OPT, EventType.CARRYIN_STK):
            seq += 1
            current = _new_cycle('Yash_Household', 'CRM', seq, ev.trade_date)
        else:
            print(f"{i:>4d} {ev.date_time:>15s} {ev.account_id:>12s} "
                  f"{et.value:>22s} {ev.quantity:>5.0f} "
                  f"{str(ev.strike or ''):>8s} {str(ev.expiry or ''):>10s} "
                  f"{str(ev.right or ''):>2s} | *** ORPHAN ***")
            break

    _apply_event(current, ev, et)
    print(f"{i:>4d} {ev.date_time:>15s} {ev.account_id:>12s} "
          f"{et.value:>22s} {ev.quantity:>5.0f} "
          f"{str(ev.strike or ''):>8s} {str(ev.expiry or ''):>10s} "
          f"{str(ev.right or ''):>2s} | "
          f"{current.shares_held:>5.0f} {current.open_short_puts:>3d} "
          f"{current.open_short_calls:>3d}")

    prev_td = ev.trade_date

if current is not None:
    if (current.shares_held == 0
            and current.open_short_puts == 0
            and current.open_short_calls == 0):
        print(f"     {'--- EOD CLOSURE on ' + prev_td + ' ---':>60s}")
    else:
        print(f"     {'--- ACTIVE ---':>60s} | "
              f"{current.shares_held:>5.0f} {current.open_short_puts:>3d} "
              f"{current.open_short_calls:>3d}")

conn.close()
os.unlink(db_path)
