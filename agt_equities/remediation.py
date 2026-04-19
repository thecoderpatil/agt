"""Remediation registry + GitLab merge/close helpers.

ADR-007 Step 5 status (2026-04-16)
----------------------------------
The state-machine helpers in this module (``get_state``, ``list_awaiting``,
``register_incident``, ``mark_awaiting``, ``mark_merged``, ``mark_rejected``,
``mark_architect``, ``record_nudge``, ``extract_incidents_from_directive``)
are DEPRECATED. New callers should use ``agt_equities.incidents_repo``,
which writes the structured ``incidents`` table and dual-writes to
``remediation_incidents`` for one sprint of compatibility.

The GitLab API helpers (``gitlab_get_mr``, ``gitlab_lower_approval_rule``,
``gitlab_merge_mr``, ``gitlab_close_mr``) are schema-agnostic and remain
the canonical GitLab surface — do NOT duplicate these elsewhere.

This module is retired in lockstep with the legacy ``remediation_incidents``
table (follow-up ticket, not this sprint).

Consumed by:
- scripts/rem_incidents.py (legacy CLI; agt-remediation-weekly task)
- telegram_bot.py /approve_rem /reject_rem /list_rem handlers use ONLY
  the GitLab helpers here; state reads/writes now go through
  ``agt_equities.incidents_repo`` (ADR-007 Step 5)

Data model (remediation_incidents, created by schema.py):

    incident_id         TEXT PRIMARY KEY   — Opus directive ALL_CAPS id
    first_detected      TEXT NOT NULL      — ISO8601 UTC
    directive_source    TEXT               — path / commit-hash of directive
    fix_authored_at     TEXT
    mr_iid              INTEGER
    branch_name         TEXT
    status              TEXT NOT NULL      — new | awaiting_approval | merged |
                                              rejected_once | rejected_twice |
                                              rejected_permanently | needs_architect
    rejection_reasons   TEXT               — JSON [{at, from_status, reason}]
    last_nudged_at      TEXT
    architect_reason    TEXT
    updated_at          TEXT

State transitions:
    new -> awaiting_approval           (remediation task opens MR)
    awaiting_approval -> merged        (/approve_rem or GitLab merge)
    awaiting_approval -> rejected_once (/reject_rem first time)
    rejected_once -> awaiting_approval (next run re-authors)
    rejected_once -> rejected_twice    (/reject_rem second time)
    rejected_twice -> rejected_permanently (/reject_rem third time)
    * -> needs_architect               (task-initiated escalation)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.db import get_db_connection, get_ro_connection
from agt_equities.incidents_repo import STATUS_AWAITING, STATUS_ARCHITECT, STATUS_MERGED, STATUS_REJECTED_ONCE, STATUS_REJECTED_PERM, STATUS_REJECTED_TWICE

__all__ = [
    "extract_incidents_from_directive",
    "get_state",
    "list_awaiting",
    "register_incident",
    "mark_awaiting",
    "mark_merged",
    "mark_rejected",
    "mark_architect",
    "record_nudge",
    "gitlab_get_mr",
    "gitlab_lower_approval_rule",
    "gitlab_merge_mr",
    "gitlab_close_mr",
    "STATUS_NEW",
    "STATUS_AWAITING",
    "STATUS_MERGED",
    "STATUS_REJECTED_ONCE",
    "STATUS_REJECTED_TWICE",
    "STATUS_REJECTED_PERM",
    "STATUS_ARCHITECT",
]

# Status constants — one place, fail loud if mis-typed elsewhere.
STATUS_NEW = "new"

_VALID_STATUS = frozenset({
    STATUS_NEW, STATUS_AWAITING, STATUS_MERGED,
    STATUS_REJECTED_ONCE, STATUS_REJECTED_TWICE, STATUS_REJECTED_PERM,
    STATUS_ARCHITECT,
})


# ---------------------------------------------------------------------------
# Directive parsing
# ---------------------------------------------------------------------------

_INCIDENT_SECTION_RE = re.compile(
    r"##\s*Critical Incidents.*?\n(.*?)(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_INCIDENT_ITEM_RE = re.compile(
    r"^\s*\d+\.\s*\*\*([A-Z][A-Z0-9_]+)\*\*\s*[\u2014\u2013-]\s*(.+?)(?=\n\s*\d+\.\s*\*\*|\Z)",
    re.DOTALL | re.MULTILINE,
)


def extract_incidents_from_directive(directive_path: str | Path) -> list[dict[str, str]]:
    """DEPRECATED: ADR-007 Step 5 retired the prose directive workflow.

    Parses a weekly directive markdown and returns critical incidents.
    Still imported by ``scripts/rem_incidents.py extract`` for backward
    compatibility with the legacy ``agt-remediation-weekly`` task prompt.
    Returns ``[]`` if the directive path does not exist — which will be
    the common case now that ``_WEEKLY_ARCHITECT_DIRECTIVE.md`` is gone
    from origin/main (commit 30ea993a).

    New tooling should consume ``agt_equities.incidents_repo`` directly
    or ``scripts/incidents_digest.py`` for markdown rollups. This helper
    will be removed together with the legacy ``remediation_incidents``
    table in a follow-up sprint.

    Each returned dict has keys: ``incident_id`` (ALL_CAPS), ``summary``
    (first line of the item), ``body`` (full item text including
    summary). Returns [] and swallows I/O errors — the caller decides
    whether an empty directive is a no-op or a failure.
    """
    try:
        text = Path(directive_path).read_text(encoding="utf-8")
    except Exception:
        return []

    section_match = _INCIDENT_SECTION_RE.search(text)
    if not section_match:
        return []
    section = section_match.group(1)

    incidents: list[dict[str, str]] = []
    for m in _INCIDENT_ITEM_RE.finditer(section):
        incident_id = m.group(1).strip()
        body = m.group(2).strip()
        summary = body.splitlines()[0].strip() if body else ""
        incidents.append({
            "incident_id": incident_id,
            "summary": summary,
            "body": body,
        })
    return incidents


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Read helpers (RO connection)
# ---------------------------------------------------------------------------

def get_state(
    incident_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the remediation_incidents row for ``incident_id``, or None."""
    conn = get_ro_connection(db_path=db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM remediation_incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_awaiting(
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Rows currently awaiting Yash approval, oldest first."""
    conn = get_ro_connection(db_path=db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM remediation_incidents WHERE status = ? "
            "ORDER BY COALESCE(fix_authored_at, first_detected) ASC",
            (STATUS_AWAITING,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Write helpers (RW connection, single txn each)
# ---------------------------------------------------------------------------

def register_incident(
    incident_id: str,
    *,
    directive_source: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Insert a new row if incident_id is unknown. Idempotent.

    Returns the current state row after insert-or-noop.
    """
    existing = get_state(incident_id, db_path=db_path)
    if existing is not None:
        return existing

    conn = get_db_connection(db_path=db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO remediation_incidents "
                "(incident_id, first_detected, directive_source, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (incident_id, _utc_now(), directive_source, STATUS_NEW, _utc_now()),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return get_state(incident_id, db_path=db_path) or {}


def mark_awaiting(
    incident_id: str,
    *,
    mr_iid: int,
    branch_name: str,
    db_path: str | Path | None = None,
) -> None:
    """Record that a remediation MR has been opened for ``incident_id``."""
    conn = get_db_connection(db_path=db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE remediation_incidents "
                "SET status = ?, mr_iid = ?, branch_name = ?, "
                "    fix_authored_at = ?, updated_at = ? "
                "WHERE incident_id = ?",
                (STATUS_AWAITING, int(mr_iid), branch_name, _utc_now(), _utc_now(),
                 incident_id),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_merged(
    incident_id: str,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Transition row to ``merged`` — called on successful /approve_rem."""
    conn = get_db_connection(db_path=db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE remediation_incidents "
                "SET status = ?, updated_at = ? WHERE incident_id = ?",
                (STATUS_MERGED, _utc_now(), incident_id),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_rejected(
    incident_id: str,
    reason: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Append the rejection reason; advance the state machine.

    Transitions:
        new | awaiting_approval | needs_architect -> rejected_once
        rejected_once                             -> rejected_twice
        rejected_twice                            -> rejected_permanently
        rejected_permanently | merged             -> no-op (returns current row)
    """
    current = get_state(incident_id, db_path=db_path)
    if current is None:
        raise ValueError(f"unknown incident: {incident_id}")

    cur_status = current.get("status") or STATUS_NEW
    if cur_status in (STATUS_NEW, STATUS_AWAITING, STATUS_ARCHITECT):
        next_status = STATUS_REJECTED_ONCE
    elif cur_status == STATUS_REJECTED_ONCE:
        next_status = STATUS_REJECTED_TWICE
    elif cur_status == STATUS_REJECTED_TWICE:
        next_status = STATUS_REJECTED_PERM
    else:
        next_status = cur_status

    reasons_raw = current.get("rejection_reasons") or "[]"
    try:
        reasons = json.loads(reasons_raw)
        if not isinstance(reasons, list):
            reasons = []
    except Exception:
        reasons = []
    reasons.append({"at": _utc_now(), "from_status": cur_status, "reason": reason})

    conn = get_db_connection(db_path=db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE remediation_incidents "
                "SET status = ?, rejection_reasons = ?, updated_at = ? "
                "WHERE incident_id = ?",
                (next_status, json.dumps(reasons, default=str), _utc_now(), incident_id),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return get_state(incident_id, db_path=db_path) or {}


def mark_architect(
    incident_id: str,
    reason: str,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Escalate to Architect — halts autonomous re-authoring for this id."""
    conn = get_db_connection(db_path=db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE remediation_incidents "
                "SET status = ?, architect_reason = ?, updated_at = ? "
                "WHERE incident_id = ?",
                (STATUS_ARCHITECT, reason, _utc_now(), incident_id),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def record_nudge(
    incident_id: str,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Update ``last_nudged_at`` for re-nudge bookkeeping."""
    conn = get_db_connection(db_path=db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE remediation_incidents "
                "SET last_nudged_at = ?, updated_at = ? WHERE incident_id = ?",
                (_utc_now(), _utc_now(), incident_id),
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GitLab API helpers — used by /approve_rem and /reject_rem
# ---------------------------------------------------------------------------

_GITLAB_BASE = "https://gitlab.com/api/v4"
_PROJECT_ID = "81096827"


def _gitlab_token() -> str:
    override = os.environ.get("AGT_GITLAB_TOKEN_PATH")
    if override:
        token_path = Path(override)
    else:
        repo_root = Path(__file__).resolve().parent.parent
        token_path = repo_root / ".gitlab-token"
    try:
        return token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"GitLab token missing at {token_path}") from exc


def _gitlab_request(method: str, path: str, payload: dict | None = None) -> Any:
    url = f"{_GITLAB_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", _gitlab_token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitLab {method} {path} -> {exc.code}: {body}") from exc


def gitlab_get_mr(mr_iid: int) -> dict[str, Any]:
    """Fetch MR details — state, pipeline, approval rules."""
    return _gitlab_request(
        "GET",
        f"/projects/{_PROJECT_ID}/merge_requests/{int(mr_iid)}",
    )


def gitlab_lower_approval_rule(mr_iid: int) -> None:
    """Lower all approval rules to 0 — per feedback_project_approval_policy.

    Silent no-op if the rule lookup fails. Caller falls back to direct merge
    which will surface any remaining approval blockers.
    """
    try:
        rules = _gitlab_request(
            "GET",
            f"/projects/{_PROJECT_ID}/merge_requests/{int(mr_iid)}/approval_rules",
        )
    except Exception:
        return
    if not isinstance(rules, list):
        return
    for rule in rules:
        try:
            rid = rule.get("id")
            if rid is None:
                continue
            _gitlab_request(
                "PUT",
                f"/projects/{_PROJECT_ID}/merge_requests/{int(mr_iid)}"
                f"/approval_rules/{rid}",
                payload={"approvals_required": 0},
            )
        except Exception:
            continue


def gitlab_merge_mr(mr_iid: int, *, commit_message: str | None = None) -> dict[str, Any]:
    """Merge the MR. Caller should have lowered approvals first."""
    payload: dict[str, Any] = {"should_remove_source_branch": True}
    if commit_message:
        payload["squash_commit_message"] = commit_message
        payload["squash"] = True
    return _gitlab_request(
        "PUT",
        f"/projects/{_PROJECT_ID}/merge_requests/{int(mr_iid)}/merge",
        payload=payload,
    )


def gitlab_close_mr(mr_iid: int) -> dict[str, Any]:
    """Close (not merge) an MR — used by /reject_rem."""
    return _gitlab_request(
        "PUT",
        f"/projects/{_PROJECT_ID}/merge_requests/{int(mr_iid)}",
        payload={"state_event": "close"},
    )
