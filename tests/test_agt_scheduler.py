"""Unit tests for ``agt_scheduler`` skeleton (Decoupling Sprint A Unit A1).

These tests verify the import-side-effect-free invariant and the
USE_SCHEDULER_DAEMON gate. Job-registration and behavior tests land in
later units.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.sprint_a


def test_import_no_side_effects(monkeypatch):
    """Importing agt_scheduler must not start a scheduler, open IB, or touch DB."""
    # Force-purge to re-trigger module top-level on next import.
    sys.modules.pop("agt_scheduler", None)
    monkeypatch.delenv("USE_SCHEDULER_DAEMON", raising=False)
    import agt_scheduler  # noqa: F401
    assert agt_scheduler.DAEMON_NAME == "agt_scheduler"
    assert agt_scheduler.DEFAULT_CLIENT_ID == 2
    assert agt_scheduler.SCHEDULER_THREADPOOL_MAX_WORKERS == 10


def test_use_scheduler_daemon_default_false(monkeypatch):
    monkeypatch.delenv("USE_SCHEDULER_DAEMON", raising=False)
    import agt_scheduler
    assert agt_scheduler.use_scheduler_daemon() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False),
])
def test_use_scheduler_daemon_truthy_values(monkeypatch, val, expected):
    monkeypatch.setenv("USE_SCHEDULER_DAEMON", val)
    import agt_scheduler
    assert agt_scheduler.use_scheduler_daemon() is expected


def test_main_exits_zero_when_disabled(monkeypatch, capsys):
    """Default-off behavior: main() returns 0 cleanly without touching IB."""
    monkeypatch.delenv("USE_SCHEDULER_DAEMON", raising=False)
    import agt_scheduler
    rc = agt_scheduler.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "USE_SCHEDULER_DAEMON" in err


def test_scheduler_client_id_default(monkeypatch):
    monkeypatch.delenv("SCHEDULER_IB_CLIENT_ID", raising=False)
    import agt_scheduler
    assert agt_scheduler.scheduler_client_id() == 2


def test_scheduler_client_id_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_IB_CLIENT_ID", "5")
    import agt_scheduler
    assert agt_scheduler.scheduler_client_id() == 5


def test_build_scheduler_threadpool_size():
    """Blind-spot #3 mitigation: explicit max_workers=10 (not default 1)."""
    import agt_scheduler
    sched = agt_scheduler.build_scheduler()
    try:
        executor = sched._executors["default"]
        # APScheduler ThreadPoolExecutor wraps a concurrent.futures pool.
        # The underlying pool exposes _max_workers.
        underlying = getattr(executor, "_pool", None)
        if underlying is not None:
            assert underlying._max_workers == agt_scheduler.SCHEDULER_THREADPOOL_MAX_WORKERS
        else:
            # Fallback: APScheduler's executor stores max_workers directly.
            assert getattr(executor, "_max_workers", None) == \
                agt_scheduler.SCHEDULER_THREADPOOL_MAX_WORKERS \
                or getattr(executor, "max_workers", None) == \
                agt_scheduler.SCHEDULER_THREADPOOL_MAX_WORKERS
    finally:
        # Don't actually start the scheduler.
        pass


def test_build_scheduler_timezone_pinned():
    """Timezone must be America/New_York to match ET-anchored job times."""
    import agt_scheduler
    sched = agt_scheduler.build_scheduler()
    tz_name = str(sched.timezone)
    assert "New_York" in tz_name


def test_register_jobs_a2_baseline():
    """A2 ships heartbeat_writer + orphan_sweep. A5 adds the 13 production jobs."""
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    registered = agt_scheduler.register_jobs(sched, conn)
    assert registered == ["heartbeat_writer", "orphan_sweep"]
    job_ids = {j.id for j in sched.get_jobs()}
    assert {"heartbeat_writer", "orphan_sweep"}.issubset(job_ids)
