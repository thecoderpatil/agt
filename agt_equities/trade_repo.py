"""
agt_equities.trade_repo — Read interface for Walker cycle data.

Sole responsibility: materialize TradeEvent streams from master_log_*
tables, hand them to the Walker, and cache/return cycle results.

No other module reads from master_log_* directly (except flex_sync.py
which writes). See REFACTOR_SPEC_v3.md section 7.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .config import HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD
from .db import get_db_connection
from .walker import Cycle, TradeEvent, walk_cycles, UnknownEventError

logger = logging.getLogger(__name__)

# Tickers excluded from Walker (index options, not wheel candidates)
EXCLUDED_TICKERS = frozenset({'SPX', 'VIX', 'NDX', 'RUT', 'XSP'})

# Cache: keyed by as_of_report_date
_cycle_cache: dict[str, list[Cycle]] = {}


def _get_db(db_path: "str | Path | None" = None) -> sqlite3.Connection:
    """Open a read connection to agt_desk.db.

    Default path (db_path=None): delegates to the shared agt_equities.db
    module's get_db_connection(), which sets row_factory=Row and
    busy_timeout=15000ms. This is the production path.

    Explicit path (db_path=<path>): opens a direct sqlite3.connect() to
    the override with identical pragmas. Used exclusively by test
    fixtures and ad-hoc scripts that need to target a non-production DB.

    The FU-A cleanup sprint (2026-04) replaced the v22 module-attribute
    override mechanism (trade_repo.DB_PATH = ...) with explicit kwarg
    threading. All 4 public read functions accept db_path and pass it
    through to this helper. See HANDOFF_ARCHITECT_v22 FU-A and DT ruling
    Q1 for the decision rationale.
    """
    if db_path is None:
        return get_db_connection()
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000;")
    return conn


def invalidate_cache() -> None:
    """Clear the cycle cache. Called after a successful sync."""
    _cycle_cache.clear()


def _parse_float(val) -> float:
    if val is None or val == '':
        return 0.0
    return float(val)


def _parse_int_or_none(val):
    if val is None or val == '':
        return None
    return int(val)


# ---------------------------------------------------------------------------
# TradeEvent materialization from master_log_trades
# ---------------------------------------------------------------------------

def _load_trade_events(
    conn: sqlite3.Connection,
    household: Optional[str] = None,
    ticker: Optional[str] = None,
) -> list[TradeEvent]:
    """Load TradeEvents from master_log_trades, optionally filtered."""
    where_parts = []
    params = []

    if household:
        accts = HOUSEHOLD_MAP.get(household, [])
        if not accts:
            return []
        placeholders = ','.join('?' * len(accts))
        where_parts.append(f"account_id IN ({placeholders})")
        params.extend(accts)

    if ticker:
        where_parts.append("(underlying_symbol = ? OR (underlying_symbol IS NULL AND symbol = ?))")
        params.extend([ticker, ticker])

    where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    rows = conn.execute(
        f"SELECT * FROM master_log_trades {where_clause} ORDER BY date_time",
        params,
    ).fetchall()

    events = []
    for r in rows:
        raw = dict(r)
        acct = r['account_id']
        hh = ACCOUNT_TO_HOUSEHOLD.get(acct, 'Unknown_Household')
        # Determine ticker: prefer underlying_symbol, fall back to symbol for STK
        tk = r['underlying_symbol'] or r['symbol']
        if tk in EXCLUDED_TICKERS:
            continue

        ev = TradeEvent(
            source='FLEX_TRADE',
            account_id=acct,
            household_id=hh,
            ticker=tk,
            trade_date=r['trade_date'] or '',
            date_time=r['date_time'] or '',
            ib_order_id=_parse_int_or_none(r['ib_order_id']),
            transaction_id=r['transaction_id'],
            asset_category=r['asset_category'] or '',
            right=r['put_call'] or None,
            strike=_parse_float(r['strike']) or None,
            expiry=r['expiry'] or None,
            buy_sell=r['buy_sell'] or '',
            open_close=r['open_close'] or None,
            quantity=abs(_parse_float(r['quantity'])),
            trade_price=_parse_float(r['trade_price']),
            net_cash=_parse_float(r['net_cash']),
            fifo_pnl_realized=_parse_float(r['fifo_pnl_realized']),
            transaction_type=r['transaction_type'] or '',
            notes=r['notes'] or '',
            currency=r['currency'] or 'USD',
            raw=raw,
        )
        events.append(ev)

    return events


def _load_carryin_events(
    conn: sqlite3.Connection,
    household: Optional[str] = None,
    ticker: Optional[str] = None,
) -> list[TradeEvent]:
    """Load carry-in events from inception_carryin table."""
    where_parts = []
    params = []

    if household:
        where_parts.append("household_id = ?")
        params.append(household)

    if ticker:
        where_parts.append("symbol = ?")
        params.append(ticker)

    where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    rows = conn.execute(
        f"SELECT * FROM inception_carryin {where_clause}", params
    ).fetchall()

    events = []
    for r in rows:
        if r['account_id'] not in ACCOUNT_TO_HOUSEHOLD: continue  # paper mode: skip carryins for accounts not in active map
        raw = dict(r)
        ev = TradeEvent(
            source='INCEPTION_CARRYIN',
            account_id=r['account_id'],
            household_id=r['household_id'],
            ticker=r['symbol'],
            trade_date=r['as_of_date'] or '',
            date_time=(r['as_of_date'] or '') + ';235959',
            ib_order_id=None,
            transaction_id=f"CARRYIN_{r['account_id']}_{r['symbol']}_{r['conid']}",
            asset_category=r['asset_class'] or '',
            right=r['right'] or None,
            strike=_parse_float(r['strike']) or None,
            expiry=r['expiry'] or None,
            buy_sell='SELL' if (r['asset_class'] == 'OPT' and _parse_float(r['quantity']) < 0) else 'BUY',
            open_close='O',
            quantity=abs(_parse_float(r['quantity'])),
            trade_price=_parse_float(r['basis_price']),
            net_cash=0.0,
            fifo_pnl_realized=0.0,
            transaction_type='InceptionCarryin',
            notes='',
            currency='USD',
            raw=raw,
        )
        events.append(ev)

    return events


def _load_transfer_events(
    conn: sqlite3.Connection,
    household: Optional[str] = None,
    ticker: Optional[str] = None,
) -> list[TradeEvent]:
    """Load INTERNAL transfer events from master_log_transfers.

    Converts IBKR transfer rows into TradeEvent objects for the Walker.
    Only INTERNAL transfers are loaded (ACATS handled via inception_carryin).
    Cash-only transfers (qty=0) are skipped.
    """
    where_parts = ["type = 'INTERNAL'"]
    params: list = []

    if household:
        accts = HOUSEHOLD_MAP.get(household, [])
        if not accts:
            return []
        placeholders = ','.join('?' * len(accts))
        where_parts.append(f"account_id IN ({placeholders})")
        params.extend(accts)

    if ticker:
        where_parts.append(
            "(symbol LIKE ? OR symbol LIKE ?)"
        )
        params.extend([ticker + '%', ticker + ' %'])

    where_clause = "WHERE " + " AND ".join(where_parts)

    try:
        rows = conn.execute(
            f"SELECT * FROM master_log_transfers {where_clause} ORDER BY date",
            params,
        ).fetchall()
    except Exception as exc:
        logger.warning("_load_transfer_events failed: %s", exc)
        return []

    events = []
    for r in rows:
        raw = dict(r)
        qty = abs(_parse_float(r['quantity']))
        if qty == 0:
            continue  # skip cash-only transfers

        acct = r['account_id']
        hh = ACCOUNT_TO_HOUSEHOLD.get(acct, 'Unknown_Household')
        symbol = r['symbol'] or ''
        # Extract underlying ticker from option symbol (e.g. "ADBE  251010P00332500" → "ADBE")
        tk = (r['underlying_symbol'] or symbol.split()[0] or '').strip()
        if not tk or tk == '--':
            continue

        if tk in EXCLUDED_TICKERS:
            continue

        direction = r['direction'] or ''
        asset_cat = r['asset_category'] or 'STK'
        right_val = r['put_call'] or None
        strike_val = _parse_float(r['strike']) or None
        expiry_val = r['expiry'] or None

        ev = TradeEvent(
            source='FLEX_TRANSFER',
            account_id=acct,
            household_id=hh,
            ticker=tk,
            trade_date=r['date'] or '',
            date_time=(r['date_time'] or r['date'] or '') + ';120000',
            ib_order_id=None,
            transaction_id=r['transaction_id'] or f"XFER_{acct}_{symbol}_{r['date'] or ''}",
            asset_category=asset_cat,
            right=right_val,
            strike=strike_val,
            expiry=expiry_val,
            buy_sell='SELL' if direction == 'OUT' else 'BUY',
            open_close=direction,  # 'IN' or 'OUT' — used by classify_event
            quantity=qty,
            trade_price=_parse_float(r['transfer_price']),
            net_cash=0.0,  # transfers have zero P&L
            fifo_pnl_realized=0.0,
            transaction_type='Transfer',
            notes='',
            currency=r['currency'] or 'USD',
            raw=raw,
        )
        events.append(ev)

    return events


def _get_latest_sync_date(conn: sqlite3.Connection) -> Optional[str]:
    """Get the toDate from the most recent successful sync."""
    row = conn.execute(
        "SELECT to_date FROM master_log_sync WHERE status='success' "
        "ORDER BY sync_id DESC LIMIT 1"
    ).fetchone()
    return row['to_date'] if row else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _run_walker_for_all(
    conn: sqlite3.Connection,
    household: Optional[str] = None,
    ticker: Optional[str] = None,
) -> list[Cycle]:
    """Materialize events and run the Walker. Returns all cycles."""
    trade_events = _load_trade_events(conn, household, ticker)
    carryin_events = _load_carryin_events(conn, household, ticker)
    transfer_events = _load_transfer_events(conn, household, ticker)
    all_events = carryin_events + trade_events + transfer_events

    if not all_events:
        return []

    # Group by (household_id, ticker) and walk each group
    from itertools import groupby
    all_events.sort(key=lambda e: (e.household_id, e.ticker))

    all_cycles = []
    frozen_tickers = []

    for (hh, tk), group_iter in groupby(all_events, key=lambda e: (e.household_id, e.ticker)):
        group = list(group_iter)
        try:
            cycles = walk_cycles(group, excluded_tickers=EXCLUDED_TICKERS)
            all_cycles.extend(cycles)
        except UnknownEventError as exc:
            logger.error("Walker froze %s/%s: %s", hh, tk, exc)
            frozen_tickers.append((hh, tk, str(exc)))

    if frozen_tickers:
        logger.warning("Frozen tickers: %s", frozen_tickers)

    return all_cycles


def get_active_cycles(
    household: Optional[str] = None,
    ticker: Optional[str] = None,
    as_of_report_date: Optional[str] = None,
    *,
    db_path: "str | Path | None" = None,
) -> list[Cycle]:
    """Returns ACTIVE cycles. Settled-state read path. No intraday overlay.

    db_path: optional explicit database path for test fixtures. Default
    (None) routes to production via get_db_connection().
    """
    conn = _get_db(db_path)
    try:
        all_cycles = _run_walker_for_all(conn, household, ticker)
        return [c for c in all_cycles if c.status == 'ACTIVE']
    finally:
        conn.close()


def get_closed_cycles(
    household: str,
    ticker: str,
    limit: int = 20,
    *,
    db_path: "str | Path | None" = None,
) -> list[Cycle]:
    """Returns closed cycles for one (household, ticker) in reverse chron.

    db_path: optional explicit database path for test fixtures. Default
    (None) routes to production via get_db_connection().
    """
    conn = _get_db(db_path)
    try:
        all_cycles = _run_walker_for_all(conn, household, ticker)
        closed = [c for c in all_cycles if c.status == 'CLOSED']
        closed.reverse()
        return closed[:limit]
    finally:
        conn.close()


def get_cycles_for_ticker(
    household: str,
    ticker: str,
    *,
    db_path: "str | Path | None" = None,
) -> list[Cycle]:
    """All cycles (active + closed) for one ticker.

    db_path: optional explicit database path for test fixtures. Default
    (None) routes to production via get_db_connection().
    """
    conn = _get_db(db_path)
    try:
        return _run_walker_for_all(conn, household, ticker)
    finally:
        conn.close()


def get_cycles_with_intraday_overlay(
    household: str,
    ticker: str,
) -> list[Cycle]:
    """Phase 3 deliverable — settled + intraday overlay."""
    raise NotImplementedError("Phase 3 deliverable")


def get_active_cycles_with_intraday_delta(
    household: Optional[str] = None,
    ticker: Optional[str] = None,
    as_of_datetime: "datetime | None" = None,  # noqa: F821
    *,
    db_path: "str | Path | None" = None,
) -> list[Cycle]:
    """Walker-derived cycles + same-day fill overlay (ADR-006).

    Reads master_log_trades for authoritative state, then overlays
    fill_log entries created after the most recent master_log_trades
    last_synced_at watermark to produce a merged intraday view.
    Idempotent and pure — does not write.

    ADR-006 + dispatch addendum 2026-04-10: watermark source is
    MAX(last_synced_at) FROM master_log_trades rather than a dedicated
    flex_sync_log table (which does not exist). Known limitation:
    if IBKR Flex reporting lags a same-day fill, the watermark may
    advance while the specific lagged fill is neither in master_log
    nor in the delta window. See Followup #9 for the reconciliation
    gap long-term fix.

    Args:
        household: optional household filter
        ticker: optional ticker filter
        as_of_datetime: optional override for "now" (testing)
        db_path: optional explicit database path for test fixtures.
            Default (None) routes to production via get_db_connection().

    Returns:
        list[Cycle] with master_log state plus same-day fills applied.
    """
    from contextlib import closing

    # Step 1: get baseline walker cycles from master_log only
    baseline_cycles = get_active_cycles(
        household=household, ticker=ticker, db_path=db_path,
    )

    # Step 2: find the master_log_trades watermark (ADR-006 addendum)
    try:
        with closing(_get_db(db_path)) as conn:
            row = conn.execute(
                "SELECT MAX(last_synced_at) as watermark FROM master_log_trades"
            ).fetchone()
            watermark = row["watermark"] if row and row["watermark"] else None
    except Exception as exc:
        logger.warning(
            "intraday_delta: watermark lookup failed (%s), returning baseline", exc,
        )
        return baseline_cycles

    if not watermark:
        # No prior master_log data — return baseline as-is (no delta semantics possible)
        return baseline_cycles

    # Step 3: read fill_log delta since watermark
    try:
        with closing(_get_db(db_path)) as conn:
            delta_rows = conn.execute(
                "SELECT * FROM fill_log WHERE created_at > ? "
                "ORDER BY created_at ASC, exec_id ASC",
                (watermark,),
            ).fetchall()
    except Exception as exc:
        logger.warning(
            "intraday_delta: fill_log read failed (%s), returning baseline", exc,
        )
        return baseline_cycles

    if not delta_rows:
        return baseline_cycles

    # Step 4: filter delta to the requested household/ticker scope
    filtered_delta = []
    for row in delta_rows:
        if household is not None and row["household_id"] != household:
            continue
        if ticker is not None and row["ticker"] != ticker.upper():
            continue
        filtered_delta.append(row)

    if not filtered_delta:
        return baseline_cycles

    # Step 5: convert fill_log rows to TradeEvent objects and rewalk
    # Only rewalks cycles whose (household, ticker) appears in the delta.
    affected_keys = {
        (row["household_id"], row["ticker"]) for row in filtered_delta
    }

    merged_cycles = []
    for cycle in baseline_cycles:
        key = (cycle.household_id, cycle.ticker)
        if key not in affected_keys:
            merged_cycles.append(cycle)
            continue

        # Rebuild event list: original events + delta events
        original_events = list(cycle.events)
        delta_events = [
            _fill_log_row_to_trade_event(row)
            for row in filtered_delta
            if (row["household_id"], row["ticker"]) == key
        ]
        merged_events = sorted(
            original_events + delta_events,
            key=lambda ev: (ev.date_time, ev.transaction_id or ""),
        )

        # Rewalk just this (household, ticker)
        rewalked = walk_cycles(merged_events)
        active_rewalked = [c for c in rewalked if c.status == "ACTIVE"]
        if active_rewalked:
            merged_cycles.append(active_rewalked[-1])
        # else: delta closed the cycle — drop from active list

    return merged_cycles


def _fill_log_row_to_trade_event(row) -> TradeEvent:
    """Convert a fill_log row to a walker TradeEvent for delta overlay (ADR-006).

    fill_log schema (verified 2026-04-10): exec_id, ticker, action, quantity,
    price, premium_delta, account_id, household_id, created_at.

    action values: STK_BUY, STK_SELL, SELL_CALL, BUY_CALL, SELL_PUT, BUY_PUT.
    """
    action = row["action"]
    ticker = row["ticker"]

    # Map fill_log action to walker's buy_sell + asset_category + open_close
    if action == "STK_BUY":
        asset_category = "STK"
        buy_sell = "BUY"
        open_close = None
        right = None
    elif action == "STK_SELL":
        asset_category = "STK"
        buy_sell = "SELL"
        open_close = None
        right = None
    elif action == "SELL_CALL":
        asset_category = "OPT"
        buy_sell = "SELL"
        open_close = "O"
        right = "C"
    elif action == "BUY_CALL":
        asset_category = "OPT"
        buy_sell = "BUY"
        open_close = "C"  # assume close (BTC); walker classifier may override
        right = "C"
    elif action == "SELL_PUT":
        asset_category = "OPT"
        buy_sell = "SELL"
        open_close = "O"
        right = "P"
    elif action == "BUY_PUT":
        asset_category = "OPT"
        buy_sell = "BUY"
        open_close = "C"
        right = "P"
    else:
        raise ValueError(f"_fill_log_row_to_trade_event: unknown action {action!r}")

    # Use created_at from fill_log row (ADR-006 addendum: fill_time does not exist)
    # Format in DB: 'YYYY-MM-DD HH:MM:SS' via SQLite datetime('now')
    fill_time_raw = row["created_at"] or ""
    if "T" in fill_time_raw:
        dt_part = fill_time_raw.split(".")[0].replace("T", ";")
    else:
        dt_part = fill_time_raw.replace(" ", ";")
    trade_date = dt_part[:10].replace("-", "") if "-" in dt_part[:10] else dt_part[:8]
    date_time = dt_part.replace("-", "").replace(":", "")[:15]

    return TradeEvent(
        source="INTRADAY_DELTA",
        account_id=row["account_id"],
        household_id=row["household_id"],
        ticker=ticker,
        trade_date=trade_date,
        date_time=date_time,
        ib_order_id=None,
        transaction_id=row["exec_id"],
        asset_category=asset_category,
        right=right,
        strike=None,
        expiry=None,
        buy_sell=buy_sell,
        open_close=open_close,
        quantity=float(row["quantity"] or 0),
        trade_price=float(row["price"] or 0),
        net_cash=float(row["premium_delta"] or 0),
        fifo_pnl_realized=0.0,
        transaction_type="IntradayDelta",
        notes="",
        currency="USD",
        raw=dict(row),
    )


def verify_live_match(
    household: str,
    ticker: str,
    overlay_cycle: Cycle,
    live_position: float,
    live_short_options: int,
) -> bool:
    """Phase 3 deliverable — guardrail for order-driving commands."""
    raise NotImplementedError("Phase 3 deliverable")
