"""Tests for urgency_policy.decide_roll_urgency."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agt_equities.urgency_policy import (
    N_URGENT_TRADING_HOURS,
    decide_roll_urgency,
)

pytestmark = pytest.mark.sprint_a


def test_far_from_expiry_is_patient():
    now = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)
    expiry = datetime(2026, 4, 24, 20, 0, tzinfo=timezone.utc)
    assert decide_roll_urgency(expiry, now_dt=now) == "patient"


def test_just_inside_window_is_urgent():
    now = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    expiry = now + timedelta(hours=N_URGENT_TRADING_HOURS - 0.1)
    assert decide_roll_urgency(expiry, now_dt=now) == "urgent"


def test_exactly_at_threshold_is_urgent():
    """Boundary: exactly N_URGENT_TRADING_HOURS out is urgent (<=)."""
    now = datetime(2026, 4, 17, 14, 0, tzinfo=timezone.utc)
    expiry = now + timedelta(hours=N_URGENT_TRADING_HOURS)
    assert decide_roll_urgency(expiry, now_dt=now) == "urgent"


def test_just_outside_window_is_patient():
    now = datetime(2026, 4, 17, 14, 0, tzinfo=timezone.utc)
    expiry = now + timedelta(hours=N_URGENT_TRADING_HOURS + 0.1)
    assert decide_roll_urgency(expiry, now_dt=now) == "patient"


def test_already_expired_is_urgent():
    """Expired (negative delta) — assignment-imminent, always urgent."""
    now = datetime(2026, 4, 17, 16, 30, tzinfo=timezone.utc)
    expiry = datetime(2026, 4, 17, 16, 0, tzinfo=timezone.utc)
    assert decide_roll_urgency(expiry, now_dt=now) == "urgent"


def test_naive_datetime_assumed_utc():
    """Naive tzinfo is treated as UTC (conservative)."""
    now = datetime(2026, 4, 17, 14, 0)
    expiry = datetime(2026, 4, 17, 15, 0)
    assert decide_roll_urgency(expiry, now_dt=now) == "urgent"


def test_defaults_to_now_when_now_dt_none():
    """No crash with default now_dt; 30-day-out expiry is patient."""
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    assert decide_roll_urgency(expiry) == "patient"
