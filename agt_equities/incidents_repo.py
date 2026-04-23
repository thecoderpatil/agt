"""ADR-007 Step 3 — structured `incidents` CRUD.

The `incidents` table is the machine-readable source of truth for the
self-healing loop. Rows are authored by:

- `scripts/check_invariants.py` on scheduler heartbeat (Step 4).
- The weekly Opus digest task (Step 5, replacing the prose directive).
- Manual writes by operator tooling.

State machine (condensed — see ADR-007 §4.2 for full narrative):

    open
      │
      ▼
    authoring         ◄─── (optional: Author LLM working)
      │
      ▼
    awaiting_approval ◄─── (MR staged, Critic cleared, Telegram gate pending)
      │
      ├─► merged
      ├─► rejected_once
      │     │
      │     ▼
      │   rejected_twice
      │     │
      │     ▼
      │   rejected_permanently
      ├─► needs_architect
      └─► resolved       ◄─── (invariant auto-cleared without an MR)

Dual-write discipline
---------------------

For the first two sprints after ADR-007 lands, every state-advancing call
mirrors into the legacy `remediation_incidents` table so the weekly
remediation pipeline (scripts/rem_incidents.py, /approve_rem, /list_rem)
keeps functioning without code changes. Step 5 of the roadmap retires
that table; until then, treat both tables as linked by `incident_key`.

Kwarg convention
----------------

Every public function accepts a keyword-only `db_path` per the Sprint A
FU-A ruling. Writers funnel through `agt_equities.db.get_db_connection`
(so WAL + busy_timeout behavior is uniform). Reads use
`agt_equities.db.get_ro_connection`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agt_equities.db import get_db_connection, get_ro_connection, tx_immediate

__all__ = [
    # Status constants
    "STATUS_OPEN",
    "STATUS_AUTHORING",
    "STATUS_AWAITING",
    "STATUS_MERGED",
    "STATUS_REJECTED_ONCE",
    "STATUS_REJECTED_TWICE",
    "STATUS_REJECTED_PERM",
    "STATUS_ARCHITECT",
    "STATUS_RESOLVED",
    "ACTIVE_STATUSES",
    "CLOSED_STATUSES",
    # Severity / scrutiny enums (string-typed; not enforced by DB)
    "SEVERITIES",
    "SCRUTINY_TIERS",
    # Reads
    "get",
    "get_by_key",
    "list_by_status",
    "list_active_for_invariant",
    "list_authorable",
    "list_architect_only",
    "DEFAULT_AUTHORABLE_STATUSES",
    "AUTHORABLE_SCRUTINY_TIERS",
    "ARCHITECT_ONLY_SCRUTINY_TIERS",
    # Writes
    "register",
    "mark_authoring",
    "mark_awaiting_approval",
    "mark_merged",
    "mark_rejected",
    "mark_needs_architect",
    "mark_resolved",
    "append_rejection_reason",
]


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_OPEN = "open"
STATUS_AUTHORING = "authoring"
STATUS_AWAITING = "awaiting_approval"
STATUS_MERGED = "merged"
STATUS_REJECTED_ONCE = "rejected_once"
STATUS_REJECTED_TWICE = "rejected_twice"
STATUS_REJECTED_PERM = "rejected_permanently"
STATUS_ARCHITECT = "needs_architect"
STATUS_RESOLVED = "resolved"

_ALL_STATUSES = frozenset({
    STATUS_OPEN, STATUS_AUTHORING, STATUS_AWAITING, STATUS_MERGED,
    STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE, STATUS_REJECTED_PERM,
    STATUS_ARCHITECT, STATUS_RESOLVED,
})

ACTIVE_STATUSES = frozenset({
    STATUS_OPEN, STATUS_AUTHORING, STATUS_AWAITING, STATUS_ARCHITECT,
    STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE,
})

# ADR-007 Step 7a: subset of ACTIVE_STATUSES that are eligible for the
# Author LLM to pick up. Excludes AUTHORING/AWAITING/ARCHITECT which are
# already past the Author-kickoff gate.
DEFAULT_AUTHORABLE_STATUSES: tuple[str, ...] = (
    STATUS_OPEN, STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE,
)

CLOSED_STATUSES = frozenset({
    STATUS_MERGED, STATUS_RESOLVED, STATUS_REJECTED_PERM,
})

SEVERITIES = frozenset({"info", "warn", "medium", "high", "crit", "critical"})
SCRUTINY_TIERS = frozenset({"low", "medium", "high", "architect_only"})

# ADR-007 Step 7b: scrutiny-tier routing for the Author/Critic pipeline.
# ``AUTHORABLE_SCRUTINY_TIERS`` gates ``list_authorable`` -- the tiers
# whose incidents are SAFE to hand to the Author/Critic LLM pipeline
# ("architect_only" is excluded because ``run_mechanical_critic`` hard-
# blocks it anyway, so authoring them wastes LLM spend + accumulates
# strikes on a row that can never pass mechanical review).
# ``ARCHITECT_ONLY_SCRUTINY_TIERS`` is the complement, consumed by
# ``list_architect_only`` for the escalation lane of the digest.
AUTHORABLE_SCRUTINY_TIERS: frozenset[str] = frozenset({"low", "medium", "high"})
ARCHITECT_ONLY_SCRUTINY_TIERS: frozenset[str] = frozenset({"architect_only"})


# Map incidents.status → remediation_incidents.status for dual-write.
# `authoring` and `resolved` have no analogue in the legacy table; we map
# them to the closest neighbour so the legacy pipeline does not trip on
# unknown statuses.
_REM_STATUS_MAP: dict[str, str] = {
    STATUS_OPEN: "new",
    STATUS_AUTHORING: "new",
    STATUS_AWAITING: "awaiting_approval",
    STATUS_MERGED: "merged",
    STATUS_RESOLVED: "merged",
    STATUS_REJECTED_ONCE: "rejected_once",
    STATUS_REJECTED_TWICE: "rejected_twice",
    STATUS_REJECTED_PERM: "rejected_permanently",
    STATUS_ARCHITECT: "needs_architect",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _jsonify(value: Any) -> str | None:
    """JSON-serialize a dict/list payload. None passes through."""
    if value is None:
        return None
    if isinstance(value, str):
        # Caller already serialized — trust it, but validate.
        try:
            json.loads(value)
        except Exception as exc:
            raise ValueError(f"invalid JSON string payload: {exc}") from exc
        return value
    return json.dumps(value, default=str, sort_keys=True)


def _validate_status(status: str) -> None:
    if status not in _ALL_STATUSES:
        raise ValueError(
            f"unknown status {status!r}; expected one of {sorted(_ALL_STATUSES)}"
        )


def _active_row_for_key(
    conn: sqlite3.Connection, incident_key: str
) -> sqlite3.Row | None:
    """Return the currently-active row for this key, or None.

    An incident_key may have at most one row in an active status — this
    is the invariant enforced by the partial unique index in schema.py.
    Closed (merged/resolved/rejected_permanently) rows are ignored so a
    re-breach can legitimately open a fresh row.
    """
    return conn.execute(
        """
        SELECT * FROM incidents
        WHERE incident_key = ?
          AND status NOT IN ('merged','resolved','rejected_permanently')
        ORDER BY id DESC LIMIT 1
        """,
        (incident_key,),
    ).fetchone()


def _tables_present(conn: sqlite3.Connection, names: Iterable[str]) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    existing = {r[0] for r in rows}
    return {n for n in names if n in existing}


# ---------------------------------------------------------------------------
# Dual-write adapter (incidents ↔ remediation_incidents)
# ---------------------------------------------------------------------------

def _mirror_register(
    conn: sqlite3.Connection,
    *,
    incident_key: str,
    detector: str,
    detected_at: str,
) -> None:
    """INSERT OR IGNORE a `new` row into remediation_incidents.

    Swallowed table-missing is acceptable: tests spin up isolated DBs with
    only the new incidents table, and the dual-write must not block them.
    """
    if "remediation_incidents" not in _tables_present(conn, ("remediation_incidents",)):
        return
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO remediation_incidents
                (incident_id, first_detected, directive_source, status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (incident_key, detected_at, detector, "new", detected_at),
        )
    except sqlite3.OperationalError:
        # Schema skew on an older DB — prefer the new table's write over
        # a hard failure. A migration sweep is part of retiring the
        # legacy table in Step 5.
        pass


def _mirror_update_status(
    conn: sqlite3.Connection,
    *,
    incident_key: str,
    new_status: str,
    updated_at: str,
    mr_iid: int | None = None,
    branch_name: str | None = None,
    architect_reason: str | None = None,
    rejection_reasons_json: str | None = None,
) -> None:
    """Mirror a state transition into remediation_incidents."""
    if "remediation_incidents" not in _tables_present(conn, ("remediation_incidents",)):
        return
    mapped = _REM_STATUS_MAP.get(new_status)
    if mapped is None:
        return
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [mapped, updated_at]
    if mr_iid is not None:
        sets.append("mr_iid = ?")
        params.append(int(mr_iid))
    if branch_name is not None:
        sets.append("branch_name = ?")
        params.append(branch_name)
    if new_status == STATUS_AWAITING:
        sets.append("fix_authored_at = COALESCE(fix_authored_at, ?)")
        params.append(updated_at)
    if architect_reason is not None:
        sets.append("architect_reason = ?")
        params.append(architect_reason)
    if rejection_reasons_json is not None:
        sets.append("rejection_reasons = ?")
        params.append(rejection_reasons_json)
    params.append(incident_key)
    try:
        conn.execute(
            f"UPDATE remediation_incidents SET {', '.join(sets)} "
            f"WHERE incident_id = ?",
            params,
        )
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get(
    incident_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Fetch a single incident row by numeric id."""
    conn = get_ro_connection(db_path=db_path)
    try:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (int(incident_id),)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_by_key(
    incident_key: str,
    *,
    active_only: bool = True,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Latest row for `incident_key`. If `active_only`, excludes closed rows."""
    conn = get_ro_connection(db_path=db_path)
    try:
        if active_only:
            row = conn.execute(
                """
                SELECT * FROM incidents
                WHERE incident_key = ?
                  AND status NOT IN ('merged','resolved','rejected_permanently')
                ORDER BY id DESC LIMIT 1
                """,
                (incident_key,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM incidents WHERE incident_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (incident_key,),
            ).fetchone()
        return _row_to_dict(row)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_by_status(
    statuses: Iterable[str],
    *,
    limit: int | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """All rows matching any of the given statuses, oldest first."""
    status_list = [s for s in statuses if s]
    if not status_list:
        return []
    for s in status_list:
        _validate_status(s)
    placeholders = ",".join("?" * len(status_list))
    sql = (
        f"SELECT * FROM incidents WHERE status IN ({placeholders}) "
        f"ORDER BY COALESCE(last_action_at, detected_at) ASC, id ASC"
    )
    params: list[Any] = list(status_list)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = get_ro_connection(db_path=db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_authorable(
    *,
    statuses: Iterable[str] = DEFAULT_AUTHORABLE_STATUSES,
    scrutiny_tiers: Iterable[str] = AUTHORABLE_SCRUTINY_TIERS,
    manifest: list[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return active incidents that have stabilized past their manifest
    threshold AND fall under a scrutiny tier that is safe to autonomously
    author -- i.e. rows eligible for the Author/Critic LLM pipeline.

    An incident is authorable when:
        row["status"] in statuses
        AND row["consecutive_breaches"] >= manifest_entry["max_consecutive_violations"]
        AND row["scrutiny_tier"] in scrutiny_tiers

    Rows whose ``invariant_id`` does not appear in the manifest are
    fail-open on the *threshold* check (included). Rationale: the Author
    consuming the digest should see everything unaccounted-for -- better
    to generate a spurious author pass than to silently drop an incident
    that has lost its manifest entry (e.g. mid-rename).

    Rows with no ``invariant_id`` at all (legacy detectors that don't
    declare one) are also fail-open on the threshold check for the same
    reason.

    The *scrutiny tier* check is ALSO fail-open: a row with an unknown or
    empty ``scrutiny_tier`` is included, so a schema skew never silently
    drops an incident. This matches the threshold fail-open rationale.

    ADR-007 Step 7b: by default, ``scrutiny_tiers`` excludes
    ``"architect_only"`` -- ``author_critic.run_mechanical_critic`` hard-
    blocks that tier anyway, so authoring them wastes LLM spend and
    accumulates rejection strikes on rows that can never pass. The
    escalation lane for those incidents lives in ``list_architect_only``.

    Args:
        statuses: status filter. Defaults to
            ``DEFAULT_AUTHORABLE_STATUSES`` (open, rejected_once,
            rejected_twice). Pass a wider tuple to include, e.g.,
            awaiting_approval for read-only inspection.
        scrutiny_tiers: tier filter. Defaults to
            ``AUTHORABLE_SCRUTINY_TIERS`` (``low``/``medium``/``high``).
            Pass ``SCRUTINY_TIERS`` to include every tier, or
            ``ARCHITECT_ONLY_SCRUTINY_TIERS`` to invert (though
            ``list_architect_only`` is the ergonomic way).
        manifest: optional injection for tests. Defaults to
            ``agt_equities.invariants.runner.load_invariants()``.
        db_path: override db path.

    Returns:
        List of row dicts (same shape as ``list_by_status``), in the
        same ascending-last_action_at order, filtered to authorable rows.
    """
    if manifest is None:
        from agt_equities.invariants.runner import load_invariants
        manifest = load_invariants()

    thresholds: dict[str, int] = {}
    for entry in manifest:
        inv_id = entry.get("id")
        if not inv_id:
            continue
        try:
            thresholds[inv_id] = int(entry.get("max_consecutive_violations", 1))
        except (TypeError, ValueError):
            # Bad manifest entry -- fall back to 1 so the row is treated
            # as eligible on first detection (safer than dropping).
            thresholds[inv_id] = 1

    tier_allow: set[str] = {str(t).lower() for t in scrutiny_tiers}

    rows = list_by_status(list(statuses), db_path=db_path)

    out: list[dict[str, Any]] = []
    for r in rows:
        inv_id = r.get("invariant_id")
        breaches = int(r.get("consecutive_breaches") or 0)

        # Threshold gate (fail-open on missing manifest entry).
        if not inv_id or inv_id not in thresholds:
            threshold_ok = True
        else:
            threshold_ok = breaches >= thresholds[inv_id]
        if not threshold_ok:
            continue

        # Scrutiny tier gate (fail-open on missing/unknown tier so a
        # schema skew never silently drops an incident).
        row_tier = (r.get("scrutiny_tier") or "").strip().lower()
        if row_tier and row_tier not in tier_allow:
            continue

        out.append(r)
    return out


def list_architect_only(
    *,
    statuses: Iterable[str] = DEFAULT_AUTHORABLE_STATUSES,
    manifest: list[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return stabilized incidents that require human (architect) action.

    The complement of ``list_authorable`` along the scrutiny-tier axis:
    same threshold + status gating, but filtered to
    ``ARCHITECT_ONLY_SCRUTINY_TIERS`` -- i.e. tiers whose remediation the
    Author/Critic pipeline refuses to autonomously approve (compliance
    rails, live-only behavior, etc.; see the manifest in
    ``agt_equities/safety_invariants.yaml``).

    Consumed by ``scripts/incidents_digest.py --authorable`` to surface
    architect-only rows in a distinct "escalate" section of the Opus
    weekly digest, separate from the rows handed to the Author/Critic
    pipeline. ADR-007 Step 7b.
    """
    return list_authorable(
        statuses=statuses,
        scrutiny_tiers=ARCHITECT_ONLY_SCRUTINY_TIERS,
        manifest=manifest,
        db_path=db_path,
    )


def list_active_for_invariant(
    invariant_id: str,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Active (non-closed) incidents tied to a specific invariant_id."""
    conn = get_ro_connection(db_path=db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM incidents
            WHERE invariant_id = ?
              AND status NOT IN ('merged','resolved','rejected_permanently')
            ORDER BY detected_at ASC, id ASC
            """,
            (invariant_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def register(
    incident_key: str,
    *,
    severity: str,
    scrutiny_tier: str,
    detector: str,
    invariant_id: str | None = None,
    observed_state: Any = None,
    desired_state: Any = None,
    confidence: float | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Idempotent register.

    If an active row already exists for this ``incident_key`` (per ADR-007
    §9.3 dedup rule), increment its ``consecutive_breaches`` counter,
    refresh ``last_action_at``, and merge the newer ``observed_state``
    (the authoring layer needs the most recent evidence). Return the
    existing row.

    Otherwise, INSERT a new ``open`` row and mirror-insert a ``new`` row
    into ``remediation_incidents`` for backwards compatibility.

    Raises:
        ValueError: if severity / scrutiny_tier are outside the known
            enums, or if JSON payload columns are malformed strings.
    """
    if not incident_key:
        raise ValueError("incident_key is required")
    if severity not in SEVERITIES:
        raise ValueError(
            f"unknown severity {severity!r}; expected one of {sorted(SEVERITIES)}"
        )
    if scrutiny_tier not in SCRUTINY_TIERS:
        raise ValueError(
            f"unknown scrutiny_tier {scrutiny_tier!r}; expected one of "
            f"{sorted(SCRUTINY_TIERS)}"
        )
    if not detector:
        raise ValueError("detector is required")

    observed_json = _jsonify(observed_state)
    desired_json = _jsonify(desired_state)

    now = _utc_now()
    conn = get_db_connection(db_path=db_path)
    try:
        with tx_immediate(conn):
            existing = _active_row_for_key(conn, incident_key)
            if existing is not None:
                conn.execute(
                    """
                    UPDATE incidents
                    SET consecutive_breaches = consecutive_breaches + 1,
                        last_action_at       = ?,
                        observed_state       = COALESCE(?, observed_state),
                        desired_state        = COALESCE(?, desired_state),
                        confidence           = COALESCE(?, confidence)
                    WHERE id = ?
                    """,
                    (
                        now, observed_json, desired_json,
                        confidence, existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO incidents (
                        incident_key, invariant_id, severity, scrutiny_tier,
                        status, detector, detected_at, last_action_at,
                        consecutive_breaches, observed_state, desired_state,
                        confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        incident_key, invariant_id, severity, scrutiny_tier,
                        STATUS_OPEN, detector, now, now,
                        observed_json, desired_json, confidence,
                    ),
                )
                _mirror_register(
                    conn,
                    incident_key=incident_key,
                    detector=detector,
                    detected_at=now,
                )
        refreshed = get_by_key(
            incident_key, active_only=True, db_path=db_path
        )
        return refreshed or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _advance(
    incident_id: int,
    *,
    target_status: str,
    allowed_from: Iterable[str],
    db_path: str | Path | None,
    extra_set: list[str] | None = None,
    extra_params: list[Any] | None = None,
    mirror_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared state-transition primitive.

    Validates the from→to transition against `allowed_from`, updates
    `last_action_at` (and `closed_at` when entering a terminal state),
    and mirrors into remediation_incidents if the caller supplied
    mirror_kwargs.
    """
    _validate_status(target_status)
    allowed = frozenset(allowed_from)
    now = _utc_now()
    conn = get_db_connection(db_path=db_path)
    try:
        with tx_immediate(conn):
            cur = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (int(incident_id),)
            ).fetchone()
            if cur is None:
                raise ValueError(f"unknown incident id: {incident_id}")
            if cur["status"] not in allowed:
                raise ValueError(
                    f"illegal transition {cur['status']!r} -> "
                    f"{target_status!r} (allowed from {sorted(allowed)})"
                )
            sets = ["status = ?", "last_action_at = ?"]
            params: list[Any] = [target_status, now]
            if target_status in CLOSED_STATUSES:
                sets.append("closed_at = ?")
                params.append(now)
            if extra_set:
                sets.extend(extra_set)
                params.extend(extra_params or [])
            params.append(int(incident_id))
            conn.execute(
                f"UPDATE incidents SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if mirror_kwargs is not None:
                _mirror_update_status(
                    conn,
                    incident_key=cur["incident_key"],
                    new_status=target_status,
                    updated_at=now,
                    **mirror_kwargs,
                )
        refreshed = get(incident_id, db_path=db_path)
        return refreshed or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_authoring(
    incident_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """open → authoring (Author LLM picked the incident up)."""
    return _advance(
        incident_id,
        target_status=STATUS_AUTHORING,
        allowed_from=(STATUS_OPEN, STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE),
        db_path=db_path,
        mirror_kwargs={},  # 'authoring' maps to legacy 'new', no-op fields
    )


def mark_awaiting_approval(
    incident_id: int,
    *,
    mr_iid: int,
    ddiff_url: str | None = None,
    branch_name: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """authoring|open → awaiting_approval — records the staged MR."""
    extra_set = ["mr_iid = ?"]
    extra_params: list[Any] = [int(mr_iid)]
    if ddiff_url is not None:
        extra_set.append("ddiff_url = ?")
        extra_params.append(ddiff_url)
    return _advance(
        incident_id,
        target_status=STATUS_AWAITING,
        allowed_from=(
            STATUS_OPEN, STATUS_AUTHORING,
            STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE,
        ),
        db_path=db_path,
        extra_set=extra_set,
        extra_params=extra_params,
        mirror_kwargs={"mr_iid": mr_iid, "branch_name": branch_name},
    )


def mark_merged(
    incident_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """awaiting_approval → merged — set closed_at."""
    return _advance(
        incident_id,
        target_status=STATUS_MERGED,
        allowed_from=(STATUS_AWAITING,),
        db_path=db_path,
        mirror_kwargs={},
    )


def mark_resolved(
    incident_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Any active status → resolved (invariant auto-cleared, no MR).

    Used when a re-run of the detector shows the condition is gone and
    no code change was needed — e.g. an operator manually fixed DB
    state. Dual-writes ``merged`` into the legacy table so the weekly
    remediation pipeline treats it as closed.
    """
    return _advance(
        incident_id,
        target_status=STATUS_RESOLVED,
        allowed_from=ACTIVE_STATUSES,
        db_path=db_path,
        mirror_kwargs={},
    )


def mark_rejected(
    incident_id: int,
    reason: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """/reject_rem — advance one step along the 3-reject ladder.

    Transitions:
        awaiting_approval | needs_architect | authoring | open → rejected_once
        rejected_once     → rejected_twice
        rejected_twice    → rejected_permanently
        rejected_permanently → no-op (returns current row unchanged)

    The reason is appended to ``rejection_history`` as a JSON object so
    ADR-007 §4.7 ALHF-lite can feed prior rejections into the next
    Author attempt's prompt.
    """
    if not reason or not reason.strip():
        raise ValueError("rejection reason is required (ADR-007 §4.7)")

    now = _utc_now()
    conn = get_db_connection(db_path=db_path)
    try:
        with tx_immediate(conn):
            cur = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (int(incident_id),)
            ).fetchone()
            if cur is None:
                raise ValueError(f"unknown incident id: {incident_id}")

            cur_status = cur["status"]
            if cur_status in (
                STATUS_OPEN, STATUS_AUTHORING, STATUS_AWAITING,
                STATUS_ARCHITECT,
            ):
                next_status = STATUS_REJECTED_ONCE
            elif cur_status == STATUS_REJECTED_ONCE:
                next_status = STATUS_REJECTED_TWICE
            elif cur_status == STATUS_REJECTED_TWICE:
                next_status = STATUS_REJECTED_PERM
            else:
                # rejected_permanently, merged, resolved — no-op.
                return _row_to_dict(cur) or {}

            history_raw = cur["rejection_history"] or "[]"
            try:
                history = json.loads(history_raw)
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
            history.append({
                "at": now,
                "from_status": cur_status,
                "reason": reason.strip(),
            })
            history_json = json.dumps(history, default=str)

            sets = [
                "status = ?",
                "last_action_at = ?",
                "rejection_history = ?",
            ]
            params: list[Any] = [next_status, now, history_json]
            if next_status in CLOSED_STATUSES:
                sets.append("closed_at = ?")
                params.append(now)
            params.append(int(incident_id))
            conn.execute(
                f"UPDATE incidents SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            _mirror_update_status(
                conn,
                incident_key=cur["incident_key"],
                new_status=next_status,
                updated_at=now,
                rejection_reasons_json=history_json,
            )
        refreshed = get(incident_id, db_path=db_path)
        return refreshed or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_needs_architect(
    incident_id: int,
    reason: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Escalate to Architect — halts autonomous re-authoring.

    Unlike mark_rejected, this stores ``reason`` in observed_state as a
    structured envelope so the Architect reads full context in one
    place. The legacy table's architect_reason column receives the raw
    reason string.
    """
    if not reason or not reason.strip():
        raise ValueError("architect escalation reason is required")
    envelope = json.dumps(
        {"architect_escalation": reason.strip(), "at": _utc_now()},
        default=str,
    )
    return _advance(
        incident_id,
        target_status=STATUS_ARCHITECT,
        allowed_from=(
            STATUS_OPEN, STATUS_AUTHORING, STATUS_AWAITING,
            STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE,
            STATUS_REJECTED_PERM,
        ),
        db_path=db_path,
        extra_set=["desired_state = ?"],
        extra_params=[envelope],
        mirror_kwargs={"architect_reason": reason.strip()},
    )


def append_rejection_reason(
    incident_id: int,
    reason: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Append a reason to rejection_history WITHOUT advancing status.

    Used by the Critic layer (Step 6) when internal Author/Critic loops
    want to preserve rejection context without consuming one of the
    human-gated 3 strikes.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    now = _utc_now()
    conn = get_db_connection(db_path=db_path)
    try:
        with tx_immediate(conn):
            cur = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (int(incident_id),)
            ).fetchone()
            if cur is None:
                raise ValueError(f"unknown incident id: {incident_id}")
            history_raw = cur["rejection_history"] or "[]"
            try:
                history = json.loads(history_raw)
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
            history.append({
                "at": now,
                "from_status": cur["status"],
                "reason": reason.strip(),
                "internal": True,
            })
            conn.execute(
                "UPDATE incidents SET rejection_history = ?, last_action_at = ? "
                "WHERE id = ?",
                (json.dumps(history, default=str), now, int(incident_id)),
            )
        refreshed = get(incident_id, db_path=db_path)
        return refreshed or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
