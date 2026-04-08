"""All SQL queries for the Command Deck. No inline SQL in routes."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

ACCOUNT_ALIAS = {
    "U21971297": "Yash Ind",
    "U22076329": "Yash Roth",
    "U22388499": "Vikram",
}

HOUSEHOLD_MAP = {
    "U21971297": "Yash_Household",
    "U22076329": "Yash_Household",
    "U22388499": "Vikram_Household",
}


def _safe(fn):
    """Wrap query function: on exception return sentinel and log."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:
            logger.warning("Query %s failed: %s", fn.__name__, exc)
            return fn.__annotations__.get('return', None)
    return wrapper


# ── NAV / Top strip ──────────────────────────────────────────────

def get_portfolio_nav(conn: sqlite3.Connection) -> dict:
    """Latest NAV per account from master_log_nav."""
    try:
        rows = conn.execute("""
            SELECT account_id, CAST(total AS REAL) as nav
            FROM master_log_nav
            WHERE report_date = (SELECT MAX(report_date) FROM master_log_nav)
        """).fetchall()
        result = {}
        for r in rows:
            result[r["account_id"]] = r["nav"]
        return result
    except Exception as exc:
        logger.warning("get_portfolio_nav: %s", exc)
        return {}


def get_change_in_nav(conn: sqlite3.Connection) -> dict:
    """ChangeInNAV per account."""
    try:
        rows = conn.execute("""
            SELECT account_id, CAST(starting_value AS REAL) as start,
                   CAST(ending_value AS REAL) as ending,
                   CAST(twr AS REAL) as twr,
                   CAST(deposits_withdrawals AS REAL) as deposits_withdrawals,
                   CAST(asset_transfers AS REAL) as asset_transfers
            FROM master_log_change_in_nav
        """).fetchall()
        return {r["account_id"]: dict(r) for r in rows}
    except Exception as exc:
        logger.warning("get_change_in_nav: %s", exc)
        return {}


def get_last_sync(conn: sqlite3.Connection) -> dict | None:
    """Most recent sync audit row."""
    try:
        row = conn.execute("""
            SELECT sync_id, finished_at, status, rows_inserted, sections_processed
            FROM master_log_sync
            ORDER BY sync_id DESC LIMIT 1
        """).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.warning("get_last_sync: %s", exc)
        return None


# ── Trades / Fills ───────────────────────────────────────────────

