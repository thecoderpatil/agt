#!/usr/bin/env python
"""
PreToolUse hook — deterministic guardrail against destructive git commands.

Installed 2026-04-19 as part of PM-19g (Phase 3 of multi-agent sync
architecture). Enforces the worktree-isolation invariant at the
execution layer.

Rule:
  Destructive git commands (reset --hard, push --force, clean -fd,
  checkout ., worktree remove, etc.) are ALLOWED when the invoking
  shell's CWD is inside C:\\AGT_Telegram_Bridge\\.worktrees\\coder,
  and BLOCKED everywhere else — specifically in the main worktree
  at C:\\AGT_Telegram_Bridge\\ where Architect's markdown drafts
  live.

Rationale:
  Markdown "standing orders" in CLAUDE.md are probabilistic — they
  fail under LLM context degradation at ~45% context fill (observed
  in 2026 agentic-coding incident reports and twice directly in this
  project on 2026-04-19). Physical interception at the tool layer is
  deterministic. This script is that interception.

Exits:
  0 — allow (non-destructive, or destructive-but-inside-coder-worktree)
  2 — block (destructive and outside coder worktree). Returns stderr
      message to the agent as a deterministic refusal.

Audit:
  Every block event is appended to .claude/hooks/audit.jsonl for
  visibility. Allows are not logged (would be noisy).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Regex patterns for destructive git commands. Case-insensitive.
DESTRUCTIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+(--force|-f)(\s|$)", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+[^|;&]*-[a-z]*f", re.IGNORECASE),
    re.compile(r"\bgit\s+checkout\s+(--\s+)?\.\s*$", re.IGNORECASE),
    re.compile(r"\bgit\s+checkout\s+(--\s+)?\.(\s|$)", re.IGNORECASE),
    re.compile(r"\bgit\s+worktree\s+remove\b", re.IGNORECASE),
    re.compile(r"\bgit\s+update-ref\s+-d\b", re.IGNORECASE),
    re.compile(r"\bgit\s+branch\s+-D\s+main\b", re.IGNORECASE),
    re.compile(r"\bgit\s+filter-(branch|repo)\b", re.IGNORECASE),
]

# Path substrings that signal safe zones for destructive git commands.
# Each entry is matched as a substring against the normalized (lowercase,
# forward-slash) cwd. Adding a new agent worktree = append a new marker here.
ALLOWED_WORKTREE_MARKERS: tuple[str, ...] = (
    "/.worktrees/coder",
    "/.worktrees/codex",
)
AUDIT_PATH = Path(__file__).parent / "audit.jsonl"


def _normalize(path: str) -> str:
    """Lowercase + forward-slash normalization so Windows path variants match."""
    return (path or "").replace("\\", "/").lower()


def _read_input() -> dict:
    """Read JSON payload from stdin. Return {} on any parse failure."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}


def _log_block(command: str, cwd: str, matched_pattern: str) -> None:
    """Append a JSONL audit record. Best-effort — hook must not fail on log errors."""
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "block",
            "command": command,
            "cwd": cwd,
            "matched_pattern": matched_pattern,
        }
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # audit failure must never cascade into tool-block failure


def main() -> int:
    data = _read_input()
    tool_name = data.get("tool_name", "")

    # Only intercept Bash tool calls; everything else passes through.
    if tool_name != "Bash":
        return 0

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "") or ""

    # CWD may be supplied by Claude Code; fall back to os.getcwd() for safety.
    cwd = data.get("cwd") or os.getcwd()

    if not command:
        return 0

    for pattern in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            cwd_norm = _normalize(cwd)
            if any(marker in cwd_norm for marker in ALLOWED_WORKTREE_MARKERS):
                # Safe zone — allow
                return 0
            # Blocked zone — refuse and surface
            _log_block(command, cwd, pattern.pattern)
            sys.stderr.write(
                "BLOCKED by PM-19g PreToolUse hook: destructive git command attempted "
                "outside the Coder worktree.\n"
                f"  Command: {command}\n"
                f"  Current working directory: {cwd}\n"
                f"  Allowed worktrees: C:\\AGT_Telegram_Bridge\\.worktrees\\coder, C:\\AGT_Telegram_Bridge\\.worktrees\\codex\n"
                f"  Matched pattern: {pattern.pattern}\n"
                "\n"
                "Why this is blocked:\n"
                "  The main worktree at C:\\AGT_Telegram_Bridge\\ is Architect's\n"
                "  workspace. Destructive commands there would wipe ADR drafts,\n"
                "  standing-orders amendments, and session logs. The Coder worktree\n"
                "  is the only safe place for reset --hard, push --force, etc.\n"
                "\n"
                "To fix:\n"
                "  cd C:\\AGT_Telegram_Bridge\\.worktrees\\coder   # or \\codex\n"
                "  # then re-issue the command\n"
                "\n"
                "If you're already in the coder worktree and seeing this, the hook\n"
                "is matching incorrectly — report to Architect with the matched\n"
                "pattern and current cwd. Do not override by renaming the hook.\n"
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
