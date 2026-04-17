"""Safety invariants - ADR-007 self-healing loop detection core.

Public surface:
    Violation, CheckContext - core dataclasses
    load_invariants, run_all, build_context - runner entry points
    CHECK_REGISTRY - id -> function map
"""
from .runner import build_context, load_invariants, run_all
from .types import CheckContext, Violation

__all__ = [
    "CheckContext",
    "Violation",
    "build_context",
    "load_invariants",
    "run_all",
]
