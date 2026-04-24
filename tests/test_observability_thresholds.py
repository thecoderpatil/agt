"""tests/test_observability_thresholds.py — ADR-017 §9 Mega-MR B.

Covers compute_threshold_flags: hybrid absolute + max(5, 3× 7d median) relative
with cold-start skip and absolute-floor guard against median=0 spam.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agt_equities.observability.thresholds import (
    ThresholdFlag,
    compute_threshold_flags,
)

pytestmark = pytest.mark.sprint_a


FIXED_NOW = datetime(2026, 4, 24, 22, 35, tzinfo=timezone.utc)
TODAY_ISO_PREFIX = "2026-04-24T"


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_key TEXT,
            invariant_id TEXT,
            severity TEXT,
            scrutiny_tier TEXT,
            status TEXT,
            detector TEXT,
            detected_at TEXT,
            closed_at TEXT,
            last_action_at TEXT,
            consecutive_breaches INTEGER,
            observed_state TEXT,
            desired_state TEXT,
            confidence REAL,
            mr_iid INTEGER,
            ddiff_url TEXT,
            rejection_history TEXT,
            fault_source TEXT,
            severity_tier INTEGER,
            burn_weight REAL,
            error_budget_tier INTEGER,
            budget_consumed_pct REAL
        );
        CREATE TABLE daemon_heartbeat (
            daemon_name TEXT,
            last_beat_utc TEXT,
            pid INTEGER,
            client_id INTEGER,
            notes TEXT
        );
        CREATE TABLE cross_daemon_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts REAL,
            kind TEXT,
            severity TEXT,
            payload_json TEXT,
            status TEXT,
            sent_ts REAL,
            attempts INTEGER,
            last_error TEXT
        );
        """
    )


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    _schema(conn)
    conn.commit()
    conn.close()
    return str(db)


def _ins_incident(db_path, **fields):
    conn = sqlite3.connect(db_path)
    keys = ",".join(fields.keys())
    qs = ",".join("?" * len(fields))
    conn.execute(f"INSERT INTO incidents ({keys}) VALUES ({qs})", list(fields.values()))
    conn.commit()
    conn.close()


def _ins_heartbeat(db_path, name, last_beat_utc):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid, client_id, notes) "
        "VALUES (?, ?, 1, 1, '')",
        (name, last_beat_utc),
    )
    conn.commit()
    conn.close()


def _ins_alert(db_path, kind, created_ts):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cross_daemon_alerts (kind, severity, payload_json, status, "
        "created_ts, attempts) VALUES (?, 'warn', '{}', 'pending', ?, 0)",
        (kind, created_ts),
    )
    conn.commit()
    conn.close()


def test_absolute_architect_only_fires(tmp_db):
    _ins_incident(tmp_db, incident_key="k1", invariant_id="FAKE_A",
                  scrutiny_tier="architect_only", status="open",
                  detected_at=TODAY_ISO_PREFIX + "10:00:00+00:00",
                  consecutive_breaches=1)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    archs = [f for f in flags if f.source == "architect_only_incident"]
    assert len(archs) == 1
    assert archs[0].kind == "absolute"
    assert archs[0].invariant_id == "FAKE_A"


def test_absolute_tier_0_1_fires(tmp_db):
    _ins_incident(tmp_db, incident_key="k2", invariant_id="FAKE_T0",
                  scrutiny_tier="high", status="open",
                  detected_at=TODAY_ISO_PREFIX + "11:00:00+00:00",
                  consecutive_breaches=1, error_budget_tier=0)
    _ins_incident(tmp_db, incident_key="k3", invariant_id="FAKE_T1",
                  scrutiny_tier="high", status="open",
                  detected_at=TODAY_ISO_PREFIX + "11:10:00+00:00",
                  consecutive_breaches=1, error_budget_tier=1)
    _ins_incident(tmp_db, incident_key="k4", invariant_id="FAKE_T2",
                  scrutiny_tier="low", status="open",
                  detected_at=TODAY_ISO_PREFIX + "11:20:00+00:00",
                  consecutive_breaches=1, error_budget_tier=2)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    tiers = [f for f in flags if f.source == "error_budget_tier"]
    assert len(tiers) == 2
    ids = {f.invariant_id for f in tiers}
    assert ids == {"FAKE_T0", "FAKE_T1"}


def test_absolute_stale_heartbeat_fires(tmp_db):
    # 10 minutes old → stale
    stale = (FIXED_NOW - timedelta(minutes=10)).isoformat()
    fresh = (FIXED_NOW - timedelta(seconds=30)).isoformat()
    _ins_heartbeat(tmp_db, "agt_bot", fresh)
    _ins_heartbeat(tmp_db, "agt_scheduler", stale)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    stale_flags = [f for f in flags if f.source == "stale_heartbeat"]
    assert len(stale_flags) == 1
    assert stale_flags[0].evidence["daemon_name"] == "agt_scheduler"


