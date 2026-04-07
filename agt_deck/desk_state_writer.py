"""
agt_deck/desk_state_writer.py — Generates desk_state.md as single source of truth.

Atomic write via temp file + os.replace to prevent partial reads.
Called by: flex_sync.py post-sync, 5-min APScheduler job, manual trigger.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DESK_STATE_PATH = Path(__file__).resolve().parent.parent / "desk_state.md"


def generate_desk_state(
    mode: str,
    household_data: dict[str, dict],
    rule_evaluations: list,
    glide_paths: list,
    walker_warning_count: int,
    walker_worst_severity: str | None,
    recent_transitions: list[dict],
    report_date: str | None = None,
) -> str:
    """Generate desk_state.md content as a Markdown string.

    Pure function — no I/O. Caller writes to disk.
    """
    now = datetime.utcnow().isoformat(timespec='seconds')
    rd = report_date or now[:10]

    lines = [
        f"# AGT Desk State",
        f"",
        f"**Generated:** {now} UTC",
        f"**Report date:** {rd}",
        f"**Mode:** {mode}",
        f"",
    ]

    # Per-household summary
    for hh, data in sorted(household_data.items()):
        hh_short = hh.replace("_Household", "")
        lines.append(f"## {hh_short}")
        lines.append(f"")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| NAV | ${data.get('nlv', 0):,.2f} |")
        lines.append(f"| Leverage | {data.get('leverage', 0):.2f}x |")
        if data.get('el') is not None:
            lines.append(f"| EL | ${data['el']:,.2f} ({data.get('el_pct', 0):.1f}%) |")
        else:
            lines.append(f"| EL | unavailable |")
        lines.append(f"| Active cycles | {data.get('active_cycles', 0)} |")
        lines.append(f"")

        # Top concentrations
        conc = data.get('concentrations', [])
        if conc:
            lines.append(f"**Concentrations:** " + ", ".join(
                f"{t['ticker']} {t['pct']:.1f}%" for t in conc[:5]
            ))
            lines.append(f"")

    # Rule evaluations
    lines.append(f"## Rule Evaluations")
    lines.append(f"")
    lines.append(f"| Rule | Household | Ticker | Value | Status | Message |")
    lines.append(f"|------|-----------|--------|-------|--------|---------|")
    for ev in rule_evaluations:
        hh = (ev.household or "—").replace("_Household", "")
        tk = ev.ticker or "—"
        val = f"{ev.raw_value}" if ev.raw_value is not None else "—"
        lines.append(f"| {ev.rule_id} | {hh} | {tk} | {val} | {ev.status} | {ev.message} |")
    lines.append(f"")

    # Walker warnings
    lines.append(f"## Walker Warnings")
    lines.append(f"")
    lines.append(f"Count: {walker_warning_count}, Worst severity: {walker_worst_severity or 'none'}")
    lines.append(f"")

    # Glide paths
    if glide_paths:
        lines.append(f"## Active Glide Paths")
        lines.append(f"")
        lines.append(f"| Household | Rule | Ticker | Baseline | Target | Start | Due |")
        lines.append(f"|-----------|------|--------|----------|--------|-------|-----|")
        for gp in glide_paths:
            hh = gp.household_id.replace("_Household", "")
            tk = gp.ticker or "—"
            lines.append(
                f"| {hh} | {gp.rule_id} | {tk} | {gp.baseline_value} | "
                f"{gp.target_value} | {gp.start_date} | {gp.target_date} |"
            )
        lines.append(f"")

    # Recent mode transitions
    if recent_transitions:
        lines.append(f"## Recent Mode Transitions")
        lines.append(f"")
        for t in recent_transitions[:5]:
            lines.append(f"- {t.get('timestamp', '?')}: {t.get('old_mode')} → {t.get('new_mode')}"
                         f" (trigger: {t.get('trigger_rule', '—')})")
        lines.append(f"")

    # Handoff doc freshness
    lines.append(f"## Handoff Docs")
    lines.append(f"")
    _handoffs_dir = Path(__file__).resolve().parent.parent / "reports" / "handoffs"
    for label, fname in [
        ("Architect", "HANDOFF_ARCHITECT_latest.md"),
        ("Coder", "HANDOFF_CODER_latest.md"),
    ]:
        fp = _handoffs_dir / fname
        if fp.exists():
            mtime = datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {label}: reports/handoffs/{fname} (modified: {mtime})")
        else:
            lines.append(f"- {label}: reports/handoffs/{fname} (not found)")
    lines.append(f"")

    return "\n".join(lines)


def write_desk_state_atomic(content: str, path: Path | None = None) -> None:
    """Write desk_state.md atomically via temp file + os.replace."""
    target = path or DESK_STATE_PATH
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), suffix=".tmp", prefix="desk_state_"
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp_path, str(target))
            logger.info("desk_state.md written (%d bytes)", len(content))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.error("Failed to write desk_state.md: %s", exc)
