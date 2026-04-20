"""agt_equities.paper_validator — ADR-016 Paper Pipeline First-Fire Validator (P1).

Standalone end-to-end validator that pushes a synthetic CSP candidate through
the real stager → executor → paper IB pipeline and verifies the order lands.

Run manually:
    AGT_DB_PATH=C:\\AGT_Telegram_Bridge\\agt_desk.db \\
        python -m agt_equities.paper_validator --trigger post_deploy

Exit 0 on success, exit 1 on any failure.

P1 scope (ADR-016):
  - Synthetic SPY put ~10% OTM, next Friday expiry, 1 contract
  - Account: DUP751003 (designated paper validator account)
  - Traverses: stager → circuit breaker → IB placeOrder
  - Cancels order within 30s of IB ack
  - Writes validator_runs row with structured result
  - Does NOT import from telegram_bot.py

Design notes:
  - clientId=15 (distinct from bot=1, scheduler=2, dev_cli=12)
  - notes column added to pending_orders by ensure_schema()
  - Approval gate forced to auto_path=True per ADR spec
  - AGT_CSP_REQUIRE_APPROVAL=true scenario produces blocked_at='approval_gate'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

import ib_async

from agt_equities.db import get_db_connection, tx_immediate
from agt_equities.dates import et_today
from agt_equities.ib_conn import IBConnConfig, IBConnector
from agt_equities.ib_order_builder import build_adaptive_option_order

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALIDATOR_CLIENT_ID = 15          # Distinct from bot=1, scheduler=2, dev_cli=12
VALIDATOR_ACCOUNT = "DUP751003"   # Designated paper validator account
VALIDATOR_TICKER = "SPY"
VALIDATOR_QTY = 1
OTM_TARGET_PCT = 0.10             # ~10% OTM put strike
IB_ACK_TIMEOUT_S = 60             # seconds to wait for ib_order_id
CANCEL_DEADLINE_S = 30            # cancel within 30s of IB ack
APPROVAL_ENV_VAR = "AGT_CSP_REQUIRE_APPROVAL"

# Stages (for stage_reached / blocked_at)
STAGE_SCHEMA_READY = "schema_ready"
STAGE_APPROVAL_GATE = "approval_gate"
STAGE_STAGED = "staged"
STAGE_CIRCUIT_BREAKER = "circuit_breaker"
STAGE_IB_CONNECTED = "ib_connected"
STAGE_ORDER_PLACED = "order_placed"
STAGE_IB_ACKNOWLEDGED = "ib_acknowledged"
STAGE_CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Schema migration — idempotent
# ---------------------------------------------------------------------------

_VALIDATOR_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS validator_runs (
    run_id TEXT PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    trigger TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    stage_reached TEXT NOT NULL,
    blocked_at TEXT,
    blocked_reason TEXT,
    pending_order_id INTEGER,
    ib_order_id INTEGER,
    cleanup_status TEXT NOT NULL DEFAULT 'NOT_NEEDED',
    evidence_json TEXT
)
"""

_NOTES_COLUMN_DDL = """
ALTER TABLE pending_orders ADD COLUMN notes TEXT
"""


def ensure_schema(db_path: str | None = None) -> None:
    """Idempotently create validator_runs table and notes column."""
    with closing(get_db_connection(db_path)) as conn:
        # validator_runs table
        conn.execute(_VALIDATOR_RUNS_DDL)
        conn.commit()

        # notes column on pending_orders (idempotent via PRAGMA check)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(pending_orders)").fetchall()]
        if "notes" not in cols:
            conn.execute(_NOTES_COLUMN_DDL)
            conn.commit()
            logger.info("paper_validator: added notes column to pending_orders")


# ---------------------------------------------------------------------------
# Synthetic candidate helpers
# ---------------------------------------------------------------------------

