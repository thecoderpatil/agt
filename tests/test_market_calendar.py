"""market_calendar.is_trading_day -- weekend, holiday, observed-day handling."""
from __future__ import annotations

import datetime as _dt

import pytest

from agt_equities.market_calendar import US_MARKET_HOLIDAYS, is_trading_day

pytestmark = pytest.mark.sprint_a


@pytest.mark.parametrize("d", [
    _dt.date(2026, 4, 27),  # Mon
    _dt.date(2026, 4, 28),  # Tue
    _dt.date(2026, 4, 29),  # Wed
    _dt.date(2026, 4, 30),  # Thu
    _dt.date(2026, 5, 1),   # Fri
    _dt.date(2027, 8, 23),  # Mon
])
def test_weekdays_are_trading_days(d):
    assert is_trading_day(d) is True


@pytest.mark.parametrize("d", [
    _dt.date(2026, 4, 25),  # Sat
    _dt.date(2026, 4, 26),  # Sun
    _dt.date(2027, 8, 22),  # Sun
    _dt.date(2027, 8, 21),  # Sat
])
def test_weekends_are_not_trading_days(d):
    assert is_trading_day(d) is False


@pytest.mark.parametrize("d", sorted(US_MARKET_HOLIDAYS))
def test_holidays_are_not_trading_days(d):
    assert is_trading_day(d) is False


def test_independence_day_observed_2026():
    # 7/4/2026 is Saturday -> observed Friday 7/3 is full closure
    assert is_trading_day(_dt.date(2026, 7, 3)) is False
    # 7/4 itself is a Saturday -> not a trading day regardless
    assert is_trading_day(_dt.date(2026, 7, 4)) is False


def test_independence_day_observed_2027():
    # 7/4/2027 is Sunday -> observed Monday 7/5 is full closure
    assert is_trading_day(_dt.date(2027, 7, 5)) is False


def test_2026_holiday_count():
    holidays_2026 = [d for d in US_MARKET_HOLIDAYS if d.year == 2026]
    assert len(holidays_2026) == 10


def test_2027_holiday_count():
    holidays_2027 = [d for d in US_MARKET_HOLIDAYS if d.year == 2027]
    assert len(holidays_2027) == 10
