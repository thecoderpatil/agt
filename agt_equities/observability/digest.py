"""observability.digest — snapshot builder + Telegram card renderer.

ADR-017 §9 Mega-MR A.1. Five-section deterministic digest per §3:

  1. Architect-only incidents           (incidents_repo.list_architect_only)
  2. Authorable incidents               (incidents_repo.list_authorable)
  3. Daemon heartbeat status            (daemon_heartbeat table)
  4. Flex sync freshness + zero-row     (flex_sync_watchdog public API)
  5. Promotion-gate blockers per engine (paper_baseline.evaluate_all)

Each section preserves its native severity semantics — do NOT collapse
into a shared label (ADR-017 §6 prohibition). Failures fail-soft: the
failed section carries a `section_error` and the renderer surfaces it
with a ⚠️ warning rather than swallowing.

Consumers:
  - telegram_bot._scheduled_oversight_digest_send (Mega-MR A.2)
  - telegram_bot.cmd_oversight_status             (Mega-MR C)
"""
from __future__ import annotations

import dataclasses
import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from agt_equities.db import get_ro_connection

HeartbeatState = Literal["fresh", "warn", "stale"]

_FRESH_THRESHOLD_SECONDS: float = 120.0
_WARN_THRESHOLD_SECONDS: float = 300.0
_ENGINES: tuple[str, ...] = ("entry", "exit", "harvest", "roll")


@dataclass(frozen=True)
class HeartbeatStatus:
    daemon_name: str
    last_beat_utc: datetime | None
    age_seconds: float | None
    status: HeartbeatState


@dataclass(frozen=True)
class FlexStatus:
    last_sync_utc: datetime | None
    status: str | None
    zero_row_suspicion: bool
    stale: bool
    sync_id: int | None


@dataclass(frozen=True)
class PromotionGateRow:
    engine: str
    gate_id: str  # G1..G5
    status: str   # "green", "red", "insufficient_data", "not yet instrumented"
    message: str


@dataclass(frozen=True)
class ObservabilitySnapshot:
    generated_at_utc: datetime
    architect_only: list[dict[str, Any]]
    architect_only_error: str | None
    authorable: list[dict[str, Any]]
    authorable_error: str | None
    heartbeats: list[HeartbeatStatus]
    heartbeats_error: str | None
    flex: FlexStatus | None
    flex_error: str | None
    promotion: list[PromotionGateRow]
    promotion_error: str | None


def _heartbeat_state(age_seconds: float | None) -> HeartbeatState:
    if age_seconds is None:
        return "stale"
    if age_seconds <= _FRESH_THRESHOLD_SECONDS:
        return "fresh"
    if age_seconds <= _WARN_THRESHOLD_SECONDS:
        return "warn"
    return "stale"


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _query_heartbeats(
    *, now_utc: datetime, db_path: str | Path | None = None
) -> list[HeartbeatStatus]:
    with get_ro_connection(db_path=db_path) as conn:
        rows = conn.execute(
            "SELECT daemon_name, MAX(last_beat_utc) AS last "
            "FROM daemon_heartbeat GROUP BY daemon_name"
        ).fetchall()
    out: list[HeartbeatStatus] = []
    for r in rows:
        name = r[0]
        last = _parse_ts(r[1])
        age = (now_utc - last).total_seconds() if last else None
        out.append(
            HeartbeatStatus(
                daemon_name=name,
                last_beat_utc=last,
                age_seconds=age,
                status=_heartbeat_state(age),
            )
        )
    return out


