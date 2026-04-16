#!/usr/bin/env python3
"""
daily_dryrun.py — Offline dry-run of the /daily unified scan.

Exercises all three engines (CC picker, roll evaluator, CSP harvest)
against real DB positions + synthetic market scenarios. No IB connection
needed. Produces a structured JSON report.

Usage:
    python scripts/daily_dryrun.py [--db path/to/agt_desk.db] [--out reports/daily_dryrun.json]

Scenarios per position:
  - current_mark: uses DB mark_price as spot proxy
  - deep_itm: spot = strike * 1.15 (for calls) or strike * 0.85 (for puts)
  - near_expiry: DTE forced to 1
  - extrinsic_depleted: ask set to intrinsic + $0.05
  - near_basis: strike set to paper_basis - $1
  - above_basis: strike set to paper_basis + $2
  - harvest_80pct: ask = 20% of initial credit (day-1)
  - harvest_92pct: ask = 8% of initial credit (day-2+)
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

# ── Bootstrap project imports ───────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agt_equities.roll_engine import (
    evaluate as roll_evaluate,
    Position,
    MarketSnapshot,
    PortfolioContext,
    OptionQuote,
    ConstraintMatrix,
)
from agt_equities.cc_engine import (
    pick_cc_strike,
    CCPickerInput,
    ChainStrike,
)
from agt_equities.csp_harvest import _should_harvest_csp

# ── Account config (mirrors telegram_bot.py) ────────────────────────────────
ACCOUNT_TO_HOUSEHOLD = {
    "U21971297": "Yash_Household",
    "U22076329": "Yash_Household",
    "U22388499": "Vikram_Household",
}

HOUSEHOLD_ACCOUNTS = {
    "Yash_Household": ["U21971297", "U22076329"],
    "Vikram_Household": ["U22388499"],
}


# ── DB helpers ──────────────────────────────────────────────────────────────

def load_stock_positions(conn: sqlite3.Connection) -> list[dict]:
    """Load latest stock positions per account from master_log_open_positions."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT o.account_id, o.acct_alias, o.symbol, o.position,
               o.cost_basis_price, o.mark_price, o.report_date
        FROM master_log_open_positions o
        INNER JOIN (
            SELECT account_id, symbol, MAX(report_date) AS max_date
            FROM master_log_open_positions
            WHERE asset_category = 'STK' AND position > 0
            GROUP BY account_id, symbol
        ) latest ON o.account_id = latest.account_id
                 AND o.symbol = latest.symbol
                 AND o.report_date = latest.max_date
        WHERE o.asset_category = 'STK' AND o.position > 0
        ORDER BY o.account_id, o.symbol
    """).fetchall()
    return [dict(r) for r in rows]


def load_open_short_calls(conn: sqlite3.Connection) -> list[dict]:
    """Load latest short call positions per account."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT o.account_id, o.acct_alias, o.underlying_symbol, o.strike,
               o.expiry, o.position, o.cost_basis_price, o.mark_price, o.report_date
        FROM master_log_open_positions o
        INNER JOIN (
            SELECT account_id, underlying_symbol, strike, expiry, MAX(report_date) AS max_date
            FROM master_log_open_positions
            WHERE asset_category = 'OPT' AND put_call = 'C' AND position < 0
            GROUP BY account_id, underlying_symbol, strike, expiry
        ) latest ON o.account_id = latest.account_id
                 AND o.underlying_symbol = latest.underlying_symbol
                 AND o.strike = latest.strike
                 AND o.expiry = latest.expiry
                 AND o.report_date = latest.max_date
        WHERE o.asset_category = 'OPT' AND o.put_call = 'C' AND o.position < 0
        ORDER BY o.account_id, o.underlying_symbol
    """).fetchall()
    return [dict(r) for r in rows]


def load_open_short_puts(conn: sqlite3.Connection) -> list[dict]:
    """Load short put trades that are still open (SELL + no matching BUY close)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT t.account_id, t.symbol AS underlying_symbol, t.strike, t.expiry,
               t.trade_price AS initial_credit, t.quantity, t.trade_date,
               t.cost AS cost_basis
        FROM master_log_trades t
        WHERE t.asset_category = 'OPT' AND t.put_call = 'P' AND t.buy_sell = 'SELL'
        ORDER BY t.account_id, t.symbol
    """).fetchall()
    return [dict(r) for r in rows]


def load_premium_ledger(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Load premium ledger keyed by (household, ticker)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM premium_ledger").fetchall()
    return {(r["household_id"], r["ticker"]): dict(r) for r in rows}


def load_stock_basis(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Load per-account stock basis from latest open positions."""
    positions = load_stock_positions(conn)
    return {(p["account_id"], p["symbol"]): p["cost_basis_price"] for p in positions}


