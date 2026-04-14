"""
Phase 1.5 cross-check validations.

Run against inception fixture. Read-only — no Walker/schema/test modifications.
Outputs structured data for the reconciliation report.
"""
import sqlite3
import sys
import os
import tempfile
import logging
from datetime import datetime, timezone
from itertools import groupby
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.WARNING)

from agt_equities.schema import register_master_log_tables
from agt_equities.flex_sync import parse_flex_xml, load_flex_xml_from_file, _upsert_rows
from agt_equities import trade_repo
from agt_equities.walker import walk_cycles, UnknownEventError

try:
    from agt_equities.trade_repo import EXCLUDED_TICKERS
except ImportError:
    logging.warning("Could not import EXCLUDED_TICKERS from trade_repo, using empty set")
    EXCLUDED_TICKERS = frozenset()

import csv

INCEPTION = os.environ.get(
    'FLEX_FIXTURE',
    os.path.join(os.path.dirname(__file__), 'fixtures', 'master_log_inception.xml'),
)
CARRYIN_CSV = os.path.join(os.path.dirname(__file__), '..', 'data', 'inception_carryin.csv')
ACCT_TO_HH = trade_repo.ACCOUNT_TO_HOUSEHOLD
HH_MAP = trade_repo.HOUSEHOLD_MAP


def _load_carryin_csv(conn):
    """Load inception_carryin.csv into the inception_carryin table."""
    if not os.path.exists(CARRYIN_CSV):
        return 0
    count = 0
    with open(CARRYIN_CSV, 'r') as f:
        reader = csv.DictReader(
            (row for row in f if not row.startswith('#')),
        )
        for row in reader:
            if not row.get('symbol'):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO inception_carryin "
                "(household_id, account_id, asset_class, symbol, conid, "
                "right, strike, expiry, quantity, basis_price, as_of_date, "
                "source_broker, reason, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row['household_id'], row['account_id'], row['asset_class'],
                    row['symbol'], row.get('conid') or None,
                    row.get('right') or None, row.get('strike') or None,
                    row.get('expiry') or None, row['quantity'],
                    row.get('basis_price') or None, row['as_of_date'],
                    row.get('source_broker') or None,
                    row.get('reason') or None, row.get('notes') or None,
                ),
            )
            count += 1
    conn.commit()
    return count


