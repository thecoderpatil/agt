"""US equity-market trading-day calendar -- full closures only.

Coverage: 2026-2027. Maintenance: extend US_MARKET_HOLIDAYS by year.
Does NOT cover early-close days (Black Friday half-day, Christmas Eve);
those count as full trading days for proof-report purposes.
"""
from __future__ import annotations

import datetime as _dt

US_MARKET_HOLIDAYS: frozenset[_dt.date] = frozenset({
    # 2026
    _dt.date(2026, 1, 1),    # New Year's Day
    _dt.date(2026, 1, 19),   # MLK Day
    _dt.date(2026, 2, 16),   # Presidents Day
    _dt.date(2026, 4, 3),    # Good Friday
    _dt.date(2026, 5, 25),   # Memorial Day
    _dt.date(2026, 6, 19),   # Juneteenth
    _dt.date(2026, 7, 3),    # Independence Day observed (7/4 is Sat)
    _dt.date(2026, 9, 7),    # Labor Day
    _dt.date(2026, 11, 26),  # Thanksgiving
    _dt.date(2026, 12, 25),  # Christmas
    # 2027
    _dt.date(2027, 1, 1),
    _dt.date(2027, 1, 18),
    _dt.date(2027, 2, 15),
    _dt.date(2027, 3, 26),   # Good Friday
    _dt.date(2027, 5, 31),
    _dt.date(2027, 6, 18),   # Juneteenth observed (6/19 is Sat)
    _dt.date(2027, 7, 5),    # Independence Day observed (7/4 is Sun)
    _dt.date(2027, 9, 6),
    _dt.date(2027, 11, 25),
    _dt.date(2027, 12, 24),  # Christmas observed (12/25 is Sat)
})


def is_trading_day(d: _dt.date) -> bool:
    """True if d is a US equity-market trading day (weekday + not full-close holiday)."""
    if d.weekday() >= 5:
        return False
    return d not in US_MARKET_HOLIDAYS


__all__ = ["US_MARKET_HOLIDAYS", "is_trading_day"]