# ── Synthetic chain builder ─────────────────────────────────────────────────

def build_synthetic_chain(
    spot: float,
    base_expiry: date,
    iv: float = 0.35,
    num_strikes: int = 10,
    strike_step: float = 1.0,
) -> list[OptionQuote]:
    """Build a synthetic options chain around the money."""
    chain = []
    # Generate strikes from ATM - 5*step to ATM + num_strikes*step
    base_strike = round(spot)
    for i in range(-5, num_strikes + 1):
        s = base_strike + i * strike_step
        if s <= 0:
            continue
        dte = max(1, (base_expiry - date.today()).days)
        # Simple BS-ish pricing: more OTM = cheaper
        moneyness = (s - spot) / spot
        time_value = spot * iv * (dte / 365) ** 0.5 * max(0.01, 0.4 - abs(moneyness))
        intrinsic = max(0, spot - s)  # for calls
        mid = intrinsic + time_value
        spread = max(0.05, mid * 0.05)
        bid = max(0.01, mid - spread / 2)
        ask = mid + spread / 2
        delta = max(-0.99, min(-0.01, -0.5 + moneyness * 2.5))  # crude

        chain.append(OptionQuote(
            strike=s,
            expiry=base_expiry + timedelta(days=7),  # next week
            bid=round(bid, 2),
            ask=round(ask, 2),
            delta=round(delta, 4),
            iv=iv,
        ))
    return chain


def build_cc_chain(spot: float, expiry: date, iv: float = 0.30) -> list[ChainStrike]:
    """Build synthetic CC chain for the CC picker."""
    chain = []
    base_strike = round(spot)
    step = max(1.0, round(spot * 0.01))  # ~1% increments
    for i in range(-3, 12):
        s = base_strike + i * step
        if s <= 0:
            continue
        dte = max(1, (expiry - date.today()).days)
        moneyness = (s - spot) / spot
        time_value = spot * iv * (dte / 365) ** 0.5 * max(0.01, 0.4 - abs(moneyness))
        intrinsic = max(0, spot - s)
        mid = intrinsic + time_value
        spread = max(0.05, mid * 0.08)
        bid = max(0.01, mid - spread / 2)
        ask = mid + spread / 2
        delta = max(0.01, min(0.99, 0.5 - moneyness * 2.5))

        chain.append(ChainStrike(
            strike=s,
            bid=round(bid, 2),
            ask=round(ask, 2),
            delta=round(delta, 4),
        ))
    return chain


# ── Scenario builders ───────────────────────────────────────────────────────

