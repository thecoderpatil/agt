"""agt_equities.telegram_dispatch — async Telegram approval surface.

Wraps the synchronous csp_approval_gate.telegram_approval_gate in
asyncio.to_thread + returns frozenset[int] of approved indices instead
of the approved-candidate subset. This matches the scan_orchestrator's
Option 1 shape (approval between phases, indexed filter preserving
screener sort order).

Why a separate module: the underlying gate is sync (time.sleep polling),
lives in csp_approval_gate.py, and returns candidate objects. The
orchestrator needs async + indices. Keeping the wrapper here isolates
the async-sync boundary + index mapping, so csp_approval_gate.py stays
untouched and legacy callers (if any remain post-MR 3) keep working.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from agt_equities.csp_approval_gate import (
    _APPROVAL_TIMEOUT_MINUTES,
    telegram_approval_gate,
)

if TYPE_CHECKING:
    from agt_equities.runtime import RunContext

logger = logging.getLogger(__name__)


async def await_csp_approval(
    candidates: tuple,
    ctx: "RunContext",
    *,
    db_path: str | None = None,
    timeout_minutes: int = _APPROVAL_TIMEOUT_MINUTES,
) -> frozenset[int]:
    """Dispatch a CSP approval digest and await the operator response.

    Returns a frozenset of 0-based indices into the `candidates` tuple.
    An empty frozenset means the operator rejected, timed out, or a
    dispatch error occurred — the orchestrator treats that as zero
    approved candidates and skips allocation.

    The underlying gate is synchronous + polls via time.sleep. We run
    it in a worker thread so the PTB event loop is not blocked.
    """
    if not candidates:
        return frozenset()

    candidates_list = list(candidates)

    approved = await asyncio.to_thread(
        telegram_approval_gate,
        candidates_list,
        db_path=db_path,
        timeout_minutes=timeout_minutes,
    )

    # telegram_approval_gate returns candidates_list[idx] for each
    # approved index — same object references as input. Reverse-map
    # by identity (id()) so we recover the indices cleanly even if
    # the candidate objects have custom __eq__ or are unhashable.
    id_to_index = {id(c): i for i, c in enumerate(candidates_list)}
    approved_indices: list[int] = []
    for c in approved:
        idx = id_to_index.get(id(c))
        if idx is None:
            logger.warning(
                "await_csp_approval: approved candidate not found by identity "
                "(dropping) — ticker=%s",
                getattr(c, "ticker", "?"),
            )
            continue
        approved_indices.append(idx)

    return frozenset(approved_indices)


__all__ = ["await_csp_approval"]
