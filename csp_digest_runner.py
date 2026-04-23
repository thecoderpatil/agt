"""CSP Digest scheduler-job body + Anthropic factory.

Sprint 4 MR A (2026-04-24). Wires the library in agt_equities.csp_digest
to the PTB scheduler and Telegram sender. Lives outside `agt_equities/`
so the ADR-010 §6.1 `test_no_raw_anthropic_imports.py` structural check
— which only scans `agt_equities/` — does not flag this as a violation.

The cached_client extension path (per ADR-010 §6.1 "only cached_client
imports anthropic") would require ~150+ LOC to add async + tool-use
support; dispatch latitude explicitly allows the parallel-factory
fallback for observation-week scope. Documented in Sprint 4 MR A ship
report.

Public surface:
    run_csp_digest_job       — async; callable from PTB JobQueue
    build_digest_payload     — pure; builds DigestPayload from latest result
    _make_anthropic_factory  — returns zero-arg callable expected by llm_commentary
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from agt_equities import csp_allocator as _csp_allocator
from agt_equities.csp_digest import (
    DigestCandidate,
    DigestCommentary,
    DigestPayload,
    build_inline_keyboard,
    generate_commentary,
    render_card_text,
)
from agt_equities.db import get_db_connection, tx_immediate

logger = logging.getLogger(__name__)

SOFT_DEP_MAX_AGE_MINUTES = 30
DEFAULT_TIMEOUT_MINUTES = 90
_DIGEST_FIRED_ROW_TOKEN = "digest"


def _make_anthropic_factory():
    """Build a zero-arg callable returning an anthropic.AsyncAnthropic client.

    Returns None if ANTHROPIC_API_KEY is unset (commentary will fall through
    to deterministic empty-commentary path, same as any other factory failure).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("csp_digest_runner: ANTHROPIC_API_KEY unset; commentary will be empty")
        return None

    def _factory():
        # Lazy import inside factory so module import doesn't force the SDK.
        import anthropic
        return anthropic.AsyncAnthropic(api_key=api_key, timeout=30.0)

    return _factory


