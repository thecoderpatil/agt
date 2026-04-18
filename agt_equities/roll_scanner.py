from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import closing
from datetime import date, datetime as _datetime
from pathlib import Path
from typing import Callable, Any

import ib_async
import pandas as pd

from agt_equities import roll_engine
from agt_equities.roll_engine import (
    AlertResult,
    AssignResult,
    ConstraintMatrix,
    HarvestResult,
    HoldResult,
    LiquidateResult,
    MarketSnapshot,
    OptionQuote,
    PortfolioContext,
    Position,
    RollResult,
)
from agt_equities.config import ACCOUNT_TO_HOUSEHOLD, EXCLUDED_TICKERS
from agt_equities.db import get_db_connection as _get_db_connection

logger = logging.getLogger("agt_bridge")

_CORP_CALENDAR_CACHE_DIR = Path("agt_desk_cache/corporate_intel")


def _lookup_inception_from_flex(

    conid: int,

    account_id: str,

    expiry_iso: str,

) -> tuple["date", float, int] | None:

    """Look up authoritative inception metadata for an open short option contract.



    Preferred source: master_log_open_positions (flex-populated EOD snapshot)

    carries open_date_time + open_price per (account_id, conid). Fallback:

    most-recent Sell-to-Open row in master_log_trades for the same conid.



    Returns (opened_at, initial_credit_per_contract, initial_dte) on hit,

    None on miss. Caller falls back to V_r=0 graceful degradation.



    Safe: read-only queries under _get_db_connection(); any exception → None.

    """

    if not conid or not account_id:

        return None

    try:

        with closing(_get_db_connection()) as conn:

            row = conn.execute(

                """

                SELECT open_date_time, open_price

                FROM master_log_open_positions

                WHERE conid = ? AND account_id = ? AND open_date_time IS NOT NULL

                ORDER BY report_date DESC

                LIMIT 1

                """,

                (int(conid), str(account_id)),

            ).fetchone()

            if row is None or row["open_date_time"] is None or row["open_price"] is None:

                row = conn.execute(

                    """

                    SELECT date_time AS open_date_time, trade_price AS open_price

                    FROM master_log_trades

                    WHERE conid = ? AND account_id = ?

                      AND buy_sell = 'SELL' AND open_close = 'O'

                    ORDER BY date_time DESC

                    LIMIT 1

                    """,

                    (int(conid), str(account_id)),

                ).fetchone()

            if row is None or row["open_date_time"] is None or row["open_price"] is None:

                return None



            raw_dt = str(row["open_date_time"])

            try:

                if ";" in raw_dt:

                    datepart = raw_dt.split(";", 1)[0]

                else:

                    datepart = raw_dt[:10].replace("-", "")

                opened_at = date(int(datepart[0:4]), int(datepart[4:6]), int(datepart[6:8]))

            except (ValueError, IndexError):

                return None



            try:

                initial_credit = abs(float(row["open_price"])) * 100.0

            except (TypeError, ValueError):

                return None



            try:

                if "-" in expiry_iso:

                    exp_d = date.fromisoformat(expiry_iso)

                else:

                    exp_d = date(int(expiry_iso[0:4]), int(expiry_iso[4:6]), int(expiry_iso[6:8]))

            except (ValueError, IndexError):

                return None



            initial_dte = max(0, (exp_d - opened_at).days)

            return (opened_at, initial_credit, initial_dte)

    except Exception as exc:

        logger.debug(

            "_lookup_inception_from_flex conid=%s account=%s failed: %s",

            conid, account_id, exc,

        )

        return None

