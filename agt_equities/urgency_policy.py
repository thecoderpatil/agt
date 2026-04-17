"""Urgency policy — centralized decision logic for Adaptive algo priority.

Patient is the default for AGT order placement. Urgent is reserved for
paths where fill certainty dominates price improvement:
- Expiring rolls (DTE <= N_URGENT_TRADING_HOURS from decision time)
- Defensive closes on breaker trip (explicit payload field — MR !104)
- DTE=0 ITM assignment-avoidance (MR !103 or later)

This module is a pure decision layer — no IB calls, no DB reads.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

Urgency = Literal["patient", "urgent"]

# Trigger threshold: a roll placed within this many wall-clock hours of the
# underlying short's expiry goes Urgent. Outside the window, Patient —
# IB will work the spread. True trading-hours math (skipping overnight /
# weekend) is a future refinement.
N_URGENT_TRADING_HOURS = 2.0


def decide_roll_urgency(
    expiry_dt: datetime,
    now_dt: datetime | None = None,
) -> Urgency:
    """Return urgency for a roll order based on time-to-expiry.

    Args:
        expiry_dt: the underlying short option's expiration datetime.
            For equity options use 20:00 UTC (= 16:00 ET market close).
        now_dt: decision time (default: utcnow). Injectable for tests.

    Returns:
        "urgent" if within N_URGENT_TRADING_HOURS of expiry_dt,
        "patient" otherwise.
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)

    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    delta_hours = (expiry_dt - now_dt).total_seconds() / 3600.0

    if delta_hours <= N_URGENT_TRADING_HOURS:
        return "urgent"
    return "patient"
