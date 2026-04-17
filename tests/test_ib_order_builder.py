"""Tests for ib_order_builder — Adaptive algo helpers."""
from __future__ import annotations

import pytest

from agt_equities.ib_order_builder import (
    build_adaptive_option_order,
    build_adaptive_roll_combo,
    build_adaptive_sell_order,
    build_adaptive_stk_order,
)

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Lifted-helper sanity (behavior-preserving regression coverage)
# ---------------------------------------------------------------------------

def test_option_helper_patient_default():
    """Lifted option helper still produces Patient + Adaptive."""
    o = build_adaptive_option_order("SELL", 2, 2.50, "U12345")
    assert o.algoStrategy == "Adaptive"
    assert any(tv.tag == "adaptivePriority" and tv.value == "Patient"
               for tv in o.algoParams)
    assert o.orderType == "LMT"
    assert o.lmtPrice == 2.50
    assert o.account == "U12345"
    assert o.transmit is True


def test_sell_helper_delegates_to_option_helper():
    """Sell helper is a thin wrapper — same Adaptive/Patient wiring."""
    o = build_adaptive_sell_order(3, 1.75, "U99999")
    assert o.action == "SELL"
    assert o.algoStrategy == "Adaptive"
    assert any(tv.tag == "adaptivePriority" and tv.value == "Patient"
               for tv in o.algoParams)


def test_roll_combo_patient_default():
    """Roll combo defaults to Patient after MR !103 urgency routing."""
    o = build_adaptive_roll_combo(1, 0.50, "U12345")
    assert o.algoStrategy == "Adaptive"
    assert any(tv.tag == "adaptivePriority" and tv.value == "Patient"
               for tv in o.algoParams)
    assert o.action == "BUY"
    assert o.orderType == "LMT"


def test_roll_combo_urgent_override():
    """Caller can override to urgent (time-gate path)."""
    o = build_adaptive_roll_combo(1, 0.50, "U12345", urgency="urgent")
    assert any(tv.tag == "adaptivePriority" and tv.value == "Urgent"
               for tv in o.algoParams)


# ---------------------------------------------------------------------------
# New STK helper
# ---------------------------------------------------------------------------

def test_stk_lmt_patient_default():
    o = build_adaptive_stk_order("SELL", 100, order_type="LMT", limit_price=150.25)
    assert o.orderType == "LMT"
    assert o.lmtPrice == 150.25
    assert o.algoStrategy == "Adaptive"
    assert any(tv.tag == "adaptivePriority" and tv.value == "Patient"
               for tv in o.algoParams)


def test_stk_mkt_patient_default():
    o = build_adaptive_stk_order("BUY", 50, order_type="MKT")
    assert o.orderType == "MKT"
    assert o.algoStrategy == "Adaptive"
    assert any(tv.tag == "adaptivePriority" and tv.value == "Patient"
               for tv in o.algoParams)


def test_stk_urgent_override():
    o = build_adaptive_stk_order("SELL", 25, order_type="LMT",
                                  limit_price=10.0, urgency="urgent")
    assert any(tv.tag == "adaptivePriority" and tv.value == "Urgent"
               for tv in o.algoParams)


def test_stk_validation_errors():
    with pytest.raises(ValueError, match="action must be"):
        build_adaptive_stk_order("HOLD", 1, order_type="MKT")
    with pytest.raises(ValueError, match="total_quantity"):
        build_adaptive_stk_order("BUY", 0, order_type="MKT")
    with pytest.raises(ValueError, match="limit_price required"):
        build_adaptive_stk_order("BUY", 1, order_type="LMT")
    with pytest.raises(ValueError, match="limit_price must be None"):
        build_adaptive_stk_order("BUY", 1, order_type="MKT", limit_price=5.0)
    with pytest.raises(ValueError, match="urgency must be"):
        build_adaptive_stk_order("BUY", 1, order_type="MKT", urgency="yolo")  # type: ignore[arg-type]