def _build_position_for_evaluator(

    pos,  # ib_async.Position

    ledger_snapshot: dict | None,

    exp_date: date,

) -> "Position":

    """Construct a roll_engine.Position from IBKR position + ledger snapshot.



    Velocity-ratio-sensitive fields (opened_at, initial_credit, initial_dte)

    are best-effort from avgCost. When unavailable, evaluator degrades

    gracefully: velocity-ratio harvest gate returns 0.0 and is bypassed;

    extrinsic-value + gamma-cutoff paths still route correctly.

    """

    ticker = pos.contract.symbol.upper()

    acct_id = pos.account

    household = ACCOUNT_TO_HOUSEHOLD.get(acct_id, "")

    strike = float(pos.contract.strike)

    qty = abs(int(pos.position))



    assigned_basis = None

    adjusted_basis = None

    if ledger_snapshot:

        raw_initial = ledger_snapshot.get("initial_basis")

        raw_adjusted = ledger_snapshot.get("adjusted_basis")

        if raw_initial is not None:

            assigned_basis = float(raw_initial)

        if raw_adjusted is not None:

            adjusted_basis = float(raw_adjusted)



    # WHEEL-4b: prefer authoritative inception from flex-populated tables

    # (master_log_open_positions with master_log_trades fallback). Enables

    # real velocity-ratio routing on positions flex has captured (all open

    # shorts older than the current trading session).

    inception = None

    try:

        conid_val = int(getattr(pos.contract, "conId", 0) or 0)

        expiry_iso = str(getattr(pos.contract, "lastTradeDateOrContractMonth", "") or "")

        if conid_val and expiry_iso:

            inception = _lookup_inception_from_flex(conid_val, acct_id, expiry_iso)

    except Exception:

        inception = None



    today = date.today()

    if inception is not None:

        opened_at, initial_credit, initial_dte = inception

    else:

        # Graceful degradation: flex has not captured this position yet

        # (same-session open) — fall back to avgCost for premium anchor and

        # V_r=0 (opened_at=today → t_pct=0) for routing.

        initial_credit = abs(float(getattr(pos, "avgCost", 0.0) or 0.0)) / 100.0

        opened_at = today

        initial_dte = max(0, (exp_date - today).days)



    return Position(

        ticker=ticker,

        account_id=acct_id,

        household=household,

        strike=strike,

        expiry=exp_date,

        quantity=qty,

        cost_basis=assigned_basis,

        inception_delta=None,  # vestigial in WHEEL-3 evaluator

        opened_at=opened_at,

        avg_premium_collected=initial_credit,

        assigned_basis=assigned_basis,

        adjusted_basis=adjusted_basis,

        initial_credit=initial_credit,

        initial_dte=initial_dte,

    )

def _read_corp_calendar_cache(

    ticker: str,

) -> tuple[date | None, float | None, date | None]:

    """Return (next_ex_div_date, ex_div_amount, next_earnings_date).



    Reads the JSON blob written by YFinanceCorporateIntelligenceProvider.

    Never raises; returns all-None on any failure.

    """

    try:

        path = _CORP_CALENDAR_CACHE_DIR / f"{ticker.upper()}_calendar.json"

        if not path.exists():

            return (None, None, None)

        data = json.loads(path.read_text())

        ex_div = (

            date.fromisoformat(data["ex_dividend_date"])

            if data.get("ex_dividend_date") else None

        )

        amt_raw = data.get("dividend_amount")

        amt = float(amt_raw) if amt_raw not in (None, 0, 0.0) else None

        earnings = (

            date.fromisoformat(data["next_earnings"])

            if data.get("next_earnings") else None

        )

        return (ex_div, amt, earnings)

    except Exception as exc:

        logger.warning(

            "WHEEL-4: corporate calendar cache read failed for %s: %s",

            ticker, exc,

        )

        return (None, None, None)

