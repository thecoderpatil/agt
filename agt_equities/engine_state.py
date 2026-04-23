"""ADR-011 §4 engine_state repo. Read + transition surface.

One row per engine. Transitions are tracked in
``last_transition_utc``. Halt is atomic: `halt_engine()` flips
`halted=1`, stamps `halted_reason` + `halted_at_utc`, and advances
`last_transition_utc`. Resume is explicit: `resume_engine()` flips
`halted=0` back. Promotion between canary steps goes through
``advance_canary_step()`` with validation against the ADR-011 §5
ordering.

All writes use `tx_immediate` per the db.py WAL discipline.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agt_equities.db import get_db_connection, get_ro_connection, tx_immediate


ENGINES = ("exit", "roll", "harvest", "entry")
CANARY_STEPS = ("paper", "canary_1", "canary_2", "canary_3", "live")
_VALID_ADVANCE_PAIRS = {
    ("paper", "canary_1"),
    ("canary_1", "canary_2"),
    ("canary_2", "canary_3"),
    ("canary_3", "live"),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_state(engine: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    with get_ro_connection(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT engine, canary_step, halted, halted_reason, "
            "halted_at_utc, last_transition_utc, notes "
            "FROM engine_state WHERE engine = ?",
            (engine,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_all(*, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_ro_connection(db_path=db_path) as conn:
        rows = conn.execute(
            "SELECT engine, canary_step, halted, halted_reason, "
            "halted_at_utc, last_transition_utc, notes "
            "FROM engine_state ORDER BY engine"
        ).fetchall()
    return [dict(r) for r in rows]


def halt_engine(
    engine: str,
    *,
    reason: str,
    db_path: str | Path | None = None,
) -> bool:
    """Atomic halt. Returns True if a row was updated."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    if not reason or not reason.strip():
        raise ValueError("reason is required (non-empty string)")
    now = _utc_now_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "UPDATE engine_state SET halted = 1, halted_reason = ?, "
                "halted_at_utc = ?, last_transition_utc = ? WHERE engine = ?",
                (reason.strip(), now, now, engine),
            )
            return cur.rowcount > 0


def resume_engine(
    engine: str,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Clear halt. Returns True if a row was updated."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    now = _utc_now_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "UPDATE engine_state SET halted = 0, halted_reason = NULL, "
                "halted_at_utc = NULL, last_transition_utc = ? WHERE engine = ?",
                (now, engine),
            )
            return cur.rowcount > 0


def advance_canary_step(
    engine: str,
    *,
    from_step: str,
    to_step: str,
    db_path: str | Path | None = None,
) -> bool:
    """Advance canary step validated against the ADR-011 §5 ordering.

    Rejects: (a) unknown engine; (b) unknown steps; (c) pairs not in
    the canonical forward-only advance set; (d) cases where the row's
    current canary_step is not `from_step`.

    Engines in `halted` state may be advanced only after a `resume`
    (not enforced here — enforced by the pre-gateway which gates the
    order-flow separately).
    """
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    if from_step not in CANARY_STEPS or to_step not in CANARY_STEPS:
        raise ValueError(
            f"canary_step must be in {CANARY_STEPS}, got from={from_step!r} to={to_step!r}"
        )
    if (from_step, to_step) not in _VALID_ADVANCE_PAIRS:
        raise ValueError(
            f"invalid forward advance {from_step!r} -> {to_step!r}. "
            f"Valid pairs: {sorted(_VALID_ADVANCE_PAIRS)}"
        )
    now = _utc_now_iso()
    with closing(get_db_connection(db_path=db_path)) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "UPDATE engine_state SET canary_step = ?, last_transition_utc = ? "
                "WHERE engine = ? AND canary_step = ?",
                (to_step, now, engine, from_step),
            )
            return cur.rowcount > 0


def any_engine_in_prior_canary(
    engine: str,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Sequence guard: ADR-011 §3 says an engine cannot flip while a prior-
    sequence engine is still in canary or rollback. Returns True if any
    engine earlier in the sequence is currently in canary_1/2/3 or halted.
    """
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    target_idx = ENGINES.index(engine)
    priors = ENGINES[:target_idx]
    if not priors:
        return False
    placeholders = ",".join("?" * len(priors))
    with get_ro_connection(db_path=db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM engine_state "
            f"WHERE engine IN ({placeholders}) "
            "AND (canary_step IN ('canary_1','canary_2','canary_3') OR halted = 1)",
            priors,
        ).fetchone()
    return bool(row[0])


__all__ = [
    "ENGINES",
    "CANARY_STEPS",
    "get_state",
    "list_all",
    "halt_engine",
    "resume_engine",
    "advance_canary_step",
    "any_engine_in_prior_canary",
]
