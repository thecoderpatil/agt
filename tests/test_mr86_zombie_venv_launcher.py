"""MR !86: NO_ZOMBIE_BOT_PROCESS must collapse Windows venv launcher pairs.

Incident 412 fingerprint: ``pid_count=2`` where PID 5756 (ppid 11292) spawned
PID 4508 (ppid 5756) ~70ms later, both running the exact same command line
``C:\\AGT_Telegram_Bridge\\.venv\\Scripts\\python.exe telegram_bot.py``. That
pair is the Windows venv launcher wrapper pattern -- one logical bot
instance, not a zombie.

These tests exercise the psutil-available path directly by injecting a fake
psutil module into ``sys.modules``. The subprocess fallback path is covered
by the two MR !85 tests in ``tests/test_mr85_invariant_hygiene.py`` and is
unchanged by this MR.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agt_equities.invariants.checks import (
    _collapse_venv_launcher_pairs,
    check_no_zombie_bot_process,
)
from agt_equities.invariants.types import CheckContext

pytestmark = pytest.mark.sprint_a


NOW = datetime(2026, 4, 17, 22, 0, 0, tzinfo=timezone.utc)

BOT_CMD_STR = (
    r"C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe telegram_bot.py"
)
BOT_CMD_LIST = [
    r"C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe",
    "telegram_bot.py",
]


@pytest.fixture
def ctx() -> CheckContext:
    return CheckContext(
        now_utc=NOW,
        db_path=":memory:",
        paper_mode=True,
        live_accounts=frozenset({"U21971297", "U22076329"}),
        paper_accounts=frozenset({"DUP751003", "DUP751004", "DUP751005"}),
        expected_daemons=frozenset({"agt_bot"}),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    try:
        yield c
    finally:
        c.close()


def _fake_proc(pid: int, ppid: int | None, cmdline: list[str]):
    """Build a psutil-like process object with an ``info`` dict."""
    return SimpleNamespace(info={
        "pid": pid,
        "ppid": ppid,
        "name": "python.exe",
        "cmdline": cmdline,
    })


def _install_fake_psutil(monkeypatch, procs):
    """Inject a fake psutil whose process_iter yields ``procs``."""
    fake = SimpleNamespace(
        process_iter=lambda fields: iter(procs),
        # Exception classes the check's try/except references; we never raise
        # them in these tests, but the except clause must resolve them.
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)


# -----------------------------------------------------------------------------
# Helper tests -- exercise _collapse_venv_launcher_pairs in isolation.
# -----------------------------------------------------------------------------
def test_helper_collapses_parent_child_same_cmd():
    """Parent+child with identical cmdline collapse to just the parent."""
    out = _collapse_venv_launcher_pairs([
        (5756, 11292, BOT_CMD_STR),
        (4508, 5756, BOT_CMD_STR),
    ])
    assert out == [5756]


def test_helper_keeps_two_unrelated_bots():
    """Two bots with no parent/child link survive the collapse."""
    out = _collapse_venv_launcher_pairs([
        (100, 1, BOT_CMD_STR),
        (200, 1, BOT_CMD_STR),
    ])
    assert sorted(out) == [100, 200]


def test_helper_does_not_collapse_when_cmdlines_differ():
    """Different cmdlines (e.g. an extra flag) means these are NOT a launcher pair."""
    out = _collapse_venv_launcher_pairs([
        (5756, 11292, BOT_CMD_STR),
        (4508, 5756, BOT_CMD_STR + " --extra-flag"),
    ])
    assert sorted(out) == [4508, 5756]


def test_helper_orphan_child_whose_ppid_not_in_candidate_set_is_kept():
    """If the ppid is not in our candidate set, the child stays (can't infer launcher)."""
    out = _collapse_venv_launcher_pairs([
        (5756, 11292, BOT_CMD_STR),
        (4508, 99999, BOT_CMD_STR),  # ppid 99999 not in set
    ])
    assert sorted(out) == [4508, 5756]


def test_helper_collapses_three_level_chain_to_root():
    """A -> B -> C all same cmd: both B and C are launcher children."""
    out = _collapse_venv_launcher_pairs([
        (100, 1, BOT_CMD_STR),
        (200, 100, BOT_CMD_STR),
        (300, 200, BOT_CMD_STR),
    ])
    assert out == [100]


def test_helper_handles_none_ppid():
    """``None`` ppid (psutil couldn't resolve parent) must not crash."""
    out = _collapse_venv_launcher_pairs([
        (100, None, BOT_CMD_STR),
    ])
    assert out == [100]


# -----------------------------------------------------------------------------
# Scenario tests -- exercise check_no_zombie_bot_process end-to-end via fake psutil.
# -----------------------------------------------------------------------------
def test_scenario_a_venv_launcher_pair_yields_no_violation(
    conn, ctx, monkeypatch
):
    """(a) Parent 5756 + child 4508 with identical cmdline = one bot, zero violations.

    This is the exact fingerprint of incident 412.
    """
    procs = [
        _fake_proc(pid=5756, ppid=11292, cmdline=BOT_CMD_LIST),
        _fake_proc(pid=4508, ppid=5756, cmdline=BOT_CMD_LIST),
    ]
    _install_fake_psutil(monkeypatch, procs)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert vios == [], f"expected no violation; got {vios}"


def test_scenario_b_two_unrelated_bots_trigger_singleton_violation(
    conn, ctx, monkeypatch
):
    """(b) Two bots with independent ppids = real zombie, ONE Violation."""
    procs = [
        _fake_proc(pid=100, ppid=1, cmdline=BOT_CMD_LIST),
        _fake_proc(pid=200, ppid=1, cmdline=BOT_CMD_LIST),
    ]
    _install_fake_psutil(monkeypatch, procs)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert len(vios) == 1
    v = vios[0]
    assert v.invariant_id == "NO_ZOMBIE_BOT_PROCESS"
    assert v.evidence["pid_count"] == 2
    assert sorted(v.evidence["pids"]) == [100, 200]
    assert v.stable_key == "NO_ZOMBIE_BOT_PROCESS"
    assert v.severity == "high"


def test_scenario_c_launcher_pair_plus_unrelated_zombie_pid_count_equals_two(
    conn, ctx, monkeypatch
):
    """(c) A(parent) + B(child, same cmd) + C(unrelated): after collapse, real zombie = {A, C}."""
    procs = [
        _fake_proc(pid=5756, ppid=11292, cmdline=BOT_CMD_LIST),
        _fake_proc(pid=4508, ppid=5756, cmdline=BOT_CMD_LIST),
        _fake_proc(pid=9999, ppid=1, cmdline=BOT_CMD_LIST),
    ]
    _install_fake_psutil(monkeypatch, procs)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert len(vios) == 1
    v = vios[0]
    assert v.evidence["pid_count"] == 2
    assert sorted(v.evidence["pids"]) == [5756, 9999]
    assert v.stable_key == "NO_ZOMBIE_BOT_PROCESS"


def test_scenario_d_three_unrelated_bots_still_yield_singleton_violation(
    conn, ctx, monkeypatch
):
    """(d) Preserves existing singleton behavior: even 3 zombies = ONE Violation, not three."""
    procs = [
        _fake_proc(pid=100, ppid=1, cmdline=BOT_CMD_LIST),
        _fake_proc(pid=200, ppid=1, cmdline=BOT_CMD_LIST),
        _fake_proc(pid=300, ppid=1, cmdline=BOT_CMD_LIST),
    ]
    _install_fake_psutil(monkeypatch, procs)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert len(vios) == 1
    v = vios[0]
    assert v.evidence["pid_count"] == 3
    assert sorted(v.evidence["pids"]) == [100, 200, 300]
    assert v.stable_key == "NO_ZOMBIE_BOT_PROCESS"


def test_single_bot_baseline_no_violation(conn, ctx, monkeypatch):
    """Regression guard: one bot running alone = no violation (pre-existing behavior)."""
    procs = [_fake_proc(pid=5756, ppid=11292, cmdline=BOT_CMD_LIST)]
    _install_fake_psutil(monkeypatch, procs)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert vios == []


def test_launcher_child_with_different_cmdline_is_not_collapsed(
    conn, ctx, monkeypatch
):
    """Safety: only byte-equal cmdlines collapse. An extra flag = two distinct instances."""
    procs = [
        _fake_proc(pid=5756, ppid=11292, cmdline=BOT_CMD_LIST),
        _fake_proc(
            pid=4508,
            ppid=5756,
            cmdline=BOT_CMD_LIST + ["--dry-run"],
        ),
    ]
    _install_fake_psutil(monkeypatch, procs)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["pid_count"] == 2
    assert sorted(vios[0].evidence["pids"]) == [4508, 5756]


def test_non_python_process_with_telegram_bot_in_cmd_is_ignored(
    conn, ctx, monkeypatch
):
    """Regression: filter is ``python in name AND telegram_bot.py in cmd``. A bash wrapper
    mentioning telegram_bot.py must not count.
    """
    fake = SimpleNamespace(
        process_iter=lambda fields: iter([
            SimpleNamespace(info={
                "pid": 1234,
                "ppid": 1,
                "name": "bash",
                "cmdline": ["bash", "-c", "echo telegram_bot.py"],
            }),
            _fake_proc(pid=5756, ppid=11292, cmdline=BOT_CMD_LIST),
        ]),
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    vios = check_no_zombie_bot_process(conn, ctx)
    assert vios == []