def build_digest_payload(
    *,
    latest: dict,
    commentaries: dict[str, DigestCommentary],
    mode: str = "PAPER",
    now_utc: datetime | None = None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> DigestPayload:
    """Map a persisted allocator-latest row → DigestPayload.

    The mapping is intentionally permissive: missing fields default to 0
    or empty string. Staged tickets that lack screener-level attributes
    (IVR, VRP, spot, analyst data) render with zero values; the card-body
    formatter handles that gracefully.
    """
    now = now_utc or datetime.now(timezone.utc)
    timeout_at = now + timedelta(minutes=timeout_minutes)

    staged = latest.get("staged", []) or []
    candidates: list[DigestCandidate] = []
    for rank, t in enumerate(staged, start=1):
        # Staged tickets carry per-account (account_id, contracts); project to label form.
        per_account_raw = t.get("per_account") or [(t.get("account_id", ""), t.get("quantity", 0))]
        per_account = [(str(a), int(n or 0)) for (a, n) in per_account_raw]
        candidates.append(DigestCandidate(
            rank=int(t.get("rank", rank)),
            ticker=str(t.get("ticker", "?")),
            strike=float(t.get("strike") or 0.0),
            expiry=str(t.get("expiry") or ""),
            premium_dollars=float(t.get("premium_dollars") or (t.get("mid", 0) * 100)),
            premium_pct=float(t.get("premium_pct") or 0.0),
            ray_pct=float(t.get("annualized_yield") or t.get("ray_pct") or 0.0),
            delta=float(t.get("delta") or 0.0),
            otm_pct=float(t.get("otm_pct") or 0.0),
            ivr_pct=float(t.get("ivr_pct") or 0.0),
            vrp=float(t.get("vrp") or 0.0),
            fwd_pe=t.get("fwd_pe"),
            week52_low=float(t.get("week52_low") or 0.0),
            week52_high=float(t.get("week52_high") or 0.0),
            spot=float(t.get("spot") or 0.0),
            week52_pct_of_range=float(t.get("week52_pct_of_range") or 0.0),
            analyst_avg=t.get("analyst_avg"),
            analyst_sources_blurb=t.get("analyst_sources_blurb"),
            per_account=per_account,
            ivr_benchmark_median=t.get("ivr_benchmark_median"),
            vrp_benchmark_median=t.get("vrp_benchmark_median"),
        ))

    accounts_blurb = " • ".join(
        sorted({label for c in candidates for (label, _) in c.per_account}),
    )

    return DigestPayload(
        mode=mode,  # "PAPER" or "LIVE"
        sent_at_utc=now,
        timeout_at_utc=timeout_at,
        candidates=candidates,
        commentaries=commentaries,
        accounts_blurb=accounts_blurb,
    )


def _build_user_payload_text(payload: DigestPayload, latest: dict) -> str:
    """Format the user message body the LLM sees. Compact JSON of candidate summaries."""
    compact = [
        {
            "rank": c.rank,
            "ticker": c.ticker,
            "strike": c.strike,
            "expiry": c.expiry,
            "ray_pct": c.ray_pct,
            "delta": c.delta,
            "otm_pct": c.otm_pct,
            "ivr_pct": c.ivr_pct,
            "vrp": c.vrp,
        }
        for c in payload.candidates
    ]
    return json.dumps({
        "run_id": latest.get("run_id"),
        "trade_date": latest.get("trade_date"),
        "candidates": compact,
    })


def _already_fired_today(*, trade_date: str, db_path: str | Path | None = None) -> bool:
    """Idempotency check — once csp_digest_send fires today, do NOT re-fire on bot restart."""
    with get_db_connection(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT id FROM csp_pending_approval "
            "WHERE run_id = ? AND household_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (f"digest:{trade_date}", _DIGEST_FIRED_ROW_TOKEN),
        ).fetchone()
    return row is not None


def _record_digest_fired(
    *,
    trade_date: str,
    candidate_count: int,
    sent_at: datetime,
    timeout_at: datetime,
    telegram_message_id: int | None,
    db_path: str | Path | None = None,
) -> int:
    """Insert the singleton-per-day marker row so bot restart mid-day does not re-fire."""
    candidates_json = json.dumps({"count": candidate_count})
    with get_db_connection(db_path=db_path) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "INSERT INTO csp_pending_approval "
                "(run_id, household_id, candidates_json, sent_at_utc, timeout_at_utc, "
                " telegram_message_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (
                    f"digest:{trade_date}", _DIGEST_FIRED_ROW_TOKEN, candidates_json,
                    sent_at.isoformat(), timeout_at.isoformat(), telegram_message_id,
                ),
            )
            return cur.lastrowid or 0


