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
    ]

    inserted = 0
    for (hh, rule, ticker, baseline, target, start, target_dt, pause, notes) in paths:
        conn.execute(
            "INSERT OR REPLACE INTO glide_paths "
            "(household_id, rule_id, ticker, baseline_value, target_value, "
            "start_date, target_date, pause_conditions, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hh, rule, ticker, baseline, target, start, target_dt, pause, notes),
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
