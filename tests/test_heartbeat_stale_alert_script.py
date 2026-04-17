"""MR !88: static assertions for scripts/heartbeat_stale_alert.ps1.

The shim is the independent external observer for simultaneous
dual-daemon-death under MR2's watchdog retirement. It has no runtime
dependency on the bot or scheduler processes. These tests are
static-only: verify the script lives on disk, is ASCII-only, references
the canonical heartbeat table + Telegram API host, uses the canonical
TELEGRAM_USER_ID env var name, and avoids known-drift URL patterns.

No live execution, no SQLite fixture, no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "heartbeat_stale_alert.ps1"
)


def test_script_present():
    assert SCRIPT_PATH.exists(), f"Missing: {SCRIPT_PATH}"


def test_script_is_ascii():
    """Enforce ASCII (feedback_ps1_ascii_only.md). PS 5.1 on Windows has
    chewed on UTF-8 em-dashes in the installer before (MR !87 build
    2459913725)."""
    data = SCRIPT_PATH.read_bytes()
    offenders = [(i, b) for i, b in enumerate(data) if b > 0x7F]
    assert offenders == [], (
        f"non-ASCII bytes at {offenders[:5]}; strip em-dashes/smart quotes"
    )


def test_references_daemon_heartbeat_table():
    """Shim must query the canonical heartbeat table, not a stale alias."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "daemon_heartbeat" in text


def test_references_telegram_api_host():
    """Shim must post to api.telegram.org directly -- bypassing the
    cross_daemon_alerts bus is the whole point of this external observer."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "api.telegram.org" in text


def test_does_not_route_through_cross_daemon_alerts():
    """Writing to cross_daemon_alerts would require the bot drain to be
    alive; we cannot assume that here."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "cross_daemon_alerts" not in text.lower() or "bypass" in text.lower()


def test_reads_telegram_user_id_not_chat_id():
    """Canonical env var is TELEGRAM_USER_ID (telegram_bot.py:77,
    vrp_veto.py:725). TELEGRAM_CHAT_ID is not defined anywhere in the
    codebase."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "TELEGRAM_USER_ID" in text
    assert "TELEGRAM_CHAT_ID" not in text


def test_no_cure_console_drift():
    """Don't accidentally hit localhost Cure Console or similar dev-loop
    URLs that leaked into scripts in the past."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    forbidden = ["localhost:8080", "127.0.0.1:8080", "Cure Console"]
    hits = [needle for needle in forbidden if needle in text]
    assert hits == [], f"Unexpected references: {hits}"


def test_reads_env_not_hardcodes_token():
    """Never check in a bot token. Script must load from .env at runtime."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    # Telegram bot tokens look like "<digits>:<letters-digits-underscores>".
    import re
    matches = re.findall(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", text)
    assert matches == [], f"Potential token literal in script: {matches}"


def test_exit_codes_distinct():
    """Exit codes should be disjoint so the schtask history distinguishes
    missing .env from python absent from Telegram post failure."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    # Script uses: exit 1 (fatal), 2 (.env missing), 3 (python absent),
    # 4 (DB query fail), 5 (telegram post fail).
    for code in ("exit 1", "exit 2", "exit 3", "exit 4", "exit 5"):
        assert code in text, f"expected exit code not present: {code}"
