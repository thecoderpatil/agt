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


# -----------------------------------------------------------------------------
# MR !90: evict_zombie_daemons -- boot-path zombie eviction.
#
# Surfaced by the MR1.5 crash-restart test: NSSM killing the outer venv
# launcher leaves the inner python.exe grandchild holding IBKR clientId=1
# and the singleton lock file. NSSM's restart of the outer dies on that.
# This module evicts any other telegram_bot.py (or agt_scheduler.py)
# process before the new daemon tries to grab the lock.
#
# These tests mock psutil at sys.modules["psutil"] and patch
# incidents_repo.register to a recorder -- no real processes, no real DB.
# -----------------------------------------------------------------------------


class _EvictableProc:
    """psutil.Process stand-in with terminate()/kill()/is_running()/wait().

    ``terminate_kills``: True means terminate() exits the process (Unix-SIGTERM
    with a well-behaved handler, or Windows TerminateProcess). False simulates
    a SIGTERM-resistant process.

    ``kill_survives``: True means kill() ALSO leaves the process alive (degenerate
    case; exercises the ``.zombies_survived_sigkill`` branch).
    """

    def __init__(
        self,
        pid: int,
        ppid: int | None,
        cmdline: list[str],
        *,
        terminate_kills: bool = True,
        kill_survives: bool = False,
    ) -> None:
        self.info = {
            "pid": pid,
            "ppid": ppid,
            "name": "python.exe",
            "cmdline": cmdline,
        }
        self._alive = True
        self._terminate_kills = terminate_kills
        self._kill_survives = kill_survives
        self.terminate_called = False
        self.kill_called = False

    def terminate(self) -> None:
        self.terminate_called = True
        if self._terminate_kills:
            self._alive = False

    def kill(self) -> None:
        self.kill_called = True
        if not self._kill_survives:
            self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def is_running(self) -> bool:
        return self._alive


def _install_fake_psutil_with_procs(monkeypatch, procs):
    fake = SimpleNamespace(
        process_iter=lambda fields: iter(procs),
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)


@pytest.fixture
def incident_recorder(monkeypatch):
    """Intercept incidents_repo.register; return the capture list."""
    calls: list[dict] = []

    def _fake_register(**kwargs):
        calls.append(kwargs)
        return {"id": 999, "incident_key": kwargs.get("incident_key")}

    # Also patch the module path our code imports through.
    import agt_equities.incidents_repo as _repo
    monkeypatch.setattr(_repo, "register", _fake_register)
    return calls


def test_evict_no_zombies_returns_empty_result(monkeypatch, incident_recorder):
    """Happy path: only self (+ venv-launcher pair) present -> no zombies, no incident."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    procs = [
        _EvictableProc(pid=100, ppid=1, cmdline=BOT_CMD_LIST),           # self
        _EvictableProc(pid=200, ppid=100, cmdline=BOT_CMD_LIST),         # self's child (launcher pair)
    ]
    _install_fake_psutil_with_procs(monkeypatch, procs)

    result = evict_zombie_daemons(
        cmdline_marker="telegram_bot.py", self_pid=100
    )
    assert result.zombies_found == []
    assert result.zombies_evicted == []
    assert result.zombies_survived_sigkill == []
    # launcher child PID 200 recognized as self-ancestry
    assert result.evictions_skipped_self_ancestry == [200]
    # No incident when nothing evicted
    assert incident_recorder == []
    # Neither proc was touched
    assert not any(p.terminate_called or p.kill_called for p in procs)


def test_evict_single_zombie_terminate_path(monkeypatch, incident_recorder):
    """One foreign zombie -> terminate() suffices, incident row written."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    self_proc = _EvictableProc(pid=100, ppid=1, cmdline=BOT_CMD_LIST)
    zombie = _EvictableProc(pid=999, ppid=1, cmdline=BOT_CMD_LIST)
    _install_fake_psutil_with_procs(monkeypatch, [self_proc, zombie])

    result = evict_zombie_daemons(
        cmdline_marker="telegram_bot.py", self_pid=100, sigterm_grace_s=0.5
    )
    assert result.zombies_found == [999]
    assert result.zombies_evicted == [999]
    assert result.zombies_survived_sigkill == []
    assert zombie.terminate_called is True
    assert zombie.kill_called is False  # never escalated
    assert not self_proc.terminate_called
    # Incident written once
    assert len(incident_recorder) == 1
    rec = incident_recorder[0]
    assert rec["incident_key"] == "ZOMBIE_DAEMON_EVICTED:telegram_bot.py"
    assert rec["severity"] == "warn"
    assert rec["scrutiny_tier"] == "low"
    assert rec["observed_state"]["evicted_pids"] == [999]
    assert rec["observed_state"]["sigkill_survivors"] == []


