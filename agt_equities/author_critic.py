"""ADR-007 Step 6 — Author/Critic mechanical pipeline.

The autonomous remediation task ("agt-remediation-weekly") authors a
fix as a GitLab MR with one Author LLM call, then passes through this
module for mechanical Critic review before any human Telegram approval
is requested.

ADR-007 §4.4 originally proposed "Author LLM + Critic LLM". §9.1
(v0.2, 2026-04-16) revises that: the default Critic is a mechanical
pipeline that costs zero LLM quota; an LLM-Critic is invoked only on
``scrutiny_tier == "high"`` incidents when the Author reports
confidence < 0.6. This module ships the mechanical pipeline + the
escalation gate + the state write-back; the LLM-Critic call itself
lives in the scheduled-task prompt (outside git), with this module
persisting its payload.

The three mechanical checks in v1:

    1. Path whitelist          — refuse MRs that touch protected files
                                 (walker.py, flex_sync.py, cure_*.html,
                                 test_command_prune.py, boot_desk.bat).
    2. Invariant still present — re-run the incident's invariant check
                                 against current DB state; if the
                                 condition has cleared, Critic signals
                                 ``resolve`` instead of awaiting approval.
    3. Pytest on changed paths — shallow-clone the feature branch (done
                                 by the calling scheduled task) and run
                                 pytest against any test files the MR
                                 changed; on failure, Author gets exactly
                                 one fixup attempt (§9.1).

ruff + pyflexes + phantom-API grep are deliberately deferred to a
follow-up inside Step 6 — they are additive and shouldn't gate the
mechanical/LLM-gate primitives that the other roadmap steps depend on.

Outputs land in the ``incidents`` table:

    - ``incidents.confidence`` — final confidence score (mech-computed,
      optionally min'd with LLM-Critic confidence).
    - ``incidents.status``     — flipped to ``authoring`` → ``awaiting_approval``
      on a clean mechanical pass, or to ``rejected_*`` / ``needs_architect``
      on mechanical failure per the ladder in ``record_critic_outcome``.
    - ``incidents.desired_state`` — receives a JSON envelope with the
      Author's branch/mr_iid so the Critic (and Telegram renderer) can
      look up the live MR without a second GitLab call.

The module is intentionally subprocess/IO light — pytest and
``git clone`` are the calling scheduled task's responsibility (to keep
this module unit-testable). The module only runs ``sys.executable -m
pytest`` against a caller-supplied ``workdir``.

DOES NOT revive the DEPRECATED state helpers from
``agt_equities.remediation``; every state read/write funnels through
``agt_equities.incidents_repo``.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities import incidents_repo
from agt_equities.db import get_db_connection, tx_immediate

log = logging.getLogger(__name__)

__all__ = [
    "PROTECTED_PATHS",
    "HIGH_SCRUTINY_TIER",
    "ARCHITECT_ONLY_TIER",
    "LLM_CRITIC_CONFIDENCE_THRESHOLD",
    "MAX_LLM_CALLS_PER_INCIDENT",
    "MechanicalCriticResult",
    "fetch_mr_changed_paths",
    "run_mechanical_critic",
    "should_escalate_to_llm_critic",
    "record_author_outcome",
    "record_critic_outcome",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Paths whose mutation blocks a critic pass regardless of everything else.
# Source: project CLAUDE.md "Prohibited file touches" + ADR-007 §9.1.
PROTECTED_PATHS: frozenset[str] = frozenset({
    "agt_equities/walker.py",
    "agt_equities/flex_sync.py",
    "boot_desk.bat",
    "cure_lifecycle.html",
    "cure_smart_friction.html",
    "tests/test_command_prune.py",
})

# Scrutiny tier tokens (strings, not an enum — keeps the DB layer
# schema-agnostic and the YAML authoring surface light).
HIGH_SCRUTINY_TIER = "high"
ARCHITECT_ONLY_TIER = "architect_only"

# ADR-007 §9.1: LLM-Critic escalates only when the Author-reported
# confidence falls below this threshold *and* the incident is high-tier.
LLM_CRITIC_CONFIDENCE_THRESHOLD = 0.6

# ADR-007 §9.1 per-incident budget (1 Author + ≤1 pytest fixup +
# ≤1 Opus retry + ≤1 LLM-Critic). Exported so the scheduled-task prompt
# can sanity-check its own call counter.
MAX_LLM_CALLS_PER_INCIDENT = 4

# Default pytest timeout for the mechanical Critic's subprocess call.
# Enough headroom for a small regression-test file; still bounded so a
# runaway pytest can't starve the scheduler.
_DEFAULT_PYTEST_TIMEOUT_SEC = 300


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class MechanicalCriticResult:
    """Machine-readable verdict from the mechanical pipeline.

    ``computed_confidence`` is the Critic's own score. It begins at
    ``author_confidence`` and is progressively lowered by failing
    checks:
        - hard block (path whitelist / architect_only)      -> 0.0
        - pytest-only failure (fixup retry path)            -> min(x, 0.3)
        - invariant no longer firing (auto-resolve)         -> x (unchanged)
        - mechanical pass                                   -> x (unchanged)
    The LLM-Critic, if invoked, can lower this further via
    ``record_critic_outcome``.

    ``needs_fixup`` is True iff the *only* failing check is pytest —
    Author gets exactly one re-author attempt per §9.1.

    ``needs_architect_reason`` carries a human-readable reason when the
    mechanical pipeline decides the incident cannot be autonomously
    remediated at all (path whitelist violation, architect-tier incident
    routed here by mistake).
    """

    passed: bool
    failed_checks: list[str]
    evidence: dict[str, Any]
    computed_confidence: float
    needs_fixup: bool = False
    needs_architect_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> "MechanicalCriticResult":
        data = json.loads(blob)
        # Normalise optional fields — older JSON may omit needs_fixup/reason.
        return cls(
            passed=bool(data.get("passed")),
            failed_checks=list(data.get("failed_checks") or []),
            evidence=dict(data.get("evidence") or {}),
            computed_confidence=float(data.get("computed_confidence", 0.0)),
            needs_fixup=bool(data.get("needs_fixup", False)),
            needs_architect_reason=data.get("needs_architect_reason") or None,
        )


# ---------------------------------------------------------------------------
# GitLab helper — Option (b) from the Step-6 kickoff: this module
# fetches its own diff metadata given (mr_iid), not the task.
# ---------------------------------------------------------------------------

def fetch_mr_changed_paths(mr_iid: int) -> list[str]:
    """Return ``new_path`` for every file in the MR diff.

    Honours renames via fallback to ``old_path``. Swallows GitLab errors
    (returns []) — the caller decides whether an empty list is a skip
    or a failure. Uses ``agt_equities.remediation._gitlab_request`` so
    we keep exactly one GitLab auth pattern in the repo.
    """
    try:
        from agt_equities.remediation import _gitlab_request  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - import failure path
        log.warning("remediation module unavailable: %s", exc)
        return []
    try:
        body = _gitlab_request(
            "GET", f"/projects/81096827/merge_requests/{int(mr_iid)}/changes"
        )
    except Exception as exc:
        log.warning("MR %s changes fetch failed: %s", mr_iid, exc)
        return []
    changes = body.get("changes") if isinstance(body, dict) else None
    if not changes:
        return []
    paths: list[str] = []
    for c in changes:
        if not isinstance(c, dict):
            continue
        p = c.get("new_path") or c.get("old_path")
        if p and isinstance(p, str):
            paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Individual mechanical checks (internal)
# ---------------------------------------------------------------------------

def _check_path_whitelist(
    changed_paths: list[str],
) -> tuple[bool, list[str]]:
    """Return (passed, protected_hits)."""
    hits = [p for p in changed_paths if p in PROTECTED_PATHS]
    return (not hits, hits)


def _check_invariant_still_present(
    *,
    invariant_id: str | None,
    db_path: str | Path | None,
) -> tuple[bool, dict[str, Any]]:
    """Return (still_present, details).

    ``still_present=False`` => the invariant's check function does NOT
    fire against current DB state; the bug already looks fixed. The
    caller will signal ``resolve``.

    ``still_present=True``  => the invariant still fires; the Author's
    fix is legitimate, proceed with approval.

    ``invariant_id`` None, unknown, or unable to load => skipped,
    returns ``(True, {'skipped': True, 'reason': ...})`` so the caller
    continues down the ladder. Conservative default: an unloadable
    invariant should NOT be the reason we auto-resolve.
    """
    if not invariant_id:
        return True, {"skipped": True, "reason": "no invariant_id on incident"}
    try:
        from agt_equities.invariants.runner import (
            build_context,
            load_invariants,
            run_all,
        )
    except Exception as exc:
        return True, {"skipped": True, "reason": f"runner import failed: {exc}"}
    try:
        manifest = load_invariants()
    except Exception as exc:
        return True, {"skipped": True, "reason": f"manifest load failed: {exc}"}
    known = {e.get("id") for e in manifest if isinstance(e, dict)}
    if invariant_id not in known:
        return True, {"skipped": True, "reason": "invariant not in manifest"}
    try:
        if db_path is None:
            ctx = build_context()
        else:
            ctx = build_context(db_path=str(db_path))
    except Exception as exc:
        return True, {"skipped": True, "reason": f"ctx build failed: {exc}"}
    try:
        results = run_all(ctx=ctx, db_path=ctx.db_path)
    except Exception as exc:
        return True, {"skipped": True, "reason": f"run_all failed: {exc}"}
    violations = results.get(invariant_id) or []
    return (len(violations) > 0, {"violations": len(violations)})


def _check_pytest(
    *,
    workdir: Path,
    changed_paths: list[str],
    timeout_sec: int = _DEFAULT_PYTEST_TIMEOUT_SEC,
) -> tuple[bool, dict[str, Any]]:
    """Run pytest in ``workdir`` against changed test files.

    If the MR changes no ``tests/*.py`` file, falls back to a sprint_a
    marker smoke run — Author shipping a fix with zero regression test
    is a confidence-lowering signal, but not a hard fail.
    """
    test_targets = [
        p for p in changed_paths
        if p.startswith("tests/") and p.endswith(".py")
    ]
    if test_targets:
        args = ["--tb=short", "-q", "-x", *test_targets]
        evidence_extra: dict[str, Any] = {
            "mode": "targeted", "targets": test_targets,
        }
    else:
        args = ["-m", "sprint_a", "--tb=short", "-q", "-x"]
        evidence_extra = {
            "mode": "smoke_sprint_a",
            "reason": "MR changed no test files",
        }
    cmd = [sys.executable, "-m", "pytest", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, {
            "timeout": True,
            "timeout_sec": timeout_sec,
            **evidence_extra,
        }
    except FileNotFoundError as exc:
        return False, {
            "exec_failed": str(exc),
            **evidence_extra,
        }
    tail_lines = []
    if proc.stdout:
        tail_lines.extend(proc.stdout.splitlines()[-20:])
    if proc.stderr:
        tail_lines.append("---stderr---")
        tail_lines.extend(proc.stderr.splitlines()[-10:])
    return (
        proc.returncode == 0,
        {
            "returncode": proc.returncode,
            "tail": "\n".join(tail_lines),
            **evidence_extra,
        },
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_mechanical_critic(
    *,
    incident: dict[str, Any],
    author_confidence: float,
    changed_paths: list[str] | None = None,
    mr_iid: int | None = None,
    workdir: str | Path | None = None,
    db_path: str | Path | None = None,
    skip_pytest: bool = False,
    pytest_timeout_sec: int = _DEFAULT_PYTEST_TIMEOUT_SEC,
) -> MechanicalCriticResult:
    """Run the three Step-6 mechanical checks and return a verdict.

    Parameters
    ----------
    incident
        Row dict from ``incidents_repo.get``/``get_by_key``.
    author_confidence
        0.0..1.0 Author-reported score. Upper-bound for
        ``computed_confidence``.
    changed_paths
        Pre-fetched MR new_paths. If None *and* ``mr_iid`` is given,
        the module fetches via ``fetch_mr_changed_paths``.
    mr_iid
        MR internal id — used only to fetch paths on demand.
    workdir
        Path to a checkout of the feature branch. Pytest runs there.
        If None, pytest is skipped even if ``skip_pytest`` is False
        (we never invent a working directory).
    db_path
        DB used for the invariant-still-present check. None ⇒
        ``build_context`` default (prod).
    skip_pytest
        Fast-path for CI/unit tests: bypass the pytest subprocess.
    """
    if not (0.0 <= author_confidence <= 1.0):
        raise ValueError(
            f"author_confidence {author_confidence} must be in [0.0, 1.0]"
        )

    failed: list[str] = []
    evidence: dict[str, Any] = {}
    architect_reason: str | None = None
    needs_fixup = False

    # Scrutiny tier hard-block: architect_only incidents never pass
    # mechanical review — they must go to a human.
    scrutiny = (incident.get("scrutiny_tier") or "").lower()
    evidence["scrutiny_tier"] = scrutiny or None
    if scrutiny == ARCHITECT_ONLY_TIER:
        failed.append("architect_only_tier")
        architect_reason = (
            f"incident {incident.get('incident_key')!r} is "
            f"scrutiny_tier={ARCHITECT_ONLY_TIER}; mechanical critic "
            f"refuses to autonomously approve"
        )

    if changed_paths is None and mr_iid is not None:
        changed_paths = fetch_mr_changed_paths(int(mr_iid))
    paths = list(changed_paths or [])
    evidence["changed_paths"] = paths

    # Check 1: path whitelist. Hard architect-escalation on hit.
    wl_ok, hits = _check_path_whitelist(paths)
    evidence["path_whitelist"] = {"passed": wl_ok, "protected_hits": hits}
    if not wl_ok:
        failed.append("path_whitelist")
        architect_reason = (
            f"MR touches protected path(s): {sorted(hits)} "
            f"(see CLAUDE.md 'Prohibited file touches')"
        )

    # Check 2: invariant still present. Never architect-escalates;
    # signals auto-resolve when the bug has already cleared.
    inv_present, inv_detail = _check_invariant_still_present(
        invariant_id=incident.get("invariant_id"),
        db_path=db_path,
    )
    evidence["invariant_still_present"] = {
        "still_present": inv_present, **inv_detail,
    }

    # Check 3: pytest. Optional (tests + fast paths skip it).
    if skip_pytest or workdir is None:
        evidence["pytest"] = {
            "skipped": True,
            "reason": "skip_pytest set" if skip_pytest else "no workdir supplied",
        }
    else:
        pt_ok, pt_detail = _check_pytest(
            workdir=Path(workdir),
            changed_paths=paths,
            timeout_sec=pytest_timeout_sec,
        )
        evidence["pytest"] = pt_detail
        if not pt_ok:
            failed.append("pytest")
            needs_fixup = True  # §9.1 one-shot fixup path

    # Confidence arithmetic.
    hard_block = architect_reason is not None
    if hard_block:
        computed = 0.0
    elif needs_fixup:
        computed = min(author_confidence, 0.3)
    else:
        computed = author_confidence

    return MechanicalCriticResult(
        passed=not failed,
        failed_checks=failed,
        evidence=evidence,
        computed_confidence=round(float(computed), 4),
        needs_fixup=(needs_fixup and not hard_block),
        needs_architect_reason=architect_reason,
    )


def should_escalate_to_llm_critic(
    *,
    incident: dict[str, Any],
    author_confidence: float,
    mech: MechanicalCriticResult,
) -> bool:
    """ADR-007 §9.1 gate.

    LLM-Critic escalates iff ALL of:
      - mechanical Critic passed (never escalate over a mech failure;
        those either go to human or to fixup retry).
      - incident.scrutiny_tier == "high".
      - author_confidence < LLM_CRITIC_CONFIDENCE_THRESHOLD (0.6).
    """
    if not mech.passed:
        return False
    scrutiny = (incident.get("scrutiny_tier") or "").lower()
    if scrutiny != HIGH_SCRUTINY_TIER:
        return False
    return float(author_confidence) < LLM_CRITIC_CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# DB write-back wrappers
# ---------------------------------------------------------------------------

def record_author_outcome(
    incident_id: int,
    *,
    author_confidence: float,
    branch: str,
    mr_iid: int,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record the Author's MR reference and confidence. Idempotent.

    - Transitions open/rejected_once/rejected_twice → authoring via
      ``incidents_repo.mark_authoring``. No-op if already authoring.
    - Stores ``author_confidence`` into ``incidents.confidence``.
    - Stashes ``{branch, mr_iid}`` under ``desired_state.author`` so
      the Critic + Telegram renderer can find the MR without a second
      GitLab call.
    """
    if not (0.0 <= author_confidence <= 1.0):
        raise ValueError(
            f"author_confidence {author_confidence} must be in [0.0, 1.0]"
        )
    if not branch:
        raise ValueError("branch is required")
    if mr_iid is None:
        raise ValueError("mr_iid is required")

    cur = incidents_repo.get(incident_id, db_path=db_path)
    if cur is None:
        raise ValueError(f"unknown incident id: {incident_id}")

    if cur.get("status") in (
        incidents_repo.STATUS_OPEN,
        incidents_repo.STATUS_REJECTED_ONCE,
        incidents_repo.STATUS_REJECTED_TWICE,
    ):
        try:
            incidents_repo.mark_authoring(incident_id, db_path=db_path)
        except ValueError:
            # Transition already won by a concurrent writer — fine.
            log.debug(
                "mark_authoring lost race for incident %s", incident_id,
            )

    _write_confidence(
        incident_id, confidence=author_confidence, db_path=db_path,
    )
    _stash_author_metadata(
        incident_id, branch=branch, mr_iid=mr_iid, db_path=db_path,
    )
    return incidents_repo.get(incident_id, db_path=db_path) or {}


def record_critic_outcome(
    incident_id: int,
    *,
    mech: MechanicalCriticResult,
    llm_critic: dict[str, Any] | None = None,
    mr_iid: int | None = None,
    branch: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Dispatch the incident's state transition from a Critic verdict.

    Decision ladder (first match wins):
        1. mech.needs_architect_reason        -> mark_needs_architect
        2. mech.needs_fixup                   -> append_rejection_reason
                                                 (internal, no strike)
        3. invariant_still_present == False
           AND mech.passed                    -> mark_resolved
        4. llm_critic["confidence"] < 0.5     -> mark_needs_architect
        5. mech.passed                        -> mark_awaiting_approval
        6. default                            -> mark_rejected
                                                 (consumes one strike)

    The final ``confidence`` written to ``incidents.confidence`` is
    ``min(mech.computed_confidence, llm_critic.confidence)`` — the more
    pessimistic of the two.
    """
    cur = incidents_repo.get(incident_id, db_path=db_path)
    if cur is None:
        raise ValueError(f"unknown incident id: {incident_id}")

    llm_confidence: float | None = None
    llm_reason: str = ""
    if llm_critic is not None:
        raw = llm_critic.get("confidence")
        if raw is not None:
            llm_confidence = float(raw)
            if not (0.0 <= llm_confidence <= 1.0):
                raise ValueError(
                    f"llm_critic.confidence {llm_confidence} outside [0,1]"
                )
        llm_reason = str(llm_critic.get("reason") or "")

    final_conf = float(mech.computed_confidence)
    if llm_confidence is not None:
        final_conf = min(final_conf, llm_confidence)

    inv_detail = mech.evidence.get("invariant_still_present") or {}
    still_present = bool(inv_detail.get("still_present", True))

    # Ladder.
    if mech.needs_architect_reason:
        incidents_repo.mark_needs_architect(
            incident_id, mech.needs_architect_reason, db_path=db_path,
        )
    elif mech.needs_fixup:
        incidents_repo.append_rejection_reason(
            incident_id,
            _fmt_fixup_reason(mech),
            db_path=db_path,
        )
    elif (not still_present) and mech.passed:
        incidents_repo.mark_resolved(incident_id, db_path=db_path)
    elif llm_confidence is not None and llm_confidence < 0.5:
        incidents_repo.mark_needs_architect(
            incident_id,
            _fmt_llm_reject_reason(llm_confidence, llm_reason),
            db_path=db_path,
        )
    elif mech.passed:
        if mr_iid is None:
            # Cannot transition to awaiting_approval without an MR
            # reference — fail loud so the scheduled task can retry
            # with the MR iid present.
            incidents_repo.mark_rejected(
                incident_id,
                "mechanical critic passed but no mr_iid supplied to "
                "record_critic_outcome (scheduled-task wiring bug)",
                db_path=db_path,
            )
        else:
            kwargs: dict[str, Any] = {"mr_iid": int(mr_iid)}
            if branch is not None:
                kwargs["branch_name"] = branch
            incidents_repo.mark_awaiting_approval(
                incident_id, db_path=db_path, **kwargs,
            )
    else:
        incidents_repo.mark_rejected(
            incident_id,
            _fmt_generic_reject_reason(mech),
            db_path=db_path,
        )

    _write_confidence(
        incident_id, confidence=final_conf, db_path=db_path,
    )
    return incidents_repo.get(incident_id, db_path=db_path) or {}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _write_confidence(
    incident_id: int,
    *,
    confidence: float,
    db_path: str | Path | None,
) -> None:
    conn = get_db_connection(db_path=db_path)
    try:
        with tx_immediate(conn):
            conn.execute(
                "UPDATE incidents SET confidence = ? WHERE id = ?",
                (float(confidence), int(incident_id)),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _stash_author_metadata(
    incident_id: int,
    *,
    branch: str,
    mr_iid: int,
    db_path: str | Path | None,
) -> None:
    """Non-destructive JSON merge into ``incidents.desired_state``."""
    conn = get_db_connection(db_path=db_path)
    try:
        with tx_immediate(conn):
            row = conn.execute(
                "SELECT desired_state FROM incidents WHERE id = ?",
                (int(incident_id),),
            ).fetchone()
            if row is None:
                return
            prev_raw = row[0] or "{}"
            try:
                prev = json.loads(prev_raw)
                if not isinstance(prev, dict):
                    prev = {"legacy": prev_raw}
            except Exception:
                prev = {"legacy": prev_raw}
            prev["author"] = {"branch": branch, "mr_iid": int(mr_iid)}
            conn.execute(
                "UPDATE incidents "
                "SET desired_state = ?, last_action_at = ? "
                "WHERE id = ?",
                (
                    json.dumps(prev, default=str, sort_keys=True),
                    _iso_now(),
                    int(incident_id),
                ),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate(s: Any, n: int) -> str:
    if s is None:
        return ""
    text = str(s)
    if len(text) <= n:
        return text
    return text[: max(0, n - 3)] + "..."


def _fmt_fixup_reason(mech: MechanicalCriticResult) -> str:
    tail = (mech.evidence.get("pytest") or {}).get("tail", "")
    return (
        "mechanical pytest failed; Author gets one fixup attempt "
        f"(ADR-007 §9.1). tail={_truncate(tail, 400)}"
    )


def _fmt_llm_reject_reason(confidence: float, reason: str) -> str:
    return (
        f"LLM-Critic confidence {confidence:.2f} < 0.5 threshold; "
        f"reason: {_truncate(reason, 400)}"
    )


def _fmt_generic_reject_reason(mech: MechanicalCriticResult) -> str:
    ev = _truncate(json.dumps(mech.evidence, default=str, sort_keys=True), 400)
    return (
        f"Mechanical Critic rejected "
        f"(failed_checks={mech.failed_checks}); evidence: {ev}"
    )