async def _build_market_snapshot_for_evaluator(

    ib_conn,

    ticker: str,

    spot: float,

    current_call_quote: "OptionQuote",

    exp_date: date,

    strike: float,

    adjusted_basis: float | None,

    *,

    ibkr_get_expirations: "Callable",

    ibkr_get_chain: "Callable",

) -> "MarketSnapshot":

    """Pre-fetch the option chain window needed for cascade evaluation.



    Fetches expirations in [today+7, today+45] DTE band, strikes from

    current_strike down one step to current_strike + 2 steps up (covers

    all 4 cascade tiers: +0, +1, +1, +2). Returns a tuple of OptionQuote.



    On fetch failure: returns MarketSnapshot with empty chain — evaluator

    emits AlertResult(CRITICAL) from cascade exhaustion.

    """

    today = date.today()

    chain_quotes: list[OptionQuote] = []



    try:

        raw_expirations = await ibkr_get_expirations(ticker)

    except Exception as exc:

        logger.warning("WHEEL-4: expirations fetch failed for %s: %s", ticker, exc)

        raw_expirations = []



    target_expirations: list[tuple[str, date, int]] = []

    for exp_str in raw_expirations or []:

        try:

            d = date.fromisoformat(exp_str)

        except (TypeError, ValueError):

            continue

        dte = (d - today).days

        if 7 <= dte <= 45:

            target_expirations.append((exp_str, d, dte))



    # Strike window: from current_strike (tier4) to ~3 strikes above

    # (more than enough for tier1/2/3 +1/+1/+2 steps).

    strike_floor = strike

    strike_ceiling_candidates = [strike * 1.25, spot * 1.25]

    if adjusted_basis is not None:

        strike_ceiling_candidates.append(adjusted_basis * 1.30)

    strike_ceiling = max(strike_ceiling_candidates)



    for exp_str, exp_d, _dte in target_expirations:

        try:

            chain_data = await ibkr_get_chain(

                ticker, exp_str, right="C",

                min_strike=strike_floor, max_strike=strike_ceiling,

            )

        except Exception as exc:

            logger.warning("WHEEL-4: chain fetch failed for %s %s: %s", ticker, exp_str, exc)

            continue



        for row in chain_data or []:

            try:

                q_strike = float(row.get("strike"))

                q_bid = row.get("bid")

                q_ask = row.get("ask")

                q_delta = row.get("delta")

            except (TypeError, ValueError):

                continue

            if q_bid is None or pd.isna(q_bid):

                continue

            if q_ask is None or pd.isna(q_ask):

                continue

            if q_delta is None or pd.isna(q_delta):

                q_delta_val = 0.0

            else:

                q_delta_val = float(q_delta)

            chain_quotes.append(OptionQuote(

                strike=q_strike,

                expiry=exp_d,

                bid=float(q_bid),

                ask=float(q_ask),

                delta=q_delta_val,

                iv=float(row.get("impliedVol") or 0.0),

            ))



    # R4 + earnings-week gate: read from nightly corporate calendar cache.

    # Cache-miss returns all-None â€” both gates fail open. Never fetches.

    ex_div_date, ex_div_amount, next_earnings_date = _read_corp_calendar_cache(ticker)



    return MarketSnapshot(

        ticker=ticker,

        spot=float(spot),

        iv30=float(current_call_quote.iv or 0.0),

        chain=tuple(chain_quotes),

        current_call=current_call_quote,

        asof=today,

        next_ex_div_date=ex_div_date,

        next_div_amount=ex_div_amount,

        next_earnings_date=next_earnings_date,

    )

def _build_portfolio_context_for_evaluator(
    household: str,
    *,
    get_desk_mode: "Callable",
) -> "PortfolioContext":

    """Build PortfolioContext from current desk mode + leverage snapshot.



    Leverage is informational for the evaluator (defense regime runs

    regardless of household mode). Fallback to 0.0 on any read failure

    rather than block the scan.

    """

    try:

        current_mode = get_desk_mode()

    except Exception as exc:

        logger.warning("WHEEL-4: mode lookup failed (%s) — assuming PEACETIME", exc)

        current_mode = "PEACETIME"



    # Leverage: pulled from el_snapshots if available; otherwise 0.0.

    leverage = 0.0

    try:

        with closing(_get_db_connection()) as conn:

            row = conn.execute(

                """SELECT leverage FROM el_snapshots

                   WHERE household_id = ?

                   ORDER BY snapshot_date DESC, snapshot_time DESC LIMIT 1""",

                (household,),

            ).fetchone()

            if row and row[0] is not None:

                leverage = float(row[0])

    except Exception as exc:

        logger.debug("WHEEL-4: leverage lookup failed for %s: %s", household, exc)



    return PortfolioContext(

        household=household or "UNKNOWN",

        mode=current_mode,

        leverage=leverage,

    )