def test_evict_sigterm_resistant_escalates_to_kill(monkeypatch, incident_recorder):
    """Zombie survives terminate() -> kill() must be called + confirm dead."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    self_proc = _EvictableProc(pid=100, ppid=1, cmdline=BOT_CMD_LIST)
    tough = _EvictableProc(
        pid=999, ppid=1, cmdline=BOT_CMD_LIST, terminate_kills=False,
    )
    _install_fake_psutil_with_procs(monkeypatch, [self_proc, tough])

    result = evict_zombie_daemons(
        cmdline_marker="telegram_bot.py", self_pid=100, sigterm_grace_s=0.5
    )
    assert result.zombies_evicted == [999]
    assert result.zombies_survived_sigkill == []
    assert tough.terminate_called is True
    assert tough.kill_called is True


def test_evict_zombie_survives_even_kill(monkeypatch, incident_recorder):
    """Pathological: kill() does not dislodge -> reported in survivors list."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    self_proc = _EvictableProc(pid=100, ppid=1, cmdline=BOT_CMD_LIST)
    undead = _EvictableProc(
        pid=999, ppid=1, cmdline=BOT_CMD_LIST,
        terminate_kills=False, kill_survives=True,
    )
    _install_fake_psutil_with_procs(monkeypatch, [self_proc, undead])

    result = evict_zombie_daemons(
        cmdline_marker="telegram_bot.py", self_pid=100, sigterm_grace_s=0.3
    )
    assert result.zombies_evicted == []
    assert result.zombies_survived_sigkill == [999]
    assert undead.terminate_called and undead.kill_called
    # Incident still written (observability) even though eviction failed
    assert len(incident_recorder) == 1
    assert incident_recorder[0]["observed_state"]["sigkill_survivors"] == [999]


def test_evict_never_touches_self(monkeypatch, incident_recorder):
    """Even when self and a zombie are both enumerated, self is never terminated."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    self_proc = _EvictableProc(pid=100, ppid=1, cmdline=BOT_CMD_LIST)
    zombie = _EvictableProc(pid=777, ppid=1, cmdline=BOT_CMD_LIST)
    _install_fake_psutil_with_procs(monkeypatch, [self_proc, zombie])

    evict_zombie_daemons(cmdline_marker="telegram_bot.py", self_pid=100,
                        sigterm_grace_s=0.2)

    assert self_proc.terminate_called is False
    assert self_proc.kill_called is False
    assert zombie.terminate_called is True


def test_evict_never_touches_self_venv_launcher_parent(monkeypatch, incident_recorder):
    """Self's direct parent with identical cmdline is the venv launcher -- never touched."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    parent = _EvictableProc(pid=50, ppid=1, cmdline=BOT_CMD_LIST)   # venv launcher outer
    self_proc = _EvictableProc(pid=100, ppid=50, cmdline=BOT_CMD_LIST)  # self (inner)
    real_zombie = _EvictableProc(pid=777, ppid=1, cmdline=BOT_CMD_LIST)
    _install_fake_psutil_with_procs(monkeypatch, [parent, self_proc, real_zombie])

    result = evict_zombie_daemons(
        cmdline_marker="telegram_bot.py", self_pid=100, sigterm_grace_s=0.2
    )

    assert parent.terminate_called is False
    assert parent.kill_called is False
    assert real_zombie.terminate_called is True
    assert result.zombies_found == [777]
    assert 50 in result.evictions_skipped_self_ancestry


def test_evict_scheduler_marker_independent_of_bot(monkeypatch, incident_recorder):
    """The marker filter must be exact-substring -- telegram_bot.py procs are
    invisible when evicting for scheduler, and vice versa."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    scheduler_cmd = [
        r"C:\AGT_Telegram_Bridge\.venv\Scripts\python.exe",
        "agt_scheduler.py",
    ]
    # Mix: a bot (should be ignored), scheduler self, scheduler zombie
    bot_other = _EvictableProc(pid=50, ppid=1, cmdline=BOT_CMD_LIST)
    sched_self = _EvictableProc(pid=100, ppid=1, cmdline=scheduler_cmd)
    sched_zombie = _EvictableProc(pid=777, ppid=1, cmdline=scheduler_cmd)
    _install_fake_psutil_with_procs(
        monkeypatch, [bot_other, sched_self, sched_zombie]
    )

    result = evict_zombie_daemons(
        cmdline_marker="agt_scheduler.py", self_pid=100, sigterm_grace_s=0.2
    )

    assert result.zombies_found == [777]
    assert sched_zombie.terminate_called is True
    assert bot_other.terminate_called is False  # different marker, not touched
    assert incident_recorder[0]["incident_key"] == (
        "ZOMBIE_DAEMON_EVICTED:agt_scheduler.py"
    )


def test_evict_psutil_unavailable_is_soft_fail(monkeypatch, incident_recorder):
    """If psutil import raises, return empty result + no crash -- boot must not
    die inside the evictor."""
    from agt_equities.zombie_evict import evict_zombie_daemons
    # Make `import psutil` fail by injecting a module that raises on import.
    # Simpler: remove the real one and insert a sentinel that fails attr access.
    import builtins
    real_import = builtins.__import__

    def fail_psutil_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated psutil absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_psutil_import)

    result = evict_zombie_daemons(
        cmdline_marker="telegram_bot.py", self_pid=100
    )
    assert result.zombies_found == []
    assert result.zombies_evicted == []
    assert incident_recorder == []
