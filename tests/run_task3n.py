"""Task 3N: ADBE realized P&L divergence investigation."""
import sqlite3, sys, os, tempfile, logging, csv
from itertools import groupby
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.ERROR)

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import parse_flex_xml, load_flex_xml_from_file, _upsert_rows
from agt_equities import trade_repo
from agt_equities.walker import walk_cycles, classify_event, canonical_sort_key, EventType

INCEPTION = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_inception.xml')
db_path = os.path.join(tempfile.gettempdir(), 'test_3n.db')
if os.path.exists(db_path):
    os.unlink(db_path)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
register_master_log_tables(conn)
conn.commit()
for sd in parse_flex_xml(load_flex_xml_from_file(INCEPTION)):
    _upsert_rows(conn, sd['table'], sd['rows'], sd['pk_cols'], 'now')
with open(os.path.join(os.path.dirname(__file__), '..', 'data', 'inception_carryin.csv'), 'r') as f:
    for row in csv.DictReader((r for r in f if not r.startswith('#'))):
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

all_ev = trade_repo._load_trade_events(conn)
ci = trade_repo._load_carryin_events(conn)
combined = ci + all_ev

# ── (a) Walker per-event realized P&L ──
print("=" * 100)
print("(a) WALKER ADBE YASH PER-EVENT REALIZED P&L")
print("=" * 100)

adbe = sorted([e for e in combined if e.household_id == 'Yash_Household' and e.ticker == 'ADBE'],
              key=canonical_sort_key)
cycles = walk_cycles(adbe)

running = 0.0
for c in cycles:
    for ev, et in zip(c.events, c.event_types):
        if ev.fifo_pnl_realized != 0:
            running += ev.fifo_pnl_realized
            print(f"  {ev.date_time} {ev.account_id} {et.value:22s} "
                  f"qty={ev.quantity:>5.0f} price={ev.trade_price:>8.2f} "
                  f"fpnl={ev.fifo_pnl_realized:>12.5f} running={running:>12.2f}")

walker_total = sum(c.realized_pnl for c in cycles)
print(f"\nWalker total realized (sum of cycle.realized_pnl): {walker_total:.5f}")
print(f"Running sum of fifo_pnl_realized events:           {running:.5f}")

# ── (b) IBKR realized from master_log_realized_unrealized_perf ──
print()
print("=" * 100)
print("(b) IBKR ADBE REALIZED FROM FIFO PERFORMANCE SUMMARY")
print("=" * 100)

max_rd = conn.execute(
    "SELECT MAX(report_date) FROM master_log_realized_unrealized_perf"
).fetchone()[0]

ibkr_rows = conn.execute(
    "SELECT account_id, symbol, asset_category, total_realized_pnl, "
    "realized_st_profit, realized_st_loss, realized_lt_profit, realized_lt_loss, "
    "description "
    "FROM master_log_realized_unrealized_perf "
    "WHERE (underlying_symbol = 'ADBE' OR symbol = 'ADBE') "
    "AND report_date = ? "
    "ORDER BY account_id, symbol",
    (max_rd,)
).fetchall()

ibkr_total = 0.0
ibkr_by_account = {}
print(f"Report date: {max_rd}")
print(f"{'acct':>12s} {'symbol':>30s} {'cat':>4s} {'total_realized':>14s} "
      f"{'st_profit':>12s} {'st_loss':>12s} {'lt_profit':>12s} {'lt_loss':>12s}")
for r in ibkr_rows:
    acct = r['account_id']
    hh = trade_repo.ACCOUNT_TO_HOUSEHOLD.get(acct, '?')
    if hh != 'Yash_Household':
        continue
    total = float(r['total_realized_pnl'] or 0)
    ibkr_total += total
    ibkr_by_account.setdefault(acct, 0.0)
    ibkr_by_account[acct] += total
    print(f"  {acct:>12s} {r['symbol']:>30s} {r['asset_category'] or '':>4s} "
          f"{total:>14.5f} "
          f"{float(r['realized_st_profit'] or 0):>12.5f} "
          f"{float(r['realized_st_loss'] or 0):>12.5f} "
          f"{float(r['realized_lt_profit'] or 0):>12.5f} "
          f"{float(r['realized_lt_loss'] or 0):>12.5f}")

print(f"\nIBKR total realized (Yash ADBE): {ibkr_total:.5f}")
for acct, val in sorted(ibkr_by_account.items()):
    print(f"  {acct}: {val:.5f}")

print(f"\nDelta (Walker - IBKR): {walker_total - ibkr_total:.5f}")

# ── (c) Break down by account ──
print()
print("=" * 100)
print("(c) PER-ACCOUNT COMPARISON")
print("=" * 100)

walker_by_account = {}
for c in cycles:
    for ev in c.events:
        if ev.fifo_pnl_realized != 0:
            walker_by_account.setdefault(ev.account_id, 0.0)
            walker_by_account[ev.account_id] += ev.fifo_pnl_realized

for acct in sorted(set(list(walker_by_account.keys()) + list(ibkr_by_account.keys()))):
    w = walker_by_account.get(acct, 0.0)
    i = ibkr_by_account.get(acct, 0.0)
    d = w - i
    print(f"  {acct}: walker={w:>12.5f} ibkr={i:>12.5f} delta={d:>10.5f}")

# ── (d) Direct DB sum vs Walker sum ──
print()
print("=" * 100)
print("(d) DIRECT DB SUM vs WALKER CYCLE SUM")
print("=" * 100)

for acct in ['U21971297', 'U22076329', 'U22076184']:
    db_sum = conn.execute(
        "SELECT SUM(CAST(fifo_pnl_realized AS REAL)) "
        "FROM master_log_trades "
        "WHERE account_id = ? AND (underlying_symbol = 'ADBE' OR symbol = 'ADBE')",
        (acct,)
    ).fetchone()[0] or 0
    w = walker_by_account.get(acct, 0.0)
    print(f"  {acct}: db_sum={db_sum:>12.5f} walker={w:>12.5f} delta={db_sum - w:>10.5f}")

# Total DB sum
total_db = conn.execute(
    "SELECT SUM(CAST(fifo_pnl_realized AS REAL)) "
    "FROM master_log_trades "
    "WHERE (underlying_symbol = 'ADBE' OR symbol = 'ADBE') "
    "AND account_id IN ('U21971297', 'U22076329', 'U22076184')"
).fetchone()[0] or 0
print(f"\n  Total DB: {total_db:.5f}")
print(f"  Total Walker: {walker_total:.5f}")
print(f"  Total IBKR FIFO: {ibkr_total:.5f}")
print(f"  DB - Walker: {total_db - walker_total:.5f}")
print(f"  DB - IBKR: {total_db - ibkr_total:.5f}")

conn.close()
os.unlink(db_path)