def test_absolute_flex_empty_suspicious_fires(tmp_db):
    _ins_alert(tmp_db, "FLEX_SYNC_EMPTY_SUSPICIOUS",
               FIXED_NOW.timestamp() - 3600)
    _ins_alert(tmp_db, "SOMETHING_ELSE",
               FIXED_NOW.timestamp() - 3600)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    hits = [f for f in flags if f.source == "flex_empty_suspicious"]
    assert len(hits) == 1


def test_relative_3x_median_fires(tmp_db):
    # 7 prior days with counts [1, 2, 2, 2, 3, 2, 2] → median = 2
    # threshold = max(5, 3 * 2) = 6. Today count 7 > 6 → fires.
    for day in range(7):
        prior = FIXED_NOW - timedelta(days=day + 1)
        count = [1, 2, 2, 2, 3, 2, 2][day]
        for i in range(count):
            _ins_incident(tmp_db, incident_key=f"h_{day}_{i}",
                          invariant_id="SPIKE", scrutiny_tier="low",
                          status="open", detected_at=prior.isoformat(),
                          consecutive_breaches=1)
    for i in range(7):
        _ins_incident(tmp_db, incident_key=f"t_{i}",
                      invariant_id="SPIKE", scrutiny_tier="low",
                      status="open",
                      detected_at=TODAY_ISO_PREFIX + f"{10 + i:02d}:00:00+00:00",
                      consecutive_breaches=1)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    spikes = [f for f in flags if f.source == "invariant_spike"]
    assert len(spikes) == 1
    assert spikes[0].invariant_id == "SPIKE"
    assert spikes[0].evidence["today_count"] == 7


def test_relative_absolute_floor_guard(tmp_db):
    # median=0 (nothing for 7 days has the invariant but we still need
    # >= _COLD_START_MIN_DAYS rows to compute — simulate with 3+ zero-count
    # days by inserting a DIFFERENT invariant each day so TARGET has 0 rows
    # in history but the invariant IS present today. Actually with our
    # history query grouped by invariant_id, TARGET with 0 prior days falls
    # into cold-start and is SKIPPED. So floor guard is tested with an
    # invariant that has median=1 history: floor=5 > 3*1=3.
    for day in range(4):
        prior = FIXED_NOW - timedelta(days=day + 1)
        _ins_incident(tmp_db, incident_key=f"floor_{day}",
                      invariant_id="LOW", scrutiny_tier="low",
                      status="open", detected_at=prior.isoformat(),
                      consecutive_breaches=1)
    # Today: 4 hits. max(5, 3*1)=5. 4 < 5 → no fire.
    for i in range(4):
        _ins_incident(tmp_db, incident_key=f"today_{i}",
                      invariant_id="LOW", scrutiny_tier="low",
                      status="open",
                      detected_at=TODAY_ISO_PREFIX + f"{10 + i:02d}:00:00+00:00",
                      consecutive_breaches=1)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    spikes = [f for f in flags if f.source == "invariant_spike"]
    assert spikes == [], f"floor guard failed: {spikes}"

    # Today 6 hits → max(5, 3)=5 → 6 > 5 → fires.
    for i in range(2):
        _ins_incident(tmp_db, incident_key=f"today_extra_{i}",
                      invariant_id="LOW", scrutiny_tier="low",
                      status="open",
                      detected_at=TODAY_ISO_PREFIX + f"{14 + i:02d}:00:00+00:00",
                      consecutive_breaches=1)
    flags2 = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    spikes2 = [f for f in flags2 if f.source == "invariant_spike"]
    assert len(spikes2) == 1
    assert spikes2[0].invariant_id == "LOW"


def test_relative_cold_start_skip(tmp_db):
    # Only 2 prior days of history for COLD; today has 100 hits. Must be
    # skipped (cold-start) — no relative flag.
    for day in range(2):
        prior = FIXED_NOW - timedelta(days=day + 1)
        _ins_incident(tmp_db, incident_key=f"c_{day}", invariant_id="COLD",
                      scrutiny_tier="low", status="open",
                      detected_at=prior.isoformat(),
                      consecutive_breaches=1)
    for i in range(100):
        _ins_incident(tmp_db, incident_key=f"ct_{i}", invariant_id="COLD",
                      scrutiny_tier="low", status="open",
                      detected_at=TODAY_ISO_PREFIX + f"{10 + (i % 10):02d}:"
                                                    f"{i % 60:02d}:00+00:00",
                      consecutive_breaches=1)
    flags = compute_threshold_flags(db_path=tmp_db, for_date=FIXED_NOW)
    spikes = [f for f in flags if f.source == "invariant_spike"
              and f.invariant_id == "COLD"]
    assert spikes == []
