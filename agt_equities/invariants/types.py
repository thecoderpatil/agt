"""Core dataclasses for ADR-007 invariant checks.

A Violation is an immutable record of a single breach of an invariant.
Checks return list[Violation]; an empty list means the invariant holds.

CheckContext carries all runtime configuration that a check may need to
consult. Checks are PURE: they read from conn + ctx and return Violations.
They never mutate either.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class Violation:
    """A single detected invariant violation.

    Attributes:
        invariant_id: Uppercase slug matching safety_invariants.yaml.
        description: One-line human-readable explanation.
        evidence: Structured data proving the breach (row ids, counts, etc.).
                  Used by downstream Author/Critic steps to synthesize a fix.
        severity: low | medium | high | critical.
        detected_at: UTC timestamp when the violation was detected.
    """
    invariant_id: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)
    severity: str = "medium"
    detected_at: datetime = field(default_factory=_utcnow)


@dataclass
class CheckContext:
    """Runtime context passed to every check function.

    Checks must treat ctx as read-only. TTL fields drive age-based checks
    so tests can override them deterministically.
    """
    now_utc: datetime
    db_path: str
    paper_mode: bool
    live_accounts: frozenset[str]
    paper_accounts: frozenset[str]
    expected_daemons: frozenset[str]
    daemon_heartbeat_ttl_s: int = 120
    stranded_staged_ttl_s: int = 3600
    stuck_processing_ttl_s: int = 7200
    red_alert_stale_ttl_s: int = 86_400
