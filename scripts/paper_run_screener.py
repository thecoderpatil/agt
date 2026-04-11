"""
scripts/paper_run_screener.py

End-to-end paper run of the screener pipeline. READ-ONLY — no DB writes
beyond the existing market_data_log audit rows from ib_chains, no order
placement, no staging. Connects to live IB Gateway on port 4001.

Usage:
    python scripts/paper_run_screener.py

Expected runtime: ~14-18 minutes (dominated by Phase 1 Finnhub throttling).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure the repo root is on sys.path so agt_equities is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Configure logging to show screener phase logs at INFO level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Quiet down noisy third-party loggers
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.WARNING)

logger = logging.getLogger("paper_run")


async def main() -> int:
    overall_start = time.monotonic()

    print("=" * 70)
    print("SCREENER PAPER RUN")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 70)

    # Load env
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=True)

    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if not finnhub_key:
        print("ERROR: FINNHUB_API_KEY missing from .env")
        return 1

    # -----------------------------------------------------------------
    # Imports — all screener phases
    # -----------------------------------------------------------------
    from agt_equities.screener.finnhub_client import FinnhubClient
    from agt_equities.screener.universe import run_phase_1
    from agt_equities.screener.technicals import run_phase_2
    from agt_equities.screener.fundamentals import run_phase_3
    from agt_equities.screener.correlation import run_phase_3_5
    from agt_equities.screener.vol_event_armor import run_phase_4
    from agt_equities.screener.chain_walker import run_phase_5
    from agt_equities.screener.ray_filter import run_phase_6

    import ib_async

    # -----------------------------------------------------------------
    # Phase 1 — Finnhub Free profile2
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PHASE 1 — Universe (Finnhub profile2)")
    print("=" * 70)
    phase_start = time.monotonic()
    try:
        client = FinnhubClient(api_key=finnhub_key)
        universe = await run_phase_1(client)
        elapsed = time.monotonic() - phase_start
        print(f"\nPhase 1 result: {len(universe)} survivors in {elapsed:.1f}s")
        if not universe:
            print("ERROR: Phase 1 returned zero survivors. Aborting.")
            return 1
        # Show first 10 survivors for sanity
        print("First 10 survivors:")
        for u in universe[:10]:
            print(f"  {u.ticker:6s} {u.name[:40]:40s} {u.sector[:25]:25s} ${u.market_cap_usd/1e9:8.1f}B")
    except Exception as exc:
        print(f"PHASE 1 FAILED: {type(exc).__name__}: {exc}")
        return 1

    # -----------------------------------------------------------------
    # Phase 2 — yfinance batch technicals
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PHASE 2 — Technical pullback (yfinance batch)")
    print("=" * 70)
    phase_start = time.monotonic()
    try:
        phase2_out = run_phase_2(universe)
        elapsed = time.monotonic() - phase_start
        print(f"\nPhase 2 result: {len(phase2_out.survivors)} survivors in {elapsed:.1f}s")
        if not phase2_out.survivors:
            print("WARNING: Phase 2 returned zero survivors. Nothing to pass downstream.")
            print("This is normal if the market is in a broad uptrend with no pullbacks.")
            return 0
        print("Survivors:")
        for tc in phase2_out.survivors:
            print(
                f"  {tc.ticker:6s} spot=${tc.current_price:7.2f} "
                f"sma200=${tc.sma_200:7.2f} rsi={tc.rsi_14:5.1f} "
                f"bb_low=${tc.bband_lower:7.2f}"
            )
    except Exception as exc:
        print(f"PHASE 2 FAILED: {type(exc).__name__}: {exc}")
        return 1

    # -----------------------------------------------------------------
    # Phase 3 — yfinance per-ticker fundamentals
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PHASE 3 — Fundamental fortress (yfinance per-ticker)")
    print("=" * 70)
    phase_start = time.monotonic()
    try:
        fund_cands = run_phase_3(phase2_out.survivors)
        elapsed = time.monotonic() - phase_start
        print(f"\nPhase 3 result: {len(fund_cands)} survivors in {elapsed:.1f}s")
        if not fund_cands:
            print("No Phase 3 survivors. Pipeline stops here.")
            return 0
        print("Survivors:")
        for fc in fund_cands:
            print(
                f"  {fc.ticker:6s} Z={fc.altman_z:5.1f} "
                f"FCF={fc.fcf_yield*100:5.1f}% ND/E={fc.net_debt_to_ebitda:5.2f} "
                f"ROIC={fc.roic*100:5.1f}% SI={fc.short_interest_pct*100:4.1f}%"
            )
    except Exception as exc:
        print(f"PHASE 3 FAILED: {type(exc).__name__}: {exc}")
        return 1

    # -----------------------------------------------------------------
    # Phase 3.5 — correlation fit
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("PHASE 3.5 — Correlation fit (global, no holdings)")
    print("=" * 70)
    phase_start = time.monotonic()
    try:
        # For the paper run, we use EMPTY current_holdings. This is explicit:
        # we are measuring pipeline runtime behavior, not filtering against
        # the real book. With empty holdings, every fundamental candidate
        # passes the correlation gate with max_abs_correlation=0.0.
        # Real holdings wiring is C7 scope.
        corr_cands = run_phase_3_5(
            candidates=fund_cands,
            price_history=phase2_out.price_history,
            current_holdings=[],
        )
        elapsed = time.monotonic() - phase_start
        print(f"\nPhase 3.5 result: {len(corr_cands)} survivors in {elapsed:.1f}s")
        if not corr_cands:
            print("No Phase 3.5 survivors. Pipeline stops here.")
            return 0
    except Exception as exc:
        print(f"PHASE 3.5 FAILED: {type(exc).__name__}: {exc}")
        return 1

    # -----------------------------------------------------------------
    # Connect to IB Gateway
    # -----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("IB GATEWAY CONNECTION")
    print("=" * 70)
    ib = ib_async.IB()
    try:
        await ib.connectAsync("127.0.0.1", 4001, clientId=98)
        print(f"Connected to Gateway. Managed accounts: {ib.managedAccounts()}")
    except Exception as exc:
        print(f"IB CONNECTION FAILED: {type(exc).__name__}: {exc}")
        print("Is IB Gateway running and logged in on port 4001?")
        return 1

    try:
        # -------------------------------------------------------------
        # Phase 4 — vol/event armor
        # -------------------------------------------------------------
        print("\n" + "=" * 70)
        print("PHASE 4 — Vol/event armor (IBKR hist IV + corporate cal)")
        print("=" * 70)
        phase_start = time.monotonic()
        try:
            vol_cands = await run_phase_4(corr_cands, ib)
            elapsed = time.monotonic() - phase_start
            print(f"\nPhase 4 result: {len(vol_cands)} survivors in {elapsed:.1f}s")
            if not vol_cands:
                print("No Phase 4 survivors. Pipeline stops here.")
                return 0
            print("Survivors:")
            for vc in vol_cands:
                next_earn = vc.next_earnings.isoformat() if vc.next_earnings else "None"
                print(
                    f"  {vc.ticker:6s} IVR={vc.ivr_pct:5.1f}% "
                    f"IV={vc.iv_latest*100:5.1f}% "
                    f"next_earn={next_earn}"
                )
        except Exception as exc:
            print(f"PHASE 4 FAILED: {type(exc).__name__}: {exc}")
            return 1

        # -------------------------------------------------------------
        # Phase 5 — IBKR chain walker
        # -------------------------------------------------------------
        print("\n" + "=" * 70)
        print("PHASE 5 — IBKR option chain walker")
        print("=" * 70)
        phase_start = time.monotonic()
        try:
            strikes = await run_phase_5(vol_cands, ib)
            elapsed = time.monotonic() - phase_start
            print(f"\nPhase 5 result: {len(strikes)} strike candidates in {elapsed:.1f}s")
            if not strikes:
                print("No Phase 5 strikes. Pipeline stops here.")
                return 0
        except Exception as exc:
            print(f"PHASE 5 FAILED: {type(exc).__name__}: {exc}")
            return 1

        # -------------------------------------------------------------
        # Phase 6 — RAY filter
        # -------------------------------------------------------------
        print("\n" + "=" * 70)
        print("PHASE 6 — RAY band filter (terminal)")
        print("=" * 70)
        phase_start = time.monotonic()
        try:
            rays = run_phase_6(strikes)
            elapsed = time.monotonic() - phase_start
            print(f"\nPhase 6 result: {len(rays)} RAY survivors in {elapsed:.2f}s")
        except Exception as exc:
            print(f"PHASE 6 FAILED: {type(exc).__name__}: {exc}")
            return 1

        # -------------------------------------------------------------
        # Final output
        # -------------------------------------------------------------
        print("\n" + "=" * 70)
        print("FINAL PIPELINE OUTPUT")
        print("=" * 70)
        total_elapsed = time.monotonic() - overall_start
        print(f"Total pipeline runtime: {total_elapsed:.1f}s")
        print(f"Funnel: {len(universe)} -> {len(phase2_out.survivors)} -> "
              f"{len(fund_cands)} -> {len(corr_cands)} -> "
              f"{len(vol_cands)} -> {len(strikes)} -> {len(rays)}")

        if rays:
            print(f"\nAll {len(rays)} RAY candidates:")
            print(f"  {'TICKER':6s} {'EXPIRY':12s} {'DTE':>4s} "
                  f"{'STRIKE':>8s} {'MID':>7s} {'YIELD':>8s} "
                  f"{'OTM%':>7s} {'IVR':>6s}")
            print("  " + "-" * 66)
            for rc in rays:
                print(
                    f"  {rc.ticker:6s} {rc.expiry:12s} {rc.dte:4d} "
                    f"${rc.strike:7.2f} ${rc.mid:6.2f} "
                    f"{rc.annualized_yield:6.1f}%  "
                    f"{rc.otm_pct:5.1f}%  {rc.ivr_pct:4.1f}%"
                )
        else:
            print("\nNo RAY candidates survived the pipeline.")
            print("This is not necessarily a bug. It means the market currently")
            print("offers no strikes in the 30-130% annualized yield band for")
            print("the names that passed the upstream quality filters.")

        return 0

    finally:
        try:
            ib.disconnect()
            print("\nIB Gateway disconnected cleanly.")
        except Exception:
            pass


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)