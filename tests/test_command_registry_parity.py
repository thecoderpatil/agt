"""Tests for COMMAND_REGISTRY parity against telegram_bot.py registration block.

Replaces the source-grep + static-list approach from test_command_prune.py
(which is a deselected dirty file with stale kill-lists). This test uses
COMMAND_REGISTRY as the source of truth and parses telegram_bot.py for
CommandHandler("name") patterns to catch drift.

All tests marked sprint_a.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agt_equities.command_registry import COMMAND_REGISTRY

pytestmark = pytest.mark.sprint_a

_BOT_PATH = Path(__file__).parent.parent / "telegram_bot.py"

_EXPECTED_COUNT = 26  # update when adding/removing commands (Sprint 7 MR C: +oversight_status)


def _get_registered_commands() -> set[str]:
    """Parse telegram_bot.py for CommandHandler("name") patterns."""
    src = _BOT_PATH.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r'CommandHandler\("(\w+)"', src))


def test_all_registry_commands_are_registered():
    """Every key in COMMAND_REGISTRY must have a CommandHandler in telegram_bot.py."""
    registered = _get_registered_commands()
    missing = [cmd for cmd in COMMAND_REGISTRY if cmd not in registered]
    assert not missing, (
        f"Commands in COMMAND_REGISTRY but not registered in telegram_bot.py: {missing}. "
        "Add app.add_handler(CommandHandler(name, handler_fn)) for each."
    )


def test_all_registered_commands_are_in_registry():
    """Every CommandHandler in telegram_bot.py must appear in COMMAND_REGISTRY."""
    registered = _get_registered_commands()
    untracked = [cmd for cmd in registered if cmd not in COMMAND_REGISTRY]
    assert not untracked, (
        f"Commands registered in telegram_bot.py but absent from COMMAND_REGISTRY: {untracked}. "
        "Add an entry to agt_equities/command_registry.py for each."
    )


def test_registry_has_expected_command_count():
    """Registry count is a canary for accidental additions or deletions."""
    assert len(COMMAND_REGISTRY) == _EXPECTED_COUNT, (
        f"Expected {_EXPECTED_COUNT} commands, got {len(COMMAND_REGISTRY)}. "
        "Update _EXPECTED_COUNT in this test when the set changes intentionally."
    )


def test_rem_commands_in_registry():
    """Remediation commands are in COMMAND_REGISTRY (replaces source-grep in test_cmd_rem_redirect.py)."""
    for cmd in ("list_rem", "approve_rem", "reject_rem"):
        assert cmd in COMMAND_REGISTRY, (
            f"/{cmd} missing from COMMAND_REGISTRY. "
            "Add a CommandSpec entry and verify CommandHandler registration."
        )