def _dispatch_eval_result(

    result,  # EvalResult

    pos,  # ib_async.Position

    current_call_contract,  # qualified Contract for the current short

    qty: int,

    strike: float,

    exp_fmt: str,

    acct_id: str,

    ticker: str,

    *,

    account_labels: dict,

) -> tuple[str | None, list[dict]]:

    """Translate an EvalResult into (alert_line, tickets_to_stage).



    Tickets returned here are NOT yet appended — caller is responsible for

    `append_pending_tickets` so it can batch + error-handle per ticker.

    """

    # HOLD — no-op, no alert (reduces noise on a normal scan)

    if isinstance(result, HoldResult):

        return None, []



    # HARVEST — BTC at ask

    if isinstance(result, HarvestResult):

        ticket = {

            "timestamp": _datetime.now().isoformat(),

            "account_id": acct_id,

            "account_label": account_labels.get(acct_id, acct_id),

            "ticker": ticker,

            "sec_type": "OPT",

            "action": "BUY",

            "order_type": "LMT",

            "right": "C",

            "strike": strike,

            "expiry": exp_fmt,

            "quantity": qty,

            "limit_price": round(result.btc_limit, 2),

            "status": "staged",

            "transmit": True,

            "strategy": "WHEEL-4 Harvest BTC",

            "mode": "HARVEST",

            "origin": "roll_engine",

            "v2_state": "HARVEST",

            "v2_rationale": result.reason,

        }

        return f"[HARVEST] {ticker} {result.reason}", [ticket]



    # ROLL — 2-leg BAG

    if isinstance(result, RollResult):

        # Caller qualifies the new sell contract (needs ib_conn) — we return

        # the ticket skeleton and let the scan finalize it. See scan body.

        ticket = {

            "timestamp": _datetime.now().isoformat(),

            "account_id": acct_id,

            "account_label": account_labels.get(acct_id, acct_id),

            "ticker": ticker,

            "sec_type": "BAG",

            "action": "BUY",

            "quantity": qty,

            "order_type": "LMT",

            # net_credit is received → limit price is NEGATIVE of credit,

            # i.e., we "buy" the spread for a debit of -credit. A positive

            # credit_per_contract means we pay -credit (i.e., receive).

            "limit_price": round(-result.net_credit_per_contract, 2),

            "status": "staged",

            "transmit": True,

            "strategy": f"WHEEL-4 Cascade Roll T{result.cascade_tier}",

            "mode": "DEFEND",

            "origin": "roll_engine",

            "v2_state": "DEFEND",

            "v2_rationale": result.reason,

            "strike": result.new_strike,

            "expiry": result.new_expiry.strftime("%Y%m%d") if result.new_expiry else None,

            "short_expiry": exp_fmt,

            "right": "C",

            # combo_legs filled in by caller (needs ib_conn.qualifyContractsAsync)

            "_roll_result": result,  # marker for caller

        }

        return f"[DEFEND T{result.cascade_tier}] {ticker} → {result.new_strike} @ {result.new_expiry} credit={result.net_credit_per_contract:.2f}", [ticket]



    # ASSIGN — alert only

    if isinstance(result, AssignResult):

        return f"[ASSIGN] {ticker} {result.reason}", []



    # LIQUIDATE — alert only, requires_human_approval. No auto-stage.

    if isinstance(result, LiquidateResult):

        return (

            f"[LIQUIDATE REQUESTED] {ticker} × {result.contracts}c / {result.shares}sh  "

            f"BTC@{result.btc_limit:.2f} STC@spot≈{result.stc_market_ref:.2f}  "

            f"net={result.net_proceeds_per_share:.2f}/sh  — MANUAL APPROVAL REQUIRED  "

            f"({result.reason})"

        ), []



    # ALERT — severity → prefix

    if isinstance(result, AlertResult):

        prefix = "[CRITICAL]" if result.severity == "CRITICAL" else "[WARN]"

        return f"{prefix} {ticker} {result.reason}", []



    logger.error("WHEEL-4: unknown EvalResult variant: %r", result)

    return f"[CRITICAL] {ticker} unknown evaluator result variant", []





# ---------------------------------------------------------------------------

# WHEEL-4 replacement for _scan_and_stage_defensive_rolls

# ---------------------------------------------------------------------------

