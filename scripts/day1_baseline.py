"""Day 1 baseline verification script for Phase 3A.5a triage."""
import sys, os, sqlite3
from collections import defaultdict
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_equities.rule_engine import (
    PortfolioState, CorrelationData, AccountELSnapshot, evaluate_all,
)
from agt_equities.mode_engine import (
    compute_mode, evaluate_glide_path, load_glide_paths,
)
from agt_equities.data_provider import IBKRProvider, DataProviderError

HOUSEHOLD_MAP = {
    'U21971297': 'Yash_Household',
    'U22076329': 'Yash_Household',
    'U22076184': 'Yash_Household',
    'U22388499': 'Vikram_Household',
}

def main():
    # 1. Load glide paths
    conn = sqlite3.connect('agt_desk.db')
    conn.row_factory = sqlite3.Row
    glide_paths = load_glide_paths(conn)
    print(f"Loaded {len(glide_paths)} glide paths")

    # 2. Real positions from Flex
    positions_raw = conn.execute(
        "SELECT account_id, symbol, position, mark_price "
        "FROM master_log_open_positions "
        "WHERE asset_category = 'STK' AND report_date = '20260406' AND position > 0"
    ).fetchall()

    hh_ticker_shares = defaultdict(lambda: defaultdict(int))
    for r in positions_raw:
        hh = HOUSEHOLD_MAP.get(r['account_id'], 'Unknown')
        hh_ticker_shares[hh][r['symbol']] += int(r['position'])

    cycles = []
    for hh, tickers in hh_ticker_shares.items():
        for ticker, shares in tickers.items():
            c = MagicMock()
            c.household_id = hh
            c.ticker = ticker
            c.shares_held = shares
            c.status = 'ACTIVE'
            c.paper_basis = 100.0
            cycles.append(c)

    print(f"Built {len(cycles)} cycles from Flex")
    for c in cycles:
        print(f"  {c.household_id:20s} {c.ticker:10s} {c.shares_held:>5} sh")

    # 3. NLV from Flex
    nav_rows = conn.execute(
        "SELECT account_id, total FROM master_log_nav WHERE report_date = '20260406'"
    ).fetchall()
    account_nlv_map = {}
    hh_nlv = defaultdict(float)
    for r in nav_rows:
        acct = r['account_id']
        nlv = float(r['total'])
        account_nlv_map[acct] = nlv
        hh = HOUSEHOLD_MAP.get(acct, 'Unknown')
        hh_nlv[hh] += nlv

    # 4. Spots from Flex mark prices
    spot_rows = conn.execute(
        "SELECT DISTINCT symbol, mark_price FROM master_log_open_positions "
        "WHERE asset_category = 'STK' AND report_date = '20260406'"
    ).fetchall()
    spots = {r['symbol']: float(r['mark_price']) for r in spot_rows}
    conn.close()

    # 5. Live EL from IBKR
    provider = IBKRProvider(host='127.0.0.1', port=4001, client_id=94, market_data_mode='delayed')
    try:
        yash_s = provider.get_account_summary('U21971297')
        vik_s = provider.get_account_summary('U22388499')
        yash_snap = AccountELSnapshot(
            excess_liquidity=yash_s.excess_liquidity,
            net_liquidation=yash_s.net_liquidation,
            timestamp=yash_s.timestamp.isoformat(), stale=False,
        )
        vik_snap = AccountELSnapshot(
            excess_liquidity=vik_s.excess_liquidity,
            net_liquidation=vik_s.net_liquidation,
            timestamp=vik_s.timestamp.isoformat(), stale=False,
        )
        print(f"\nLive EL:")
        print(f"  Yash U21971297: EL=${yash_s.excess_liquidity:,.2f} NLV=${yash_s.net_liquidation:,.2f}")
        print(f"  Vik  U22388499: EL=${vik_s.excess_liquidity:,.2f} NLV=${vik_s.net_liquidation:,.2f}")
    except DataProviderError as e:
        print(f"IBKR error: {e}")
        return
    finally:
        provider.disconnect()

    # 6. Correlations (live ADBE-CRM-QCOM, approximated others)
    corrs = {
        ('ADBE', 'CRM'): CorrelationData(0.6915, 179, False, 'ibkr_live'),
        ('ADBE', 'QCOM'): CorrelationData(0.3418, 179, False, 'ibkr_live'),
        ('CRM', 'QCOM'): CorrelationData(0.3136, 179, False, 'ibkr_live'),
        ('ADBE', 'MSFT'): CorrelationData(0.55, 179, False, 'approx'),
        ('ADBE', 'PYPL'): CorrelationData(0.50, 179, False, 'approx'),
        ('ADBE', 'UBER'): CorrelationData(0.40, 179, False, 'approx'),
        ('CRM', 'MSFT'): CorrelationData(0.52, 179, False, 'approx'),
        ('CRM', 'PYPL'): CorrelationData(0.48, 179, False, 'approx'),
        ('CRM', 'UBER'): CorrelationData(0.35, 179, False, 'approx'),
        ('MSFT', 'PYPL'): CorrelationData(0.45, 179, False, 'approx'),
        ('MSFT', 'QCOM'): CorrelationData(0.42, 179, False, 'approx'),
        ('MSFT', 'UBER'): CorrelationData(0.38, 179, False, 'approx'),
        ('PYPL', 'QCOM'): CorrelationData(0.30, 179, False, 'approx'),
        ('PYPL', 'UBER'): CorrelationData(0.33, 179, False, 'approx'),
        ('QCOM', 'UBER'): CorrelationData(0.28, 179, False, 'approx'),
    }

    # 7. Build PortfolioState
    ps = PortfolioState(
        household_nlv=dict(hh_nlv),
        household_el={'Yash_Household': yash_s.excess_liquidity,
                      'Vikram_Household': vik_s.excess_liquidity},
        active_cycles=cycles,
        spots=spots,
        betas={t: 1.0 for t in spots},
        industries={
            'ADBE': 'Software - Application', 'CRM': 'Software - Application',
            'QCOM': 'Semiconductors', 'PYPL': 'Software - Infrastructure',
            'MSFT': 'Software - Infrastructure', 'UBER': 'Travel Services',
            'SLS': 'Biotechnology', 'GTLB': 'Software - Infrastructure',
            'IBKR': 'Capital Markets',
        },
        sector_overrides={'UBER': 'Consumer Cyclical'},
        vix=22.0,
        report_date='20260407',
        correlations=corrs,
        account_el={'U21971297': yash_snap, 'U22388499': vik_snap},
        account_nlv=account_nlv_map,
    )

    print(f"\nHousehold NLV: Yash=${hh_nlv['Yash_Household']:,.2f} Vik=${hh_nlv['Vikram_Household']:,.2f}")

    # 8. Evaluate
    print("\n=== RAW EVALUATIONS ===")
    all_evals = []
    for hh in ['Yash_Household', 'Vikram_Household']:
        results = evaluate_all(ps, hh)
        all_evals.extend(results)
        print(f"\n--- {hh} ---")
        for r in results:
            if r.status != 'PENDING':
                print(f"  {r.rule_id:8s} {r.status:7s} raw={r.raw_value}  {r.message[:65]}")

    # 9. Apply glide paths
    print("\n=== GLIDE PATH SOFTENING ===")
    softened = []
    for ev in all_evals:
        matching_gps = [gp for gp in glide_paths
                        if gp.rule_id == ev.rule_id
                        and gp.household_id == (ev.household or '')
                        and (gp.ticker == ev.ticker or gp.ticker is None)]
        if matching_gps and ev.status == 'RED' and ev.raw_value is not None:
            gp = matching_gps[0]
            gp_status, expected, delta = evaluate_glide_path(gp, ev.raw_value, '2026-04-07')
            if gp_status != ev.status:
                print(f"  {ev.rule_id:8s} {ev.household:20s} {ev.status} -> {gp_status} "
                      f"(raw={ev.raw_value:.4f} exp={expected:.4f} delta={delta:.4f})")
            ev_copy = type(ev)(
                rule_id=ev.rule_id, rule_name=ev.rule_name,
                household=ev.household, ticker=ev.ticker,
                raw_value=ev.raw_value, status=gp_status,
                message=ev.message, cure_math=ev.cure_math, detail=ev.detail,
            )
            softened.append(ev_copy)
        else:
            softened.append(ev)

    # 10. Compute mode
    mode, trigger_rule, trigger_hh, trigger_val = compute_mode(softened)
    print(f"\n=== OVERALL MODE: {mode} ===")
    if trigger_rule:
        print(f"  Trigger: {trigger_rule} ({trigger_hh}) = {trigger_val}")

    print("\n=== Post-softening non-GREEN/non-PENDING ===")
    found_non_green = False
    for ev in softened:
        if ev.status not in ('GREEN', 'PENDING'):
            print(f"  {ev.rule_id:8s} {ev.household:20s} {ev.status:7s} raw={ev.raw_value}")
            found_non_green = True
    if not found_non_green:
        print("  (all GREEN or PENDING)")

    print()
    if mode == 'PEACETIME':
        print("DAY 1 BASELINE: PEACETIME -- PASS")
    else:
        print(f"DAY 1 BASELINE: {mode} -- HARD STOP")

if __name__ == '__main__':
    main()
