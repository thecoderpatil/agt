"""AGT Telegram command registry -- authoritative manifest for slash commands.

COMMAND_REGISTRY is the single source of truth for the command surface.
telegram_bot.py registration (app.add_handler(CommandHandler(...))) stays
explicit -- this registry is the *definition*, not the registration driver.

To add a command:
  1. Add an entry here with a CommandSpec.
  2. Add app.add_handler(CommandHandler(name, handler_fn)) in telegram_bot.py.
  3. The parity test (test_command_registry_parity.py) will catch drift.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    """Specification for a Telegram slash command.

    handler_name: name of the handler function in telegram_bot.py.
    description:  one-liner for /start menu and operator docs.
    visible:      True = appears in /start menu.
    """
    handler_name: str
    description: str
    visible: bool = True


COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "start":       CommandSpec("cmd_start",       "Show command menu"),
    "status":      CommandSpec("cmd_status",       "System status and positions"),
    "orders":      CommandSpec("cmd_orders",       "List pending orders"),
    "rollcheck":   CommandSpec("cmd_rollcheck",    "Check CC roll candidates"),
    "csp_harvest": CommandSpec("cmd_csp_harvest",  "Run CSP harvest scan"),
    "cc":          CommandSpec("cmd_cc",           "Covered call daily scan"),
    "budget":      CommandSpec("cmd_budget",       "Budget utilisation"),
    "clear":       CommandSpec("cmd_clear",        "Clear staging queue"),
    "reconnect":   CommandSpec("cmd_reconnect",    "Reconnect IBKR gateway"),
    "vrp":         CommandSpec("cmd_vrp",          "VRP scan"),
    "think":       CommandSpec("cmd_think",        "Think through a scenario"),
    "deep":        CommandSpec("cmd_deep",         "Deep analysis"),
    "approve":     CommandSpec("cmd_approve",      "Approve staged order"),
    "reject":      CommandSpec("cmd_reject",       "Reject staged order"),
    "cure":        CommandSpec("cmd_cure",         "Open Cure Console"),
    "recover_transmitting": CommandSpec(
                   "cmd_recover_transmitting",     "Recover stuck TRANSMITTING orders"),
    "halt":        CommandSpec("cmd_halt",         "Halt all execution"),
    "resume":      CommandSpec("cmd_resume",       "Resume execution"),
    "daily":       CommandSpec("cmd_daily",        "Trigger daily CC scan"),
    "report":      CommandSpec("cmd_report",       "Generate daily report"),
    "list_rem":    CommandSpec("cmd_list_rem",     "List active remediation incidents"),
    "approve_rem": CommandSpec("cmd_approve_rem",  "Approve remediation action"),
    "reject_rem":  CommandSpec("cmd_reject_rem",   "Reject remediation action"),
    "scan":        CommandSpec("cmd_scan",         "Run scan orchestrator",
                               visible=False),
    "flex_status": CommandSpec("cmd_flex_status",  "Flex-sync freshness status"),
    "oversight_status": CommandSpec(
                   "cmd_oversight_status",         "Observability digest (ADR-017)"),
    "flex_manual_reconcile": CommandSpec(
                   "cmd_flex_manual_reconcile",    "Manual Flex backfill for a date (ADR-018 Phase 2)"),
}
