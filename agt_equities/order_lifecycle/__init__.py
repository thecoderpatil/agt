"""ADR-020 Phase A piece 3 — pending_orders state lifecycle hygiene.

Terminal-state sweeper: identifies stuck non-terminal pending_orders rows
and transitions them to the correct terminal state with structured history.
"""
from .sweeper import sweep_terminal_states, SweepResult

__all__ = ["sweep_terminal_states", "SweepResult"]
