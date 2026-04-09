#!/usr/bin/env python
"""One-shot rule evaluation dump for P3.2-alt Day 1.4 smoke test.

Read-only. No mutations. No telegram_bot imports. No IB calls.
All data from DB + yfinance.

Usage:
    python scripts/dump_rules.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows console encoding fix — force UTF-8 output
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = ROOT / "agt_desk.db"

_gaps: list[str] = []


# ---------------------------------------------------------------------------
# Data loaders (each wrapped in try/except, returns empty on failure)
# ---------------------------------------------------------------------------

def _load_nav(conn: sqlite3.Connection) -> dict[str, float]:
    """Per-account NAV from master_log_nav (per-account MAX(report_date))."""
    try:
        rows = conn.execute("""
            SELECT m1.account_id, CAST(m1.total AS REAL) as nav, m1.report_date
            FROM master_log_nav m1
            WHERE m1.report_date = (
                SELECT MAX(m2.report_date) FROM master_log_nav m2
                WHERE m2.account_id = m1.account_id
            )
        """).fetchall()
        result = {}
        today = date.today().strftime("%Y%m%d")
        for r in rows:
            acct = r["account_id"]
            nav_val = r["nav"]
            rd = r["report_date"]
            result[acct] = nav_val
            if rd and rd < today:
                try:
                    rd_date = datetime.strptime(rd, "%Y%m%d").date()
                    days_stale = (date.today() - rd_date).days
                    print(f"  {acct}: ${nav_val:>12,.2f}  (report_date={rd}, {days_stale}d stale)")
                except Exception:
                    print(f"  {acct}: ${nav_val:>12,.2f}  (report_date={rd})")
            else:
                print(f"  {acct}: ${nav_val:>12,.2f}  (report_date={rd}, current)")
        return result
    except Exception as exc:
        print(f"[WARN] NAV load failed: {exc}", file=sys.stderr)
        _gaps.append(f"NAV: load failed ({exc})")
        return {}


def _derive_household_nlv(nav_by_acct: dict[str, float]) -> dict[str, float]:
    from agt_equities.config import ACCOUNT_TO_HOUSEHOLD
    hh_nlv: dict[str, float] = {}
    for acct, nav in nav_by_acct.items():
        hh = ACCOUNT_TO_HOUSEHOLD.get(acct)
        if hh:
            hh_nlv[hh] = hh_nlv.get(hh, 0.0) + nav
    return hh_nlv


def _load_active_cycles() -> list:
    try:
        from agt_equities import trade_repo
        trade_repo.DB_PATH = DB_PATH
        return trade_repo.get_active_cycles()
    except Exception as exc:
        print(f"[WARN] Active cycles load failed: {exc}", file=sys.stderr)
        _gaps.append(f"Active cycles: load failed ({exc})")
        return []


def _fetch_spots_yfinance(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    try:
        import yfinance as yf
        data = yf.download(tickers, period="1d", progress=False)
        result = {}
        if not data.empty:
            close = data["Close"]
            if hasattr(close, "iloc"):
                last = close.iloc[-1]
                for t in tickers:
                    try:
                        val = float(last[t]) if t in last.index else None
                        if val and val > 0:
                            result[t] = round(val, 2)
                    except Exception:
                        pass
        missed = [t for t in tickers if t not in result]
        if missed:
            _gaps.append(f"Spots: yfinance missed {missed}")
        return result
    except Exception as exc:
        print(f"[WARN] yfinance spots fetch failed: {exc}", file=sys.stderr)
        _gaps.append(f"Spots: yfinance failed ({exc})")
        return {}


def _load_betas(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, float]:
    try:
        rows = conn.execute("SELECT ticker, beta, fetched_ts FROM beta_cache").fetchall()
        result = {}
        for r in rows:
            result[r["ticker"]] = r["beta"]
        # Report staleness
        if rows:
            latest_ts = max(r["fetched_ts"] for r in rows if r["fetched_ts"])
            print(f"  Beta cache: {len(rows)} tickers, last refresh: {latest_ts}")
        # Fallback to 1.0 for missing
        for t in tickers:
            if t not in result:
                result[t] = 1.0
        return result
    except Exception as exc:
        print(f"[WARN] Beta cache load failed: {exc}", file=sys.stderr)
        _gaps.append(f"Betas: load failed ({exc})")
        return {t: 1.0 for t in tickers}


def _fetch_vix() -> float | None:
    try:
        import yfinance as yf
        vix_data = yf.Ticker("^VIX").history(period="1d")
        if not vix_data.empty:
            val = float(vix_data["Close"].iloc[-1])
            return round(val, 2)
        return None
    except Exception as exc:
        print(f"[WARN] VIX fetch failed: {exc}", file=sys.stderr)
        _gaps.append(f"VIX: fetch failed ({exc})")
        return None


def _load_industries(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT ticker, gics_industry_group FROM ticker_universe "
            "WHERE gics_industry_group IS NOT NULL"
        ).fetchall()
        return {r["ticker"]: r["gics_industry_group"] for r in rows}
    except Exception as exc:
        print(f"[WARN] Industries load failed: {exc}", file=sys.stderr)
        _gaps.append(f"Industries: load failed ({exc})")
        return {}


def _load_sector_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT ticker, sector FROM sector_overrides").fetchall()
        return {r["ticker"]: r["sector"] for r in rows}
    except Exception:
        return {}


def _load_el_from_snapshots(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Load EL from el_snapshots (stale if bot not running)."""
    from agt_equities.config import ACCOUNT_TO_HOUSEHOLD, MARGIN_ELIGIBLE_ACCOUNTS
    from agt_equities.rule_engine import AccountELSnapshot

    household_el: dict[str, float | None] = {}
    account_el: dict[str, AccountELSnapshot] = {}

    try:
        # Get latest snapshot per account
        for acct in ACCOUNT_TO_HOUSEHOLD:
            row = conn.execute(
                "SELECT excess_liquidity, nlv, timestamp "
                "FROM el_snapshots WHERE account_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (acct,),
            ).fetchone()
            if row and row["nlv"]:
                hh = ACCOUNT_TO_HOUSEHOLD[acct]
                el_val = row["excess_liquidity"] or 0
                nlv_val = row["nlv"]
                ts_str = row["timestamp"] or ""

                # Staleness
                stale = False
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age_s = int((datetime.now(timezone.utc) - ts).total_seconds())
                    stale = age_s > 300  # >5min = stale
                    print(f"  EL {acct}: EL=${el_val:,.0f} NLV=${nlv_val:,.0f} ({age_s}s old{'  STALE' if stale else ''})")
                except Exception:
                    print(f"  EL {acct}: EL=${el_val:,.0f} NLV=${nlv_val:,.0f} (timestamp parse failed)")

                household_el.setdefault(hh, 0.0)
                household_el[hh] = (household_el[hh] or 0) + el_val

                account_el[acct] = AccountELSnapshot(
                    excess_liquidity=el_val,
                    net_liquidation=nlv_val,
                    timestamp=ts_str,
                    stale=stale,
                )
    except Exception as exc:
        print(f"[WARN] EL snapshots load failed: {exc}", file=sys.stderr)
        _gaps.append(f"EL: load failed ({exc})")

    if not account_el:
        _gaps.append("EL: no snapshots found (bot may not be running)")

    return household_el, account_el


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _print_eval(ev) -> None:
    status_icon = {"GREEN": "+", "AMBER": "~", "RED": "!", "PENDING": "?"}
    icon = status_icon.get(ev.status, " ")
    ticker_str = f" ({ev.ticker})" if ev.ticker else ""
    print(f"  [{icon}] [{ev.rule_id.upper().replace('RULE_', 'R')}] {ev.rule_name}{ticker_str}: {ev.status}")
    if ev.raw_value is not None:
        print(f"        value={ev.raw_value:.4f}")
    if ev.message:
        msg = ev.message[:120]
        print(f"        {msg}")
    if ev.cure_math:
        print(f"        cure_math={ev.cure_math}")


