"""
agt_equities/mode_engine.py — 3-mode state engine for desk operations.

Modes: PEACETIME / AMBER / WARTIME
Computed from rule evaluations against glide paths.
Transitions logged to mode_history table (Bucket 3).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

logger = logging.getLogger(__name__)

MODE_PEACETIME = "PEACETIME"
MODE_AMBER = "AMBER"
MODE_WARTIME = "WARTIME"

# Status priority for computing mode from worst rule status
_STATUS_PRIORITY = {"GREEN": 0, "PENDING": 0, "AMBER": 1, "RED": 2}
_MODE_FROM_WORST = {0: MODE_PEACETIME, 1: MODE_AMBER, 2: MODE_WARTIME}


# ---------------------------------------------------------------------------
# Leverage hysteresis tracker (stateful, lives in mode engine layer)
# ---------------------------------------------------------------------------

@dataclass
class LeverageHysteresisTracker:
    """Tracks per-household leverage breach state for hysteresis.

    Once breached (≥1.50), stays breached until leverage drops below
    release threshold (1.40). This prevents flip-flopping at the boundary.
    """
    breach_state: dict[str, bool]
    breach_threshold: float = 1.50
    release_threshold: float = 1.40

    def update(self, household: str, leverage: float) -> str:
        """Update hysteresis state and return effective status."""
        was_breached = self.breach_state.get(household, False)
        if leverage >= self.breach_threshold:
            self.breach_state[household] = True
            return "BREACHED"
        elif was_breached and leverage >= self.release_threshold:
            return "BREACHED"  # hysteresis zone
        else:
            self.breach_state[household] = False
            if leverage >= 1.30:
                return "AMBER"
            return "OK"


# ---------------------------------------------------------------------------
# Glide path evaluation
# ---------------------------------------------------------------------------

# Glide path noise tolerance per rule.
# Phase 3A.5a triage 2026-04-07: sub-percent intraday NLV drift was
# triggering 'worsened' status on Day 0 baselines, blocking PEACETIME.
# These tolerances reject measurement noise without masking real
# trajectory deterioration. Tolerances are FLAT ABSOLUTE values, not
# percentage of baseline, because the noise floor of each metric type
# is approximately constant regardless of baseline magnitude.
# Worsened only fires when actual exceeds baseline by MORE than tolerance.
GLIDE_PATH_TOLERANCE: dict[str, float] = {
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

    from datetime import date as _date
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


# ---------------------------------------------------------------------------
# Mode computation
# ---------------------------------------------------------------------------

def compute_mode(rule_evaluations: list) -> tuple[str, str | None, str | None, float | None]:
    """Compute desk mode from rule evaluations.

    Returns (mode, trigger_rule, trigger_household, trigger_value).
    PENDING rules are treated as GREEN (not yet evaluable).
    """
    worst_priority = 0
    trigger_rule = None
    trigger_household = None
    trigger_value = None

    for ev in rule_evaluations:
        p = _STATUS_PRIORITY.get(ev.status, 0)
        if p > worst_priority:
            worst_priority = p
            trigger_rule = ev.rule_id
            trigger_household = ev.household
            trigger_value = ev.raw_value

    mode = _MODE_FROM_WORST.get(worst_priority, MODE_PEACETIME)
    return mode, trigger_rule, trigger_household, trigger_value


# ---------------------------------------------------------------------------
# Mode transition logging
# ---------------------------------------------------------------------------

def log_mode_transition(
    conn: sqlite3.Connection,
    old_mode: str,
    new_mode: str,
    trigger_rule: str | None = None,
    trigger_household: str | None = None,
    trigger_value: float | None = None,
    notes: str | None = None,
) -> None:
    """Write a mode transition to mode_history table. Idempotent."""
    try:
        conn.execute(
            "INSERT INTO mode_history "
            "(timestamp, old_mode, new_mode, trigger_rule, trigger_household, trigger_value, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), old_mode, new_mode,
             trigger_rule, trigger_household, trigger_value, notes),
        )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to log mode transition: %s", exc)


def get_current_mode(conn: sqlite3.Connection) -> str:
    """Read the most recent mode from mode_history. Defaults to PEACETIME."""
    try:
        row = conn.execute(
            "SELECT new_mode FROM mode_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row[0] if isinstance(row, tuple) else row["new_mode"]
    except Exception:
        pass
    return MODE_PEACETIME


def get_recent_transitions(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Read recent mode transitions."""
    try:
        rows = conn.execute(
            "SELECT * FROM mode_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Programmatic WARTIME definition (Gemini Q10 + ADR-004)
# ---------------------------------------------------------------------------

def is_wartime_condition_met(
    yash_leverage: float,
    vikram_leverage: float,
    vikram_el_ratio: float,
) -> tuple[bool, list[str]]:
    """Programmatic WARTIME definition per Gemini Q10 + ADR-004.

    Returns (is_wartime, reasons_list).

    DOCUMENTATION ONLY in 3A.5c2-alpha. This function is callable
    and tested but is NOT wired to automatic mode flips. Manual
    /declare_wartime remains the only entry path until Phase 3B
    automated mode pipeline.
    """
    reasons: list[str] = []
    if yash_leverage > 1.50 or vikram_leverage > 1.50:
        reasons.append(
            f"Leverage breach: Yash {yash_leverage:.2f}x, Vik {vikram_leverage:.2f}x"
        )
    if vikram_el_ratio < 0.15:
        reasons.append(f"Vikram EL critical: {vikram_el_ratio:.1%}")
    return (len(reasons) > 0, reasons)


# ---------------------------------------------------------------------------
# Glide path DB helpers
# ---------------------------------------------------------------------------

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