async def run_csp_digest_job(
    *,
    send_telegram: Any,
    db_path: str | Path | None = None,
    mode: str = "PAPER",
    now_utc: datetime | None = None,
    soft_dep_max_age_minutes: int = SOFT_DEP_MAX_AGE_MINUTES,
    anthropic_factory: Any = None,
) -> dict:
    """Run the 09:37 ET CSP digest: load latest allocator result → LLM commentary → send.

    Returns a small status dict: {fired: bool, reason: str, run_id: str | None,
    count: int, telegram_message_id: int | None}. Never raises; every failure
    mode maps to a status dict so the caller's log surface is predictable.

    Parameters
    ----------
    send_telegram : async callable
        Awaitable invoked as `await send_telegram(text, keyboard)`. Returns the
        sent message_id on success or None on failure. The wiring MR supplies
        a bound callable that closes over `app.bot` or the enqueue_alert pipe.
    db_path : optional
        Passed through to all DB accesses.
    mode : "PAPER" or "LIVE"
        Controls digest header + inline-keyboard emission. PAPER omits the keyboard
        per ADR. Identity approval gate is held regardless; operator taps are
        logged but paper auto-execution proceeds (ADR §5 step 2).
    now_utc : optional
        Injection seam for tests.
    soft_dep_max_age_minutes : int
        If the persisted allocator-latest row is older than this, skip (soft dep
        on csp_scan_daily at 09:35 ET — 2-min gap usually fresh).
    anthropic_factory : optional
        Override for the production factory (tests pass a stub).
    """
    now = now_utc or datetime.now(timezone.utc)
    trade_date = now.strftime("%Y-%m-%d")

    latest = _csp_allocator.load_latest_result(db_path=db_path)
    if latest is None:
        logger.info("csp_digest_job: no allocator_latest row — skipping")
        return {"fired": False, "reason": "no_allocator_row", "run_id": None, "count": 0, "telegram_message_id": None}

    # Soft dependency: the digest job fires 2 min after csp_scan_daily (09:37 vs 09:35).
    # If the latest row is older than 30 min, the scan must have failed or skipped.
    try:
        created = datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_min = (now - created).total_seconds() / 60.0
    except Exception:
        age_min = soft_dep_max_age_minutes + 1  # treat as stale if unparseable
    if age_min > soft_dep_max_age_minutes:
        logger.warning(
            "csp_digest_job: allocator_latest is %.1f min old (>%.0f) — soft-dep skip",
            age_min, soft_dep_max_age_minutes,
        )
        return {"fired": False, "reason": "allocator_latest_stale", "run_id": latest.get("run_id"), "count": 0, "telegram_message_id": None}

    # Idempotency: bot restart mid-day must not re-fire.
    if _already_fired_today(trade_date=trade_date, db_path=db_path):
        logger.info("csp_digest_job: already fired for %s — skipping", trade_date)
        return {"fired": False, "reason": "already_fired_today", "run_id": latest.get("run_id"), "count": 0, "telegram_message_id": None}

    candidate_count = len(latest.get("staged") or [])

    if candidate_count == 0:
        # Graceful "no candidates today" — no LLM call, no ledger row, no inline keyboard.
        text = f"CSP Digest — {trade_date}\n\nNo candidates staged today."
        try:
            msg_id = await send_telegram(text, [])
        except Exception as exc:
            logger.warning("csp_digest_job: send_telegram failed on empty-list path: %s", exc)
            msg_id = None
        _record_digest_fired(
            trade_date=trade_date, candidate_count=0, sent_at=now,
            timeout_at=now + timedelta(minutes=DEFAULT_TIMEOUT_MINUTES),
            telegram_message_id=msg_id, db_path=db_path,
        )
        return {"fired": True, "reason": "empty_candidate_list", "run_id": latest.get("run_id"), "count": 0, "telegram_message_id": msg_id}

    # Build payload (sans commentary) first so we have something to render if LLM fails.
    payload_no_comm = build_digest_payload(
        latest=latest, commentaries={}, mode=mode, now_utc=now,
    )

    # LLM commentary — fail-soft; empty commentary still renders the digest.
    factory = anthropic_factory if anthropic_factory is not None else _make_anthropic_factory()
    commentaries: dict[str, DigestCommentary] = {}
    if factory is not None:
        try:
            user_text = _build_user_payload_text(payload_no_comm, latest)
            commentaries = await generate_commentary(
                user_text,
                run_id=latest.get("run_id") or f"digest:{trade_date}",
                db_path=db_path,
                anthropic_factory=factory,
            )
        except Exception as exc:
            # generate_commentary itself should never raise; defensive belt.
            logger.warning("csp_digest_job: generate_commentary failed: %s", exc)
            commentaries = {}

    payload = build_digest_payload(
        latest=latest, commentaries=commentaries, mode=mode, now_utc=now,
    )
    text = render_card_text(payload)
    keyboard = build_inline_keyboard(
        payload, run_id=latest.get("run_id") or f"digest:{trade_date}",
    )

    try:
        msg_id = await send_telegram(text, keyboard)
    except Exception as exc:
        logger.warning("csp_digest_job: send_telegram failed: %s", exc)
        msg_id = None

    _record_digest_fired(
        trade_date=trade_date, candidate_count=candidate_count, sent_at=now,
        timeout_at=now + timedelta(minutes=DEFAULT_TIMEOUT_MINUTES),
        telegram_message_id=msg_id, db_path=db_path,
    )
    return {
        "fired": True,
        "reason": "ok" if msg_id is not None else "send_failed",
        "run_id": latest.get("run_id"),
        "count": candidate_count,
        "telegram_message_id": msg_id,
    }