def _print_glide_paths(conn: sqlite3.Connection) -> None:
    print(f"\n{'='*60}")
    print("  GLIDE PATHS")
    print(f"{'='*60}")
    try:
        rows = conn.execute(
            "SELECT household_id, rule_id, ticker, baseline_value, target_value, "
            "start_date, target_date, pause_conditions FROM glide_paths "
            "ORDER BY household_id, rule_id, ticker"
        ).fetchall()
        if not rows:
            print("  (none)")
            return
        today = date.today()
        for r in rows:
            hh = r["household_id"].replace("_Household", "")
            rule = r["rule_id"].replace("rule_", "R")
            tk = r["ticker"] or "(portfolio)"
            baseline = r["baseline_value"]
            target = r["target_value"]
            paused = False
            if r["pause_conditions"]:
                try:
                    pc = json.loads(r["pause_conditions"])
                    paused = pc.get("paused", False)
                except Exception:
                    pass
            days_rem = 0
            try:
                td = datetime.strptime(r["target_date"], "%Y-%m-%d").date()
                days_rem = max(0, (td - today).days)
            except Exception:
                pass
            pause_tag = " [PAUSED]" if paused else ""
            print(f"  {hh:8s} {rule:4s} {tk:10s}  baseline={baseline:>6.2f} → target={target:>6.2f}  {days_rem:>3d}d remaining{pause_tag}")
    except Exception as exc:
        print(f"  [WARN] glide_paths query failed: {exc}")


