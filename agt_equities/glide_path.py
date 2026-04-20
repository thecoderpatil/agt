"""Glide-path evaluation for Rule 2 / Rule 6 / Rule 11 trajectory tracking.

Extracted from mode_engine.py during ADR-014 mode-state-machine retirement.
Glide-path logic is rule-centric, not mode-centric.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date as _date


# Glide path noise tolerance per rule.
# Phase 3A.5a triage 2026-04-07: sub-percent intraday NLV drift was
# triggering 'worsened' status on Day 0 baselines, blocking PEACETIME.
# These tolerances reject measurement noise without masking real
# trajectory deterioration. Tolerances are FLAT ABSOLUTE values, not
# percentage of baseline, because the noise floor of each metric type
# is approximately constant regardless of baseline magnitude.
# Worsened only fires when actual exceeds baseline by MORE than tolerance.
GLIDE_PATH_TOLERANCE = {  # type: dict[str, float]
    "rule_1":  0.01,   # 1 percentage point on concentration ratio
    "rule_2":  0.01,   # 1 percentage point on EL retention ratio
    "rule_4":  0.02,   # 2 basis points on correlation
    "rule_6":  0.01,   # 1 percentage point on Vikram EL ratio
    "rule_11": 0.02,   # 2 basis points of leverage
}


@dataclass(frozen=True)
class GlidePath:
    """A single glide path record from the glide_paths table."""
    household_id: str
    rule_id: str
    ticker: str | None
    baseline_value: float
    target_value: float
    start_date: str          # YYYY-MM-DD
    target_date: str         # YYYY-MM-DD
    pause_conditions: str | None  # JSON or None
    accelerator_clause: str | None = None  # Phase 3A.5a: e.g. "thesis_deterioration"


def evaluate_glide_path(
    gp: GlidePath, actual_value: float, as_of_date: str,
) -> tuple[str, float, float]:
    """Evaluate actual vs glide path expected value.

    Returns (status, expected_today, delta).
    For reduction targets (baseline > target): behind means actual > expected.
    """
    # Check pause conditions
    if gp.pause_conditions:
        try:
            pause = json.loads(gp.pause_conditions)
            if pause.get("paused"):
                return "GREEN", gp.baseline_value, 0.0  # paused = always green
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        start = _date.fromisoformat(gp.start_date)
        target = _date.fromisoformat(gp.target_date)
        today = _date.fromisoformat(as_of_date)
    except ValueError:
        return "GREEN", gp.baseline_value, 0.0  # can't parse dates, assume ok

    total_days = (target - start).days
    if total_days <= 0:
        return "GREEN", gp.target_value, 0.0

    days_elapsed = max(0, (today - start).days)
    progress = min(days_elapsed / total_days, 1.0)

    expected_today = gp.baseline_value + (gp.target_value - gp.baseline_value) * progress

    # Delta: how far behind schedule
    # For reduction targets (baseline > target): actual > expected means behind
    delta = actual_value - expected_today

    # Weekly rate of progress
    weekly_rate = abs(gp.target_value - gp.baseline_value) / total_days * 7
    two_weeks_worth = weekly_rate * 2

    # Tolerance: per-rule flat absolute noise rejection band.
    # Applied symmetrically to BOTH worsened (RED) and behind (AMBER) checks.
    # A sub-tolerance drift in either direction is NOT a mode transition.
    # Phase 3A.5a triage 2026-04-07: intraday NLV drift was triggering
    # mode transitions on Day 0. Tolerance is noise rejection, not leniency.
    tolerance = GLIDE_PATH_TOLERANCE.get(gp.rule_id, 0.01)
    is_reduction = gp.baseline_value > gp.target_value

    if is_reduction:
        # Must decrease. Worsened = rose above (baseline + tolerance).
        worsened = actual_value > (gp.baseline_value + tolerance)
        # Behind = actual > (expected + tolerance). Sub-tolerance drift = on track.
        behind = actual_value > (expected_today + tolerance + 1e-9)
    else:
        # Must increase. Worsened = dropped below (baseline - tolerance).
        worsened = actual_value < (gp.baseline_value - tolerance)
        # Behind = actual < (expected - tolerance). Sub-tolerance drift = on track.
        behind = actual_value < (expected_today - tolerance - 1e-9)

    # WORSENED takes precedence over BEHIND when both fire.
    if worsened:
        status = "RED"
    elif behind:
        status = "AMBER"
    else:
        status = "GREEN"

    return status, expected_today, delta


def load_glide_paths(conn: sqlite3.Connection) -> list[GlidePath]:
    """Load all glide paths from DB."""
    try:
        rows = conn.execute("SELECT * FROM glide_paths").fetchall()
        return [
            GlidePath(
                household_id=r["household_id"],
                rule_id=r["rule_id"],
                ticker=r["ticker"],
                baseline_value=float(r["baseline_value"]),
                target_value=float(r["target_value"]),
                start_date=r["start_date"],
                target_date=r["target_date"],
                pause_conditions=r["pause_conditions"],
                accelerator_clause=r["accelerator_clause"] if "accelerator_clause" in r.keys() else None,
            )
            for r in rows
        ]
    except Exception:
        return []
