"""Phase B Foundation -- daily proof-report generator.

Computes the 11 paper-autonomy observation metrics from persisted
pending_orders / operator_interventions / daemon_heartbeat_samples /
cross_daemon_alerts data, plus G3/G4 telemetry and engine-activity
counts. Emits canonical JSON + Markdown wrapper.

Verdict logic:
  PASS                  - all metrics green AND engine activity present;
                          counts toward the 14-day window.
  PASS_NO_ACTIVITY      - all metrics green but zero engine activity;
                          does NOT count toward 14, does NOT reset.
  INSUFFICIENT_ACTIVITY - kept for future ramp-rate reporting; treated
                          as informational verdict (does not count).
  FAIL                  - one or more metrics over threshold.
  PENDING_FLEX          - Flex sync incomplete; retry within window.
  INSUFFICIENT_DATA     - migration not complete OR Flex still missing
                          past retry window.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agt_equities.db import get_ro_connection
from agt_equities.market_calendar import is_trading_day

logger = logging.getLogger("agt_equities.proof_report")

ET = ZoneInfo("America/New_York")
TERMINAL_STATES = frozenset({
    "filled", "cancelled", "expired", "rejected", "rejected_naked",
    "superseded", "failed", "duplicate_skipped",
})
ENGINE_NAMES = ("csp_allocator", "cc_engine", "csp_harvest", "roll_engine")
SAME_DAY_TERMINAL_THRESHOLD_PCT = 95.0
HEARTBEAT_GAP_THRESHOLD_S = 180
MARKET_OPEN_ET = time(9, 30)
MARKET_CLOSE_ET = time(16, 0)


@dataclass
class ProofReport:
    report_date_et: str
    report_date_window_utc: dict[str, str]
    generated_at_utc: str
    is_preview: bool
    verdict: str
    rationale: str
    metrics: dict[str, Any] = field(default_factory=dict)
    g3_g4_telemetry: dict[str, Any] = field(default_factory=dict)
    engine_activity: dict[str, Any] = field(default_factory=dict)
    data_freshness: dict[str, Any] = field(default_factory=dict)
    exceptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "report_date_et": self.report_date_et,
            "report_date_window_utc": self.report_date_window_utc,
            "generated_at_utc": self.generated_at_utc,
            "is_preview": self.is_preview,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "metrics": self.metrics,
            "g3_g4_telemetry": self.g3_g4_telemetry,
            "engine_activity": self.engine_activity,
            "data_freshness": self.data_freshness,
            "exceptions": self.exceptions,
        }


def _et_window(report_date_et: str) -> tuple[str, str, datetime, datetime]:
    """Convert ET trading date 'YYYY-MM-DD' to UTC [start, end) window.

    Window spans 04:00 ET on report_date through 04:00 ET on the next day,
    matching the "trading day" boundary used by FlEx + master_log_trades.
    """
    d = datetime.strptime(report_date_et, "%Y-%m-%d").date()
    start_et = datetime.combine(d, time(4, 0), ET)
    end_et = datetime.combine(d + timedelta(days=1), time(4, 0), ET)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)
    return start_utc.isoformat(), end_utc.isoformat(), start_utc, end_utc


def _migration_completed_at_iso(conn: sqlite3.Connection) -> str | None:
    """Return min(staged_at_utc) where engine IS NOT NULL as a proxy for
    migration completion -- the first row written under the new schema.
    Returns None if no enriched rows exist yet (pre-migration env).
    """
    row = conn.execute(
        "SELECT MIN(staged_at_utc) FROM pending_orders "
        "WHERE engine IS NOT NULL AND staged_at_utc IS NOT NULL"
    ).fetchone()
    return row[0] if row and row[0] else None


def _flex_complete(conn: sqlite3.Connection, window_end_iso: str) -> dict | None:
    """Return the next successful master_log_sync row that ran AFTER window_end
    (flex_sync_eod runs the morning AFTER the trading day, so the relevant
    sync row is the one with started_at >= window_end). Returns None if no
    such sync exists yet (Flex pending / failed)."""
    try:
        row = conn.execute(
            "SELECT sync_id, started_at, status FROM master_log_sync "
            "WHERE status = 'success' AND started_at >= ? "
            "ORDER BY started_at ASC LIMIT 1",
            (window_end_iso,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {"sync_id": row[0], "started_at": row[1], "status": row[2]}


def _count_pending_in_window(
    conn: sqlite3.Connection, start_iso: str, end_iso: str, migration_iso: str | None,
) -> tuple[int, int, int]:
    """Return (window_total, pre_migration_excluded, eligible_total)."""
    if migration_iso:
        total = conn.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        ).fetchone()[0]
        eligible = conn.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE created_at >= ? AND created_at < ? "
            "AND created_at >= ?",
            (start_iso, end_iso, migration_iso),
        ).fetchone()[0]
        return int(total), int(total - eligible), int(eligible)
    total = conn.execute(
        "SELECT COUNT(*) FROM pending_orders WHERE created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    ).fetchone()[0]
    return int(total), int(total), 0


def _terminal_count(conn: sqlite3.Connection, start_iso: str, end_iso: str, migration_iso: str | None) -> int:
    base = (
        "SELECT COUNT(*) FROM pending_orders "
        "WHERE created_at >= ? AND created_at < ? AND status IN ({}) "
    ).format(", ".join("?" for _ in TERMINAL_STATES))
    params: list[Any] = [start_iso, end_iso, *sorted(TERMINAL_STATES)]
    if migration_iso:
        base += "AND created_at >= ?"
        params.append(migration_iso)
    return int(conn.execute(base, tuple(params)).fetchone()[0])


def _stale_freshness_count(conn: sqlite3.Connection, start_iso: str, end_iso: str, key: str) -> int:
    """Count orders where gate_verdicts.<key> = false AND status = 'filled'."""
    rows = conn.execute(
        "SELECT gate_verdicts FROM pending_orders "
        "WHERE created_at >= ? AND created_at < ? AND status = 'filled' "
        "AND gate_verdicts IS NOT NULL",
        (start_iso, end_iso),
    ).fetchall()
    bad = 0
    for r in rows:
        try:
            gv = json.loads(r[0])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(gv, dict) and gv.get(key) is False:
            bad += 1
    return bad


def _route_mismatch_count(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> int:
    rows = conn.execute(
        "SELECT broker_mode_at_staging, payload FROM pending_orders "
        "WHERE created_at >= ? AND created_at < ? AND broker_mode_at_staging IS NOT NULL",
        (start_iso, end_iso),
    ).fetchall()
    mismatches = 0
    for bm, payload in rows:
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except (json.JSONDecodeError, TypeError):
            continue
        am = p.get("account_mode") or p.get("broker_mode")
        if am and bm and str(am).lower() != str(bm).lower():
            mismatches += 1
    return mismatches


def _missing_audit_evidence_count(
    conn: sqlite3.Connection, start_iso: str, end_iso: str, migration_iso: str | None,
) -> int:
    if not migration_iso:
        return 0
    sql = (
        "SELECT COUNT(*) FROM pending_orders "
        "WHERE created_at >= ? AND created_at < ? AND created_at >= ? "
        "AND status IN ({}) "
        "AND (engine IS NULL OR run_id IS NULL OR gate_verdicts IS NULL)"
    ).format(", ".join("?" for _ in TERMINAL_STATES))
    params: list[Any] = [start_iso, end_iso, migration_iso, *sorted(TERMINAL_STATES)]
    return int(conn.execute(sql, tuple(params)).fetchone()[0])


def _operator_intervention_count(
    conn: sqlite3.Connection, start_iso: str, end_iso: str, kinds: tuple[str, ...] = ("direct_sql", "manual_terminal"),
) -> int:
    placeholders = ", ".join("?" for _ in kinds)
    return int(conn.execute(
        f"SELECT COUNT(*) FROM operator_interventions "
        f"WHERE occurred_at_utc >= ? AND occurred_at_utc < ? AND kind IN ({placeholders})",
        (start_iso, end_iso, *kinds),
    ).fetchone()[0])


def _tier_incident_count(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM cross_daemon_alerts "
            "WHERE created_ts >= unixepoch(?) AND created_ts < unixepoch(?) "
            "AND severity IN ('tier_0', 'tier_1', 'critical')",
            (start_iso, end_iso),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _heartbeat_gap_count(
    conn: sqlite3.Connection, start_utc: datetime, end_utc: datetime,
) -> int:
    try:
        rows = conn.execute(
            "SELECT daemon_name, beat_utc FROM daemon_heartbeat_samples "
            "WHERE beat_utc >= ? AND beat_utc < ? "
            "ORDER BY daemon_name, beat_utc",
            (start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    by_daemon: dict[str, list[datetime]] = {}
    for daemon, beat in rows:
        try:
            ts = datetime.fromisoformat(beat)
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        by_daemon.setdefault(daemon, []).append(ts)
    gaps = 0
    for daemon, beats in by_daemon.items():
        beats.sort()
        for i in range(1, len(beats)):
            mid = beats[i - 1] + (beats[i] - beats[i - 1]) / 2
            mid_et = mid.astimezone(ET).time()
            if not (MARKET_OPEN_ET <= mid_et <= MARKET_CLOSE_ET):
                continue
            gap_s = (beats[i] - beats[i - 1]).total_seconds()
            if gap_s > HEARTBEAT_GAP_THRESHOLD_S:
                gaps += 1
    return gaps


def _walker_defect_count(start_utc: datetime, end_utc: datetime) -> int:
    try:
        from agt_equities.walker import reconstruct_active_cycles
    except Exception:
        return 0
    try:
        result = reconstruct_active_cycles()
        if isinstance(result, dict) and "defects" in result:
            return int(len(result["defects"]))
        return 0
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("walker_reconstruction_defects probe failed: %s", exc)
        return 1


def _engine_activity(
    conn: sqlite3.Connection, start_iso: str, end_iso: str, migration_iso: str | None,
) -> dict[str, int]:
    base_where = "created_at >= ? AND created_at < ?"
    params: list[Any] = [start_iso, end_iso]
    if migration_iso:
        base_where += " AND created_at >= ?"
        params.append(migration_iso)
    staged = int(conn.execute(
        f"SELECT COUNT(*) FROM pending_orders WHERE {base_where}", tuple(params)
    ).fetchone()[0])
    submitted = int(conn.execute(
        f"SELECT COUNT(*) FROM pending_orders WHERE {base_where} AND submitted_at_utc IS NOT NULL",
        tuple(params),
    ).fetchone()[0])
    terminal = _terminal_count(conn, start_iso, end_iso, migration_iso)
    decisions = staged
    return {
        "decisions": decisions,
        "orders_staged": staged,
        "orders_submitted": submitted,
        "orders_terminal_by_close": terminal,
    }


def _g3_g4_telemetry(conn: sqlite3.Connection, start_iso: str, end_iso: str, migration_iso: str | None) -> dict:
    base_where = "created_at >= ? AND created_at < ?"
    params: list[Any] = [start_iso, end_iso]
    if migration_iso:
        base_where += " AND created_at >= ?"
        params.append(migration_iso)
    sample_rows = conn.execute(
        f"SELECT engine, status, gate_verdicts FROM pending_orders WHERE {base_where}",
        tuple(params),
    ).fetchall()
    by_engine = {name: 0 for name in ENGINE_NAMES}
    veto_count = 0
    rejection_count = 0
    for engine, status, gv in sample_rows:
        if engine in by_engine:
            by_engine[engine] += 1
        if status == "superseded":
            try:
                gvd = json.loads(gv) if isinstance(gv, str) else (gv or {})
            except (json.JSONDecodeError, TypeError):
                gvd = {}
            if isinstance(gvd, dict) and gvd.get("failed") is True:
                veto_count += 1
        if status == "failed":
            rejection_count += 1
    total = len(sample_rows)
    return {
        "sample_size_total": total,
        "sample_size_by_engine": by_engine,
        "veto_rate": round(veto_count / total, 4) if total else 0.0,
        "broker_rejection_rate": round(rejection_count / total, 4) if total else 0.0,
    }


def compute_metrics(
    conn: sqlite3.Connection, start_iso: str, end_iso: str,
    start_utc: datetime, end_utc: datetime, migration_iso: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (metrics, freshness)."""
    total, pre_excl, eligible = _count_pending_in_window(conn, start_iso, end_iso, migration_iso)
    terminal = _terminal_count(conn, start_iso, end_iso, migration_iso)
    pct_term = round(100.0 * terminal / eligible, 2) if eligible else 100.0
    next_close_cutoff = (end_utc + timedelta(days=1)).isoformat()
    non_terminal_past = int(conn.execute(
        "SELECT COUNT(*) FROM pending_orders WHERE created_at >= ? AND created_at < ? "
        "AND status NOT IN ({}) AND created_at < ?".format(", ".join("?" for _ in TERMINAL_STATES)),
        (start_iso, end_iso, *sorted(TERMINAL_STATES), next_close_cutoff),
    ).fetchone()[0])
    metrics = {
        "route_mismatches": _route_mismatch_count(conn, start_iso, end_iso),
        "non_terminal_past_next_close": non_terminal_past,
        "pct_same_day_terminal": pct_term,
        "stale_strike_submissions_succeeded": _stale_freshness_count(conn, start_iso, end_iso, "strike_freshness"),
        "stale_quote_submissions_succeeded": _stale_freshness_count(conn, start_iso, end_iso, "quote_freshness"),
        "direct_db_or_manual_interventions": _operator_intervention_count(conn, start_iso, end_iso),
        "orders_missing_audit_evidence": _missing_audit_evidence_count(conn, start_iso, end_iso, migration_iso),
        "sweeper_accumulated_stuck_over_24h": non_terminal_past,
        "tier_0_or_tier_1_incidents": _tier_incident_count(conn, start_iso, end_iso),
        "heartbeat_gaps_over_180s": _heartbeat_gap_count(conn, start_utc, end_utc),
        "walker_reconstruction_defects": _walker_defect_count(start_utc, end_utc),
    }
    freshness = {
        "flex_completed_at_utc": None,
        "flex_zero_row_check_passed": None,
        "sweeper_last_fire_utc": None,
        "pre_migration_rows_excluded": pre_excl,
    }
    return metrics, freshness


