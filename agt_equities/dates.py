"""
agt_equities.dates — timezone-aware date helpers.

US equity markets operate on Eastern Time.  Using ``date.today()``
(which returns the *system-local* date) for DTE math and expiry
filtering causes off-by-one errors when the bot runs on a UTC host:
after 20:00 ET (00:00 UTC), ``date.today()`` returns tomorrow's date
in UTC, miscounting DTE and potentially filtering out valid same-day
expirations.

This module provides ``et_today()`` as the canonical replacement for
``date.today()`` in all DTE-math and expiry-comparison code paths.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def et_today() -> date:
    """Return today's date in US Eastern Time.

    Drop-in replacement for ``date.today()`` in DTE calculations,
    expiry filtering, and any other context where "today" means
    "today for the US equity market."
    """
    return datetime.now(_ET).date()
