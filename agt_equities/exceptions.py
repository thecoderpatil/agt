"""AGT system-level exception hierarchy.

AgtSystemFailure is the base for system-level faults the self-healing loop
can recognize and react to -- retry, alert, circuit-break.

Do NOT use for business-logic exceptions (ExecutionDisabledError, mode-gating
decisions). Those stay as local raises in their own modules.
"""
from __future__ import annotations


class AgtSystemFailure(RuntimeError):
    """Base for system-level faults the self-healing loop can recognize.

    Subclass this when a component encounters a system-level fault
    (control-plane unreadable, broker connection lost, DB corruption)
    that the self-healing loop should react to -- retry, alert, circuit-break.
    """


class ControlPlaneUnreadable(AgtSystemFailure):
    """Control-plane DB read failed in a strict-variant caller.

    Raised by assert_execution_enabled_strict() when the execution_state
    table is unreadable. Order-driving code should propagate this;
    reporting/UX code catches it.

    Policy (2026-04-21): a control-plane outage must NOT unlock the system.
    Fail-closed for all order-driving paths. Only Yash can change this.
    """
