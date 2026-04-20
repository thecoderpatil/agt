"""AGT approval policy — per-action, per-context gate helpers.

All gates that ask "does this action require human approval?" route through
this module. Never read PAPER_MODE or AGT_PAPER_MODE directly — always
consume ctx.broker_mode. Policy decisions are documented with their date
and the rule that "only Yash can change this policy."

Usage:
    from agt_equities.approval_policy import needs_csp_approval
    if needs_csp_approval(ctx):
        approved_indices = await telegram_dispatch.await_csp_approval(...)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agt_equities.runtime import RunContext


def needs_csp_approval(ctx: "RunContext") -> bool:
    """CSP entry: live + csp engine requires operator approval via Telegram digest.

    Paper CSP is auto-approved (full autonomous mode enabled 2026-04-19).
    Non-CSP engines (cc, roll, harvest) do not trigger this gate.

    Policy decision: 2026-04-20. Only Yash can change this policy.
    Any MR that flips the return value requires ADR-update in same commit.
    """
    return ctx.broker_mode == "live" and ctx.engine == "csp"


def needs_liquidate_approval(ctx: "RunContext") -> bool:
    """Close-now / liquidate actions: auto on paper, manual on live.

    Rationale: a false-positive close_now on a live position has
    irreversible P&L impact. Paper is experimental surface and learns
    by doing; auto-staging on paper is explicitly desired.

    Policy decision: 2026-04-20. Only Yash can change this policy.
    Any MR that flips the return value requires ADR-update in same commit.
    """
    return ctx.broker_mode == "live"


def needs_roll_approval(ctx: "RunContext") -> bool:
    """Roll actions: auto-approve on both paper and live per spec.

    Rolls are low-risk relative to entry/close. Placeholder helper so
    a future policy change is a single-line edit here, not a grep.

    Policy decision: 2026-04-20. Only Yash can change this policy.
    """
    return False
