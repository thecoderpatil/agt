"""Integration: urgency flows from decide_roll_urgency → build_adaptive_roll_combo → Order."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agt_equities.ib_order_builder import build_adaptive_roll_combo
from agt_equities.urgency_policy import decide_roll_urgency

pytestmark = pytest.mark.sprint_a


def test_roll_far_from_expiry_gets_patient_adaptive():
    now = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)
    expiry = now + timedelta(days=7)
    urgency = decide_roll_urgency(expiry, now_dt=now)
    order = build_adaptive_roll_combo(1, 0.50, "U12345", urgency=urgency)
    assert any(tv.tag == "adaptivePriority" and tv.value == "Patient"
               for tv in order.algoParams)


def test_roll_near_expiry_gets_urgent_adaptive():
    now = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    expiry = now + timedelta(hours=1)
    urgency = decide_roll_urgency(expiry, now_dt=now)
    order = build_adaptive_roll_combo(1, 0.50, "U12345", urgency=urgency)
    assert any(tv.tag == "adaptivePriority" and tv.value == "Urgent"
               for tv in order.algoParams)


def test_pending_order_payload_urgency_round_trip():
    """Payload urgency='urgent' propagates to Order algoParams."""
    payload = {"urgency": "urgent"}
    urgency = payload.get("urgency", "patient")
    order = build_adaptive_roll_combo(2, -0.30, "U99999", urgency=urgency)
    assert any(tv.tag == "adaptivePriority" and tv.value == "Urgent"
               for tv in order.algoParams)