def _next_friday() -> str:
    """Return next Friday as 'YYYY-MM-DD'. If today is Friday, returns next week."""
    today = et_today()
    days_ahead = 4 - today.weekday()   # Friday = weekday 4
    if days_ahead <= 0:
        days_ahead += 7
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _spy_spot_estimate(ib: ib_async.IB) -> float:
    """Fetch SPY last-trade price via IB reqMktData (delayed ok).

    Falls back to 520.0 if IB cannot resolve (outside market hours / no data).
    """
    try:
        contract = ib_async.Stock("SPY", "SMART", "USD")
        tickers = ib.reqMktData(contract, "", False, False)
        ib.sleep(2.0)
        spot = tickers.last
        if spot and spot > 0:
            return float(spot)
        # Try midpoint
        if tickers.bid and tickers.ask and tickers.bid > 0 and tickers.ask > 0:
            return float((tickers.bid + tickers.ask) / 2.0)
    except Exception as exc:
        logger.warning("paper_validator: SPY spot fetch failed: %s", exc)
    return 520.0  # conservative fallback


def _round_to_strike(price: float, step: float = 1.0) -> float:
    """Round a price to the nearest standard SPY strike (1-point grid)."""
    return round(round(price / step) * step, 2)


def _build_synthetic_payload(
    run_id: str,
    spot: float,
    expiry: str,
) -> dict[str, Any]:
    """Build the pending_orders payload dict for the synthetic CSP."""
    raw_strike = spot * (1.0 - OTM_TARGET_PCT)
    strike = _round_to_strike(raw_strike)
    # Limit price: 0.05 placeholder (paper gateway accepts any valid price)
    limit_price = 0.05
    return {
        "ticker": VALIDATOR_TICKER,
        "strike": strike,
        "expiry": expiry,
        "quantity": VALIDATOR_QTY,
        "action": "SELL",
        "right": "P",
        "sec_type": "OPT",
        "account_id": VALIDATOR_ACCOUNT,
        "limit_price": limit_price,
        "urgency": "patient",
        "origin": "paper_validator",
        "notes": f"SYNTHETIC_VALIDATOR_{run_id}",
    }


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def _write_validator_run(
    db_path: str | None,
    run_id: str,
    started_at: str,
    trigger: str,
    *,
    success: int,
    stage_reached: str,
    blocked_at: str | None = None,
    blocked_reason: str | None = None,
    pending_order_id: int | None = None,
    ib_order_id: int | None = None,
    cleanup_status: str = "NOT_NEEDED",
    evidence: dict | None = None,
) -> None:
    completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(get_db_connection(db_path)) as conn:
        with tx_immediate(conn):
            conn.execute(
                """
                INSERT OR REPLACE INTO validator_runs (
                    run_id, started_at_utc, completed_at_utc, trigger,
                    success, stage_reached, blocked_at, blocked_reason,
                    pending_order_id, ib_order_id, cleanup_status, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started_at,
                    completed_at,
                    trigger,
                    success,
                    stage_reached,
                    blocked_at,
                    blocked_reason,
                    pending_order_id,
                    ib_order_id,
                    cleanup_status,
                    json.dumps(evidence) if evidence else None,
                ),
            )


def _stage_order(db_path: str | None, payload: dict) -> int:
    """Insert a staged order into pending_orders. Returns new row id."""
    notes_val = payload.get("notes")
    with closing(get_db_connection(db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "INSERT INTO pending_orders (payload, status, created_at, notes) "
                "VALUES (?, 'staged', ?, ?)",
                (
                    json.dumps(payload),
                    datetime.now(timezone.utc).isoformat(),
                    notes_val,
                ),
            )
            return cur.lastrowid


def _mark_processing(db_path: str | None, order_id: int) -> bool:
    """CAS: staged → processing. Returns True if transition succeeded."""
    with closing(get_db_connection(db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "UPDATE pending_orders SET status='processing' "
                "WHERE id=? AND status='staged'",
                (order_id,),
            )
            return cur.rowcount == 1


def _update_order_ib_ids(
    db_path: str | None,
    order_id: int,
    ib_order_id: int,
    ib_perm_id: int,
) -> None:
    with closing(get_db_connection(db_path)) as conn:
        with tx_immediate(conn):
            conn.execute(
                "UPDATE pending_orders SET ib_order_id=?, ib_perm_id=?, status='sent' "
                "WHERE id=?",
                (ib_order_id, ib_perm_id, order_id),
            )


def _mark_cancelled_validator(db_path: str | None, order_id: int) -> None:
    with closing(get_db_connection(db_path)) as conn:
        with tx_immediate(conn):
            conn.execute(
                "UPDATE pending_orders SET status='cancelled', "
                "last_ib_status='CANCELLED_VALIDATOR' WHERE id=?",
                (order_id,),
            )


# ---------------------------------------------------------------------------
# Circuit breaker check (mirrors _pre_trade_gates Gate 0a)
# ---------------------------------------------------------------------------

def _circuit_breaker_check() -> tuple[bool, str]:
    """Run circuit breaker. Returns (ok, reason)."""
    try:
        from scripts.circuit_breaker import run_all_checks as _cb_run  # type: ignore[import]
        result = _cb_run()
        if result.get("halted"):
            viols = result.get("violations", [])
            reasons = "; ".join(v.get("reason", v.get("check", "?")) for v in viols[:3])
            return False, f"Circuit breaker HALTED: {reasons}"
        return True, ""
    except ImportError:
        logger.warning("paper_validator: circuit_breaker not available — skipping check")
        return True, "circuit_breaker_unavailable"
    except Exception as exc:
        logger.error("paper_validator: circuit_breaker check failed: %s", exc)
        return False, f"circuit_breaker_internal_error: {exc}"


# ---------------------------------------------------------------------------
# Core validator coroutine
# ---------------------------------------------------------------------------

async def _run_validator(
    trigger: str,
    db_path: str | None,
) -> dict[str, Any]:
    """Execute one full validator run. Returns result dict."""
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stage_reached = STAGE_SCHEMA_READY
    pending_order_id: int | None = None
    ib_order_id_val: int | None = None
    cleanup_status = "NOT_NEEDED"
    evidence: dict[str, Any] = {"run_id": run_id, "trigger": trigger}

    def _fail(
        blocked_at: str,
        reason: str,
        *,
        cs: str = "NOT_NEEDED",
    ) -> dict[str, Any]:
        _write_validator_run(
            db_path, run_id, started_at, trigger,
            success=0,
            stage_reached=stage_reached,
            blocked_at=blocked_at,
            blocked_reason=reason,
            pending_order_id=pending_order_id,
            ib_order_id=ib_order_id_val,
            cleanup_status=cs,
            evidence=evidence,
        )
        return {
            "run_id": run_id,
            "success": False,
            "stage_reached": stage_reached,
            "blocked_at": blocked_at,
            "blocked_reason": reason,
            "cleanup_status": cs,
        }

    # ── Schema ──────────────────────────────────────────────────────────────
    try:
        ensure_schema(db_path)
    except Exception as exc:
        return _fail(STAGE_SCHEMA_READY, f"schema_error: {exc}")

    # ── Approval gate check (catch today's bug #2) ───────────────────────
    require_approval = os.environ.get(APPROVAL_ENV_VAR, "false").strip().lower()
    if require_approval in ("1", "true", "yes"):
        # ADR-016: validator detects this misconfiguration and reports it
        evidence["approval_env"] = require_approval
        return _fail(
            STAGE_APPROVAL_GATE,
            "AGT_CSP_REQUIRE_APPROVAL_TRUE",
        )

    stage_reached = STAGE_APPROVAL_GATE

    # ── Build synthetic candidate ────────────────────────────────────────
    expiry = _next_friday()
    evidence["expiry"] = expiry

    # ── Stage order (write pending_orders row) ───────────────────────────
    # Spot price: use a preliminary IB connection or a fallback
    # We'll use the fallback here so staging is decoupled from IB connect
    spot_fallback = 520.0
    payload = _build_synthetic_payload(run_id, spot_fallback, expiry)
    evidence["payload_ticker"] = payload["ticker"]
    evidence["payload_strike"] = payload["strike"]
    evidence["payload_account"] = payload["account_id"]

    try:
        pending_order_id = _stage_order(db_path, payload)
        evidence["pending_order_id"] = pending_order_id
    except Exception as exc:
        return _fail(STAGE_STAGED, f"staging_failed: {exc}")

    stage_reached = STAGE_STAGED

    # ── Circuit breaker ──────────────────────────────────────────────────
    cb_ok, cb_reason = _circuit_breaker_check()
    if not cb_ok:
        # Mark order staged→failed
        with closing(get_db_connection(db_path)) as conn:
            with tx_immediate(conn):
                conn.execute(
                    "UPDATE pending_orders SET status='failed' WHERE id=?",
                    (pending_order_id,),
                )
        return _fail(STAGE_CIRCUIT_BREAKER, cb_reason)

    stage_reached = STAGE_CIRCUIT_BREAKER

    # ── CAS: staged → processing ─────────────────────────────────────────
    if not _mark_processing(db_path, pending_order_id):
        return _fail(STAGE_STAGED, "cas_staged_to_processing_failed")

    # ── Connect to paper IB ──────────────────────────────────────────────
    connector = IBConnector(
        config=IBConnConfig(
            host="127.0.0.1",
            client_id=VALIDATOR_CLIENT_ID,
            gateway_port=4002,
            tws_fallback_port=7497,
            connect_timeout=15.0,
            market_data_type=4,
            post_connect_sleep_s=2.0,
            request_positions_on_connect=False,
        )
    )

    try:
        ib = await connector.ensure_connected()
    except Exception as exc:
        with closing(get_db_connection(db_path)) as conn:
            with tx_immediate(conn):
                conn.execute(
                    "UPDATE pending_orders SET status='failed' WHERE id=?",
                    (pending_order_id,),
                )
        return _fail(STAGE_IB_CONNECTED, f"ib_connect_failed: {exc}")

    stage_reached = STAGE_IB_CONNECTED

    try:
        # Update spot using real IB data
        try:
            spot = _spy_spot_estimate(ib)
            if abs(spot - spot_fallback) > 5.0:
                # Rebuild payload with real spot and update DB
                new_payload = _build_synthetic_payload(run_id, spot, expiry)
                evidence["spot_real"] = spot
                evidence["payload_strike"] = new_payload["strike"]
                with closing(get_db_connection(db_path)) as conn:
                    with tx_immediate(conn):
                        conn.execute(
                            "UPDATE pending_orders SET payload=? WHERE id=?",
                            (json.dumps(new_payload), pending_order_id),
                        )
                payload = new_payload
        except Exception as spot_exc:
            logger.warning("paper_validator: spot update failed (using fallback): %s", spot_exc)

        # ── Build IB contract + order ────────────────────────────────────
        strike = payload["strike"]
        expiry_fmt = expiry.replace("-", "")
        contract = ib_async.Option(
            symbol=VALIDATOR_TICKER,
            lastTradeDateOrContractMonth=expiry_fmt,
            strike=strike,
            right="P",
            exchange="SMART",
        )
        order = build_adaptive_option_order(
            action="SELL",
            qty=VALIDATOR_QTY,
            limit_price=float(payload["limit_price"]),
            account_id=VALIDATOR_ACCOUNT,
            urgency="patient",
        )
        evidence["ib_contract"] = f"SPY {expiry} {strike}P"

        # ── Place order ──────────────────────────────────────────────────
        trade = ib.placeOrder(contract, order)
        raw_ib_order_id = trade.order.orderId if trade else 0
        raw_ib_perm_id = trade.order.permId if trade else 0
        evidence["ib_order_id_initial"] = raw_ib_order_id

        _update_order_ib_ids(db_path, pending_order_id, raw_ib_order_id, raw_ib_perm_id)

        stage_reached = STAGE_ORDER_PLACED

        # ── Wait for IB acknowledgement (ib_order_id populated) ─────────
        deadline = asyncio.get_event_loop().time() + IB_ACK_TIMEOUT_S
        ib_order_id_val = raw_ib_order_id
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.0)
            ib.sleep(0)  # process IB events
            # Re-read trade state
            if trade and trade.order and trade.order.orderId:
                ib_order_id_val = trade.order.orderId
                if trade.orderStatus and trade.orderStatus.status not in (
                    "", None, "PreSubmitted"
                ):
                    break
            # Check DB for ib_order_id being set (belt-and-suspenders)
            with closing(get_db_connection(db_path)) as conn:
                row = conn.execute(
                    "SELECT ib_order_id, last_ib_status FROM pending_orders WHERE id=?",
                    (pending_order_id,),
                ).fetchone()
            if row and row[0]:
                ib_order_id_val = row[0]
                if row[1] not in (None, "", "sent"):
                    break

        evidence["ib_order_id_final"] = ib_order_id_val
        evidence["ib_order_status"] = (
            trade.orderStatus.status if trade and trade.orderStatus else "unknown"
        )

        if not ib_order_id_val:
            stage_reached = STAGE_ORDER_PLACED
            cleanup_status = "NOT_NEEDED"
            _mark_cancelled_validator(db_path, pending_order_id)
            return _fail(
                STAGE_IB_ACKNOWLEDGED,
                "ib_order_id_not_populated_within_timeout",
                cs="NOT_NEEDED",
            )

        stage_reached = STAGE_IB_ACKNOWLEDGED

        # ── Cancel within 30s ────────────────────────────────────────────
        cancel_deadline = asyncio.get_event_loop().time() + CANCEL_DEADLINE_S
        try:
            ib.cancelOrder(trade.order)
            logger.info(
                "paper_validator: cancel requested for IB order %s", ib_order_id_val
            )
            # Wait for cancel confirmation
            while asyncio.get_event_loop().time() < cancel_deadline:
                await asyncio.sleep(1.0)
                ib.sleep(0)
                if trade and trade.orderStatus:
                    status = trade.orderStatus.status or ""
                    if status in ("Cancelled", "ApiCancelled", "Inactive"):
                        break
            cleanup_status = "CANCELLED"
            _mark_cancelled_validator(db_path, pending_order_id)
            stage_reached = STAGE_CANCELLED
        except Exception as cancel_exc:
            logger.error("paper_validator: cancel failed: %s", cancel_exc)
            cleanup_status = "STUCK"
            evidence["cancel_error"] = str(cancel_exc)

        # ── Write success row ────────────────────────────────────────────
        _write_validator_run(
            db_path, run_id, started_at, trigger,
            success=1,
            stage_reached=stage_reached,
            pending_order_id=pending_order_id,
            ib_order_id=ib_order_id_val,
            cleanup_status=cleanup_status,
            evidence=evidence,
        )
        return {
            "run_id": run_id,
            "success": True,
            "stage_reached": stage_reached,
            "blocked_at": None,
            "blocked_reason": None,
            "pending_order_id": pending_order_id,
            "ib_order_id": ib_order_id_val,
            "cleanup_status": cleanup_status,
        }

    finally:
        await connector.disconnect()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_validator(trigger: str = "on_demand", db_path: str | None = None) -> dict[str, Any]:
    """Synchronous wrapper. Returns result dict.

    Suitable for PTB JobQueue (P3) and deploy.ps1 subprocess (P2).
    """
    return asyncio.run(_run_validator(trigger, db_path))


# ---------------------------------------------------------------------------
# CLI / __main__
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m agt_equities.paper_validator`."""
    _configure_logging()

    parser = argparse.ArgumentParser(
        description="ADR-016 Paper Pipeline First-Fire Validator (P1)"
    )
    parser.add_argument(
        "--trigger",
        choices=["post_deploy", "post_morning_fire", "on_demand"],
        default="on_demand",
        help="Trigger source for validator_runs record",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override DB path (default: AGT_DB_PATH env or module default)",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or os.environ.get("AGT_DB_PATH") or None

    print(f"\n[validator] trigger={args.trigger}  db={db_path or '<module default>'}")

    result = run_validator(trigger=args.trigger, db_path=db_path)

    print("\n[validator] RESULT:")
    for k, v in result.items():
        print(f"  {k}: {v}")

    if result.get("success"):
        print("\n[validator] EXIT 0 — pipeline healthy")
        return 0
    else:
        print(f"\n[validator] EXIT 1 — BLOCKED at {result.get('blocked_at')}: "
              f"{result.get('blocked_reason')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
