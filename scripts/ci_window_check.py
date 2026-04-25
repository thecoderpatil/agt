"""Phase A piece 4 — CI sensitive-window block.

Refuses to start CI during RTH (09:30-16:00 ET) and the Flex sync
window (16:55-17:15 ET). Defense-in-depth: even if containment ACLs
fail, CI cannot run during live trading or Flex sync windows.

Failure exits with code 1 + message on stderr. Success is silent.

Override: set AGT_CI_OUTSIDE_HOURS_OVERRIDE=true to bypass — for use
ONLY by maintainers running explicit out-of-band CI (e.g. emergency
hotfix). The override is logged to stdout.

Sentinel: RTH window block. Sentinel: Flex window block.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

RTH_START_HOUR_ET = 9
RTH_START_MIN_ET = 30
RTH_END_HOUR_ET = 16
RTH_END_MIN_ET = 0

FLEX_WINDOW_START_HOUR_ET = 16
FLEX_WINDOW_START_MIN_ET = 55
FLEX_WINDOW_END_HOUR_ET = 17
FLEX_WINDOW_END_MIN_ET = 15


def is_sensitive_window(now_et: datetime) -> tuple[bool, str]:
    """Return (is_sensitive, window_name) for a datetime in US/Eastern.

    Window name is one of: 'RTH window block', 'Flex window block',
    'off-hours', 'weekend'.
    """
    h, m = now_et.hour, now_et.minute
    minutes = h * 60 + m
    rth_start = RTH_START_HOUR_ET * 60 + RTH_START_MIN_ET
    rth_end = RTH_END_HOUR_ET * 60 + RTH_END_MIN_ET
    flex_start = FLEX_WINDOW_START_HOUR_ET * 60 + FLEX_WINDOW_START_MIN_ET
    flex_end = FLEX_WINDOW_END_HOUR_ET * 60 + FLEX_WINDOW_END_MIN_ET

    weekday = now_et.weekday()  # Mon=0, Sun=6
    if weekday >= 5:
        return False, "weekend"
    if rth_start <= minutes < rth_end:
        return True, "RTH window block"
    if flex_start <= minutes < flex_end:
        return True, "Flex window block"
    return False, "off-hours"


def main() -> int:
    if os.environ.get("AGT_CI_OUTSIDE_HOURS_OVERRIDE", "").lower() == "true":
        print("CI WINDOW: override active (AGT_CI_OUTSIDE_HOURS_OVERRIDE=true) — proceeding")
        return 0
    now_et = datetime.now(ZoneInfo("US/Eastern"))
    is_sensitive, window = is_sensitive_window(now_et)
    if is_sensitive:
        print(
            f"CI WINDOW BLOCK: now={now_et.isoformat()} window={window!r} — "
            f"CI cannot run during live trading or Flex sync windows. "
            f"Set AGT_CI_OUTSIDE_HOURS_OVERRIDE=true to bypass (emergency only).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
