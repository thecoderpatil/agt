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

    # Worsening check: actual went past baseline in the wrong direction
    is_reduction = gp.baseline_value > gp.target_value
    if is_reduction:
        worsened = actual_value > gp.baseline_value
        behind = delta > 0  # actual higher than expected for reduction
    else:
        worsened = actual_value < gp.baseline_value
        behind = delta < 0

    if worsened or (behind and abs(delta) >= two_weeks_worth):
        status = "RED"
    elif behind and abs(delta) > 0:
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
            )
            for r in rows
        ]
    except Exception:
        return []
