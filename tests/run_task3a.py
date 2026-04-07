"""Task 3A: ADBE and PYPL deep dive."""
import sqlite3, sys, os, tempfile, logging
from itertools import groupby
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.ERROR)

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import parse_flex_xml, load_flex_xml_from_file, _upsert_rows
from agt_equities import trade_repo
from agt_equities.walker import (
    walk_cycles, UnknownEventError, classify_event,
    canonical_sort_key, EventType
)

INCEPTION = os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_inception.xml')
db_path = os.path.join(tempfile.gettempdir(), 'test_3a.db')
if os.path.exists(db_path):
    os.unlink(db_path)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
register_master_log_tables(conn)
conn.commit()
for sd in parse_flex_xml(load_flex_xml_from_file(INCEPTION)):
    _upsert_rows(conn, sd['table'], sd['rows'], sd['pk_cols'], 'now')
conn.commit()
trade_repo.DB_PATH = db_path

all_events = trade_repo._load_trade_events(conn)

# ══════════════════════════════════════════════════════════════════
# (b) ADBE $0.23 divergence worked example
# ══════════════════════════════════════════════════════════════════
print("=" * 80)
print("ADBE CROSS-CHECK B DIVERGENCE ANALYSIS")
print("=" * 80)

adbe_yash = [e for e in all_events
             if e.household_id == 'Yash_Household' and e.ticker == 'ADBE']
adbe_sorted = sorted(adbe_yash, key=canonical_sort_key)

cycles = walk_cycles(adbe_sorted)
active = [c for c in cycles if c.status == 'ACTIVE']
if active:
    c = active[0]
    print(f"Walker active cycle: shares={c.shares_held}, paper_basis={c.paper_basis:.6f}")
    print(f"  adjusted_basis={c.adjusted_basis:.6f}")
    print(f"  premium_total={c.premium_total:.5f}")

# Per-account IBKR costBasisPrice
ibkr_adbe = conn.execute(
    "SELECT account_id, position, cost_basis_price "
    "FROM master_log_open_positions "
    "WHERE symbol='ADBE' AND asset_category='STK'"
).fetchall()
print(f"\nIBKR open positions:")
ibkr_total_pos = 0
ibkr_weighted_sum = 0
for r in ibkr_adbe:
    pos = float(r['position'])
    cbp = float(r['cost_basis_price'])
    hh = trade_repo.ACCOUNT_TO_HOUSEHOLD.get(r['account_id'], '?')
    print(f"  {r['account_id']} ({hh}): pos={pos} cbp={cbp:.6f}")
    if hh == 'Yash_Household':
        ibkr_total_pos += pos
        ibkr_weighted_sum += pos * cbp

ibkr_avg_cbp = ibkr_weighted_sum / ibkr_total_pos if ibkr_total_pos else 0
print(f"\nYash weighted avg IBKR cbp: {ibkr_avg_cbp:.6f}")
print(f"Walker paper_basis:         {c.paper_basis:.6f}")
print(f"Delta:                      {c.paper_basis - ibkr_avg_cbp:.6f}")

# Show each assignment's contribution to paper_basis
print("\nPer-assignment IRS basis computation:")
assign_events = [(ev, et) for ev, et in zip(c.events, c.event_types)
                 if et == EventType.ASSIGN_STK_LEG and ev.buy_sell == 'BUY']
running_shares = 0.0
running_basis = 0.0
for ev, et in assign_events:
    # Find the matching ASSIGN_OPT_LEG
    opt_leg = None
    for prev_ev, prev_et in zip(c.events, c.event_types):
        if (prev_et == EventType.ASSIGN_OPT_LEG
                and prev_ev.trade_date == ev.trade_date
                and prev_ev.account_id == ev.account_id
                and prev_ev.right == 'P'):
            opt_leg = prev_ev

    # Find the CSP_OPEN
    csp_open = None
    if opt_leg:
        for o_ev, o_et in zip(c.events, c.event_types):
            if (o_et == EventType.CSP_OPEN
                    and o_ev.account_id == ev.account_id
                    and o_ev.strike == opt_leg.strike
                    and o_ev.expiry == opt_leg.expiry):
                csp_open = o_ev

    if csp_open:
        prem_per_sh = csp_open.net_cash / (csp_open.quantity * 100)
        irs_basis = ev.trade_price - prem_per_sh
    else:
        prem_per_sh = 0
        irs_basis = ev.trade_price

    delta = ev.quantity
    new_shares = running_shares + delta
    new_basis = ((running_basis * running_shares) + (irs_basis * delta)) / new_shares
    running_shares = new_shares
    running_basis = new_basis

    print(f"  {ev.date_time} acct={ev.account_id} qty={delta:.0f} strike={ev.trade_price:.2f} "
          f"prem/sh={prem_per_sh:.4f} irs_basis={irs_basis:.4f} "
          f"-> running: {running_shares:.0f} shares @ {running_basis:.6f}")

