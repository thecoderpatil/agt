"""Position discovery -- shared data layer for /health, CC logic, CSP scan.

Extracted from telegram_bot.py (MR 2.5) so that agt_equities/ modules
(scan orchestrator, etc.) can import it without circular dependency.

Public API:
    discover_positions(ib_conn, margin_stats, household_filter, include_staged, *, db_path)
        -> dict with keys: "households", "all_book_nlv", "error" (on failure)

Callers are responsible for pre-fetching ib_conn (ensure_ib_connected) and
margin_stats (_query_margin_stats). This keeps the function pure-ish:
it owns the IB position/order queries but not the connection lifecycle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import closing
from pathlib import Path

from agt_equities.config import ACCOUNT_LABELS, ACCOUNT_TO_HOUSEHOLD, EXCLUDED_TICKERS, HOUSEHOLD_MAP, MARGIN_ACCOUNTS
from agt_equities.db import get_db_connection
from agt_equities.ib_chains import get_spots_batch

logger = logging.getLogger(__name__)

READ_FROM_MASTER_LOG: bool = True


_SECTOR_MAP_FALLBACK: dict[str, str] = {
    "ADBE": "Software - Application",
    "MSFT": "Software - Infrastructure",
    "CRM":  "Software - Application",
    "PYPL": "Software - Infrastructure",
    "UBER": "Software - Application",
    "QCOM": "Semiconductors",
    "OXY":  "Oil & Gas E&P",
    "XOM":  "Oil & Gas Integrated",
    "CVX":  "Oil & Gas Integrated",
    "JPM":  "Banks - Diversified",
    "AXP":  "Credit Services",
    "WMT":  "Discount Stores",
    "COST": "Discount Stores",
    "MCD":  "Restaurants",
    "TGT":  "Discount Stores",
    "UNH":  "Healthcare Plans",
    "JNJ":  "Drug Manufacturers - General",
}


def _get_industry_groups_batch(tickers: list[str], *, db_path=None) -> dict[str, str]:
    result = {t: "Unknown" for t in tickers}
    if not tickers:
        return result
    try:
        placeholders = ",".join("?" for _ in tickers)
        with closing(get_db_connection(db_path)) as conn:
            rows = conn.execute(
                f"SELECT ticker, gics_industry_group FROM ticker_universe "
                f"WHERE ticker IN ({placeholders})",
                [t.upper() for t in tickers],
            ).fetchall()
            for row in rows:
                if row["gics_industry_group"]:
                    result[row["ticker"]] = str(row["gics_industry_group"])
    except Exception as exc:
        logger.warning("_get_industry_groups_batch failed: %s", exc)
    return result


async def discover_positions(
    ib_conn,
    margin_stats: dict,
    household_filter: str | None = None,
    include_staged: bool = True,
    *,
    db_path: str | Path | None = None,
) -> dict:
    """Core position data layer.

    Args:
        ib_conn: Connected ib_async.IB client (caller must ensure connected).
        margin_stats: Result of _query_margin_stats() -- pre-fetched by caller.
        household_filter: If set, restrict to this household name.
        include_staged: If False, omit unapproved staged orders from encumbrance
            (used by /health so pending orders don't hide available contracts).
        db_path: Override for DB path (test isolation). None = production default.

    Returns:
        dict with keys "households" (per-household records) and "all_book_nlv".
        On IB failure returns {"households": {}, "all_book_nlv": 0.0, "error": str}.
    """
    try:
        positions = await ib_conn.reqPositionsAsync()
        mstats = margin_stats
    except Exception as exc:
        logger.exception("discover_positions: IB position query failed")
        return {"households": {}, "all_book_nlv": 0.0, "error": str(exc)}



    # ── Sprint 2 Fix 7: Query IBKR working SELL CALL orders ──

    working_sell_calls: dict[str, int] = {}  # "hh|ticker" -> contracts

    working_per_account: dict[str, int] = {}  # "acct|ticker" -> remaining_contracts

    try:

        open_orders = await ib_conn.reqAllOpenOrdersAsync()

        for trade_obj in open_orders:

            o = trade_obj.order if hasattr(trade_obj, "order") else None

            c = trade_obj.contract if hasattr(trade_obj, "contract") else None

            if not o or not c:

                continue

            if getattr(o, "action", "") != "SELL":

                continue

            if getattr(c, "secType", "") != "OPT":

                continue

            if getattr(c, "right", "") != "C":

                continue

            acct = getattr(o, "account", "")

            hh = ACCOUNT_TO_HOUSEHOLD.get(acct)

            if not hh:

                continue

            root = c.symbol.upper()

            wk = f"{hh}|{root}"

            # Status filter first, then remaining — never resurrect filled orders

            status_obj = getattr(trade_obj, "orderStatus", None)

            order_status = getattr(status_obj, "status", "")

            if order_status not in ("Submitted", "PreSubmitted", "PendingSubmit"):

                continue  # Not a working order — skip

            remaining = abs(int(getattr(status_obj, "remaining", 0) or 0))

            if remaining <= 0:

                continue  # Fully filled — already counted in positions

            working_sell_calls[wk] = working_sell_calls.get(wk, 0) + remaining

            ak = f"{acct}|{root}"

            working_per_account[ak] = working_per_account.get(ak, 0) + remaining

    except Exception as exc:

        logger.warning("discover_positions: working orders query failed: %s", exc)



    # ── Sprint 2 Fix 7: Query staged/processing SELL CALL orders from pending_orders ──

    staged_sell_calls: dict[str, int] = {}  # "hh|ticker" -> contracts

    staged_per_account: dict[str, int] = {}  # "acct|ticker" -> contracts

    if include_staged:

        try:

            with closing(get_db_connection(db_path)) as conn:

                staged_rows = conn.execute(

                    """

                    SELECT payload FROM pending_orders

                    WHERE status IN ('staged', 'processing')

                    """

                ).fetchall()



            for row in staged_rows:

                try:

                    raw = row["payload"] if isinstance(row, dict) else row[0]

                    p = json.loads(raw) if isinstance(raw, str) else raw



                    # Only count SELL CALL options

                    if (p.get("action") == "SELL"

                            and p.get("sec_type") == "OPT"

                            and p.get("right") == "C"):

                        p_ticker = (p.get("ticker") or "").upper()

                        p_qty = int(p.get("quantity") or 0)

                        p_hh = p.get("household") or ACCOUNT_TO_HOUSEHOLD.get(

                            p.get("account_id", ""), ""

                        )

                        if p_hh and p_ticker and p_qty > 0:

                            sk = f"{p_hh}|{p_ticker}"

                            staged_sell_calls[sk] = staged_sell_calls.get(sk, 0) + p_qty

                        ak = f"{p.get('account_id', '')}|{p_ticker}"

                        staged_per_account[ak] = staged_per_account.get(ak, 0) + p_qty

                except (json.JSONDecodeError, TypeError, ValueError):

                    continue

        except Exception as exc:

            logger.warning("discover_positions: staged orders query failed: %s", exc)



    # ── Sprint B Unit 2: DEX encumbrance from bucket3_dynamic_exit_log ──

    dex_sell_calls: dict[str, int] = {}  # "hh|ticker" -> contracts

    try:

        with closing(get_db_connection(db_path)) as conn:

            dex_rows = conn.execute(

                "SELECT ticker, household, contracts, shares, action_type "

                "FROM bucket3_dynamic_exit_log "

                "WHERE final_status IN ('STAGED', 'ATTESTED', 'TRANSMITTING')"

            ).fetchall()

        for dr in dex_rows:

            tk = dr["ticker"]

            hh = dr["household"]

            # CC: contracts encumber shares; STK_SELL: shares encumber directly

            if dr["action_type"] == "CC":

                enc = dr["contracts"] or 0

            else:

                enc = (dr["shares"] or 0) // 100  # STK_SELL: convert shares to contract-equivalent

            if hh and tk and enc > 0:

                dk = f"{hh}|{tk}"

                dex_sell_calls[dk] = dex_sell_calls.get(dk, 0) + enc

    except Exception as exc:

        logger.warning("discover_positions: DEX encumbrance query failed: %s", exc)



    # ── Group raw positions by household + root ticker ──

    raw: dict[str, dict[str, dict]] = {}  # household -> ticker -> accumulator

    for pos in positions:

        if pos.position == 0:

            continue

        acct = pos.account

        if acct not in ACCOUNT_TO_HOUSEHOLD:

            continue

        hh = ACCOUNT_TO_HOUSEHOLD[acct]

        if household_filter and hh != household_filter:

            continue

        c = pos.contract

        root = c.symbol.upper()

        if root in EXCLUDED_TICKERS:

            continue



        key = f"{hh}|{root}"

        if key not in raw:

            raw[key] = {

                "household": hh,

                "ticker": root,

                "sector": "Unknown",  # populated below via batch lookup

                "stk_shares": 0,

                "avg_cost_ibkr": 0.0,

                "short_calls": [],

                "short_puts": [],

                "accounts_with_shares": {},

            }

        rec = raw[key]



        if c.secType == "STK" and pos.position > 0:

            qty = int(pos.position)

            rec["stk_shares"] += qty

            rec["avg_cost_ibkr"] = float(pos.avgCost)

            acct_entry = rec["accounts_with_shares"].setdefault(acct, {

                "account_id": acct,

                "label": ACCOUNT_LABELS.get(acct, acct),

                "shares": 0,

                "avg_cost_ibkr": 0.0,

            })

            acct_entry["shares"] += qty

            # Per-account cost basis from IBKR (WHEEL-5 fix: no blending)

            acct_entry["avg_cost_ibkr"] = float(pos.avgCost)



        elif c.secType == "OPT" and pos.position < 0:

            right = getattr(c, "right", "")

            if right not in ("C", "P"):

                continue

            con_id = getattr(c, "conId", 0)

            raw_avg = pos.avgCost

            avg_cost_val = float(raw_avg) if raw_avg and not math.isnan(raw_avg) else 0.0

            short_entry = {

                "strike": float(c.strike),

                "expiry": str(c.lastTradeDateOrContractMonth),

                "contracts": abs(int(pos.position)),

                "right": right,

                "account": acct,

                "unrealized_pnl": None,         # populated by reqPnLSingle batch below

                "avg_cost": avg_cost_val,        # per-contract cost from reqPositions

                "con_id": con_id,

            }

            if right == "C":

                rec["short_calls"].append(short_entry)

            else:

                rec.setdefault("short_puts", []).append(short_entry)



    # ── Batch reqPnLSingle for all short option positions ──

    # reqAccountUpdates only supports ONE account at a time, so portfolio()

    # only has data for one account. reqPnLSingle works across all accounts.

    _opt_subs: list[tuple[str, int, object]] = []  # (acct, conId, pnlObj)

    try:

        for rec_val in raw.values():

            for entry in rec_val.get("short_calls", []) + rec_val.get("short_puts", []):

                _acct = entry.get("account", "")

                _cid = entry.get("con_id", 0)

                if _acct and _cid:

                    try:

                        pnl_obj = ib_conn.reqPnLSingle(_acct, "", _cid)

                        _opt_subs.append((_acct, _cid, pnl_obj))

                    except Exception:

                        continue



        if _opt_subs:

            for _ in range(4):

                ib_conn.sleep(0.5)



        _opt_pnl: dict[tuple[str, int], float] = {}

        for _acct, _cid, pnl_obj in _opt_subs:

            try:

                val = getattr(pnl_obj, "unrealizedPnL", None)

                if val is not None and not math.isnan(val):

                    _opt_pnl[(_acct, _cid)] = float(val)

            except Exception:

                continue



        # Apply PnL values to short option entries

        for rec_val in raw.values():

            for entry in rec_val.get("short_calls", []) + rec_val.get("short_puts", []):

                key = (entry.get("account", ""), entry.get("con_id", 0))

                pnl_val = _opt_pnl.get(key)

                if pnl_val is not None:

                    entry["unrealized_pnl"] = pnl_val



        # Cancel all subscriptions

        for _acct, _cid, pnl_obj in _opt_subs:

            try:

                ib_conn.cancelPnLSingle(pnl_obj)

            except Exception:

                pass

    except Exception as opt_pnl_exc:

        logger.warning("Option PnL batch fetch failed: %s", opt_pnl_exc)



    # ── Batch sector lookup from ticker_universe (with fallback) ──

    all_root_tickers = list({v["ticker"] for v in raw.values()})

    ig_map = _get_industry_groups_batch(all_root_tickers, db_path=db_path)

    for key, rec in raw.items():

        root = rec["ticker"]

        ig = ig_map.get(root, "Unknown")

        rec["sector"] = ig if ig != "Unknown" else _SECTOR_MAP_FALLBACK.get(root, "Unknown")



    # ── Fetch spot prices (IBKR batch, yfinance fallback for display) ──

    unique_tickers = list({v["ticker"] for v in raw.values()})

    spot_prices: dict[str, float] = {}

    if unique_tickers:

        try:

            spot_prices = await get_spots_batch(ib_conn, unique_tickers)

        except Exception as exc:

            logger.warning("discover_positions: IBKR batch spots failed: %s", exc)

        # MIGRATED 2026-04-07 Phase 3A.5c1 — replaced yfinance fallback

        # with IBKRPriceVolatilityProvider.get_spot() per Architect decision.

        # OLD: data = yf.download(" ".join(missed), period="1d", ...)

        missed = [t for t in unique_tickers if t not in spot_prices]

        if missed:

            try:

                from agt_equities.providers.ibkr_price_volatility import IBKRPriceVolatilityProvider

                _prov = IBKRPriceVolatilityProvider(ib_conn, market_data_mode="delayed")

                tkr = "<unknown>"

                for tkr in missed:

                    spot = _prov.get_spot(tkr)

                    if spot is not None:

                        spot_prices[tkr] = round(spot, 2)

            except Exception as exc:

                logger.warning(

                    "ibkr_price_volatility fallback failed for %s: %s",

                    tkr, exc,

                )



    # Use pre-fetched NLV from _query_margin_stats (no second IB call)

    _all_account_nlv = mstats.get("all_account_nlv", {})



    # Pre-fetch position data from Walker cycles (or legacy fallback)

    _ledger_cache: dict[tuple[str, str], dict] = {}

    if READ_FROM_MASTER_LOG:

        try:

            from agt_equities import trade_repo

            for c in trade_repo.get_active_cycles():

                if c.cycle_type != 'WHEEL':

                    continue

                lkey = (c.household_id, c.ticker)

                _ledger_cache[lkey] = {

                    "initial_basis": c.paper_basis or 0,

                    "total_premium_collected": c.premium_total,

                    "shares_owned": int(c.shares_held),

                    "adjusted_basis": round(c.adjusted_basis, 4) if c.adjusted_basis else None,

                    "_cycle": c,  # WHEEL-5: keep for paper_basis_for_account()

                }

        except Exception as ml_exc:

            logger.warning("Walker batch pre-fetch failed, falling back to legacy: %s", ml_exc)

            _ledger_cache = {}



    if not _ledger_cache:

        try:

            with closing(get_db_connection(db_path)) as conn:

                ledger_rows = conn.execute(

                    "SELECT household_id, ticker, initial_basis, "

                    "total_premium_collected, shares_owned "

                    "FROM premium_ledger"

                ).fetchall()

            for lr in ledger_rows:

                lkey = (lr["household_id"], lr["ticker"])

                shares = int(lr["shares_owned"] or 0)

                initial = float(lr["initial_basis"] or 0)

                prem = float(lr["total_premium_collected"] or 0)

                adj = (initial - prem / shares) if shares > 0 else None

                _ledger_cache[lkey] = {

                    "initial_basis": initial,

                    "total_premium_collected": prem,

                    "shares_owned": shares,

                    "adjusted_basis": round(adj, 4) if adj is not None else None,

                }

        except Exception as ledger_exc:

            logger.warning("Batch ledger pre-fetch failed: %s", ledger_exc)



    # ── Build final records with ledger join + mode classification ──

    household_buckets: dict[str, dict] = {}

    for key, rec in raw.items():

        hh = rec["household"]

        tkr = rec["ticker"]

        total_shares = rec["stk_shares"]

        if total_shares <= 0:

            continue



        # Premium ledger join (from batch cache)

        ledger = _ledger_cache.get((hh, tkr))

        if ledger and ledger.get("adjusted_basis") is not None:

            initial_basis = ledger["initial_basis"]

            total_prem = ledger["total_premium_collected"]

            adj_basis = ledger["adjusted_basis"]

        else:

            initial_basis = rec["avg_cost_ibkr"]

            total_prem = 0.0

            adj_basis = rec["avg_cost_ibkr"]



        # WHEEL-5: populate per-account paper_basis in accounts_with_shares

        _w5_cycle = ledger.get("_cycle") if ledger else None

        for _acct_id, _acct_info in rec["accounts_with_shares"].items():

            if _w5_cycle is not None:

                try:

                    _per_acct_basis = _w5_cycle.paper_basis_for_account(_acct_id)

                    if _per_acct_basis is not None:

                        _acct_info["paper_basis"] = round(_per_acct_basis, 4)

                        continue

                except Exception:

                    pass

            # Fallback: use IBKR per-account avgCost

            _acct_info.setdefault("paper_basis", _acct_info.get("avg_cost_ibkr", initial_basis))



        spot = spot_prices.get(tkr, 0.0)



        # Mode classification

        if adj_basis <= 0:

            mode = "FULLY_AMORTIZED"

        elif spot >= adj_basis:

            mode = "MODE_2"

        else:

            mode = "MODE_1"



        filled_contracts = sum(sc["contracts"] for sc in rec["short_calls"])

        pos_key = f"{hh}|{tkr}"

        working_contracts = working_sell_calls.get(pos_key, 0)

        staged_contracts = staged_sell_calls.get(pos_key, 0)

        dex_contracts = dex_sell_calls.get(pos_key, 0)  # Sprint B Unit 2

        covered_contracts = filled_contracts + working_contracts + staged_contracts + dex_contracts

        uncov_shares = max(0, total_shares - (covered_contracts * 100))



        position_rec = {

            "household": hh,

            "ticker": tkr,

            "sector": rec["sector"],

            "total_shares": total_shares,

            "avg_cost_ibkr": round(rec["avg_cost_ibkr"], 2),

            "initial_basis": round(initial_basis, 2),

            "total_premium_collected": round(total_prem, 2),

            "adjusted_basis": round(adj_basis, 2),

            "spot_price": spot,

            "market_value": round(total_shares * spot, 2),

            "mode": mode,

            "existing_short_calls": rec["short_calls"],

            "existing_short_puts": rec.get("short_puts", []),

            "covered_contracts": covered_contracts,

            "uncovered_shares": uncov_shares,

            "available_contracts": uncov_shares // 100,

            "accounts_with_shares": rec["accounts_with_shares"],

            "working_per_account": working_per_account,

            "staged_per_account": staged_per_account,

        }



        if hh not in household_buckets:

            hh_accounts = HOUSEHOLD_MAP.get(hh, [])

            hh_margin_nlv = 0.0

            hh_margin_el = 0.0

            for aid in hh_accounts:

                acct_data = mstats["accounts"].get(aid)

                if acct_data:

                    if aid in MARGIN_ACCOUNTS:

                        hh_margin_nlv += acct_data["nlv"]

                    hh_margin_el += acct_data["el"]

            # Full household NLV from pre-computed summary

            hh_full_nlv = sum(

                _all_account_nlv.get(aid, 0.0) for aid in hh_accounts

            ) or hh_margin_nlv



            household_buckets[hh] = {

                "household_nlv": round(hh_full_nlv, 2),

                "household_margin_nlv": round(hh_margin_nlv, 2),

                "household_margin_el": round(hh_margin_el, 2),

                "household_el_pct": round(

                    (hh_margin_el / hh_margin_nlv * 100) if hh_margin_nlv > 0 else 0.0, 2

                ),

                "positions": [],

            }



        household_buckets[hh]["positions"].append(position_rec)



    # Sort positions within each household by weight descending

    all_book_nlv = mstats["all_book_nlv"]

    for hh_data in household_buckets.values():

        hh_data["positions"].sort(

            key=lambda p: p["market_value"],

            reverse=True,

        )



    return {

        "households": household_buckets,

        "all_book_nlv": all_book_nlv,

        "error": mstats.get("error"),

    }





# ---------------------------------------------------------------------------

# Phase 3: /health command

# ---------------------------------------------------------------------------





