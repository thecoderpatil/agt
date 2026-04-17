"""ADR-007 invariant-detection tick — shared by bot + scheduler daemon.

The tick runs every 60s in whichever process owns the gated job set:
  - bot     when USE_SCHEDULER_DAEMON=0 (current default, Sprint A pre-MR4)
  - daemon  when USE_SCHEDULER_DAEMON=1 (post-MR4 cutover)

Before MR !84 this logic lived only in ``agt_scheduler.py``'s ``_heartbeat_job``,
so in the current flag state the invariants were *declared but never run* —
the ``incidents`` table had zero rows despite nine active invariants. MR !84
extracts the tick into this shared module and wires it into the bot's
JobQueue so detection runs in whichever process is the live owner.

Every Violation produced by ``run_all`` maps to one idempotent
``incidents_repo.register`` call keyed on ``"{inv_id}:{evidence_fingerprint}"``,
so repeat detections of the same breach bump ``consecutive_breaches`` rather
than inserting new rows. Per ADR-007 §9.3 the authoring/approval rate
limit (5/hr) applies downstream — detection is deliberately cheap and
un-rate-limited here.

Contract: this function never raises. Import failures, YAML load failures,
DB errors, and per-check exceptions are all caught and logged so a single
bad tick cannot kill the 60s loop. The scheduler owns ``clientId=2`` on
the live Gateway and the bot owns Telegram long-polling; one unguarded
exception in either loop is a live-capital hazard.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _evidence_fingerprint(evidence: dict[str, Any] | None) -> str:
    """Stable short hash of a Violation's evidence dict.

    Used as the suffix of the ``incidents_repo.register`` ``incident_key``
    so repeat detections of the same breach collapse onto a single row.
    Non-serializable evidence falls back to ``str()`` so the fingerprint
    path never crashes the tick.
    """
    try:
        blob = json.dumps(evidence or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(evidence)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def check_invariants_tick(detector: str = "adr_007.invariants.tick") -> int:
    """Run every invariant once and register resulting Violations.

    Returns the number of Violations registered (primarily for tests and
    metrics; production callers ignore the return value).

    ``detector`` lets the caller stamp which process produced the incidents
    — the scheduler daemon passes ``"agt_scheduler.heartbeat"`` and the bot
    passes ``"telegram_bot.invariants_tick"`` so incident forensics can
    distinguish ownership during the Sprint A observation window.
    """
    registered = 0
    try:
        from agt_equities import incidents_repo
        from agt_equities.invariants import load_invariants, run_all
    except Exception:
        logger.exception("invariants import failed; skipping tick")
        return 0

    try:
        manifest = load_invariants()
        invariant_meta: dict[str, dict[str, Any]] = {
            entry["id"]: entry for entry in manifest
        }
        results = run_all()
    except Exception:
        logger.exception("invariants runner failed; skipping tick")
        return 0

    for inv_id, violations in results.items():
        if not violations:
            continue
        meta = invariant_meta.get(inv_id, {})
        severity = str(meta.get("severity_floor", "medium"))
        scrutiny_tier = str(meta.get("scrutiny_tier", "medium"))
        for v in violations:
            try:
                # MR !84: prefer Violation.stable_key if the check author set
                # one (e.g. NO_STALE_RED_ALERT keys on household) so
                # repeated ticks bump consecutive_breaches instead of
                # INSERTing a new row when evidence carries time-varying
                # fields like age_hours. Falls back to evidence fingerprint
                # for checks whose evidence is naturally stable.
                stable = getattr(v, "stable_key", None)
                if stable:
                    incident_key = stable
                else:
                    incident_key = (
                        f"{inv_id}:{_evidence_fingerprint(getattr(v, 'evidence', {}))}"
                    )
                detected_at = getattr(v, "detected_at", None)
                observed_state = {
                    "description": getattr(v, "description", ""),
                    "evidence": getattr(v, "evidence", {}),
                    "detected_at": (
                        detected_at.isoformat()
                        if detected_at is not None
                        else None
                    ),
                }
                incidents_repo.register(
                    incident_key,
                    severity=severity,
                    scrutiny_tier=scrutiny_tier,
                    detector=detector,
                    invariant_id=inv_id,
                    observed_state=observed_state,
                )
                registered += 1
            except Exception:
                logger.exception(
                    "incidents_repo.register failed for invariant %s", inv_id
                )
    return registered
