"""observability.thresholds — hybrid absolute + relative threshold engine.

ADR-017 §3 + §9 Mega-MR B. Produces a list of ThresholdFlag that the
A.1 digest renderer prints inline in the authorable section.

Absolute triggers (always fire):
  - any architect-only incident today
  - any incidents.error_budget_tier IN (0, 1) today (canonical ADR-013)
  - any daemon_heartbeat row with age_seconds > 180
  - any cross_daemon_alerts row with kind='FLEX_SYNC_EMPTY_SUSPICIOUS' today

Relative triggers (per invariant_id):
  - today_count > max(5, 3 * trailing_7d_median) → flag
  - cold-start: fewer than 3 prior-day samples → skip

Read-only. Never writes. Never raises out — individual DB errors
downgrade that specific trigger.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from agt_equities.db import get_ro_connection

logger = logging.getLogger(__name__)

_HEARTBEAT_STALE_SECONDS: float = 180.0
_RELATIVE_FLOOR: int = 5
_RELATIVE_MULTIPLIER: float = 3.0
_COLD_START_MIN_DAYS: int = 3
_TRAILING_WINDOW_DAYS: int = 7

FlagKind = Literal["absolute", "relative"]


@dataclass(frozen=True)
class ThresholdFlag:
    kind: FlagKind
    source: str
    invariant_id: str | None
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _day_bounds_utc(for_date: datetime) -> tuple[str, str]:
    start = datetime(for_date.year, for_date.month, for_date.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _absolute_architect_only(conn, today_start: str, today_end: str) -> list[ThresholdFlag]:
    try:
        rows = conn.execute(
            "SELECT id, invariant_id, scrutiny_tier, status FROM incidents "
            "WHERE scrutiny_tier = 'architect_only' "
            "AND detected_at >= ? AND detected_at < ?",
            (today_start, today_end),
        ).fetchall()
    except Exception as exc:
        logger.warning("thresholds: architect_only query failed: %s", exc)
        return []
    out: list[ThresholdFlag] = []
    for r in rows:
        out.append(ThresholdFlag(
            kind="absolute",
            source="architect_only_incident",
            invariant_id=r[1],
            message=f"architect_only incident id={r[0]} invariant={r[1]} status={r[3]}",
            evidence={"incident_id": r[0]},
        ))
    return out


def _absolute_error_budget_tier_0_1(conn, today_start: str, today_end: str) -> list[ThresholdFlag]:
    try:
        rows = conn.execute(
            "SELECT id, invariant_id, error_budget_tier, status FROM incidents "
            "WHERE error_budget_tier IN (0, 1) "
            "AND detected_at >= ? AND detected_at < ?",
            (today_start, today_end),
        ).fetchall()
    except Exception as exc:
        logger.warning("thresholds: tier_0_1 query failed: %s", exc)
        return []
    out: list[ThresholdFlag] = []
    for r in rows:
        out.append(ThresholdFlag(
            kind="absolute",
            source="error_budget_tier",
            invariant_id=r[1],
            message=f"tier-{r[2]} incident id={r[0]} invariant={r[1]} status={r[3]}",
            evidence={"incident_id": r[0], "tier": r[2]},
        ))
    return out


def _absolute_stale_heartbeat(conn, now_utc: datetime) -> list[ThresholdFlag]:
    try:
        rows = conn.execute(
            "SELECT daemon_name, MAX(last_beat_utc) FROM daemon_heartbeat "
            "GROUP BY daemon_name"
        ).fetchall()
    except Exception as exc:
        logger.warning("thresholds: heartbeat query failed: %s", exc)
        return []
    out: list[ThresholdFlag] = []
    for name, last_raw in rows:
        if not last_raw:
            out.append(ThresholdFlag(
                kind="absolute",
                source="heartbeat_missing",
                invariant_id=None,
                message=f"daemon {name} has no heartbeat row",
                evidence={"daemon_name": name},
            ))
            continue
        try:
            s = str(last_raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age = (now_utc - dt).total_seconds()
        if age > _HEARTBEAT_STALE_SECONDS:
            out.append(ThresholdFlag(
                kind="absolute",
                source="stale_heartbeat",
                invariant_id=None,
                message=f"daemon {name} stale age={age:.0f}s > {_HEARTBEAT_STALE_SECONDS:.0f}s",
                evidence={"daemon_name": name, "age_seconds": age},
            ))
    return out


def _absolute_flex_empty_suspicious(conn, today_start: str, today_end: str) -> list[ThresholdFlag]:
    # created_ts is REAL (epoch seconds); convert bounds.
    try:
        start_epoch = datetime.fromisoformat(today_start).timestamp()
        end_epoch = datetime.fromisoformat(today_end).timestamp()
        rows = conn.execute(
            "SELECT id, payload_json, created_ts FROM cross_daemon_alerts "
            "WHERE kind = 'FLEX_SYNC_EMPTY_SUSPICIOUS' "
            "AND created_ts >= ? AND created_ts < ?",
            (start_epoch, end_epoch),
        ).fetchall()
    except Exception as exc:
        logger.warning("thresholds: flex_empty_suspicious query failed: %s", exc)
        return []
    out: list[ThresholdFlag] = []
    for r in rows:
        out.append(ThresholdFlag(
            kind="absolute",
            source="flex_empty_suspicious",
            invariant_id=None,
            message=f"FLEX_SYNC_EMPTY_SUSPICIOUS alert id={r[0]}",
            evidence={"alert_id": r[0]},
        ))
    return out


def _relative_invariant_spikes(
    conn, today_start: str, today_end: str, now_utc: datetime
) -> list[ThresholdFlag]:
    # Today's count per invariant.
    try:
        today_rows = conn.execute(
            "SELECT invariant_id, COUNT(*) FROM incidents "
            "WHERE detected_at >= ? AND detected_at < ? "
            "AND invariant_id IS NOT NULL "
            "GROUP BY invariant_id",
            (today_start, today_end),
        ).fetchall()
    except Exception as exc:
        logger.warning("thresholds: today_count query failed: %s", exc)
        return []

    out: list[ThresholdFlag] = []
    # trailing 7-day median is the median of per-day counts over prior 7 days.
    window_start = (datetime.fromisoformat(today_start)
                    - timedelta(days=_TRAILING_WINDOW_DAYS)).isoformat()
    try:
        history = conn.execute(
            "SELECT invariant_id, DATE(detected_at) AS d, COUNT(*) "
            "FROM incidents "
            "WHERE detected_at >= ? AND detected_at < ? "
            "AND invariant_id IS NOT NULL "
            "GROUP BY invariant_id, d",
            (window_start, today_start),
        ).fetchall()
    except Exception as exc:
        logger.warning("thresholds: history query failed: %s", exc)
        history = []

    per_invariant_days: dict[str, list[int]] = {}
    for inv, _d, c in history:
        per_invariant_days.setdefault(inv, []).append(int(c))

    for inv, today_count in today_rows:
        today_count = int(today_count)
        days = per_invariant_days.get(inv, [])
        if len(days) < _COLD_START_MIN_DAYS:
            # cold-start: skip relative trigger for this invariant
            continue
        median = statistics.median(days)
        threshold = max(_RELATIVE_FLOOR, _RELATIVE_MULTIPLIER * median)
        if today_count > threshold:
            out.append(ThresholdFlag(
                kind="relative",
                source="invariant_spike",
                invariant_id=inv,
                message=(
                    f"invariant {inv} fired {today_count}× today; "
                    f"7d median={median:.1f}; threshold={threshold:.1f}"
                ),
                evidence={
                    "today_count": today_count,
                    "trailing_7d_median": median,
                    "threshold": threshold,
                    "history_days": len(days),
                },
            ))
    return out


def compute_threshold_flags(
    *,
    db_path: str | Path | None = None,
    for_date: datetime | None = None,
) -> list[ThresholdFlag]:
    """Compute hybrid absolute + relative threshold flags for the day."""
    now_utc = for_date or datetime.now(timezone.utc)
    today_start, today_end = _day_bounds_utc(now_utc)

    flags: list[ThresholdFlag] = []
    try:
        with get_ro_connection(db_path=db_path) as conn:
            flags.extend(_absolute_architect_only(conn, today_start, today_end))
            flags.extend(_absolute_error_budget_tier_0_1(conn, today_start, today_end))
            flags.extend(_absolute_stale_heartbeat(conn, now_utc))
            flags.extend(_absolute_flex_empty_suspicious(conn, today_start, today_end))
            flags.extend(_relative_invariant_spikes(conn, today_start, today_end, now_utc))
    except Exception as exc:
        logger.warning("thresholds: get_ro_connection failed: %s", exc)
    return flags