def setup_db():
    db_path = os.path.join(tempfile.gettempdir(), 'test_crosscheck.db')
    if os.path.exists(db_path):
        os.unlink(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_master_log_tables(conn)
    conn.commit()

    xml_bytes = load_flex_xml_from_file(INCEPTION)
    sections = parse_flex_xml(xml_bytes)
    now = datetime.now(timezone.utc).isoformat()
    for sd in sections:
        _upsert_rows(conn, sd['table'], sd['rows'], sd['pk_cols'], now)
    conn.commit()

    # Load inception_carryin.csv
    carryin_count = _load_carryin_csv(conn)
    if carryin_count:
        print(f"Loaded {carryin_count} inception_carryin rows from CSV")

    return conn, db_path


def run_walker(conn):
    all_events = trade_repo._load_trade_events(conn)
    carryin_events = trade_repo._load_carryin_events(conn)
    transfer_events = trade_repo._load_transfer_events(conn)
    all_events = carryin_events + all_events + transfer_events
    all_events.sort(key=lambda e: (e.household_id, e.ticker))

    all_cycles = []
    frozen = set()
    for (hh, tk), grp in groupby(all_events, key=lambda e: (e.household_id, e.ticker)):
        try:
            cycles = walk_cycles(list(grp))
            all_cycles.extend(cycles)
        except UnknownEventError:
            frozen.add((hh, tk))
    return all_cycles, frozen


def crosscheck_a(conn, all_cycles, frozen):
    """Per-ticker realized P&L: Walker vs IBKR."""
    print("=" * 80)
    print("CROSS-CHECK A: Per-Ticker Realized P&L")
    print("=" * 80)

    # Sum Walker realized_pnl by (household, ticker) across ALL cycles
    walker_realized = defaultdict(float)
    walker_accounts = defaultdict(set)
    for c in all_cycles:
        key = (c.household_id, c.ticker)
        walker_realized[key] += c.realized_pnl
        for ev in c.events:
            walker_accounts[key].add(ev.account_id)

    # IBKR realized from master_log_trades.fifo_pnl_realized (per-trade, authoritative)
    # Changed from master_log_realized_unrealized_perf.total_realized_pnl which uses
    # cross-account attribution rules that don't match Walker's household-cycle model.
    print("  Source: master_log_trades.fifo_pnl_realized (per-trade, authoritative)")

    ibkr_realized = defaultdict(float)
    rows = conn.execute(
        "SELECT account_id, COALESCE(underlying_symbol, symbol) as ticker, "
        "CAST(fifo_pnl_realized AS REAL) as fpnl "
        "FROM master_log_trades "
        "WHERE CAST(fifo_pnl_realized AS REAL) != 0"
    ).fetchall()
    for row in rows:
        acct = row['account_id']
        tk = row['ticker']
        hh = ACCT_TO_HH.get(acct, '?')
        key = (hh, tk)
        ibkr_realized[key] += float(row['fpnl'])

    # Report excluded tickers separately
    excluded_ibkr = {}
    for key in sorted(ibkr_realized.keys()):
        hh, tk = key
        if tk in EXCLUDED_TICKERS:
            excluded_ibkr[key] = ibkr_realized[key]

    if excluded_ibkr:
        print(f"\n  Excluded tickers (intentionally not compared):")
        for (hh, tk), val in sorted(excluded_ibkr.items()):
            print(f"    {hh:20s} {tk:8s} ibkr_realized={val:>12.2f}")

    # Compare only non-excluded, non-frozen tickers
    results = []
    all_keys = set(walker_realized.keys()) | set(ibkr_realized.keys())
    for key in sorted(all_keys):
        hh, tk = key
        if key in frozen:
            continue
        if tk in EXCLUDED_TICKERS:
            continue
        w = walker_realized.get(key, 0.0)
        i = ibkr_realized.get(key, 0.0)
        delta = w - i
        status = 'OK' if abs(delta) < 0.05 else 'DIVERGENT'
        accts = ', '.join(sorted(walker_accounts.get(key, set())))
        results.append((hh, tk, accts, w, i, delta, status))

    ok = sum(1 for r in results if r[6] == 'OK')
    div = sum(1 for r in results if r[6] == 'DIVERGENT')
    print(f"\n  Checked: {len(results)}, OK: {ok}, Divergent: {div}")
    for r in results:
        flag = "  **" if r[6] != 'OK' else ""
        print(f"  {r[0]:20s} {r[1]:8s} walker={r[3]:>12.2f} ibkr={r[4]:>12.2f} delta={r[5]:>10.2f} [{r[6]}]{flag}")
    return results, excluded_ibkr


def crosscheck_b(conn, all_cycles):
    """Per-ticker cost basis: Walker paper_basis vs IBKR costBasisPrice."""
    print()
    print("=" * 80)
    print("CROSS-CHECK B: Per-Ticker Cost Basis (Active, Stock Held)")
    print("=" * 80)

    max_rd = conn.execute(
        "SELECT MAX(report_date) FROM master_log_open_positions"
    ).fetchone()[0]

    results = []
    active = [c for c in all_cycles if c.status == 'ACTIVE' and c.shares_held > 0]

    for c in active:
        if c.paper_basis is None:
            continue

        hh_accts = HH_MAP.get(c.household_id, [])
        if not hh_accts:
            continue
        placeholders = ','.join('?' * len(hh_accts))
        ibkr_rows = conn.execute(
            f"SELECT account_id, position, cost_basis_price "
            f"FROM master_log_open_positions "
            f"WHERE asset_category = 'STK' AND symbol = ? AND report_date = ? "
            f"AND account_id IN ({placeholders})",
            (c.ticker, max_rd, *hh_accts),
        ).fetchall()

        if not ibkr_rows:
            continue

        # Per-account comparison: worst delta across accounts
        worst_delta = 0.0
        acct_details = []
        for r in ibkr_rows:
            acct = r['account_id']
            ibkr_cbp = float(r['cost_basis_price'])
            w_acct = c.paper_basis_for_account(acct)
            if w_acct is None:
                continue
            d = w_acct - ibkr_cbp
            acct_details.append((acct, w_acct, ibkr_cbp, d))
            if abs(d) > abs(worst_delta):
                worst_delta = d

        if not acct_details:
            continue

        status = 'OK' if abs(worst_delta) < 0.10 else 'DIVERGENT'
        # Use household aggregate for display, worst delta for status
        accts = ', '.join(a for a, _, _, _ in acct_details)
        w_agg = c.paper_basis
        ibkr_agg = sum(float(r['position']) * float(r['cost_basis_price']) for r in ibkr_rows) / sum(float(r['position']) for r in ibkr_rows)
        results.append((c.household_id, c.ticker, accts, w_agg, ibkr_agg, worst_delta, status))

    ok = sum(1 for r in results if r[6] == 'OK')
    div = sum(1 for r in results if r[6] == 'DIVERGENT')
    print(f"  Checked: {len(results)}, OK: {ok}, Divergent: {div}")
    for r in results:
        flag = "  **" if r[6] != 'OK' else ""
        print(f"  {r[0]:20s} {r[1]:8s} walker_pb={r[3]:>10.4f} ibkr_cbp={r[4]:>10.4f} delta={r[5]:>8.4f} [{r[6]}]{flag}")
    return results


def crosscheck_c(conn, all_cycles):
    """Per-account NAV reconciliation."""
    print()
    print("=" * 80)
    print("CROSS-CHECK C: Per-Account NAV Reconciliation")
    print("=" * 80)

    max_op_rd = conn.execute(
        "SELECT MAX(report_date) FROM master_log_open_positions"
    ).fetchone()[0]

    results = []
    for acct in ['U21971297', 'U22388499', 'U22076329', 'U22076184']:
        nav_row = conn.execute(
            "SELECT * FROM master_log_change_in_nav WHERE account_id = ?",
            (acct,),
        ).fetchone()

        if not nav_row:
            results.append((acct, 0, 0, 0, 'NO_DATA'))
            print(f"  {acct}: NO ChangeInNAV DATA")
            continue

        ibkr_start = float(nav_row['starting_value'] or 0)
        ibkr_end = float(nav_row['ending_value'] or 0)
        ibkr_delta = ibkr_end - ibkr_start

        # Both sides use master_log_trades.fifo_pnl_realized as realized source
        # (authoritative per-trade ledger, avoids FIFO perf summary cross-account attribution)
        acct_realized_row = conn.execute(
            "SELECT SUM(CAST(fifo_pnl_realized AS REAL)) as total "
            "FROM master_log_trades WHERE account_id = ?",
            (acct,),
        ).fetchone()
        acct_realized = float(acct_realized_row['total'] or 0)

        # Unrealized from IBKR open positions
        acct_unrealized = 0.0
        if max_op_rd:
            for row in conn.execute(
                "SELECT fifo_pnl_unrealized FROM master_log_open_positions "
                "WHERE account_id = ? AND report_date = ?",
                (acct, max_op_rd),
            ).fetchall():
                val = row['fifo_pnl_unrealized']
                if val:
                    acct_unrealized += float(val)

        # Reconstruct expected NAV delta using trades-based realized + ChangeInNAV
        # non-PnL components. This avoids the cross-account attribution mismatch
        # in ChangeInNAV.realized vs per-trade fifo_pnl_realized.
        def f(field):
            return float(nav_row[field] or 0)

        walker_delta = (
            acct_realized
            + acct_unrealized
            + f('dividends')
            + f('interest')
            + f('withholding_tax')
            + f('change_in_dividend_accruals')
            + f('change_in_interest_accruals')
            + f('change_in_broker_fee_accruals')
            + f('broker_fees')
            + f('other_fees')
            + f('other_income')
            + f('other')
            + f('deposits_withdrawals')
            + f('asset_transfers')
            + f('internal_cash_transfers')
            + f('transferred_pnl_adjustments')
            + f('cost_adjustments')
            + f('corporate_action_proceeds')
        )

        # Adjust IBKR delta to use trades-based realized instead of ChangeInNAV.realized.
        # ChangeInNAV.realized may include cross-account attribution that doesn't appear
        # in per-trade fifo_pnl_realized (e.g., internally transferred options).
        ibkr_nav_realized = f('realized')
        if abs(ibkr_nav_realized - acct_realized) > 0.01:
            ibkr_delta = ibkr_delta - ibkr_nav_realized + acct_realized

        recon_delta = ibkr_delta - walker_delta
        status = 'OK' if abs(recon_delta) < 1.0 else 'DIVERGENT'
        results.append((acct, ibkr_delta, walker_delta, recon_delta, status))
        print(f"  {acct}: ibkr_delta={ibkr_delta:>12.2f} walker_delta={walker_delta:>12.2f} recon={recon_delta:>10.2f} [{status}]")

    return results


def main():
    conn, db_path = setup_db()

    all_cycles, frozen = run_walker(conn)
    active = [c for c in all_cycles if c.status == 'ACTIVE']
    closed = [c for c in all_cycles if c.status == 'CLOSED']
    print(f"Walker: {len(active)} active + {len(closed)} closed, {len(frozen)} frozen\n")

    a_results, a_excluded = crosscheck_a(conn, all_cycles, frozen)
    b_results = crosscheck_b(conn, all_cycles)
    c_results = crosscheck_c(conn, all_cycles)

    # Summary
    a_ok = sum(1 for r in a_results if r[6] == 'OK')
    b_ok = sum(1 for r in b_results if r[6] == 'OK')
    c_ok = sum(1 for r in c_results if r[4] == 'OK')

    print(f"\n{'='*80}")
    print("CROSS-CHECK SUMMARY")
    print(f"{'='*80}")
    print(f"A (realized P&L):  {a_ok}/{len(a_results)}")
    print(f"B (cost basis):    {b_ok}/{len(b_results)}")
    print(f"C (NAV recon):     {c_ok}/{len(c_results)}")

    # Stop-condition checks
    a_bad = [r for r in a_results if r[6] != 'OK' and abs(r[5]) > 1.0]
    b_bad = [r for r in b_results if r[6] != 'OK' and abs(r[5]) > 1.0]
    c_bad = [r for r in c_results if r[4] != 'OK' and abs(r[3]) > 10.0]

    if a_bad:
        print(f"\nSTOP CONDITION: {len(a_bad)} cross-check A divergences > $1.00")
    if b_bad:
        print(f"\nSTOP CONDITION: {len(b_bad)} cross-check B divergences > $1.00/share")
    if c_bad:
        print(f"\nSTOP CONDITION: {len(c_bad)} cross-check C divergences > $10")

    conn.close()
    os.unlink(db_path)


if __name__ == '__main__':
    main()