def _flex_snapshot(
    *, now_utc: datetime, db_path: str | Path | None = None
) -> FlexStatus:
    from agt_equities.flex_sync_watchdog import (
        DEFAULT_STALE_THRESHOLD_HOURS,
        check_zero_row_suspicion,
        query_latest_sync,
    )

    latest = query_latest_sync(db_path=db_path)
    last_dt = _parse_ts(latest.get("started_at"))
    stale = False
    if last_dt is None:
        stale = True
    else:
        stale = (now_utc - last_dt).total_seconds() / 3600.0 > DEFAULT_STALE_THRESHOLD_HOURS
    zero_row = False
    try:
        zr = check_zero_row_suspicion(now_utc=now_utc, db_path=db_path)
        zero_row = zr.get("status") == "alerted"
    except Exception:
        # Bubble up as flex section not having zero-row detail; do not mask.
        zero_row = False
    return FlexStatus(
        last_sync_utc=last_dt,
        status=latest.get("status"),
        zero_row_suspicion=zero_row,
        stale=stale,
        sync_id=latest.get("sync_id"),
    )


def _promotion_rows(*, db_path: str | Path | None = None) -> list[PromotionGateRow]:
    from agt_equities.paper_baseline import evaluate_all

    out: list[PromotionGateRow] = []
    for eng in _ENGINES:
        results = evaluate_all(eng, db_path=db_path)
        for r in results:
            if r.gate_id in ("G1", "G3", "G4"):
                out.append(
                    PromotionGateRow(
                        engine=eng,
                        gate_id=r.gate_id,
                        status="not yet instrumented",
                        message=r.message,
                    )
                )
            else:
                out.append(
                    PromotionGateRow(
                        engine=eng,
                        gate_id=r.gate_id,
                        status=r.status,
                        message=r.message,
                    )
                )
    return out


