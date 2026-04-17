#!/usr/bin/env python3
"""Emit a markdown or JSON digest of the ADR-007 incidents queue.

Used by the weekly Opus architect review task (the rewritten
``agt-opus-architect-review`` prompt) as its primary data source.
Replaces the retired prose ``_WEEKLY_ARCHITECT_DIRECTIVE.md``
workflow. Read-only; safe to run at any time.

Default groups the canonical active statuses (open, authoring,
awaiting_approval, needs_architect, rejected_once, rejected_twice)
by ``invariant_id`` and emits each row sorted by most-recent
``last_action_at``. Closed statuses (merged, resolved,
rejected_permanently) are excluded unless explicitly requested
with ``--status``.

Usage:
    python3 scripts/incidents_digest.py
    python3 scripts/incidents_digest.py --format json
    python3 scripts/incidents_digest.py --status open --status awaiting_approval
    python3 scripts/incidents_digest.py --since 2026-04-10
    python3 scripts/incidents_digest.py --limit 20

Exit code:
    0 -- read succeeded (empty queue is NOT an error).
    2 -- read failed (DB missing, schema mismatch, etc.); stderr carries
         the exception text so the scheduled task can alert.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running the CLI from outside the repo without setting PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agt_equities import incidents_repo  # noqa: E402


# Default statuses -- what Yash / the Opus task actually need to see.
# Explicitly excludes merged / resolved / rejected_permanently.
DEFAULT_STATUSES: tuple[str, ...] = (
    incidents_repo.STATUS_OPEN,
    incidents_repo.STATUS_AUTHORING,
    incidents_repo.STATUS_AWAITING,
    incidents_repo.STATUS_ARCHITECT,
    incidents_repo.STATUS_REJECTED_ONCE,
    incidents_repo.STATUS_REJECTED_TWICE,
)


def _parse_since(s: str) -> str:
    """Validate --since is ISO8601. Return original string (ISO compares lex)."""
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be ISO8601: {s!r} ({exc})"
        )
    return s


def _filter_since(rows: list[dict], since: str | None) -> list[dict]:
    """Drop rows whose last_action_at (or detected_at) is before `since`.

    ISO8601 timestamps compare correctly as strings when they use the
    same offset convention (our writers always emit UTC with trailing
    offset).
    """
    if not since:
        return rows
    out: list[dict] = []
    for r in rows:
        when = r.get("last_action_at") or r.get("detected_at") or ""
        if when and when >= since:
            out.append(r)
    return out


def _emit_md(rows: list[dict]) -> str:
    """Render the digest as markdown for email / Telegram / Opus prompt."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not rows:
        return (
            "# Incident Digest\n\n"
            f"_Generated {now_iso}_\n\n"
            "_No active incidents._\n"
        )

    by_inv: dict[str, list[dict]] = {}
    for r in rows:
        by_inv.setdefault(r.get("invariant_id") or "(none)", []).append(r)

    lines: list[str] = ["# Incident Digest", ""]
    lines.append(f"**Total active:** {len(rows)}  ")
    lines.append(f"**Invariants touched:** {len(by_inv)}  ")
    lines.append(f"**Generated:** {now_iso}")
    lines.append("")
    lines.append("## By invariant")
    lines.append("")
    for inv in sorted(by_inv):
        group = by_inv[inv]
        lines.append(f"### `{inv}` ({len(group)})")
        group_sorted = sorted(
            group,
            key=lambda x: (x.get("last_action_at") or x.get("detected_at") or ""),
            reverse=True,
        )
        for r in group_sorted:
            iid = r.get("id")
            key = r.get("incident_key") or "?"
            status = r.get("status") or "?"
            sev = r.get("severity") or "?"
            tier = r.get("scrutiny_tier") or "?"
            last = r.get("last_action_at") or r.get("detected_at") or "?"
            mr = r.get("mr_iid")
            mr_str = f", MR !{mr}" if mr else ""
            breaches = r.get("consecutive_breaches") or 1
            lines.append(
                f"- **#{iid}** `{key}` -- {status}, {sev}/{tier}, "
                f"breach x{breaches}, last {last}{mr_str}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="incidents_digest",
        description=(
            "Markdown/JSON digest of the ADR-007 incidents queue. "
            "Replaces parsing of the retired _WEEKLY_ARCHITECT_DIRECTIVE.md."
        ),
    )
    p.add_argument(
        "--status", action="append", default=None,
        help="Filter by status (repeatable). Default = all active statuses.",
    )
    p.add_argument(
        "--since", type=_parse_since, default=None,
        help="ISO8601 timestamp -- only rows with last_action_at>=since.",
    )
    p.add_argument(
        "--format", choices=("md", "json"), default="md",
        help="Output format. Default: md.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap number of rows returned by the DB (oldest first drops).",
    )
    p.add_argument(
        "--db-path", default=None,
        help="Override DB path (defaults to agt_desk.db).",
    )
    ns = p.parse_args(argv)

    statuses = ns.status if ns.status else list(DEFAULT_STATUSES)

    try:
        rows = incidents_repo.list_by_status(
            statuses, limit=ns.limit, db_path=ns.db_path,
        )
    except Exception as exc:
        sys.stderr.write(f"incidents_digest: list_by_status failed: {exc}\n")
        return 2

    rows = _filter_since(rows, ns.since)

    if ns.format == "json":
        sys.stdout.write(json.dumps(rows, indent=2, default=str))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_emit_md(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