def _print_gaps() -> None:
    print(f"\n{'='*60}")
    print("  DATA GAPS / WARNINGS")
    print(f"{'='*60}")
    if not _gaps:
        print("  None — all data sources loaded successfully.")
    else:
        for g in _gaps:
            print(f"  [!] {g}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"AGT Equities — Rule Dump")
    print(f"Timestamp: {datetime.now().isoformat(timespec='seconds')}")
    print(f"DB: {DB_PATH}")
    print()

    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")

    # 1. NAV
    print("── NAV (per-account MAX(report_date)) ──")
    nav_by_acct = _load_nav(conn)
    hh_nlv = _derive_household_nlv(nav_by_acct)
    total = sum(nav_by_acct.values())
    print(f"  TOTAL: ${total:>12,.2f}")
    for hh, nlv in sorted(hh_nlv.items()):
        print(f"  {hh}: ${nlv:>12,.2f}")
    print()

    # 2. Active cycles
    print("── Active Cycles (Walker) ──")
    cycles = _load_active_cycles()
    print(f"  Total: {len(cycles)}")
    for c in cycles:
        cc = f"{c.open_short_calls}C" if c.open_short_calls else "--"
        print(f"  {c.household_id:20s} {c.ticker:6s} sh={int(c.shares_held):>4d} cc={cc:>3s} basis=${c.paper_basis:>8.2f}" if c.paper_basis else f"  {c.household_id:20s} {c.ticker:6s} sh={int(c.shares_held):>4d} cc={cc:>3s} basis=    None")
    print()

    # 3. Spots
    wheel_tickers = sorted({c.ticker for c in cycles if c.status == 'ACTIVE'})
    print(f"── Spots (yfinance, {len(wheel_tickers)} tickers) ──")
    spots = _fetch_spots_yfinance(wheel_tickers)
    for t in wheel_tickers:
        s = spots.get(t)
        print(f"  {t:6s}: ${s:>8.2f}" if s else f"  {t:6s}: MISSING")
    print()

    # 4. Betas
    print("── Betas (beta_cache) ──")
    betas = _load_betas(conn, wheel_tickers)
    print()

    # 5. VIX
    print("── VIX ──")
    vix = _fetch_vix()
    print(f"  VIX: {vix:.2f}" if vix else "  VIX: UNAVAILABLE")
    print()

    # 6. Industries + sector overrides
    industries = _load_industries(conn)
    sector_overrides = _load_sector_overrides(conn)

    # 7. EL
    print("── EL Snapshots ──")
    household_el, account_el = _load_el_from_snapshots(conn)
    print()

    # 8. Build PortfolioState
    from agt_equities.rule_engine import (
        PortfolioState, evaluate_all, compute_leverage_pure,
    )

    ps = PortfolioState(
        household_nlv=hh_nlv,
        household_el=household_el,
        active_cycles=cycles,
        spots=spots,
        betas=betas,
        industries=industries,
        sector_overrides=sector_overrides,
        vix=vix,
        report_date=date.today().strftime("%Y%m%d"),
        account_el=account_el,
        account_nlv={a: nav_by_acct.get(a, 0) for a in nav_by_acct},
    )

    # 9. Evaluate per household
    for hh in sorted(hh_nlv.keys()):
        print(f"{'='*60}")
        print(f"  HOUSEHOLD: {hh}")
        print(f"  NLV: ${hh_nlv[hh]:,.2f}")
        print(f"{'='*60}")

        evals = evaluate_all(ps, hh, conn=conn)
        for ev in evals:
            _print_eval(ev)

        # Leverage detail
        lev = compute_leverage_pure(cycles, spots, betas, hh_nlv, hh)
        print(f"\n  Rule 11 leverage: {lev:.4f}x (limit 1.50x, hysteresis release 1.40x)")

        # Concentration detail for top positions
        hh_nav = hh_nlv.get(hh, 1)
        print(f"\n  Concentration detail:")
        concs = []
        for c in cycles:
            if c.status == 'ACTIVE' and c.shares_held > 0 and c.household_id == hh:
                spot = spots.get(c.ticker, 0)
                pct = (c.shares_held * spot / hh_nav * 100) if hh_nav > 0 else 0
                concs.append((c.ticker, pct, int(c.shares_held), spot))
        concs.sort(key=lambda x: -x[1])
        for tk, pct, sh, sp in concs:
            flag = " *** OVER 20%" if pct > 20 else ""
            print(f"    {tk:6s}: {pct:>5.1f}% ({sh} sh × ${sp:,.2f}){flag}")
        print()

    # 10. Glide paths
    _print_glide_paths(conn)

    # 11. Gaps
    _print_gaps()

    conn.close()
    print(f"\nDone. {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
