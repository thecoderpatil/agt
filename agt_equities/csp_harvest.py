"""
agt_equities/csp_harvest.py

CSP profit-take harvester. Scans open short puts across all accounts
and stages buy-to-close (BTC) tickets when profit-capture thresholds
are hit.

Architectural contract (M2, 2026-04-11):
  - Mirrors the V2 router STATE_2 HARVEST pattern in telegram_bot.py
    (_scan_and_stage_defensive_rolls, lines ~8768-8861) but for short
    PUTS instead of short calls, and with CSP-specific thresholds.
  - Dual thresholds:
       profit_pct >= 0.80  AND  dte >= 1   â†’ stage BTC  (next-day)
       profit_pct >= 0.90  AND  dte <= 1   â†’ stage BTC  (last-day)
    where profit_pct = (initial_credit - current_ask) / initial_credit
    and   initial_credit = abs(pos.avgCost) / 100.0
  - Excludes tickers in EXCLUDED_TICKERS (mirror of the telegram_bot.py
    blocklist â€” defined locally to preserve the one-way dependency
    rule: agt_equities.csp_harvest MUST NOT import telegram_bot).
  - Pure threshold check `_should_harvest_csp` has no IB/DB
    dependencies and is fully unit-testable in isolation.
  - Scanner `scan_csp_harvest_candidates` is an async function that
    receives the IB connection and a RunContext (ctx). ctx.order_sink
    replaces the former staging_callback; callers construct the
    appropriate sink (SQLiteOrderSink for live, CollectorOrderSink
    for shadow mode).

This module does NOT import from telegram_bot.py. The /csp_harvest
handler in telegram_bot.py constructs a RunContext with
SQLiteOrderSink(staging_fn=append_pending_tickets) and passes ctx=ctx.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date as _date
from datetime import datetime as _datetime
from typing import Any

from agt_equities.config import ACCOUNT_TO_HOUSEHOLD
from agt_equities.dates import et_today
from agt_equities.runtime import RunContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Profit-capture thresholds for CSP harvest (M2 dispatch).
# These are intentionally tighter than the V2 router's 0.85 CC harvest
# threshold because puts decay faster near expiry and the risk/reward
# of holding a stale short put for the last few pennies is asymmetric.
CSP_HARVEST_THRESHOLD_NEXT_DAY = 0.80   # dte >= 1
CSP_HARVEST_THRESHOLD_LAST_DAY = 0.90   # dte <= 1 (0DTE / 1DTE)


# ---------------------------------------------------------------------------
# Ticker blocklist (mirrored from telegram_bot.py:1463)
# ---------------------------------------------------------------------------
# Defined here to preserve the one-way dependency rule: csp_harvest must
# NOT import telegram_bot. Keep in sync manually â€” there are only 5 entries
# and the list changes rarely.
EXCLUDED_TICKERS: frozenset[str] = frozenset(
    {"IBKR", "TRAW.CVR", "SPX", "SLS", "GTLB"}
)


# ---------------------------------------------------------------------------
# Pure threshold check
# ---------------------------------------------------------------------------

def _should_harvest_csp(
    initial_credit: float,
    current_ask: float,
    dte: int,
    days_held: int = -1,
) -> tuple[bool, str]:
    """Pure profit-threshold predicate for CSP harvest.

    Returns (should_harvest, reason) where reason is a short machine
    string suitable for digest inclusion.

    CANONICAL SPEC (Yash, 2026-04-15):
      "Harvest if 80% gains in 1 trading day, and 90% gains up till
       the day before last trading day."

    Axes:
      - days_held: calendar days since position was opened (0 = same day).
        This is the PRIMARY decision axis per canonical spec.
      - dte: days to expiration. Used ONLY for E7 expiry-day let-ride gate.

    Decision rules (mirrors roll_engine.py E2/E5):
      - dte <= 0 â†’ NEVER harvest (E7: let it ride on expiry day)
      - days_held <= 1 AND profit_pct >= 0.80 â†’ harvest (day-1 80%)
      - days_held >= 2 AND profit_pct >= 0.90 â†’ harvest (standard 90%)
      - If days_held == -1 (unknown), falls back to days_held=2 (the
        more conservative 90% path). This is safe: over-harvesting at 90%
        is conservative vs. the alternative of firing 80% on old positions.

    Legacy compat: days_held defaults to -1 (unknown) so existing callers
    that haven't been updated continue to work at the 90% threshold.
    """
    # Reject invalid / missing inputs
    if initial_credit is None or current_ask is None:
        return False, "missing_input"
    try:
        ic = float(initial_credit)
        ca = float(current_ask)
    except (TypeError, ValueError):
        return False, "uncoercible_input"
    if math.isnan(ic) or math.isnan(ca) or math.isinf(ic) or math.isinf(ca):
        return False, "nan_or_inf_input"
    if ic <= 0.0:
        return False, "zero_credit"
    if ca < 0.0:
        return False, "negative_ask"

    profit_pct = (ic - ca) / ic

    # E7 fix (2026-04-15): expiry day â€” let it ride to expiration.
    # Paying the ask spread to close a position expiring worthless is
    # negative EV. Canonical: "on expiry day itself, let it ride."
    if dte <= 0:
        return False, f"expiry_day_let_ride:profit_pct={profit_pct:.3f}"

    # Resolve days_held: -1 means unknown â†’ default to 2 (conservative 90% path)
    effective_days_held = days_held if days_held >= 0 else 2

    # Day-1 rule (80%): position held <= 1 calendar day AND >= 80% profit.
    # Mirrors roll_engine.py _evaluate_defense E2 + _evaluate_offense day-1.
    if effective_days_held <= 1 and profit_pct >= CSP_HARVEST_THRESHOLD_NEXT_DAY:
        return True, f"day1_80:profit_pct={profit_pct:.3f},days_held={effective_days_held}"

    # Standard rule (90%): position held >= 2 days AND >= 90% profit.
    # Mirrors roll_engine.py _evaluate_defense E5 + _evaluate_offense harvest.
    if effective_days_held >= 2 and profit_pct >= CSP_HARVEST_THRESHOLD_LAST_DAY:
        return True, f"standard_90:profit_pct={profit_pct:.3f},days_held={effective_days_held}"

    return False, f"below_threshold:profit_pct={profit_pct:.3f},dte={dte},days_held={effective_days_held}"


# ---------------------------------------------------------------------------
# days_held lookup helper
# TODO(#10): _lookup_days_held SQL column bugs -- pending_orders query uses bare
# account_id (should be json_extract(payload,'$.account_id')); master_log_trades
# uses t.ticker/t.asset_class/t.action (should be t.symbol/t.asset_category/
# t.buy_sell). Helper always returns -1 until fixed. Tracked as backlog ticket #10.
# ---------------------------------------------------------------------------

def _lookup_days_held(
    account_id: str,
    ticker: str,
    strike: float,
    expiry_str: str,
    today: _date,
) -> int:
    """Best-effort lookup for when a short put was opened.

    Queries pending_orders for the original CSP SELL order. Falls back to
    master_log_trades. Returns -1 if unknown (triggers conservative 90%
    path in _should_harvest_csp).

    Pure read-only. No writes, no side effects beyond logging.
    """
    try:
        from agt_equities.db import get_ro_connection
        from contextlib import closing

        with closing(get_ro_connection()) as conn:
            # Try pending_orders first (most recent CSP sells).
            # account_id is inside the JSON payload -- must use json_extract.
            row = conn.execute(
                """
                SELECT MIN(created_at) AS opened_at
                FROM pending_orders
                WHERE json_extract(payload, '$.account_id') = ?
                  AND json_extract(payload, '$.ticker') = ?
                  AND json_extract(payload, '$.right') = 'P'
                  AND json_extract(payload, '$.action') = 'SELL'
                  AND json_extract(payload, '$.strike') = ?
                  AND status IN ('filled', 'executed', 'processing')
                """,
                (account_id, ticker, strike),
            ).fetchone()

            if row and row[0]:
                try:
                    opened = _datetime.fromisoformat(str(row[0])).date()
                    return (today - opened).days
                except (ValueError, TypeError):
                    pass

            # Fallback: master_log_trades (Flex/Walker source of truth).
            # Correct columns: symbol (not ticker), asset_category (not
            # asset_class), buy_sell (not action).
            row2 = conn.execute(
                """
                SELECT MIN(t.trade_date) AS opened_at
                FROM master_log_trades t
                WHERE t.account_id = ?
                  AND t.symbol = ?
                  AND t.asset_category = 'OPT'
                  AND t.put_call = 'P'
                  AND t.strike = ?
                  AND t.buy_sell = 'SELL'
                """,
                (account_id, ticker, strike),
            ).fetchone()

            if row2 and row2[0]:
                try:
                    opened = _date.fromisoformat(str(row2[0]))
                    return (today - opened).days
                except (ValueError, TypeError):
                    pass

    except Exception as exc:
        logger.debug("_lookup_days_held failed for %s/%s: %s", account_id, ticker, exc)

    return -1


# ---------------------------------------------------------------------------
# Async scanner
# ---------------------------------------------------------------------------

async def scan_csp_harvest_candidates(
    ib_conn: Any,
    *,
    ctx: RunContext,
) -> dict:
    """Scan open short puts across all accounts and stage BTC tickets.

    Parameters
    ----------
    ib_conn :
        An active ib_async IB connection (duck-typed: needs
        reqPositionsAsync, qualifyContractsAsync, reqMktData,
        cancelMktData, reqMarketDataType).
    ctx :
        RunContext carrying the order_sink. ctx.order_sink.stage() is
        called for each qualifying position. Use SQLiteOrderSink for
        live execution, CollectorOrderSink for shadow-mode dry runs.

    Returns
    -------
    dict with keys:
        staged  : list[dict]  â€” BTC tickets that were staged
        skipped : list[dict]  â€” positions that did NOT meet threshold
                                (includes reason)
        errors  : list[dict]  â€” positions that raised during probe
        alerts  : list[str]   â€” human-readable status lines

    Mirrors telegram_bot.py _scan_and_stage_defensive_rolls STATE_2
    HARVEST flow (lines 8768-8869) with these adjustments:
      - Filters for short PUTS (right == "P") not calls
      - Uses _should_harvest_csp thresholds instead of pnl_pct >= 0.85
      - mode="CSP_HARVEST" tags the ticket for execution-gate routing
      - No spot fetch, no Greeks, no ledger â€” pure profit-take
    """
    result: dict[str, list] = {
        "staged": [],
        "skipped": [],
        "errors": [],
        "alerts": [],
    }

    try:
        positions = await ib_conn.reqPositionsAsync()
    except Exception as exc:
        logger.exception("csp_harvest: reqPositionsAsync failed")
        result["errors"].append({"scope": "reqPositionsAsync", "error": str(exc)})
        result["alerts"].append(f"[CSP HARVEST] Failed to fetch positions: {exc}")
        return result

    short_puts = [
        p for p in positions
        if getattr(p, "position", 0) < 0
        and getattr(getattr(p, "contract", None), "secType", "") == "OPT"
        and getattr(getattr(p, "contract", None), "right", "") == "P"
        and getattr(p.contract, "symbol", "").upper() not in EXCLUDED_TICKERS
    ]

    if not short_puts:
        result["alerts"].append("[CSP HARVEST] No open short puts to scan.")
        return result

    try:
        ib_conn.reqMarketDataType(4)  # delayed-frozen fallback
    except Exception as mdt_exc:
        logger.warning("csp_harvest: reqMarketDataType(4) failed: %s", mdt_exc)

    today = et_today()

    for pos in short_puts:
        ticker = pos.contract.symbol.upper()
        strike = float(pos.contract.strike)
        qty = abs(int(pos.position))
        acct_id = getattr(pos, "account", "")
        household = ACCOUNT_TO_HOUSEHOLD.get(acct_id, "")

        # DTE
        exp_fmt = str(pos.contract.lastTradeDateOrContractMonth)
        try:
            exp_date = _date(int(exp_fmt[:4]), int(exp_fmt[4:6]), int(exp_fmt[6:8]))
            dte = (exp_date - today).days
        except (ValueError, TypeError):
            result["skipped"].append({
                "ticker": ticker, "account_id": acct_id,
                "reason": f"bad_expiry:{exp_fmt}",
            })
            continue
        if dte < 0:
            result["skipped"].append({
                "ticker": ticker, "account_id": acct_id,
                "reason": f"expired:dte={dte}",
            })
            continue

        days_held = _lookup_days_held(acct_id, ticker, strike, exp_fmt, today)
        # Qualify + market data probe
        try:
            qual_contracts = await ib_conn.qualifyContractsAsync(pos.contract)
        except Exception as qexc:
            logger.warning("csp_harvest: qualifyContractsAsync(%s) failed: %s", ticker, qexc)
            result["errors"].append({"ticker": ticker, "account_id": acct_id, "error": str(qexc)})
            continue
        if not qual_contracts:
            result["skipped"].append({
                "ticker": ticker, "account_id": acct_id,
                "reason": "qualify_empty",
            })
            continue

        try:
            ticker_data = ib_conn.reqMktData(qual_contracts[0], "", False, False)
            await asyncio.sleep(2)  # Linear wait acceptable for EOD watchdog (<10 positions)
            ask = getattr(ticker_data, "ask", getattr(ticker_data, "delayedAsk", None))
            try:
                ib_conn.cancelMktData(qual_contracts[0])
            except Exception:
                pass
        except Exception as mdexc:
            logger.warning("csp_harvest: reqMktData(%s) failed: %s", ticker, mdexc)
            result["errors"].append({"ticker": ticker, "account_id": acct_id, "error": str(mdexc)})
            continue

        if ask is None or (isinstance(ask, float) and math.isnan(ask)):
            result["skipped"].append({
                "ticker": ticker, "account_id": acct_id,
                "reason": "ask_unavailable",
            })
            continue

        initial_credit = abs(float(getattr(pos, "avgCost", 0.0) or 0.0)) / 100.0
        should, reason = _should_harvest_csp(initial_credit, float(ask), dte, days_held=days_held)

        if not should:
            result["skipped"].append({
                "ticker": ticker, "account_id": acct_id,
                "strike": strike, "dte": dte,
                "initial_credit": initial_credit, "ask": float(ask),
                "reason": reason,
            })
            continue

        ticket = {
            "timestamp": _datetime.now().isoformat(),
            "account_id": acct_id,
            "household": household,
            "ticker": ticker,
            "sec_type": "OPT",
            "action": "BUY",
            "order_type": "LMT",
            "right": "P",
            "strike": strike,
            "expiry": exp_fmt,
            "quantity": qty,
            "limit_price": round(float(ask), 2),
            "status": "staged",
            "transmit": True,
            "strategy": "CSP Harvest BTC",
            "mode": "CSP_HARVEST",
            "origin": "csp_harvest",
            "days_held": days_held,
            "v2_rationale": (
                f"{reason} initial_credit={initial_credit:.2f} "
                f"ask={float(ask):.2f} dte={dte}"
            ),
        }

        ctx.order_sink.stage(
            [ticket],
            engine="csp_harvest",
            run_id=ctx.run_id,
            meta={
                "account_id":   ticket["account_id"],
                "household":    ticket["household"],
                "ticker":       ticket["ticker"],
                "strike":       ticket["strike"],
                "expiry":       ticket["expiry"],
                "quantity":     ticket["quantity"],
                "limit_price":  ticket["limit_price"],
                "days_held":    ticket["days_held"],
                "v2_rationale": ticket["v2_rationale"],
            },
        )
        result["staged"].append(ticket)
        result["alerts"].append(
            f"[CSP HARVEST] {ticker} -{qty}p ${strike:.0f} "
            f"{exp_fmt} ({dte}d) | {reason}"
        )

    return result