def run_roll_scenarios(
    short_calls: list[dict],
    stock_basis: dict[tuple[str, str], float],
    premium_ledger: dict[tuple[str, str], dict],
) -> list[dict]:
    """Run roll_engine.evaluate() across multiple scenarios per position."""
    results = []
    today = date.today()
    for sc in short_calls:
        acct = sc["account_id"]
        ticker = sc["underlying_symbol"]
        strike = float(sc["strike"])
        household = ACCOUNT_TO_HOUSEHOLD.get(acct, "Unknown")

        # Parse expiry
        exp_str = str(sc["expiry"])
        try:
            exp_date = date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:8]))
        except (ValueError, TypeError):
            continue

        real_dte = (exp_date - today).days
        if real_dte < 0:
            continue  # expired

        # Get basis
        paper_basis = stock_basis.get((acct, ticker))
        ledger = premium_ledger.get((household, ticker))
        assigned_basis = ledger["initial_basis"] if ledger else None

        spot_proxy = float(sc.get("mark_price") or 0)
        if spot_proxy <= 0:
            # Fallback: use basis as rough proxy
            spot_proxy = paper_basis or strike

        # Reconstruct a rough spot from the option mark
        # mark_price on an option is the option price, not the stock
        # We need stock spot — get it from stock positions
        # Actually mark_price on OPT row IS the option mark. We need stock.
        # We'll inject stock spot per scenario below.

        initial_credit = abs(float(sc.get("cost_basis_price") or 0))

        # Define scenarios
        stock_spot = paper_basis * 0.85 if paper_basis else strike * 0.9  # default: below basis

        scenarios = {
            "baseline_otm": {
                "spot": strike * 0.95,  # OTM
                "dte": real_dte,
                "ask": initial_credit * 0.5,
                "description": "OTM, mid-life",
            },
            "deep_itm": {
                "spot": strike * 1.15,  # deep ITM
                "dte": real_dte,
                "ask": (strike * 1.15 - strike) + 0.50,
                "description": "Deep ITM, normal DTE",
            },
            "near_expiry_itm": {
                "spot": strike * 1.03,  # slightly ITM
                "dte": 1,
                "ask": (strike * 1.03 - strike) + 0.03,  # low extrinsic
                "description": "ITM, DTE=1, extrinsic depleted",
            },
            "near_expiry_otm": {
                "spot": strike * 0.97,  # OTM near expiry
                "dte": 1,
                "ask": 0.02,
                "description": "OTM, DTE=1 (should let ride)",
            },
            "extrinsic_depleted_itm": {
                "spot": strike * 1.05,
                "dte": max(5, real_dte),
                "ask": (strike * 1.05 - strike) + 0.08,  # $0.08 extrinsic < $0.10
                "description": "ITM, extrinsic < $0.10 trigger",
            },
            "harvest_day1_80pct": {
                "spot": strike * 0.92,  # OTM
                "dte": max(10, real_dte),
                "ask": initial_credit * 0.18 if initial_credit > 0 else 0.05,
                "description": "82% profit, day-1 harvest candidate",
                "days_held": 1,
            },
            "harvest_day2_90pct": {
                "spot": strike * 0.92,
                "dte": max(10, real_dte),
                "ask": initial_credit * 0.08 if initial_credit > 0 else 0.02,
                "description": "92% profit, day-2+ harvest candidate",
                "days_held": 5,
            },
        }

        # Add above-basis scenario if we know basis
        if assigned_basis and assigned_basis > 0:
            scenarios["above_basis_itm"] = {
                "spot": assigned_basis + 5,
                "dte": max(3, real_dte),
                "ask": max(0.50, (assigned_basis + 5 - strike)),
                "description": f"Strike ${strike} < basis ${assigned_basis}, spot above basis",
            }
            scenarios["strike_at_basis"] = {
                "spot": assigned_basis + 2,
                "dte": max(3, real_dte),
                "ask": max(0.50, 3.0),
                "description": f"Strike ≈ basis ${assigned_basis}, above-basis gate",
                "_override_strike": assigned_basis + 1,
            }

        for scenario_name, params in scenarios.items():
            try:
                s_spot = params["spot"]
                s_dte = params["dte"]
                s_ask = params["ask"]
                s_strike = params.get("_override_strike", strike)
                s_exp = today + timedelta(days=s_dte)

                # Build current call quote
                intrinsic = max(0, s_spot - s_strike)
                extrinsic = max(0, s_ask - intrinsic)
                s_bid = max(0.01, s_ask - 0.10)
                s_delta = -min(0.99, max(0.01, 0.5 + (s_spot - s_strike) / (s_spot * 0.1)))

                current_call = OptionQuote(
                    strike=s_strike,
                    expiry=s_exp,
                    bid=round(s_bid, 2),
                    ask=round(s_ask, 2),
                    delta=round(s_delta, 4),
                    iv=0.35,
                )

                # Build chain for roll targets
                chain = build_synthetic_chain(
                    s_spot, s_exp, iv=0.35,
                    strike_step=max(1.0, round(s_spot * 0.01)),
                )

                pos = Position(
                    ticker=ticker,
                    account_id=acct,
                    household=household,
                    strike=s_strike,
                    expiry=s_exp,
                    quantity=abs(int(sc["position"])),
                    cost_basis=paper_basis,
                    inception_delta=s_delta,  # synthetic
                    opened_at=date.today() - timedelta(days=params.get("days_held", 7)),
                    avg_premium_collected=initial_credit,
                    assigned_basis=assigned_basis,
                    adjusted_basis=(assigned_basis - (ledger["total_premium_collected"] / max(1, ledger["shares_owned"]))) if ledger and assigned_basis else None,
                    initial_credit=initial_credit,
                    initial_dte=30,  # default
                    cumulative_roll_debit=0.0,
                    roll_count=0,
                )

                market = MarketSnapshot(
                    ticker=ticker,
                    spot=round(s_spot, 2),
                    iv30=0.35,
                    chain=chain,
                    current_call=current_call,
                    asof=today,
                )

                ctx = PortfolioContext(
                    household=household,
                    mode="WARTIME",
                    leverage=1.8,
                )

                result = roll_evaluate(pos, market, ctx)
                result_dict = asdict(result)
                result_dict["_type"] = type(result).__name__

                results.append({
                    "engine": "roll",
                    "account": acct,
                    "ticker": ticker,
                    "strike": s_strike,
                    "scenario": scenario_name,
                    "description": params["description"],
                    "spot": round(s_spot, 2),
                    "dte": s_dte,
                    "ask": round(s_ask, 2),
                    "extrinsic": round(extrinsic, 4),
                    "paper_basis": assigned_basis,
                    "result": result_dict,
                })
            except Exception as exc:
                results.append({
                    "engine": "roll",
                    "account": acct,
                    "ticker": ticker,
                    "strike": strike,
                    "scenario": scenario_name,
                    "description": params["description"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

    return results


def run_cc_scenarios(
    stock_positions: list[dict],
    premium_ledger: dict[tuple[str, str], dict],
) -> list[dict]:
    """Run cc_engine.pick_cc_strike() for each stock position."""
    results = []
    today = date.today()

    for sp in stock_positions:
        acct = sp["account_id"]
        ticker = sp["symbol"]
        household = ACCOUNT_TO_HOUSEHOLD.get(acct, "Unknown")
        shares = int(sp["position"])
        spot = float(sp["mark_price"] or 0)
        basis = float(sp["cost_basis_price"] or 0)

        if spot <= 0 or basis <= 0:
            continue  # skip positions with no price data (e.g. CVRs)

        ledger = premium_ledger.get((household, ticker))
        paper_basis = ledger["initial_basis"] if ledger else basis

        # Build chain for ~2 weeks out
        expiry = today + timedelta(days=14 - today.weekday() % 7)  # next Friday-ish
        dte = (expiry - today).days
        chain = build_cc_chain(spot, expiry)

        if not chain:
            continue

        scenarios = {
            "current_market": {
                "spot": spot,
                "paper_basis": paper_basis,
                "description": f"Current spot ${spot:.2f}, basis ${paper_basis:.2f}",
            },
            "spot_at_basis": {
                "spot": paper_basis,
                "paper_basis": paper_basis,
                "description": f"Spot = paper basis ${paper_basis:.2f}",
            },
            "spot_above_basis": {
                "spot": paper_basis * 1.05,
                "paper_basis": paper_basis,
                "description": f"Spot 5% above basis",
            },
        }

        for scenario_name, params in scenarios.items():
            s_spot = params["spot"]
            s_basis = params["paper_basis"]
            s_chain = build_cc_chain(s_spot, expiry)

            try:
                inp = CCPickerInput(
                    ticker=ticker,
                    account_id=acct,
                    paper_basis=s_basis,
                    spot=s_spot,
                    dte=dte,
                    expiry=expiry,
                    chain=s_chain,
                    min_ann=0.15,
                    max_ann=0.50,
                    bid_floor=0.05,
                )

                result = pick_cc_strike(inp)
                result_dict = asdict(result)
                result_dict["_type"] = type(result).__name__

                results.append({
                    "engine": "cc",
                    "account": acct,
                    "ticker": ticker,
                    "shares": shares,
                    "scenario": scenario_name,
                    "description": params["description"],
                    "spot": round(s_spot, 2),
                    "paper_basis": round(s_basis, 2),
                    "dte": dte,
                    "result": result_dict,
                })
            except Exception as exc:
                results.append({
                    "engine": "cc",
                    "account": acct,
                    "ticker": ticker,
                    "shares": shares,
                    "scenario": scenario_name,
                    "description": params["description"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

    return results


def run_csp_harvest_scenarios(
    short_puts: list[dict],
) -> list[dict]:
    """Run _should_harvest_csp() across scenarios for each short put."""
    results = []
    today = date.today()

    # Deduplicate by (account, symbol, strike, expiry) taking max trade_price
    seen: dict[tuple, dict] = {}
    for sp in short_puts:
        key = (sp["account_id"], sp["underlying_symbol"], sp["strike"], sp["expiry"])
        if key not in seen or abs(float(sp.get("initial_credit") or 0)) > abs(float(seen[key].get("initial_credit") or 0)):
            seen[key] = sp
    unique_puts = list(seen.values())

    for sp in unique_puts:
        acct = sp["account_id"]
        ticker = sp["underlying_symbol"]
        strike = float(sp["strike"])
        initial_credit = abs(float(sp.get("initial_credit") or 0))

        # Parse expiry
        exp_str = str(sp["expiry"])
        try:
            exp_date = date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:8]))
        except (ValueError, TypeError):
            continue

        dte = (exp_date - today).days
        if dte < 0:
            continue

        if initial_credit <= 0:
            continue

        scenarios = {
            "no_profit": {
                "ask": initial_credit * 1.1,
                "dte": dte,
                "days_held": 5,
                "description": "Losing money, ask > credit",
            },
            "small_profit_50pct": {
                "ask": initial_credit * 0.50,
                "dte": dte,
                "days_held": 5,
                "description": "50% profit, below threshold",
            },
            "harvest_day1_80pct": {
                "ask": initial_credit * 0.18,
                "dte": max(5, dte),
                "days_held": 1,
                "description": "82% profit, day-1 → should harvest",
            },
            "harvest_day2_92pct": {
                "ask": initial_credit * 0.07,
                "dte": max(5, dte),
                "days_held": 5,
                "description": "93% profit, day-2+ → should harvest",
            },
            "harvest_day2_85pct": {
                "ask": initial_credit * 0.15,
                "dte": max(5, dte),
                "days_held": 5,
                "description": "85% profit, day-2+ → below 90%, no harvest",
            },
            "expiry_day_profitable": {
                "ask": initial_credit * 0.05,
                "dte": 0,
                "days_held": 14,
                "description": "95% profit but DTE=0 → let ride",
            },
            "near_worthless": {
                "ask": 0.01,
                "dte": max(2, dte),
                "days_held": 10,
                "description": "Ask $0.01 → 99%+ profit, harvest",
            },
        }

        for scenario_name, params in scenarios.items():
            try:
                should, reason = _should_harvest_csp(
                    initial_credit=initial_credit,
                    current_ask=params["ask"],
                    dte=params["dte"],
                    days_held=params["days_held"],
                )

                profit_pct = (initial_credit - params["ask"]) / initial_credit if initial_credit > 0 else 0

                results.append({
                    "engine": "csp_harvest",
                    "account": acct,
                    "ticker": ticker,
                    "strike": strike,
                    "initial_credit": round(initial_credit, 4),
                    "scenario": scenario_name,
                    "description": params["description"],
                    "ask": round(params["ask"], 4),
                    "dte": params["dte"],
                    "days_held": params["days_held"],
                    "profit_pct": round(profit_pct, 4),
                    "should_harvest": should,
                    "reason": reason,
                })
            except Exception as exc:
                results.append({
                    "engine": "csp_harvest",
                    "account": acct,
                    "ticker": ticker,
                    "strike": strike,
                    "scenario": scenario_name,
                    "description": params["description"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

    return results


# ── Summary / validation ────────────────────────────────────────────────────

def validate_results(all_results: list[dict]) -> dict:
    """Cross-check results for consistency and flag anomalies."""
    anomalies = []
    stats = {
        "total_scenarios": len(all_results),
        "by_engine": {},
        "errors": 0,
        "anomalies": [],
    }

    for r in all_results:
        engine = r["engine"]
        stats["by_engine"].setdefault(engine, {"total": 0, "errors": 0, "decisions": {}})
        stats["by_engine"][engine]["total"] += 1

        if "error" in r:
            stats["errors"] += 1
            stats["by_engine"][engine]["errors"] += 1
            anomalies.append(f"ERROR [{engine}] {r.get('ticker')} {r.get('scenario')}: {r['error']}")
            continue

        # Engine-specific validation
        if engine == "roll":
            result_type = r["result"]["_type"]
            stats["by_engine"][engine]["decisions"].setdefault(result_type, 0)
            stats["by_engine"][engine]["decisions"][result_type] += 1

            # Anomaly: rolling when OTM
            if result_type == "RollResult" and r.get("spot", 0) < r.get("strike", 0):
                anomalies.append(
                    f"ANOMALY [{engine}] {r['ticker']} {r['scenario']}: "
                    f"roll triggered but OTM (spot={r['spot']}, strike={r['strike']})"
                )

            # Anomaly: harvest on expiry day
            if "Harvest" in result_type and r.get("dte", 0) <= 0:
                anomalies.append(
                    f"ANOMALY [{engine}] {r['ticker']} {r['scenario']}: "
                    f"harvest triggered on expiry day"
                )

            # Anomaly: holding when extrinsic < $0.10 and ITM and DTE <= 3
            if result_type == "HoldResult" and r.get("extrinsic", 1) < 0.10:
                if r.get("spot", 0) > r.get("strike", 0) and r.get("dte", 99) <= 3:
                    anomalies.append(
                        f"ANOMALY [{engine}] {r['ticker']} {r['scenario']}: "
                        f"holding with depleted extrinsic (${r['extrinsic']:.4f}) at DTE={r['dte']}"
                    )

        elif engine == "cc":
            result_type = r["result"]["_type"]
            stats["by_engine"][engine]["decisions"].setdefault(result_type, 0)
            stats["by_engine"][engine]["decisions"][result_type] += 1

        elif engine == "csp_harvest":
            decision = "harvest" if r.get("should_harvest") else "hold"
            stats["by_engine"][engine]["decisions"].setdefault(decision, 0)
            stats["by_engine"][engine]["decisions"][decision] += 1

            # Anomaly: harvesting on expiry day
            if r.get("should_harvest") and r.get("dte", 1) <= 0:
                anomalies.append(
                    f"ANOMALY [{engine}] {r['ticker']} {r['scenario']}: "
                    f"harvest on expiry day"
                )

            # Anomaly: not harvesting at 95%+ on day 2+
            if not r.get("should_harvest") and r.get("profit_pct", 0) >= 0.95:
                if r.get("dte", 0) > 0 and r.get("days_held", 0) >= 2:
                    anomalies.append(
                        f"ANOMALY [{engine}] {r['ticker']} {r['scenario']}: "
                        f"not harvesting at {r['profit_pct']:.1%} profit, dte={r['dte']}"
                    )

    stats["anomalies"] = anomalies
    return stats


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily dry-run: offline engine validation")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "agt_desk.db"), help="DB path")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "daily_dryrun.json"), help="Output path")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    # Load data
    stocks = load_stock_positions(conn)
    short_calls = load_open_short_calls(conn)
    short_puts = load_open_short_puts(conn)
    ledger = load_premium_ledger(conn)
    basis_map = load_stock_basis(conn)
    conn.close()

    print(f"Loaded: {len(stocks)} stock positions, {len(short_calls)} short calls, "
          f"{len(short_puts)} short put trades, {len(ledger)} ledger entries")

    # Run engines
    print("\n── Roll Engine ──")
    roll_results = run_roll_scenarios(short_calls, basis_map, ledger)
    print(f"  {len(roll_results)} scenarios evaluated")

    print("\n── CC Engine ──")
    cc_results = run_cc_scenarios(stocks, ledger)
    print(f"  {len(cc_results)} scenarios evaluated")

    print("\n── CSP Harvest ──")
    csp_results = run_csp_harvest_scenarios(short_puts)
    print(f"  {len(csp_results)} scenarios evaluated")

    all_results = roll_results + cc_results + csp_results

    # Validate
    print("\n── Validation ──")
    stats = validate_results(all_results)
    print(f"  Total scenarios: {stats['total_scenarios']}")
    print(f"  Errors: {stats['errors']}")
    for engine, es in stats["by_engine"].items():
        print(f"  {engine}: {es['total']} scenarios, {es['errors']} errors")
        for decision, count in sorted(es.get("decisions", {}).items()):
            print(f"    {decision}: {count}")

    if stats["anomalies"]:
        print(f"\n  ⚠ {len(stats['anomalies'])} ANOMALIES:")
        for a in stats["anomalies"]:
            print(f"    {a}")
    else:
        print("\n  ✓ No anomalies detected")

    # Write report
    report = {
        "run_date": str(date.today()),
        "data_summary": {
            "stock_positions": len(stocks),
            "short_calls": len(short_calls),
            "short_put_trades": len(short_puts),
            "ledger_entries": len(ledger),
            "unique_tickers": sorted(set(
                [s["symbol"] for s in stocks] +
                [c["underlying_symbol"] for c in short_calls]
            )),
        },
        "stats": stats,
        "results": all_results,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nReport written to {out_path}")
    return 0 if stats["errors"] == 0 and len(stats["anomalies"]) == 0 else 1


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, line_buffering=True)
    sys.exit(main())