def build_observability_snapshot(
    *,
    db_path: str | Path | None = None,
    for_date: datetime | None = None,
) -> ObservabilitySnapshot:
    """Build the five-section snapshot. Each section fails soft."""
    from agt_equities import incidents_repo

    now_utc = for_date or datetime.now(timezone.utc)

    architect_only: list[dict[str, Any]] = []
    architect_only_error: str | None = None
    try:
        architect_only = incidents_repo.list_architect_only(db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        architect_only_error = str(exc)

    authorable: list[dict[str, Any]] = []
    authorable_error: str | None = None
    try:
        authorable = incidents_repo.list_authorable(db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        authorable_error = str(exc)

    heartbeats: list[HeartbeatStatus] = []
    heartbeats_error: str | None = None
    try:
        heartbeats = _query_heartbeats(now_utc=now_utc, db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        heartbeats_error = str(exc)

    flex: FlexStatus | None = None
    flex_error: str | None = None
    try:
        flex = _flex_snapshot(now_utc=now_utc, db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        flex_error = str(exc)

    promotion: list[PromotionGateRow] = []
    promotion_error: str | None = None
    try:
        promotion = _promotion_rows(db_path=db_path)
    except Exception as exc:  # pragma: no cover - defensive
        promotion_error = str(exc)

    return ObservabilitySnapshot(
        generated_at_utc=now_utc,
        architect_only=architect_only,
        architect_only_error=architect_only_error,
        authorable=authorable,
        authorable_error=authorable_error,
        heartbeats=heartbeats,
        heartbeats_error=heartbeats_error,
        flex=flex,
        flex_error=flex_error,
        promotion=promotion,
        promotion_error=promotion_error,
    )


def _fmt_incident(row: dict[str, Any]) -> str:
    inv = row.get("invariant_id") or "(no invariant_id)"
    breaches = row.get("consecutive_breaches") or 0
    status = row.get("status") or "?"
    tier = row.get("scrutiny_tier") or "?"
    return f"- <code>{html.escape(inv)}</code> tier={html.escape(tier)} status={html.escape(status)} breaches={breaches}"


def _fmt_heartbeat(hb: HeartbeatStatus) -> str:
    icon = {"fresh": "✅", "warn": "⚠️", "stale": "❌"}[hb.status]
    age = f"{hb.age_seconds:.0f}s" if hb.age_seconds is not None else "unknown"
    return f"- {icon} <code>{html.escape(hb.daemon_name)}</code> age={age}"


def _fmt_flex(flex: FlexStatus) -> list[str]:
    lines: list[str] = []
    last = flex.last_sync_utc.isoformat() if flex.last_sync_utc else "never"
    lines.append(f"- last sync: <code>{last}</code> status={html.escape(flex.status or '?')} sync_id={flex.sync_id}")
    lines.append(f"- stale: {'⚠️' if flex.stale else '✅'}  zero-row suspicion: {'⚠️' if flex.zero_row_suspicion else '✅'}")
    return lines


def _fmt_promotion(row: PromotionGateRow) -> str:
    if row.status == "not yet instrumented":
        return f"- <code>{html.escape(row.engine)}.{html.escape(row.gate_id)}</code> — not yet instrumented"
    icon = {"green": "✅", "red": "❌", "insufficient_data": "⏳"}.get(row.status, "•")
    return f"- {icon} <code>{html.escape(row.engine)}.{html.escape(row.gate_id)}</code> {html.escape(row.status)} — {html.escape(row.message)}"


def render_observability_card(
    snapshot: ObservabilitySnapshot,
    *,
    threshold_flags: list[Any] | None = None,
) -> str:
    """Render the five-section Telegram card. Returns HTML string."""
    lines: list[str] = []
    ts = snapshot.generated_at_utc.strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"<b>🛰 Oversight digest</b> — {ts}")
    lines.append("")

    # 1. Architect-only
    lines.append("<b>🚨 Architect-only incidents</b>")
    if snapshot.architect_only_error:
        lines.append(f"⚠️ section failed: {html.escape(snapshot.architect_only_error)}")
    elif not snapshot.architect_only:
        lines.append("<i>none</i>")
    else:
        for row in snapshot.architect_only[:20]:
            lines.append(_fmt_incident(row))
        if len(snapshot.architect_only) > 20:
            lines.append(f"<i>…and {len(snapshot.architect_only) - 20} more</i>")
    lines.append("")

    # 2. Authorable + threshold flags
    lines.append("<b>📊 Authorable incidents</b>")
    if snapshot.authorable_error:
        lines.append(f"⚠️ section failed: {html.escape(snapshot.authorable_error)}")
    elif not snapshot.authorable:
        lines.append("<i>none</i>")
    else:
        for row in snapshot.authorable[:20]:
            lines.append(_fmt_incident(row))
        if len(snapshot.authorable) > 20:
            lines.append(f"<i>…and {len(snapshot.authorable) - 20} more</i>")
    if threshold_flags:
        lines.append("")
        lines.append("<i>threshold flags:</i>")
        for flag in threshold_flags[:20]:
            kind = getattr(flag, "kind", "?")
            msg = getattr(flag, "message", str(flag))
            lines.append(f"  • [{html.escape(kind)}] {html.escape(msg)}")
    lines.append("")

    # 3. Heartbeats
    lines.append("<b>💓 Heartbeats</b>")
    if snapshot.heartbeats_error:
        lines.append(f"⚠️ section failed: {html.escape(snapshot.heartbeats_error)}")
    elif not snapshot.heartbeats:
        lines.append("<i>no heartbeat rows</i>")
    else:
        for hb in snapshot.heartbeats:
            lines.append(_fmt_heartbeat(hb))
    lines.append("")

    # 4. Flex
    lines.append("<b>📥 Flex sync</b>")
    if snapshot.flex_error:
        lines.append(f"⚠️ section failed: {html.escape(snapshot.flex_error)}")
    elif snapshot.flex is None:
        lines.append("<i>no flex snapshot</i>")
    else:
        for ln in _fmt_flex(snapshot.flex):
            lines.append(ln)
    lines.append("")

    # 5. Promotion gates
    lines.append("<b>🚦 Promotion-gate blockers</b>")
    if snapshot.promotion_error:
        lines.append(f"⚠️ section failed: {html.escape(snapshot.promotion_error)}")
    elif not snapshot.promotion:
        lines.append("<i>no gate rows</i>")
    else:
        for row in snapshot.promotion:
            lines.append(_fmt_promotion(row))

    return "\n".join(lines)
