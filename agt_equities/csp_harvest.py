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
       profit_pct >= 0.80  AND  dte >= 1   → stage BTC  (next-day)
       profit_pct >= 0.90  AND  dte <= 1   → stage BTC  (last-day)
    where profit_pct = (initial_credit - current_ask) / initial_credit
    and   initial_credit = abs(pos.avgCost) / 100.0
  - Excludes tickers in EXCLUDED_TICKERS (mirror of the telegram_bot.py
    blocklist — defined locally to preserve the one-way dependency
    rule: agt_equities.csp_harvest MUST NOT import telegram_bot).
  - Pure threshold check `_should_harvest_csp` has no IB/DB
    dependencies and is fully unit-testable in isolation.
  - Scanner `scan_csp_harvest_candidates` is an async function that
    receives the IB connection as a parameter (no module-level
    singleton) and optionally an injected staging callback so tests
    can supply a list-append sink instead of the real
    append_pending_tickets DB writer.

This module does NOT import from telegram_bot.py. The /csp_harvest
handler in telegram_bot.py is a thin shim that imports THIS module
and passes a staging_callback that wraps append_pending_tickets.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date as _date
from datetime import datetime as _datetime
from typing import Any, Callable

from agt_equities.config import ACCOUNT_TO_HOUSEHOLD

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
# NOT import telegram_bot. Keep in sync manually — there are only 5 entries
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
) -> tuple[bool, str]:
    """Pure profit-threshold predicate for CSP harvest.

    Returns (should_harvest, reason) where reason is a short machine
    string suitable for digest inclusion.

    Semantics:
      - initial_credit must be > 0 (we need a real opening credit)
      - current_ask must be a real finite non-negative number
      - dte must be an integer (negative values treated as expiry day)
      - profit_pct = (initial_credit - current_ask) / initial_credit
      - If dte <= 0 → NEVER harvest (let it ride on expiry day, E7 fix)
      - If dte >= 1 and profit_pct >= 0.80 → harvest ("next_day_80")
      - If dte == 1 and profit_pct >= 0.90 → harvest ("last_day_90")
      - Else → do not harvest
      - Note: dte == 1 qualifies under BOTH rules; next-day wins
        because it's checked first (0.80 is the easier bar).
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

    # E7 fix (2026-04-15): expiry day — let it ride to expiration.
    # Paying the ask spread to close a position expiring worthless is
    # negative EV. Canonical: "on expiry day itself, let it ride."
    if dte <= 0:
        return False, f"expiry_day_let_ride:profit_pct={profit_pct:.3f}"

    # Next-day rule (easier bar — check first)
    # NOTE (E6/E8): This SHOULD fire only on day-1 positions (held 1
    # trading day), but _should_harvest_csp has no days_held parameter.
    # Until days-held tracking is added (E8 infrastructure gap), this
    # fires on DTE which over-harvests older positions at 80%. Acceptable
    # for now — over-harvesting is conservative (locks in profit early).
    if dte >= 1 and profit_pct >= CSP_HARVEST_THRESHOLD_NEXT_DAY:
        return True, f"next_day_80:profit_pct={profit_pct:.3f}"

    # Last-day rule (tighter bar, 1DTE — no longer fires on 0DTE per E7 fix)
    if dte == 1 and profit_pct >= CSP_HARVEST_THRESHOLD_LAST_DAY:
        return True, f"last_day_90:profit_pct={profit_pct:.3f}"

    return False, f"below_threshold:profit_pct={profit_pct:.3f},dte={dte}"


# ---------------------------------------------------------------------------
# Async scanner
# ---------------------------------------------------------------------------

async def scan_csp_harvest_candidates(
    ib_conn: Any,
    staging_callback: Callable[[list[dict]], Any] | None = None,
) -> dict:
    """Scan open short puts across all accounts and stage BTC tickets.

    Parameters
    ----------
    ib_conn :
        An active ib_async IB connection (duck-typed: needs
        reqPositionsAsync, qualifyContractsAsync, reqMktData,
        cancelMktData, reqMarketDataType).
    staging_callback :
        Optional callable invoked with a list of ticket dicts. If
        None, tickets are NOT persisted — only collected in the
        returned `staged` list. Real callers (telegram_bot.cmd_csp_harvest)
        inject a wrapper around append_pending_tickets.

    Returns
    -------
    dict with keys:
        staged  : list[dict]  — BTC tickets that were staged
        skipped : list[dict]  — positions that did NOT meet threshold
                                (includes reason)
        errors  : list[dict]  — positions that raised during probe
        alerts  : list[str]   — human-readable status lines

    Mirrors telegram_bot.py _scan_and_stage_defensive_rolls STATE_2
    HARVEST flow (lines 8768-8869) with these adjustments:
      - Filters for short PUTS (right == "P") not calls
      - Uses _should_harvest_csp thresholds instead of pnl_pct >= 0.85
      - mode="CSP_HARVEST" tags the ticket for execution-gate routing
      - No spot fetch, no Greeks, no ledger — pure profit-take
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

    today = _date.today()

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
        should, reason = _should_harvest_csp(initial_credit, float(ask), dte)

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
            "v2_rationale": (
                f"{reason} initial_credit={initial_credit:.2f} "
                f"ask={float(ask):.2f} dte={dte}"
            ),
        }

        if staging_callback is not None:
            try:
                maybe = staging_callback([ticket])
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as sexc:
                logger.warning("csp_harvest: staging_callback(%s) failed: %s", ticker, sexc)
                result["errors"].append({
                    "ticker": ticker, "account_id": acct_id, "error": str(sexc),
                })
                continue

        result["staged"].append(ticket)
        result["alerts"].append(
            f"[CSP HARVEST] {ticker} -{qty}p ${strike:.0f} "
            f"{exp_fmt} ({dte}d) | {reason}"
        )

    return result