# Per-account IBKR breakdown
print("\nPer-account IBKR costBasisPrice:")
for r in ibkr_adbe:
    hh = trade_repo.ACCOUNT_TO_HOUSEHOLD.get(r['account_id'], '?')
    if hh == 'Yash_Household':
        pos = float(r['position'])
        cbp = float(r['cost_basis_price'])
        print(f"  {r['account_id']}: {pos:.0f} shares @ {cbp:.6f}")

# ══════════════════════════════════════════════════════════════════
# (c) PYPL deep dive
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("PYPL DEEP DIVE")
print("=" * 80)

pypl_yash = [e for e in all_events
             if e.household_id == 'Yash_Household' and e.ticker == 'PYPL']
pypl_sorted = sorted(pypl_yash, key=canonical_sort_key)
print(f"PYPL Yash_Household: {len(pypl_sorted)} events")
print(f"Accounts: {sorted(set(e.account_id for e in pypl_sorted))}")

try:
    pypl_cycles = walk_cycles(pypl_sorted)
    active_pypl = [c for c in pypl_cycles if c.status == 'ACTIVE']
    closed_pypl = [c for c in pypl_cycles if c.status == 'CLOSED']
    print(f"Cycles: {len(active_pypl)} active + {len(closed_pypl)} closed")

    if active_pypl:
        c = active_pypl[0]
        print(f"\nActive cycle state:")
        print(f"  shares_held:        {c.shares_held}")
        print(f"  open_short_puts:    {c.open_short_puts}")
        print(f"  open_short_calls:   {c.open_short_calls}")
        print(f"  paper_basis:        {c.paper_basis:.6f}" if c.paper_basis else "  paper_basis: None")
        print(f"  adjusted_basis:     {c.adjusted_basis:.6f}" if c.adjusted_basis else "  adjusted_basis: None")
        print(f"  premium_total:      {c.premium_total:.5f}")

        # Per-account share count
        from collections import defaultdict
        acct_shares = defaultdict(float)
        for ev, et in zip(c.events, c.event_types):
            if et == EventType.ASSIGN_STK_LEG:
                if ev.buy_sell == 'BUY':
                    acct_shares[ev.account_id] += ev.quantity
                else:
                    acct_shares[ev.account_id] -= ev.quantity
            elif et == EventType.STK_BUY_DIRECT:
                acct_shares[ev.account_id] += ev.quantity
            elif et == EventType.STK_SELL_DIRECT:
                acct_shares[ev.account_id] -= ev.quantity

        print(f"\n  Per-account shares in active cycle:")
        for acct, sh in sorted(acct_shares.items()):
            print(f"    {acct}: {sh:.0f}")
        print(f"    Total: {sum(acct_shares.values()):.0f}")

except UnknownEventError as exc:
    print(f"FROZEN: {exc}")

# IBKR positions
print(f"\nIBKR PYPL open positions (Yash accounts):")
pypl_ibkr = conn.execute(
    "SELECT account_id, position, cost_basis_price "
    "FROM master_log_open_positions "
    "WHERE symbol='PYPL' AND asset_category='STK' "
    "AND account_id IN ('U21971297','U22076329','U22076184')"
).fetchall()
ibkr_pypl_total = 0
for r in pypl_ibkr:
    print(f"  {r['account_id']}: pos={r['position']} cbp={r['cost_basis_price']}")
    ibkr_pypl_total += float(r['position'])
print(f"  Total: {ibkr_pypl_total:.0f}")

# Check U22076184 specifically
print(f"\nU22076184 PYPL events (trace):")
u184_pypl = [e for e in pypl_sorted if e.account_id == 'U22076184']
for ev in u184_pypl:
    et = classify_event(ev)
    print(f"  {ev.date_time} {et.value:20s} {ev.asset_category} {ev.buy_sell} "
          f"qty={ev.quantity} price={ev.trade_price} nc={ev.net_cash:.2f} "
          f"notes={ev.notes!r}")

# Weighted avg IBKR cbp for Yash PYPL
ibkr_pypl_wt = sum(float(r['position']) * float(r['cost_basis_price'])
                    for r in pypl_ibkr) / ibkr_pypl_total if ibkr_pypl_total else 0
print(f"\nWeighted avg IBKR cbp (Yash): {ibkr_pypl_wt:.6f}")

conn.close()
os.unlink(db_path)
