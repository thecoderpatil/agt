"""Decoupling Sprint A Unit A5e — atomic cutover guards.

Scope of this test file:
* Bot-side: AST guard confirming ``attested_sweeper`` and
  ``el_snapshot_writer`` registrations sit inside ``if not _use_scheduler_daemon():``
  blocks so that flipping ``USE_SCHEDULER_DAEMON=1`` silences them cleanly.
* Bot-side: ``cross_daemon_alerts_drain`` must stay unconditional (bot-owned).
* Scheduler-side: ``main()`` honors ``USE_SCHEDULER_DAEMON=1`` by driving
  ``_run()`` (scheduler.start + stop-event wait + clean shutdown).
* Scheduler-side: shutdown uses ``wait=True`` so in-flight jobs drain.

Pure-AST + monkeypatch — no APScheduler start, no network, no DB writes.
"""
from __future__ import annotations

import ast
import asyncio
import pytest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _find_run_repeating_by_name(tree: ast.AST, job_name: str):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "run_repeating":
            for kw in node.keywords:
                if (
                    kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == job_name
                ):
                    return node
    return None


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _contains(body, needle):
    for stmt in body:
        for n in ast.walk(stmt):
            if n is needle:
                return True
    return False


def _enclosing_if_test_src(tree: ast.AST, target: ast.Call) -> str | None:
    parents = _parent_map(tree)
    node: ast.AST | None = target
    while node is not None:
        parent = parents.get(id(node))
        if parent is None:
            return None
        if isinstance(parent, ast.If) and _contains(parent.body, node):
            return ast.unparse(parent.test)
        node = parent
    return None


# ---------------------------------------------------------------------------
# Bot-side gating
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bot_tree():
    return ast.parse((REPO_ROOT / "telegram_bot.py").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bot_src():
    return (REPO_ROOT / "telegram_bot.py").read_text(encoding="utf-8")


def test_bot_imports_use_scheduler_daemon(bot_src):
    assert "from agt_scheduler import use_scheduler_daemon" in bot_src


@pytest.mark.parametrize("job_name", ["attested_sweeper", "el_snapshot_writer"])
def test_bot_gated_jobs_live_under_use_scheduler_daemon(bot_tree, job_name):
    call = _find_run_repeating_by_name(bot_tree, job_name)
    assert call is not None, f"{job_name} registration missing from telegram_bot.py"
    gate = _enclosing_if_test_src(bot_tree, call)
    assert gate is not None, f"{job_name} is not inside any `if` block"
    assert "use_scheduler_daemon" in gate, (
        f"{job_name} gate is not keyed on use_scheduler_daemon; got: {gate!r}"
    )
    assert gate.strip().startswith("not "), (
        f"{job_name} must be registered only when NOT running under scheduler daemon; "
        f"got gate: {gate!r}"
    )


def test_bot_drain_job_is_unconditional(bot_tree):
    """cross_daemon_alerts_drain must stay bot-owned under both flag states."""
    call = _find_run_repeating_by_name(bot_tree, "cross_daemon_alerts_drain")
    assert call is not None, "cross_daemon_alerts_drain registration missing"
    gate = _enclosing_if_test_src(bot_tree, call)
    if gate is not None:
        # Allowed: _HALTED checks or unrelated top-level guards; disallowed:
        # any guard keyed on use_scheduler_daemon.
        assert "use_scheduler_daemon" not in gate, (
            f"drain must not be gated by the scheduler flag; got: {gate!r}"
        )


# ---------------------------------------------------------------------------
# Scheduler main() + _run() behavior
# ---------------------------------------------------------------------------


def test_scheduler_main_runs_run_when_flag_enabled(monkeypatch):
    monkeypatch.setenv("USE_SCHEDULER_DAEMON", "1")
    import importlib, agt_scheduler
    importlib.reload(agt_scheduler)
    called = {"n": 0}

    async def _fake_run() -> int:
        called["n"] += 1
        return 0

    monkeypatch.setattr(agt_scheduler, "_run", _fake_run)
    rc = agt_scheduler.main()
    assert rc == 0
    assert called["n"] == 1


def test_scheduler_main_default_off_still_dormant(monkeypatch, capsys):
    monkeypatch.delenv("USE_SCHEDULER_DAEMON", raising=False)
    import importlib, agt_scheduler
    importlib.reload(agt_scheduler)
    rc = agt_scheduler.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "USE_SCHEDULER_DAEMON" in err


def test_scheduler_run_starts_and_shutdown_waits(monkeypatch):
    """_run() calls scheduler.start(), awaits stop_event, calls shutdown(wait=True)."""
    import importlib, agt_scheduler
    importlib.reload(agt_scheduler)

    class _FakeSched:
        def __init__(self):
            self.started = False
            self.shutdown_wait = None

        def start(self):
            self.started = True

        def shutdown(self, wait=False):
            self.shutdown_wait = wait

    class _FakeIB:
        def __init__(self, *a, **kw):
            self.config = type("C", (), {"client_id": 2})()
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    fake = _FakeSched()
    monkeypatch.setattr(agt_scheduler, "build_scheduler", lambda: fake)
    monkeypatch.setattr(
        agt_scheduler, "register_jobs",
        lambda sched, ib: ["heartbeat_writer", "orphan_sweep",
                           "attested_sweeper", "el_snapshot_writer"],
    )
    monkeypatch.setattr(agt_scheduler, "IBConnector", _FakeIB)

    async def _driver():
        task = asyncio.create_task(agt_scheduler._run())
        for _ in range(40):
            await asyncio.sleep(0.01)
            if fake.started:
                break
        assert fake.started, "scheduler.start() was never invoked"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert fake.shutdown_wait is True, (
            f"shutdown(wait=True) required for clean drain; got wait={fake.shutdown_wait!r}"
        )

    asyncio.run(_driver())


def test_scheduler_shutdown_wait_true_in_source():
    """Textual belt-and-braces: the source holds wait=True at the shutdown site."""
    src = (REPO_ROOT / "agt_scheduler.py").read_text(encoding="utf-8")
    assert "scheduler.shutdown(wait=True)" in src
    assert "scheduler.shutdown(wait=False)" not in src
