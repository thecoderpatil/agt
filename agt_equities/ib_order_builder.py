"""Central IB order construction — Adaptive algo helpers.

Lifted from telegram_bot.py local helpers (previously at lines
~6607-6666) to provide a single source of truth + test coverage.
Behavior-preserving move for existing call sites; adds one new helper
for STK orders (was raw MarketOrder/LimitOrder).

Default urgency is Patient for all flows. Roll urgency is time-gated
by urgency_policy.decide_roll_urgency at the call site (MR !103).
"""
from __future__ import annotations

from typing import Literal

import ib_async

Urgency = Literal["patient", "urgent"]

_PRIORITY_MAP: dict[str, str] = {
    "patient": "Patient",
    "urgent":  "Urgent",
}


def _adaptive_params(urgency: str) -> list:
    """Return algoParams list for the given lowercase urgency string."""
    if urgency not in _PRIORITY_MAP:
        raise ValueError(
            f"urgency must be 'patient' or 'urgent', got {urgency!r}"
        )
    return [ib_async.TagValue("adaptivePriority", _PRIORITY_MAP[urgency])]


# ---------------------------------------------------------------------------
# Lifted helpers — behavior-preserving (signatures verbatim from telegram_bot)
# ---------------------------------------------------------------------------

def build_adaptive_option_order(
    action: str,
    qty: int,
    limit_price: float,
    account_id: str,
    urgency: Urgency = "patient",
) -> ib_async.Order:
    """Build a single-leg adaptive option order."""
    order = ib_async.Order()
    order.action = str(action or "SELL").upper()
    order.totalQuantity = qty
    order.orderType = "LMT"
    order.lmtPrice = round(limit_price, 2)
    order.algoStrategy = "Adaptive"
    order.algoParams = _adaptive_params(urgency)
    order.tif = "DAY"
    order.account = account_id
    order.transmit = True
    return order


def build_adaptive_sell_order(
    qty: int,
    limit_price: float,
    account_id: str,
    urgency: Urgency = "patient",
) -> ib_async.Order:
    """Single-leg adaptive SELL order."""
    return build_adaptive_option_order(
        action="SELL",
        qty=qty,
        limit_price=limit_price,
        account_id=account_id,
        urgency=urgency,
    )


def build_adaptive_roll_combo(
    qty: int,
    limit_price: float,
    account_id: str,
    urgency: Urgency = "patient",
) -> ib_async.Order:
    """IBKR BAG combo order for a Roll.

    Action = BUY executes the legs exactly as defined (Buy 1, Sell 1).
    Positive limit = net debit. Negative limit = net credit.
    Urgency is time-gated by the caller via urgency_policy.decide_roll_urgency.
    """
    order = ib_async.Order()
    order.action = "BUY"
    order.totalQuantity = qty
    order.orderType = "LMT"
    order.lmtPrice = round(limit_price, 2)
    order.algoStrategy = "Adaptive"
    order.algoParams = _adaptive_params(urgency)
    order.tif = "DAY"
    order.account = account_id
    order.transmit = True
    return order


# ---------------------------------------------------------------------------
# NEW — STK LIQUIDATE path (replaces raw MarketOrder/LimitOrder)
# ---------------------------------------------------------------------------

def build_adaptive_stk_order(
    action: str,
    total_quantity: int | float,
    *,
    order_type: Literal["MKT", "LMT"],
    limit_price: float | None = None,
    urgency: Urgency = "patient",
    tif: str = "DAY",
) -> ib_async.Order:
    """Adaptive algo for share orders (STK LIQUIDATE path).

    Replaces raw MarketOrder/LimitOrder at telegram_bot.py:6786-6803 (pre-MR !101).
    Patient default: share liquidations from CC/CSP assignment cycles are not
    time-critical. Urgent available via urgency kwarg (MR !102 will wire this).
    """
    if action not in ("BUY", "SELL"):
        raise ValueError(f"action must be 'BUY' or 'SELL', got {action!r}")
    if total_quantity <= 0:
        raise ValueError(f"total_quantity must be positive, got {total_quantity!r}")
    if order_type == "LMT" and limit_price is None:
        raise ValueError("limit_price required for LMT order")
    if order_type == "MKT" and limit_price is not None:
        raise ValueError("limit_price must be None for MKT order")

    if order_type == "LMT":
        order = ib_async.LimitOrder(action, total_quantity, limit_price, tif=tif)
    elif order_type == "MKT":
        order = ib_async.MarketOrder(action, total_quantity, tif=tif)
    else:
        raise ValueError(f"order_type must be 'MKT' or 'LMT', got {order_type!r}")

    order.algoStrategy = "Adaptive"
    order.algoParams = _adaptive_params(urgency)
    return order