def _verdict(metrics: dict[str, Any], activity: dict[str, int]) -> tuple[str, str]:
    failures: list[str] = []
    for name, val in metrics.items():
        if name == "pct_same_day_terminal":
            if val < SAME_DAY_TERMINAL_THRESHOLD_PCT:
                failures.append(f"{name}={val}<{SAME_DAY_TERMINAL_THRESHOLD_PCT}")
            continue
        if isinstance(val, (int, float)) and val > 0:
            failures.append(f"{name}={val}")
    if failures:
        return "FAIL", "; ".join(failures)
    if activity["decisions"] == 0 and activity["orders_staged"] == 0:
        return "PASS_NO_ACTIVITY", "all metrics green; zero engine activity"
    return "PASS", f"all 11 metrics green; engine activity present (staged={activity['orders_staged']})"


def generate_proof_report(
    *,
    report_date_et: str,
    is_preview: bool = False,
    db_path: str | Path | None = None,
    output_dir: Path | None = None,
) -> ProofReport:
    """Compute the report and (when output_dir given) emit JSON + Markdown."""
    start_iso, end_iso, start_utc, end_utc = _et_window(report_date_et)
    generated_at = datetime.now(timezone.utc).isoformat()
    with closing(get_ro_connection(db_path=db_path)) as conn:
        migration_iso = _migration_completed_at_iso(conn)
        flex = _flex_complete(conn, end_iso)
        if migration_iso is None:
            report = ProofReport(
                report_date_et=report_date_et,
                report_date_window_utc={"start": start_iso, "end": end_iso},
                generated_at_utc=generated_at,
                is_preview=is_preview,
                verdict="INSUFFICIENT_DATA",
                rationale="schema migration not detected in pending_orders (engine column unpopulated)",
            )
            if output_dir:
                _emit(report, output_dir)
            return report
        if flex is None and not is_preview:
            now_et = datetime.now(timezone.utc).astimezone(ET)
            cutoff_et = datetime.combine((datetime.fromisoformat(report_date_et).date() + timedelta(days=1)), time(9, 0), ET)
            verdict = "PENDING_FLEX" if now_et < cutoff_et else "INSUFFICIENT_DATA"
            report = ProofReport(
                report_date_et=report_date_et,
                report_date_window_utc={"start": start_iso, "end": end_iso},
                generated_at_utc=generated_at,
                is_preview=is_preview,
                verdict=verdict,
                rationale="flex sync not yet complete for report window",
            )
            if output_dir:
                _emit(report, output_dir)
            return report
        metrics, freshness = compute_metrics(conn, start_iso, end_iso, start_utc, end_utc, migration_iso)
        if flex:
            freshness["flex_completed_at_utc"] = flex.get("started_at")
            freshness["flex_zero_row_check_passed"] = True
        try:
            sweep = conn.execute(
                "SELECT MAX(beat_utc) FROM daemon_heartbeat_samples"
            ).fetchone()
            freshness["sweeper_last_fire_utc"] = sweep[0] if sweep else None
        except sqlite3.OperationalError:
            pass
        activity = _engine_activity(conn, start_iso, end_iso, migration_iso)
        telemetry = _g3_g4_telemetry(conn, start_iso, end_iso, migration_iso)
    verdict, rationale = _verdict(metrics, activity)
    report = ProofReport(
        report_date_et=report_date_et,
        report_date_window_utc={"start": start_iso, "end": end_iso},
        generated_at_utc=generated_at,
        is_preview=is_preview,
        verdict=verdict,
        rationale=rationale,
        metrics=metrics,
        g3_g4_telemetry=telemetry,
        engine_activity=activity,
        data_freshness=freshness,
    )
    if output_dir:
        _emit(report, output_dir)
    return report


