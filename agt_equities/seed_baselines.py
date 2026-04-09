"""
agt_equities/seed_baselines.py — Idempotent baseline seed for glide paths,
sector overrides, and initial mode history.

Safe to run multiple times (UPSERT via INSERT OR REPLACE on UNIQUE constraints).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

logger = logging.getLogger(__name__)

START_DATE = "2026-04-07"


def seed_glide_paths(conn: sqlite3.Connection) -> int:
    """Insert baseline glide paths. Idempotent via UNIQUE constraint."""
    paths = [
        # Rule 11: Leverage
        ("Yash_Household", "rule_11", None, 1.60, 1.50, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=4)).isoformat(), None,
         "Gross notional leverage, beta=1.0"),
        ("Vikram_Household", "rule_11", None, 2.17, 1.50, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=12)).isoformat(), None,
         "Gross notional leverage, beta=1.0"),

        # Rule 1: Concentration
        ("Yash_Household", "rule_1", "ADBE", 46.7, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         '{"earnings_pause_days": 5}', "ADBE concentration reduction"),
        ("Vikram_Household", "rule_1", "ADBE", 60.5, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         '{"earnings_pause_days": 5}', "ADBE concentration reduction"),
        ("Yash_Household", "rule_1", "PYPL", 39.9, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         '{"paused": true, "reason": "earnings-gated"}', "PYPL paused until earnings"),
        ("Vikram_Household", "rule_1", "PYPL", 45.0, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         '{"paused": true, "reason": "earnings-gated"}', "PYPL paused until earnings"),
        ("Yash_Household", "rule_1", "MSFT", 28.5, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         None, "MSFT concentration reduction"),
        ("Vikram_Household", "rule_1", "MSFT", 46.2, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         None, "MSFT concentration reduction"),
        ("Vikram_Household", "rule_1", "UBER", 26.8, 25.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         None, "UBER concentration reduction"),
        ("Vikram_Household", "rule_1", "CRM", 22.9, 20.0, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(),
         None, "CRM concentration reduction"),

        # Rule 3: Sector (Software - Application has 3 tickers — after UBER override, should clear)
        # No glide path needed for Rule 3 — UBER sector override fixes it immediately.

        # Rule 2: EL Retention (Phase 3A.5a triage — breaches surfaced by denominator fix)
        # R2 glide paths DECOUPLED from R11 by design. R11 cures fast (assignment-driven
        # exposure reduction). R2 cures slow (cash accumulation at reduced deployment velocity).
        # End-of-Q4-2026 target per Architect decision 2026-04-07.
        ("Vikram_Household", "rule_2", None, 0.542, 0.70, START_DATE,
         "2026-12-31", None,
         "R2 EL retention 54.2% vs 70% required at VIX 22. DECOUPLED from R11 12w glide. "
         "Cures via cash accumulation at reduced redeployment velocity over 38 weeks.",
         "thesis_deterioration"),
        ("Yash_Household", "rule_2", None, 0.421, 0.70, START_DATE,
         "2026-12-31", None,
         "R2 EL retention 42.1% vs 70% required at VIX 22. Known heavy deployment posture. "
         "DECOUPLED from R11 4w glide. Cures via sustained reduced deployment velocity.",
         "thesis_deterioration"),

        # Rule 4: Pairwise Correlation — ADBE-CRM
        # ticker=None because R4 evaluations are pair-level (ev.ticker is None).
        # The first R4 eval alphabetically is ADBE-CRM (the breaching pair).
        # Linked to ADBE concentration 20w glide — correlation cures as
        # concentration cures (both Software-Application).
        ("Yash_Household", "rule_4", None, 0.6915, 0.55, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(), None,
         "ADBE-CRM pairwise correlation 0.69 > 0.60 limit. Both Software-Application. "
         "Linked to ADBE concentration 20w glide. Resolves as ADBE rotates to target."),
        ("Vikram_Household", "rule_4", None, 0.6915, 0.55, START_DATE,
         (date.fromisoformat(START_DATE) + timedelta(weeks=20)).isoformat(), None,
         "ADBE-CRM pairwise correlation 0.69 > 0.60 limit. Both Software-Application. "
         "Linked to ADBE concentration 20w glide."),
    ]

    inserted = 0
    for row in paths:
        # Support 9-element (legacy) or 10-element (with accelerator_clause) tuples
        if len(row) == 9:
            hh, rule, ticker, baseline, target, start, target_dt, pause, notes = row
            accel = None
        else:
            hh, rule, ticker, baseline, target, start, target_dt, pause, notes, accel = row
        # Sprint 1F Fix 9: SQLite UNIQUE doesn't match NULL=NULL.
        # For NULL-ticker rows, explicitly DELETE before INSERT to prevent duplicates.
        if ticker is None:
            conn.execute(
                "DELETE FROM glide_paths WHERE household_id = ? AND rule_id = ? AND ticker IS NULL",
                (hh, rule),
            )
        conn.execute(
            "INSERT OR REPLACE INTO glide_paths "
            "(household_id, rule_id, ticker, baseline_value, target_value, "
            "start_date, target_date, pause_conditions, notes, accelerator_clause) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hh, rule, ticker, baseline, target, start, target_dt, pause, notes, accel),
        )
        inserted += 1

    conn.commit()
    logger.info("Seeded %d glide paths", inserted)
    return inserted


def seed_sector_overrides(conn: sqlite3.Connection) -> int:
    """Insert sector overrides. Idempotent via PRIMARY KEY."""
    overrides = [
        ("UBER", "Consumer Cyclical", "Travel Services", "manual",
         "Yahoo/GICS misclassification — UBER is ride-hailing, not Software"),
    ]
    inserted = 0
    for (ticker, sector, sub, source, notes) in overrides:
        conn.execute(
            "INSERT OR REPLACE INTO sector_overrides "
            "(ticker, sector, sub_sector, source, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, sector, sub, source, notes),
        )
        inserted += 1
    conn.commit()
    logger.info("Seeded %d sector overrides", inserted)
    return inserted


def seed_initial_mode(conn: sqlite3.Connection) -> None:
    """Insert initial PEACETIME mode if mode_history is empty."""
    row = conn.execute("SELECT COUNT(*) FROM mode_history").fetchone()
    if row[0] == 0:
        conn.execute(
            "INSERT INTO mode_history (old_mode, new_mode, notes) "
            "VALUES ('PEACETIME', 'PEACETIME', 'Phase 3A Day 1 initialization')"
        )
        conn.commit()
        logger.info("Seeded initial PEACETIME mode")


def seed_all(conn: sqlite3.Connection) -> None:
    """Run all seed operations."""
    seed_glide_paths(conn)
    seed_sector_overrides(conn)
    seed_initial_mode(conn)
