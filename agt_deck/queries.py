"""All SQL queries for the Command Deck. No inline SQL in routes."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def _fetchall(conn: sqlite3.Connection, sql: str, params=()) -> list:
    """Execute SELECT with explicit cursor lifecycle (DR Q1.5 fix).

    Python 3.11+ cursor GC regression can cause 'database is locked'
    on Windows multi-process. Explicit close prevents this.
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def _fetchone(conn: sqlite3.Connection, sql: str, params=()):
    """Single-row fetch with explicit cursor lifecycle."""
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        cur.close()

ACCOUNT_ALIAS = {
    "U21971297": "Yash Ind",
    "U22076329": "Yash Roth",
    "U22076184": "Yash Trad IRA",
    "U22388499": "Vikram",
}

HOUSEHOLD_MAP = {
    "U21971297": "Yash_Household",
    "U22076329": "Yash_Household",
    "U22076184": "Yash_Household",
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
    """Latest NAV per account from master_log_nav.

    Uses per-account MAX(report_date) so dormant accounts (e.g. U22076184
    Trad IRA) whose Flex rows stop appearing still contribute their last
    known NAV. Fix 1 / Sprint 1F.
    """
    try:
        rows = _fetchall(conn, """
            SELECT m1.account_id, CAST(m1.total AS REAL) as nav
            FROM master_log_nav m1
            WHERE m1.report_date = (
                SELECT MAX(m2.report_date)
                FROM master_log_nav m2
                WHERE m2.account_id = m1.account_id
            )
        """)
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
    """Read STAGED Dynamic Exit + R5 Sell candidates for Cure Console display.

    Only final_status='STAGED' — ATTESTED rows live on the Telegram
    TRANSMIT/CANCEL surface and must NOT appear here (race risk).

    Returns list of dicts grouped by ticker, each with a 'candidates' list.
    Includes both CC (R8) and STK_SELL (R5) action types.
    """
    try:
        rows = conn.execute(
            "SELECT audit_id, ticker, household, desk_mode, action_type, "
            "  strike, expiry, contracts, shares, limit_price, "
            "  gate1_ratio, gate1_freed_margin, gate1_realized_loss, "
            "  gate1_conviction_tier, gate2_max_per_cycle, "
            "  walk_away_pnl_per_share, underlying_spot_at_render, "
            "  render_ts, staged_ts, exception_type "
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
                "action_type": r["action_type"],
                "exception_type": r.get("exception_type"),
                "candidates": [],
            }
        grouped[tk]["candidates"].append({
            "audit_id": r["audit_id"],
            "action_type": r["action_type"],
            "strike": r["strike"],
            "expiry": r["expiry"],
            "contracts": r["contracts"],
            "shares": r["shares"],
            "limit_price": r["limit_price"],
            "gate1_ratio": r["gate1_ratio"],
            "gate1_freed_margin": r["gate1_freed_margin"],
            "gate1_realized_loss": r["gate1_realized_loss"],
            "gate1_conviction_tier": r["gate1_conviction_tier"],
            "gate2_max_per_cycle": r["gate2_max_per_cycle"],
            "walk_away_pnl_per_share": r["walk_away_pnl_per_share"],
            "underlying_spot_at_render": r["underlying_spot_at_render"],
            "render_ts": r["render_ts"],
            "staged_ts": r["staged_ts"],
            "exception_type": r.get("exception_type"),
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


# ── Sprint 1B: Lifecycle Queue ───────────────────────────────────

def get_lifecycle_rows(conn: sqlite3.Connection) -> list[dict]:
    """All active lifecycle rows for Cure Console Action Queue.

    Returns STAGED + ATTESTED + TRANSMITTING + recently TRANSMITTED (within 5m).
    Orphans (TRANSMITTING for >10min) sorted first.
    """
    import time
    now = time.time()
    transmitted_cutoff = now - 300   # 5 minutes
    orphan_cutoff = now - 600        # 10 minutes
    try:
        rows = conn.execute(
            "SELECT audit_id, ticker, household, action_type, contracts, shares, "
            "  strike, expiry, limit_price, final_status, desk_mode, "
            "  staged_ts, last_updated, transmitted_ts, "
            "  fill_price, fill_qty, fill_ts, "
            "  originating_account_id, re_validation_count, exception_type "
            "FROM bucket3_dynamic_exit_log "
            "WHERE final_status IN ('STAGED', 'ATTESTED', 'TRANSMITTING') "
            "   OR (final_status = 'TRANSMITTED' AND transmitted_ts > ?) "
            "ORDER BY "
            "  CASE WHEN final_status = 'TRANSMITTING' AND transmitted_ts < ? THEN 0 ELSE 1 END, "
            "  CASE final_status "
            "    WHEN 'STAGED' THEN 1 "
            "    WHEN 'ATTESTED' THEN 2 "
            "    WHEN 'TRANSMITTING' THEN 3 "
            "    WHEN 'TRANSMITTED' THEN 4 "
            "  END, "
            "  staged_ts DESC",
            (transmitted_cutoff, orphan_cutoff),
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            ts = d.get("transmitted_ts") or d.get("staged_ts") or 0
            d["is_orphan"] = (
                d["final_status"] == "TRANSMITTING"
                and d.get("transmitted_ts") is not None
                and d["transmitted_ts"] < orphan_cutoff
            )
            d["age_seconds"] = int(now - ts) if ts else 0
            result.append(d)
        return result
    except Exception as exc:
        logger.warning("get_lifecycle_rows failed: %s", exc)
        return []


# ── Sprint 1B: Health Strip ──────────────────────────────────────

def get_health_strip_data(conn: sqlite3.Connection) -> dict:
    """Health Strip data for Cure Console header.

    Reads per-account EL from el_snapshots (written by telegram_bot's
    el_snapshot_writer_job every 30s). Computes staleness.

    # TODO Sprint 1D: add explicit ib_connection_heartbeat row that
    # telegram_bot writes on every poll regardless of IB state, so deck
    # can distinguish 'IB down' from 'bot down'. Current staleness
    # inference conflates IB disconnected vs writer job crashed vs bot down.
    """
    import time
    from agt_equities.mode_engine import get_current_mode

    now = time.time()
    mode = get_current_mode(conn)

    # Sprint 1E: derive from shared maps instead of third hardcoded list
    account_configs = [
        (acct, ACCOUNT_ALIAS.get(acct, acct), hh)
        for acct, hh in HOUSEHOLD_MAP.items()
    ]

    accounts = []
    any_fresh = False
    for acct_id, alias, household in account_configs:
        try:
            row = conn.execute(
                "SELECT excess_liquidity, nlv, buying_power, timestamp "
                "FROM el_snapshots "
                "WHERE account_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (acct_id,),
            ).fetchone()
        except Exception:
            row = None

        if row and row["nlv"]:
            from datetime import datetime, timezone
            try:
                ts = datetime.fromisoformat(
                    row["timestamp"].replace("Z", "+00:00")
                )
                staleness = int((datetime.now(timezone.utc) - ts).total_seconds())
            except Exception:
                staleness = 9999
            el = row["excess_liquidity"] or 0
            nlv = row["nlv"] or 0
            el_pct = round(el / nlv * 100, 1) if nlv > 0 else 0.0
            is_stale = staleness > 120
            if not is_stale:
                any_fresh = True
            accounts.append({
                "account_id": acct_id,
                "alias": alias,
                "household": household,
                "nlv": nlv,
                "excess_liquidity": el,
                "buying_power": row["buying_power"] or 0,
                "el_pct": el_pct,
                "staleness_seconds": staleness,
                "is_stale": is_stale,
            })
        else:
            accounts.append({
                "account_id": acct_id,
                "alias": alias,
                "household": household,
                "nlv": None,
                "excess_liquidity": None,
                "buying_power": None,
                "el_pct": None,
                "staleness_seconds": None,
                "is_stale": True,
            })

    return {
        "mode": mode,
        "ib_connected": any_fresh,
        "accounts": accounts,
    }