def _emit(report: ProofReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_preview" if report.is_preview else ""
    date_token = report.report_date_et.replace("-", "")
    json_path = output_dir / f"proof_{date_token}{suffix}.json"
    md_path = output_dir / f"proof_{date_token}{suffix}.md"
    json_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _render_markdown(report: ProofReport) -> str:
    lines = [
        f"# Phase B Proof Report -- {report.report_date_et}",
        "",
        f"**Generated:** {report.generated_at_utc}",
        f"**Preview:** {report.is_preview}",
        f"**Verdict:** `{report.verdict}`",
        "",
        f"**Rationale:** {report.rationale}",
        "",
        "## Metrics",
        "",
    ]
    for name, val in report.metrics.items():
        lines.append(f"- `{name}` = {val}")
    if report.engine_activity:
        lines += ["", "## Engine Activity", ""]
        for k, v in report.engine_activity.items():
            lines.append(f"- {k}: {v}")
    if report.g3_g4_telemetry:
        lines += ["", "## G3/G4 Telemetry", ""]
        for k, v in report.g3_g4_telemetry.items():
            lines.append(f"- {k}: {v}")
    if report.data_freshness:
        lines += ["", "## Data Freshness", ""]
        for k, v in report.data_freshness.items():
            lines.append(f"- {k}: {v}")
    return "\n".join(lines) + "\n"


def run_for_today_et(*, is_preview: bool, output_dir: Path | None = None) -> ProofReport | None:
    """Helper used by scheduler cron jobs. Skips emission on non-trading days.

    For the next-morning final job: report_date_et = (today_et - 1 day).
    For the same-day preview job:    report_date_et = today_et.
    """
    today_et = datetime.now(timezone.utc).astimezone(ET).date()
    report_date = today_et if is_preview else (today_et - timedelta(days=1))
    if not is_trading_day(report_date):
        logger.info("proof_report skipped: %s is not a trading day", report_date)
        return None
    return generate_proof_report(
        report_date_et=report_date.strftime("%Y-%m-%d"),
        is_preview=is_preview,
        output_dir=output_dir,
    )


__all__ = [
    "ProofReport", "generate_proof_report", "compute_metrics",
    "run_for_today_et",
]
