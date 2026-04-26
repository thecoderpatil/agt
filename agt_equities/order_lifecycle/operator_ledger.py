"""Operator intervention ledger -- append-only audit trail for manual actions.

Used by Phase B proof-report to assert 'zero direct DB / manual interventions
during the 14-day observation window'. Generalizes the recovery_audit_log
pattern without replacing it.
"""
from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.db import get_db_connection, get_ro_connection

logger = logging.getLogger("agt_equities.operator_ledger")

VALID_KINDS: frozenset[str] = frozenset({
    "reject", "reject_rem", "approve", "recover_transmitting",
    "flex_manual_reconcile", "halt", "direct_sql", "manual_terminal",
    "restart_during_market",
})


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def record_intervention(
    *,
    operator_user_id: str | None,
    kind: str,
    target_table: str | None = None,
    target_id: int | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    reason: str | None = None,
    notes: str | None = None,
    occurred_at_utc: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Append-only insert into operator_interventions. Returns row id."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown intervention kind: {kind}")
    occurred = occurred_at_utc or _utc_now_iso()
    bs = json.dumps(before_state, default=str) if before_state is not None else None
    as_ = json.dumps(after_state, default=str) if after_state is not None else None
    with closing(get_db_connection(db_path=db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO operator_interventions "
            "(occurred_at_utc, operator_user_id, kind, target_table, target_id, "
            "before_state, after_state, reason, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (occurred, operator_user_id, kind, target_table, target_id, bs, as_, reason, notes),
        )
        conn.commit()
        return int(cur.lastrowid)


def safe_record_intervention(**kwargs: Any) -> int | None:
    """Best-effort wrapper. Wiring sites use this so ledger failure can't
    block the user's command -- the mutation has already happened by the
    time we record it.
    """
    try:
        return record_intervention(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("record_intervention failed: %s", exc)
        return None


def query_interventions(
    *,
    since_utc: str,
    until_utc: str | None = None,
    kind: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Read-only query for proof-report. Returns list of dicts."""
    sql = (
        "SELECT id, occurred_at_utc, operator_user_id, kind, target_table, "
        "target_id, before_state, after_state, reason, notes "
        "FROM operator_interventions WHERE occurred_at_utc >= ?"
    )
    params: list[Any] = [since_utc]
    if until_utc is not None:
        sql += " AND occurred_at_utc < ?"
        params.append(until_utc)
    if kind is not None:
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown intervention kind: {kind}")
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY occurred_at_utc ASC"
    with closing(get_ro_connection(db_path=db_path)) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        for jc in ("before_state", "after_state"):
            v = d.get(jc)
            if isinstance(v, str) and v:
                try:
                    d[jc] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        out.append(d)
    return out


__all__ = ["VALID_KINDS", "record_intervention", "safe_record_intervention", "query_interventions"]