async def scan_and_stage_defensive_rolls(
    ib_conn,
    *,
    ctx: "RunContext",
    priority_cb=None,
    ibkr_get_spot: "Callable",
    load_premium_ledger: "Callable",
    get_desk_mode: "Callable",
    ibkr_get_expirations: "Callable",
    ibkr_get_chain: "Callable",
    account_labels: dict,
    is_halted: bool = False,
) -> list[str]:

    """

    V2 master router for open short calls — WHEEL-4 cutover.



    Builds Position/MarketSnapshot/PortfolioContext from live IBKR data and

    dispatches each open short call through `roll_engine.evaluate()`.

    The inline State 1/2/3 logic that lived here through 2026-04-15 has

    been replaced by a single evaluator call.



    ADR-005: WARTIME-allowed via the v2_router site in _pre_trade_gates.

    Mode is logged here for operator visibility but NOT gated at staging —

    execution gate enforces it.

    """

    if is_halted:

        logger.info("V2 router: skipped (desk halted)")

        return ["[V2 ROUTER] Skipped — desk halted via /halt."]



    alerts: list[str] = []

    try:

        positions = await ib_conn.reqPositionsAsync()

        short_calls = [

            p for p in positions

            if p.position < 0

            and getattr(p.contract, "secType", "") == "OPT"

            and getattr(p.contract, "right", "") == "C"

            and p.contract.symbol.upper() not in EXCLUDED_TICKERS

        ]



        if not short_calls:

            return []



        # Build one PortfolioContext per household; cache across positions.

        ctx_cache: dict[str, PortfolioContext] = {}

        ledger_cache: dict[tuple[str, str], dict | None] = {}  # (acct, ticker) per ADR-006



        ib_conn.reqMarketDataType(4)  # delayed-frozen, set once

        first_household = ACCOUNT_TO_HOUSEHOLD.get(short_calls[0].account, "") if short_calls else ""

        header_ctx = ctx_cache.setdefault(

            first_household, _build_portfolio_context_for_evaluator(first_household, get_desk_mode=get_desk_mode),

        )

        alerts.append(f"━━ V2 Router [mode={header_ctx.mode}] ━━")

        logger.info("V2 router (WHEEL-4): scan starting in mode=%s", header_ctx.mode)



        for pos in short_calls:

            ticker = pos.contract.symbol.upper()

            acct_id = pos.account

            household = ACCOUNT_TO_HOUSEHOLD.get(acct_id, "")

            strike = float(pos.contract.strike)

            qty = abs(int(pos.position))



            # --- Resolve expiry -------------------------------------------------

            exp_fmt = str(pos.contract.lastTradeDateOrContractMonth)

            try:

                exp_date = date(int(exp_fmt[:4]), int(exp_fmt[4:6]), int(exp_fmt[6:8]))

            except (ValueError, TypeError):

                continue

            if exp_date < date.today():

                continue



            # --- Live spot -------------------------------------------------------

            try:

                spot = float(await ibkr_get_spot(ticker))

            except Exception as exc:

                logger.warning("WHEEL-4: spot fetch failed for %s: %s", ticker, exc)

                continue



            # --- Greeks + top-of-book for the currently-open short --------------

            qual_contracts = await ib_conn.qualifyContractsAsync(pos.contract)

            if not qual_contracts:

                continue

            current_contract = qual_contracts[0]

            ticker_data = ib_conn.reqMktData(current_contract, "106", False, False)

            await asyncio.sleep(2)

            ask = getattr(ticker_data, "ask", getattr(ticker_data, "delayedAsk", None))

            bid = getattr(ticker_data, "bid", getattr(ticker_data, "delayedBid", None))

            delta_val = None

            iv_val = None

            if getattr(ticker_data, "modelGreeks", None):

                delta_val = ticker_data.modelGreeks.delta

                iv_val = ticker_data.modelGreeks.impliedVol

            elif getattr(ticker_data, "bidGreeks", None):

                delta_val = ticker_data.bidGreeks.delta

                iv_val = getattr(ticker_data.bidGreeks, "impliedVol", None)

            ib_conn.cancelMktData(current_contract)



            if (ask is None or bid is None or delta_val is None

                    or math.isnan(ask) or math.isnan(bid) or math.isnan(delta_val)):

                continue



            current_call_quote = OptionQuote(

                strike=strike,

                expiry=exp_date,

                bid=float(bid),

                ask=float(ask),

                delta=float(delta_val),

                iv=float(iv_val) if iv_val is not None and not math.isnan(iv_val) else 0.0,

            )



            # --- Ledger snapshot (per-account, ADR-006) -------------------------

            ledger_key = (acct_id, ticker)

            if ledger_key not in ledger_cache:

                ledger_cache[ledger_key] = (

                    await asyncio.to_thread(

                        load_premium_ledger, household, ticker, acct_id,

                    )

                    if household else None

                )

            ledger_snapshot = ledger_cache.get(ledger_key)



            adj_basis = None

            if ledger_snapshot and ledger_snapshot.get("adjusted_basis") is not None:

                adj_basis = float(ledger_snapshot["adjusted_basis"])



            # --- Build evaluator inputs ----------------------------------------

            eval_pos = _build_position_for_evaluator(pos, ledger_snapshot, exp_date)

            market = await _build_market_snapshot_for_evaluator(

                ib_conn, ticker, spot, current_call_quote, exp_date, strike, adj_basis,

                ibkr_get_expirations=ibkr_get_expirations,

                ibkr_get_chain=ibkr_get_chain,

            )

            port_ctx = ctx_cache.setdefault(

                household, _build_portfolio_context_for_evaluator(household, get_desk_mode=get_desk_mode),

            )



            # --- Evaluate -------------------------------------------------------

            try:

                result = roll_engine.evaluate(eval_pos, market, port_ctx)

            except Exception as eval_exc:

                # Belt-and-suspenders: evaluate() already wraps try/except,

                # but we're not taking chances with live capital.

                logger.exception("WHEEL-4: evaluator raised unexpectedly for %s: %s", ticker, eval_exc)

                alerts.append(f"[CRITICAL] {ticker} evaluator raised: {eval_exc!r}")

                continue



            alert_line, tickets = _dispatch_eval_result(

                result, pos, current_contract, qty, strike, exp_fmt, acct_id, ticker,

                account_labels=account_labels,

            )



            # WHEEL-7: out-of-band priority pager for CRITICAL + LIQUIDATE.

            if priority_cb is not None:

                try:

                    if isinstance(result, LiquidateResult):

                        await priority_cb("LIQUIDATE", {

                            "ticker": ticker,

                            "account_id": acct_id,

                            "contracts": result.contracts,

                            "shares": result.shares,

                            "btc_limit": result.btc_limit,

                            "stc_market_ref": result.stc_market_ref,

                            "net_proceeds_per_share": result.net_proceeds_per_share,

                            "strike": strike,

                            "expiry": exp_fmt,

                            "reason": result.reason,

                        })

                    elif isinstance(result, AlertResult) and result.severity == "CRITICAL":

                        await priority_cb("CRITICAL", {

                            "ticker": ticker,

                            "account_id": acct_id,

                            "reason": result.reason,

                        })

                except Exception as _pcb_exc:

                    logger.warning("WHEEL-7: priority_cb raised: %s", _pcb_exc)



            # Finalize ROLL ticket combo_legs (needs ib_conn)

            finalized_tickets: list[dict] = []

            for tk in tickets:

                if tk.get("sec_type") == "BAG" and "_roll_result" in tk:

                    roll_res = tk.pop("_roll_result")

                    sell_contract = ib_async.Option(

                        symbol=ticker,

                        lastTradeDateOrContractMonth=(

                            roll_res.new_expiry.strftime("%Y%m%d")

                            if roll_res.new_expiry else exp_fmt

                        ),

                        strike=roll_res.new_strike,

                        right="C",

                        exchange="SMART",

                    )

                    try:

                        qual_sell = await ib_conn.qualifyContractsAsync(sell_contract)

                    except Exception as q_exc:

                        logger.warning("WHEEL-4: sell-leg qualify failed for %s: %s", ticker, q_exc)

                        alerts.append(f"[WARN] {ticker} roll qualify failed: {q_exc!r}")

                        continue

                    if not qual_sell:

                        alerts.append(f"[WARN] {ticker} roll sell-leg did not qualify")

                        continue

                    tk["combo_legs"] = [

                        {

                            "conId": current_contract.conId or pos.contract.conId,

                            "ratio": 1,

                            "action": "BUY",

                            "exchange": "SMART",

                            "strike": strike,

                            "expiry": exp_fmt,

                        },

                        {

                            "conId": qual_sell[0].conId,

                            "ratio": 1,

                            "action": "SELL",

                            "exchange": "SMART",

                            "strike": roll_res.new_strike,

                            "expiry": tk.get("expiry"),

                        },

                    ]

                finalized_tickets.append(tk)



            if finalized_tickets:

                try:

                    await asyncio.to_thread(
                        ctx.order_sink.stage,
                        finalized_tickets,
                        engine="roll_engine",
                        run_id=ctx.run_id,
                    )
                except Exception as stage_exc:
                    logger.warning(
                        "WHEEL-4: ctx.order_sink.stage failed for %s: %s",
                        ticker, stage_exc,
                    )
                    alerts.append(f"[WARN] {ticker} ticket staging failed: {stage_exc!r}")



            if alert_line:

                alerts.append(alert_line)



    except Exception as exc:

        logger.exception("WHEEL-4 scan failed: %s", exc)

        alerts.append(f"[CRITICAL] V2 Router scan crashed: {exc!r}")



    return alerts