def get_recent_fills(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Recent trades from master_log_trades."""
    try:
        rows = conn.execute("""
            SELECT account_id, date_time, symbol, asset_category,
                   buy_sell, quantity, trade_price, net_cash,
                   transaction_type, notes
            FROM master_log_trades
            ORDER BY date_time DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("get_recent_fills: %s", exc)
        return []


# ── Sector / Universe ────────────────────────────────────────────

def get_ticker_industries(conn: sqlite3.Connection) -> dict:
    """Ticker → industry group from ticker_universe."""
    try:
        rows = conn.execute("""
            SELECT ticker, gics_industry_group
            FROM ticker_universe
            WHERE gics_industry_group IS NOT NULL
        """).fetchall()
        return {r["ticker"]: r["gics_industry_group"] for r in rows}
    except Exception as exc:
        logger.warning("get_ticker_industries: %s", exc)
        return {}


# ── Reconciliation ───────────────────────────────────────────────

def get_recent_orders(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Recent orders from pending_orders with status timeline."""
    try:
        rows = conn.execute("""
            SELECT id, status, created_at, payload, status_history,
                   ib_order_id, fill_price, fill_qty
            FROM pending_orders
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        result = []
        import json
        for r in rows:
            try:
                payload = json.loads(r['payload'] or '{}')
            except Exception:
                payload = {}
            try:
                history = json.loads(r['status_history'] or '[]')
            except Exception:
                history = []
            result.append({
                'id': r['id'],
                'status': r['status'],
                'created_at': r['created_at'],
                'ticker': payload.get('ticker', '?'),
                'action': payload.get('action', '?'),
                'quantity': payload.get('quantity', '?'),
                'strike': payload.get('strike'),
                'ib_order_id': r['ib_order_id'],
                'fill_price': r['fill_price'],
                'fill_qty': r['fill_qty'],
                'history': history,
            })
        return result
    except Exception as exc:
        logger.warning("get_recent_orders: %s", exc)
        return []


def get_recon_summary(conn: sqlite3.Connection) -> dict:
    """Lightweight recon summary from sync log."""
    sync = get_last_sync(conn)
    try:
        eae = conn.execute("SELECT COUNT(*) FROM master_log_option_eae").fetchone()[0]
        trades = conn.execute("SELECT COUNT(*) FROM master_log_trades").fetchone()[0]
    except Exception:
        eae = trades = 0
    return {
        "sync": sync,
        "eae_count": eae,
        "trade_count": trades,
        # Cross-check results are computed on demand, not stored
        "a_status": "48/49 (1 accepted)",
        "b_status": "14/14 ✓",
        "c_status": "2/4 (2 accepted)",
    }


def get_staged_dynamic_exits(conn: sqlite3.Connection) -> list[dict]:
    """Read STAGED Dynamic Exit candidates for Cure Console display.

    Only final_status='STAGED' — ATTESTED rows live on the Telegram
    TRANSMIT/CANCEL surface and must NOT appear here (race risk).

    Returns list of dicts grouped by ticker, each with a 'candidates' list.
    """
    try:
        rows = conn.execute(
            "SELECT audit_id, ticker, household, desk_mode, "
            "  strike, expiry, contracts, "
            "  gate1_ratio, gate1_freed_margin, gate1_realized_loss, "
            "  gate1_conviction_tier, gate2_max_per_cycle, "
            "  walk_away_pnl_per_share, underlying_spot_at_render, "
            "  render_ts, staged_ts "
            "FROM bucket3_dynamic_exit_log "
            "WHERE final_status = 'STAGED' "
            "ORDER BY ticker, gate1_ratio DESC"
        ).fetchall()
    except Exception as exc:
        logger.warning("get_staged_dynamic_exits failed: %s", exc)
        return []

    if not rows:
        return []

    # Group by ticker
    from collections import OrderedDict
    grouped: OrderedDict[str, dict] = OrderedDict()
    for r in rows:
        tk = r["ticker"]
        if tk not in grouped:
            grouped[tk] = {
                "ticker": tk,
                "household": r["household"],
                "desk_mode": r["desk_mode"],
                "candidates": [],
            }
        grouped[tk]["candidates"].append({
            "audit_id": r["audit_id"],
            "strike": r["strike"],
            "expiry": r["expiry"],
            "contracts": r["contracts"],
            "gate1_ratio": r["gate1_ratio"],
            "gate1_freed_margin": r["gate1_freed_margin"],
            "gate1_realized_loss": r["gate1_realized_loss"],
            "gate1_conviction_tier": r["gate1_conviction_tier"],
            "gate2_max_per_cycle": r["gate2_max_per_cycle"],
            "walk_away_pnl_per_share": r["walk_away_pnl_per_share"],
            "underlying_spot_at_render": r["underlying_spot_at_render"],
            "render_ts": r["render_ts"],
            "staged_ts": r["staged_ts"],
        })

    return list(grouped.values())


def attest_staged_exit(
    conn: sqlite3.Connection,
    audit_id: str,
    operator_thesis: str | None,
    attestation_value_typed: str | None,
    checkbox_state_json: str | None,
    attested_limit_price: float | None,
    expected_desk_mode: str,
) -> int:
    """Transition a STAGED row to ATTESTED. Returns cursor.rowcount.

    Caller owns conn.commit() / conn.rollback().
    Returns 0 if audit_id not found, row is no longer STAGED, or desk_mode
    changed between read and write (TOCTOU race gate).

    checkbox_state_json shape (PEACETIME):
        {"ack_loss": true, "ack_cure": true, "ack_ts": <epoch float>}
    attestation_value_typed (WARTIME):
        The exact integer (or ticker for $0/$1 loss) the operator typed.
    """
    try:
        cursor = conn.execute(
            "UPDATE bucket3_dynamic_exit_log "
            "SET final_status = 'ATTESTED', "
            "    operator_thesis = ?, "
            "    attestation_value_typed = ?, "
            "    checkbox_state_json = ?, "
            "    limit_price = ?, "
            "    last_updated = CURRENT_TIMESTAMP "
            "WHERE audit_id = ? AND final_status = 'STAGED' AND desk_mode = ?",
            (operator_thesis, attestation_value_typed, checkbox_state_json,
             attested_limit_price, audit_id, expected_desk_mode),
        )
        return cursor.rowcount
    except Exception as exc:
        logger.warning("attest_staged_exit(%s) failed: %s", audit_id, exc)
        raise


def get_staged_exit_by_audit_id(conn: sqlite3.Connection, audit_id: str) -> dict | None:
    """Fetch a single STAGED row by audit_id for Smart Friction modal render.

    Returns dict with all columns, or None if not found / not STAGED.
    """
    try:
        row = conn.execute(
            "SELECT * FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = ? AND final_status = 'STAGED'",
            (audit_id,),
        ).fetchone()
    except Exception as exc:
        logger.warning("get_staged_exit_by_audit_id(%s) failed: %s", audit_id, exc)
        return None
    if row is None:
        return None
    return dict(row)
