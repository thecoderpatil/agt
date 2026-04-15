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


def test_register_jobs_a5e_set():
    """A2 + A5a + A5d.d + A5e: full job set including beta + corporate_intel.

    Order is the registration order — preserved here as a regression guard
    so future units appending jobs do not accidentally reorder the existing
    set (which would break dependent code that introspects `registered[0]`).
    """
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    registered = agt_scheduler.register_jobs(sched, conn)
    assert registered == [
        "heartbeat_writer",
        "orphan_sweep",
        "attested_sweeper",
        "el_snapshot_writer",
        "beta_cache_refresh",
        "beta_startup",
        "corporate_intel_refresh",
        "corporate_intel_startup",
        "flex_sync_eod",
        "universe_monthly",
        "conviction_weekly",
    ]
    job_ids = {j.id for j in sched.get_jobs()}
    assert {
        "heartbeat_writer", "orphan_sweep",
        "attested_sweeper", "el_snapshot_writer",
        "beta_cache_refresh", "beta_startup",
        "corporate_intel_refresh", "corporate_intel_startup",
        "flex_sync_eod", "universe_monthly",
        "conviction_weekly",
    }.issubset(job_ids)


def test_a5a_attested_sweeper_trigger_interval_60s():
    """A5a: attested_sweeper must fire every 60s (matches bot-side cadence)."""
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    agt_scheduler.register_jobs(sched, conn)
    job = sched.get_job("attested_sweeper")
    assert job is not None, "attested_sweeper not registered"
    # APScheduler IntervalTrigger exposes the interval as a timedelta.
    interval = getattr(job.trigger, "interval", None)
    assert interval is not None, f"unexpected trigger type: {type(job.trigger)}"
    assert interval.total_seconds() == 60, (
        f"attested_sweeper interval expected 60s, got {interval.total_seconds()}"
    )



def _get_orphan_sweep_callable(monkeypatch):
    """Build a scheduler + register_jobs, return the orphan_sweep job's
    callable. Monkeypatching on agt_equities.health.sweep_orphan_staged_orders
    *before* register_jobs runs lets us intercept the call from inside the
    closure (register_jobs does the import locally)."""
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    agt_scheduler.register_jobs(sched, conn)
    job = sched.get_job("orphan_sweep")
    assert job is not None, "orphan_sweep not registered"
    return job.func


def test_a5c_orphan_sweep_enqueues_alert_when_swept(monkeypatch):
    """A5c: orphan_sweep callback enqueues an ORPHAN_SWEEP warn alert via the
    cross_daemon_alerts bus when the sweep returns swept_count > 0."""
    captured: list[dict] = []

    def fake_sweep(*, ttl_hours, **_kw):  # noqa: ARG001 — signature-compatible
        return 7

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append(
            {"kind": kind, "payload": payload, "severity": severity, "db_path": db_path}
        )
        return 1

    # Patch BEFORE register_jobs runs so the closure captures the fake.
    monkeypatch.setattr(
        "agt_equities.health.sweep_orphan_staged_orders", fake_sweep
    )
    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    job_callable = _get_orphan_sweep_callable(monkeypatch)
    job_callable()

    assert len(captured) == 1, f"expected exactly 1 alert, got {captured}"
    rec = captured[0]
    assert rec["kind"] == "ORPHAN_SWEEP"
    assert rec["severity"] == "warn"
    assert rec["payload"]["swept_count"] == 7
    assert rec["payload"]["ttl_hours"] > 0


def test_a5c_orphan_sweep_silent_when_zero_swept(monkeypatch):
    """A5c: zero-swept runs must not enqueue an alert (no spam)."""
    captured: list[dict] = []

    monkeypatch.setattr(
        "agt_equities.health.sweep_orphan_staged_orders",
        lambda *, ttl_hours, **_kw: 0,
    )
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda *a, **kw: captured.append((a, kw)),
    )

    job_callable = _get_orphan_sweep_callable(monkeypatch)
    job_callable()

    assert captured == [], f"expected no alerts on zero-swept, got {captured}"


def test_a5c_orphan_sweep_swallows_alert_failures(monkeypatch):
    """A5c: an exception inside enqueue_alert must NOT propagate out of the
    job callback. The sweep already committed; alert-bus issues are
    best-effort and should be logged, not crash the scheduler."""
    monkeypatch.setattr(
        "agt_equities.health.sweep_orphan_staged_orders",
        lambda *, ttl_hours, **_kw: 3,
    )

    def boom(*a, **kw):
        raise RuntimeError("simulated alert-bus down")

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", boom)

    job_callable = _get_orphan_sweep_callable(monkeypatch)
    # Must not raise
    job_callable()


def test_a5c_orphan_sweep_swallows_sweep_failures(monkeypatch):
    """A5c: if the sweep itself raises, the job must log + return without
    attempting to enqueue an alert."""
    captured: list = []

    def boom(*, ttl_hours, **_kw):  # noqa: ARG001
        raise RuntimeError("DB exploded")

    monkeypatch.setattr("agt_equities.health.sweep_orphan_staged_orders", boom)
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda *a, **kw: captured.append((a, kw)),
    )

    job_callable = _get_orphan_sweep_callable(monkeypatch)
    job_callable()  # must not raise

    assert captured == [], "must not enqueue when sweep itself failed"


# ---------------------------------------------------------------------------
# A5d.d — el_snapshot_writer (scheduler-side) tests
# ---------------------------------------------------------------------------


class _FakeAccountItem:
    """Duck-type replacement for ib_async.AccountValue."""
    __slots__ = ("account", "tag", "value")

    def __init__(self, account: str, tag: str, value):
        self.account = account
        self.tag = tag
        self.value = value


class _FakeIB:
    def __init__(self, summary):
        self._summary = summary

    async def accountSummaryAsync(self):
        return self._summary


class _FakeIBConn:
    """Duck-type IBConnector with config.client_id + ensure_connected()."""

    def __init__(self, summary, *, connect_fail: bool = False):
        self._summary = summary
        self._connect_fail = connect_fail
        import types as _types
        self.config = _types.SimpleNamespace(client_id=2)

    async def ensure_connected(self):
        if self._connect_fail:
            raise RuntimeError("simulated IB down")
        return _FakeIB(self._summary)


def _get_el_writer_callable(ib_conn):
    """Register jobs with the given fake IBConnector and return the
    el_snapshot_writer async callable."""
    import agt_scheduler
    sched = agt_scheduler.build_scheduler()
    agt_scheduler.register_jobs(sched, ib_conn)
    job = sched.get_job("el_snapshot_writer")
    assert job is not None, "el_snapshot_writer not registered"
    return job.func


def _run_sync(coro):
    import asyncio
    return asyncio.run(coro)


def _sqlite_with_el_snapshots(tmp_path):
    """Open a sqlite file and create just the el_snapshots table we write to."""
    import sqlite3
    db = tmp_path / "a5dd_el.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS el_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id       TEXT,
            household        TEXT NOT NULL,
            timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
            excess_liquidity REAL,
            nlv              REAL,
            buying_power     REAL,
            source           TEXT NOT NULL DEFAULT 'ibkr_live'
        )
        """
    )
    conn.commit()
    conn.close()
    return str(db)


def test_a5dd_writer_registered_with_30s_interval():
    """el_snapshot_writer must appear in register_jobs output and run
    every 30 seconds."""
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    registered = agt_scheduler.register_jobs(sched, conn)
    assert "el_snapshot_writer" in registered
    job = sched.get_job("el_snapshot_writer")
    assert job is not None
    # Trigger should be an IntervalTrigger with 30s period.
    from apscheduler.triggers.interval import IntervalTrigger
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == 30.0


def test_a5dd_non_margin_account_writes_snapshot_no_apex(monkeypatch, tmp_path):
    """Non-margin account with healthy EL must INSERT one row and NOT
    enqueue an APEX alert."""
    acct = "U_IRA_ACCT"
    # Stand up a config where acct is active, not margin.
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [acct])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset())
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {acct: "Yash_Household"},
    )

    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    from contextlib import closing
    def fake_get_conn(*a, **kw):
        return sqlite3.connect(db_path)
    monkeypatch.setattr("agt_equities.db.get_db_connection", fake_get_conn)

    captured_alerts: list = []
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda *a, **kw: captured_alerts.append((a, kw)),
    )

    summary = [
        _FakeAccountItem(acct, "NetLiquidation", "500000"),
        _FakeAccountItem(acct, "ExcessLiquidity", "100000"),
        _FakeAccountItem(acct, "BuyingPower", "250000"),
    ]
    ib_conn = _FakeIBConn(summary)
    job = _get_el_writer_callable(ib_conn)
    _run_sync(job())

    with closing(sqlite3.connect(db_path)) as c:
        rows = c.execute(
            "SELECT account_id, household, excess_liquidity, nlv, buying_power, source "
            "FROM el_snapshots"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == acct
    assert rows[0][1] == "Yash_Household"
    assert rows[0][2] == 100000.0
    assert rows[0][3] == 500000.0
    assert rows[0][4] == 250000.0
    assert rows[0][5] == "ibkr_live"
    assert captured_alerts == [], "non-margin account must not emit APEX"


def test_a5dd_margin_account_healthy_el_writes_snapshot_no_apex(
    monkeypatch, tmp_path,
):
    """Margin account with el_pct > 0.08 must write snapshot, no APEX alert."""
    acct = "U21971297"
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [acct])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset([acct]))
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {acct: "Yash_Household"},
    )

    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    from contextlib import closing
    monkeypatch.setattr(
        "agt_equities.db.get_db_connection",
        lambda *a, **kw: sqlite3.connect(db_path),
    )
    captured: list = []
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda *a, **kw: captured.append((a, kw)),
    )

    # el_pct = 100000 / 500000 = 0.20 → above 0.08 threshold
    summary = [
        _FakeAccountItem(acct, "NetLiquidation", "500000"),
        _FakeAccountItem(acct, "ExcessLiquidity", "100000"),
        _FakeAccountItem(acct, "BuyingPower", "250000"),
    ]
    job = _get_el_writer_callable(_FakeIBConn(summary))
    _run_sync(job())

    with closing(sqlite3.connect(db_path)) as c:
        count = c.execute("SELECT COUNT(*) FROM el_snapshots").fetchone()[0]
    assert count == 1
    assert captured == [], "healthy EL must not emit APEX"


def test_a5dd_margin_account_apex_enqueues_alert_and_skips_snapshot(
    monkeypatch, tmp_path,
):
    """el_pct <= 0.08 must enqueue APEX_SURVIVAL and SKIP the DB write
    (matches bot-side behavior — no snapshot during critical condition)."""
    acct = "U21971297"
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [acct])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset([acct]))
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {acct: "Yash_Household"},
    )

    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    from contextlib import closing
    monkeypatch.setattr(
        "agt_equities.db.get_db_connection",
        lambda *a, **kw: sqlite3.connect(db_path),
    )
    captured: list = []
    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append(
            {"kind": kind, "payload": payload, "severity": severity}
        )
        return 1
    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    # el_pct = 30000 / 500000 = 0.06 → below 0.08 threshold
    summary = [
        _FakeAccountItem(acct, "NetLiquidation", "500000"),
        _FakeAccountItem(acct, "ExcessLiquidity", "30000"),
        _FakeAccountItem(acct, "BuyingPower", "150000"),
    ]
    job = _get_el_writer_callable(_FakeIBConn(summary))
    _run_sync(job())

    # APEX alert emitted with correct shape
    assert len(captured) == 1
    rec = captured[0]
    assert rec["kind"] == "APEX_SURVIVAL"
    assert rec["severity"] == "critical"
    assert rec["payload"]["account_id"] == acct
    assert rec["payload"]["household"] == "Yash_Household"
    assert abs(rec["payload"]["el_pct"] - 0.06) < 1e-6
    assert rec["payload"]["nlv"] == 500000.0
    assert rec["payload"]["excess_liquidity"] == 30000.0

    # And NO snapshot row was written for this account during APEX.
    with closing(sqlite3.connect(db_path)) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM el_snapshots WHERE account_id = ?", (acct,),
        ).fetchone()[0]
    assert count == 0


def test_a5dd_apex_debounces_repeat_alerts_within_15min(monkeypatch, tmp_path):
    """Two ticks back-to-back on the same APEX condition must yield ONE
    alert (in-process 15-min per-account debounce)."""
    acct = "U21971297"
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [acct])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset([acct]))
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {acct: "Yash_Household"},
    )

    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    monkeypatch.setattr(
        "agt_equities.db.get_db_connection",
        lambda *a, **kw: sqlite3.connect(db_path),
    )
    captured: list = []
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda kind, payload, *, severity="info", db_path=None:
            captured.append({"kind": kind, "severity": severity}),
    )

    summary = [
        _FakeAccountItem(acct, "NetLiquidation", "500000"),
        _FakeAccountItem(acct, "ExcessLiquidity", "20000"),  # el_pct 0.04
        _FakeAccountItem(acct, "BuyingPower", "100000"),
    ]
    ib_conn = _FakeIBConn(summary)
    job = _get_el_writer_callable(ib_conn)
    _run_sync(job())
    _run_sync(job())
    _run_sync(job())
    # Exactly one alert despite three ticks
    assert len(captured) == 1


def test_a5dd_swallows_ib_connect_failure(monkeypatch, tmp_path):
    """If ensure_connected raises, the job must return silently — no DB
    writes, no alerts, no propagation."""
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", ["U_X"])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset())
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {"U_X": "X"},
    )
    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    monkeypatch.setattr(
        "agt_equities.db.get_db_connection",
        lambda *a, **kw: sqlite3.connect(db_path),
    )
    captured: list = []
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda *a, **kw: captured.append((a, kw)),
    )

    job = _get_el_writer_callable(_FakeIBConn([], connect_fail=True))
    _run_sync(job())  # must not raise

    import sqlite3 as _s
    from contextlib import closing
    with closing(_s.connect(db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM el_snapshots").fetchone()[0] == 0
    assert captured == []


def test_a5dd_swallows_db_write_failure(monkeypatch, tmp_path):
    """DB-write exception on a single account must not kill the job or
    propagate. Alert path must also be untouched."""
    acct = "U_IRA"
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [acct])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset())
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {acct: "Yash_Household"},
    )
    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr("agt_equities.db.get_db_connection", boom)
    captured: list = []
    monkeypatch.setattr(
        "agt_equities.alerts.enqueue_alert",
        lambda *a, **kw: captured.append((a, kw)),
    )

    summary = [
        _FakeAccountItem(acct, "NetLiquidation", "500000"),
        _FakeAccountItem(acct, "ExcessLiquidity", "100000"),
        _FakeAccountItem(acct, "BuyingPower", "250000"),
    ]
    job = _get_el_writer_callable(_FakeIBConn(summary))
    _run_sync(job())  # must not raise

    assert captured == [], "DB failure must not trigger alerts"


def test_a5dd_ignores_inactive_accounts(monkeypatch, tmp_path):
    """Summary rows for accounts outside ACTIVE_ACCOUNTS must be dropped."""
    active = "U_ACTIVE"
    stranger = "U_STRANGER"
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [active])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset())
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {active: "Yash_Household"},
    )

    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    from contextlib import closing
    monkeypatch.setattr(
        "agt_equities.db.get_db_connection",
        lambda *a, **kw: sqlite3.connect(db_path),
    )

    summary = [
        _FakeAccountItem(active, "NetLiquidation", "500000"),
        _FakeAccountItem(active, "ExcessLiquidity", "100000"),
        _FakeAccountItem(active, "BuyingPower", "250000"),
        _FakeAccountItem(stranger, "NetLiquidation", "999999"),
        _FakeAccountItem(stranger, "ExcessLiquidity", "1"),
        _FakeAccountItem(stranger, "BuyingPower", "0"),
    ]
    job = _get_el_writer_callable(_FakeIBConn(summary))
    _run_sync(job())

    with closing(sqlite3.connect(db_path)) as c:
        rows = c.execute("SELECT account_id FROM el_snapshots").fetchall()
    assert [r[0] for r in rows] == [active]


def test_a5dd_apex_enqueue_failure_does_not_crash_job(monkeypatch, tmp_path):
    """enqueue_alert raising inside the APEX branch must not propagate."""
    acct = "U21971297"
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", [acct])
    monkeypatch.setattr("agt_equities.config.MARGIN_ACCOUNTS", frozenset([acct]))
    monkeypatch.setattr(
        "agt_equities.config.ACCOUNT_TO_HOUSEHOLD", {acct: "Yash_Household"},
    )
    db_path = _sqlite_with_el_snapshots(tmp_path)
    import sqlite3
    monkeypatch.setattr(
        "agt_equities.db.get_db_connection",
        lambda *a, **kw: sqlite3.connect(db_path),
    )

    def boom(*a, **kw):
        raise RuntimeError("bus down")
    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", boom)

    summary = [
        _FakeAccountItem(acct, "NetLiquidation", "500000"),
        _FakeAccountItem(acct, "ExcessLiquidity", "10000"),  # el_pct 0.02
        _FakeAccountItem(acct, "BuyingPower", "50000"),
    ]
    job = _get_el_writer_callable(_FakeIBConn(summary))
    _run_sync(job())  # must not raise


# ---------------------------------------------------------------------------
# A5e — beta_cache_refresh + corporate_intel_refresh migration tests
# ---------------------------------------------------------------------------


def _build_scheduler_with_jobs():
    """Helper: build scheduler + register all jobs with a fake IBConnector."""
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    agt_scheduler.register_jobs(sched, conn)
    return sched


def test_a5e_beta_cache_refresh_cron_trigger():
    """beta_cache_refresh must use CronTrigger at 04:00."""
    sched = _build_scheduler_with_jobs()
    job = sched.get_job("beta_cache_refresh")
    assert job is not None, "beta_cache_refresh not registered"
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(job.trigger, CronTrigger)
    # Verify hour=4, minute=0 in the trigger fields.
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "4"
    assert fields["minute"] == "0"


def test_a5e_beta_startup_date_trigger():
    """beta_startup must use DateTrigger (one-shot at startup + 10s)."""
    sched = _build_scheduler_with_jobs()
    job = sched.get_job("beta_startup")
    assert job is not None, "beta_startup not registered"
    from apscheduler.triggers.date import DateTrigger
    assert isinstance(job.trigger, DateTrigger)


def test_a5e_corporate_intel_refresh_cron_trigger():
    """corporate_intel_refresh must use CronTrigger at 05:00."""
    sched = _build_scheduler_with_jobs()
    job = sched.get_job("corporate_intel_refresh")
    assert job is not None, "corporate_intel_refresh not registered"
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "5"
    assert fields["minute"] == "0"


def test_a5e_corporate_intel_startup_date_trigger():
    """corporate_intel_startup must use DateTrigger (one-shot at startup + 15s)."""
    sched = _build_scheduler_with_jobs()
    job = sched.get_job("corporate_intel_startup")
    assert job is not None, "corporate_intel_startup not registered"
    from apscheduler.triggers.date import DateTrigger
    assert isinstance(job.trigger, DateTrigger)


def test_a5e_beta_cache_refresh_calls_library(monkeypatch):
    """beta_cache_refresh job must call refresh_beta_cache with active tickers."""
    import types

    captured: list[list[str]] = []

    def fake_refresh(tickers):
        captured.append(list(tickers))

    monkeypatch.setattr("agt_equities.beta_cache.refresh_beta_cache", fake_refresh)

    # Fake trade_repo.get_active_cycles returning one active cycle.
    fake_cycle = types.SimpleNamespace(ticker="AAPL", status="ACTIVE")
    monkeypatch.setattr(
        "agt_equities.trade_repo.get_active_cycles",
        lambda **kw: [fake_cycle],
    )

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("beta_cache_refresh")
    job.func()

    assert len(captured) == 1
    assert "AAPL" in captured[0]


def test_a5e_beta_cache_refresh_swallows_exceptions(monkeypatch):
    """beta_cache_refresh must not propagate exceptions."""
    def boom(tickers):
        raise RuntimeError("beta cache exploded")

    monkeypatch.setattr("agt_equities.beta_cache.refresh_beta_cache", boom)

    import types
    fake_cycle = types.SimpleNamespace(ticker="AAPL", status="ACTIVE")
    monkeypatch.setattr(
        "agt_equities.trade_repo.get_active_cycles",
        lambda **kw: [fake_cycle],
    )

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("beta_cache_refresh")
    job.func()  # must not raise


def test_a5e_beta_cache_refresh_no_tickers_noop(monkeypatch):
    """With no active cycles, refresh_beta_cache must not be called."""
    captured: list = []

    monkeypatch.setattr(
        "agt_equities.beta_cache.refresh_beta_cache",
        lambda tickers: captured.append(tickers),
    )
    monkeypatch.setattr(
        "agt_equities.trade_repo.get_active_cycles",
        lambda **kw: [],
    )

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("beta_cache_refresh")
    job.func()

    assert captured == [], "must not call refresh_beta_cache with empty tickers"


def test_a5e_corporate_intel_refresh_calls_provider(monkeypatch):
    """corporate_intel_refresh must call get_corporate_calendar per ticker."""
    import types

    called_tickers: list[str] = []

    class FakeProvider:
        def get_corporate_calendar(self, ticker):
            called_tickers.append(ticker)

    monkeypatch.setattr(
        "agt_equities.providers.yfinance_corporate_intelligence."
        "YFinanceCorporateIntelligenceProvider",
        FakeProvider,
    )

    fake_cycles = [
        types.SimpleNamespace(ticker="AAPL", status="ACTIVE"),
        types.SimpleNamespace(ticker="MSFT", status="ACTIVE"),
        types.SimpleNamespace(ticker="GOOG", status="CLOSED"),
    ]
    monkeypatch.setattr(
        "agt_equities.trade_repo.get_active_cycles",
        lambda **kw: fake_cycles,
    )

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("corporate_intel_refresh")
    job.func()

    assert set(called_tickers) == {"AAPL", "MSFT"}


def test_a5e_corporate_intel_refresh_swallows_per_ticker_errors(monkeypatch):
    """A single ticker failure must not stop remaining tickers."""
    import types

    called_tickers: list[str] = []

    class FlakyProvider:
        def get_corporate_calendar(self, ticker):
            if ticker == "AAPL":
                raise RuntimeError("yfinance down for AAPL")
            called_tickers.append(ticker)

    monkeypatch.setattr(
        "agt_equities.providers.yfinance_corporate_intelligence."
        "YFinanceCorporateIntelligenceProvider",
        FlakyProvider,
    )

    fake_cycles = [
        types.SimpleNamespace(ticker="AAPL", status="ACTIVE"),
        types.SimpleNamespace(ticker="MSFT", status="ACTIVE"),
    ]
    monkeypatch.setattr(
        "agt_equities.trade_repo.get_active_cycles",
        lambda **kw: fake_cycles,
    )

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("corporate_intel_refresh")
    job.func()  # must not raise

    assert "MSFT" in called_tickers

# ---------------------------------------------------------------------------
# A5e — flex_sync_eod migration tests
# ---------------------------------------------------------------------------


def test_a5e_flex_sync_eod_cron_trigger():
    """flex_sync_eod must use CronTrigger at 17:00 Mon-Fri."""
    sched = _build_scheduler_with_jobs()
    job = sched.get_job("flex_sync_eod")
    assert job is not None, "flex_sync_eod not registered"
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "17"
    assert fields["minute"] == "0"
    assert fields["day_of_week"] == "mon-fri"


def test_a5e_flex_sync_eod_enqueues_digest_on_success(monkeypatch):
    """On successful run_sync, flex_sync_eod must enqueue FLEX_SYNC_DIGEST."""
    import types

    captured: list[dict] = []

    fake_result = types.SimpleNamespace(
        sync_id="test-123",
        status="ok",
        sections_processed=6,
        rows_received=42,
        rows_inserted=10,
        rows_updated=2,
        anomalies=[],
        error_message=None,
    )

    monkeypatch.setattr(
        "agt_equities.flex_sync.run_sync",
        lambda mode: fake_result,
    )
    monkeypatch.setattr(
        "agt_equities.flex_sync.SyncMode",
        types.SimpleNamespace(INCREMENTAL="INCREMENTAL"),
    )

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("flex_sync_eod")
    job.func()

    assert len(captured) == 1
    rec = captured[0]
    assert rec["kind"] == "FLEX_SYNC_DIGEST"
    assert rec["severity"] == "info"
    assert rec["payload"]["sync_id"] == "test-123"
    assert rec["payload"]["rows_received"] == 42
    assert rec["payload"]["rows_inserted"] == 10


def test_a5e_flex_sync_eod_enqueues_failure_on_exception(monkeypatch):
    """On run_sync exception, must enqueue FLEX_SYNC_FAILURE with crit severity."""
    import types

    captured: list[dict] = []

    def boom(mode):
        raise RuntimeError("Flex endpoint down")

    monkeypatch.setattr("agt_equities.flex_sync.run_sync", boom)
    monkeypatch.setattr(
        "agt_equities.flex_sync.SyncMode",
        types.SimpleNamespace(INCREMENTAL="INCREMENTAL"),
    )

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("flex_sync_eod")
    job.func()  # must not raise

    assert len(captured) == 1
    rec = captured[0]
    assert rec["kind"] == "FLEX_SYNC_FAILURE"
    assert rec["severity"] == "crit"
    assert "Flex endpoint down" in rec["payload"]["error"]


def test_a5e_flex_sync_eod_swallows_alert_bus_failure(monkeypatch):
    """Alert enqueue failure after successful sync must not propagate."""
    import types

    fake_result = types.SimpleNamespace(
        sync_id="test-456",
        status="ok",
        sections_processed=3,
        rows_received=10,
        rows_inserted=5,
        rows_updated=0,
        anomalies=[],
        error_message=None,
    )

    monkeypatch.setattr(
        "agt_equities.flex_sync.run_sync",
        lambda mode: fake_result,
    )
    monkeypatch.setattr(
        "agt_equities.flex_sync.SyncMode",
        types.SimpleNamespace(INCREMENTAL="INCREMENTAL"),
    )

    def boom(*a, **kw):
        raise RuntimeError("bus down")

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", boom)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("flex_sync_eod")
    job.func()  # must not raise


def test_a5e_flex_sync_eod_warns_on_error_message(monkeypatch):
    """If run_sync returns with error_message, severity must be warn not info."""
    import types

    captured: list[dict] = []

    fake_result = types.SimpleNamespace(
        sync_id="test-789",
        status="partial",
        sections_processed=4,
        rows_received=20,
        rows_inserted=8,
        rows_updated=0,
        anomalies=[],
        error_message="Trades section had parse errors",
    )

    monkeypatch.setattr(
        "agt_equities.flex_sync.run_sync",
        lambda mode: fake_result,
    )
    monkeypatch.setattr(
        "agt_equities.flex_sync.SyncMode",
        types.SimpleNamespace(INCREMENTAL="INCREMENTAL"),
    )

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("flex_sync_eod")
    job.func()

    assert len(captured) == 1
    assert captured[0]["severity"] == "warn"
    assert "parse errors" in captured[0]["payload"]["error"]

# ---------------------------------------------------------------------------
# A5e -- universe_monthly migration tests
# ---------------------------------------------------------------------------


def test_a5e_universe_monthly_cron_trigger():
    """universe_monthly must use CronTrigger on day=1 at 06:00."""
    sched = _build_scheduler_with_jobs()
    job = sched.get_job("universe_monthly")
    assert job is not None, "universe_monthly not registered"
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "6"
    assert fields["minute"] == "0"
    assert fields["day"] == "1"


def test_a5e_universe_monthly_enqueues_alert_on_success(monkeypatch):
    """On successful refresh, must enqueue UNIVERSE_REFRESH alert."""
    captured: list[dict] = []

    monkeypatch.setattr(
        "agt_equities.universe_refresh.refresh_ticker_universe",
        lambda **kw: {"added": 5, "updated": 100, "total": 105, "error": None},
    )

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("universe_monthly")
    job.func()

    assert len(captured) == 1
    rec = captured[0]
    assert rec["kind"] == "UNIVERSE_REFRESH"
    assert rec["severity"] == "info"
    assert rec["payload"]["added"] == 5
    assert rec["payload"]["total"] == 105


def test_a5e_universe_monthly_warns_on_error(monkeypatch):
    """If refresh returns with error, severity must be warn."""
    captured: list[dict] = []

    monkeypatch.setattr(
        "agt_equities.universe_refresh.refresh_ticker_universe",
        lambda **kw: {"added": 0, "updated": 0, "total": 0,
                      "error": "Both Wikipedia scrapes failed"},
    )

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("universe_monthly")
    job.func()

    assert len(captured) == 1
    assert captured[0]["severity"] == "warn"
    assert "Wikipedia" in captured[0]["payload"]["error"]


def test_a5e_universe_monthly_enqueues_crit_on_exception(monkeypatch):
    """If refresh raises, must enqueue UNIVERSE_REFRESH with crit severity."""
    captured: list[dict] = []

    def boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "agt_equities.universe_refresh.refresh_ticker_universe", boom,
    )

    def fake_enqueue(kind, payload, *, severity="info", db_path=None):
        captured.append({"kind": kind, "payload": payload, "severity": severity})
        return 1

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("universe_monthly")
    job.func()  # must not raise

    assert len(captured) == 1
    assert captured[0]["kind"] == "UNIVERSE_REFRESH"
    assert captured[0]["severity"] == "crit"


def test_a5e_universe_monthly_swallows_alert_failure(monkeypatch):
    """Alert enqueue failure after successful refresh must not propagate."""
    monkeypatch.setattr(
        "agt_equities.universe_refresh.refresh_ticker_universe",
        lambda **kw: {"added": 1, "updated": 2, "total": 3, "error": None},
    )

    def boom(*a, **kw):
        raise RuntimeError("bus down")

    monkeypatch.setattr("agt_equities.alerts.enqueue_alert", boom)

    sched = _build_scheduler_with_jobs()
    job = sched.get_job("universe_monthly")
    job.func()  # must not raise


# ── A5e: conviction_weekly tests ───────────────────────────────────────

def _build_scheduler_and_connector():
    """Helper: like _build_scheduler_with_jobs but also returns ib_connector."""
    import agt_scheduler
    from agt_equities.ib_conn import IBConnector, IBConnConfig
    sched = agt_scheduler.build_scheduler()
    conn = IBConnector(config=IBConnConfig(client_id=2))
    agt_scheduler.register_jobs(sched, conn)
    return sched, conn


class TestConvictionWeeklyJob:
    """Tests for the conviction_weekly scheduler job."""

    def test_conviction_weekly_trigger(self):
        sched = _build_scheduler_with_jobs()
        job = sched.get_job("conviction_weekly")
        assert job is not None
        trigger = job.trigger
        from apscheduler.triggers.cron import CronTrigger
        assert isinstance(trigger, CronTrigger)

    @pytest.mark.asyncio
    async def test_conviction_weekly_calls_refresh(self, monkeypatch):
        """Job fetches IB positions, filters STK, calls refresh_conviction_data."""
        scheduler, ib_connector = _build_scheduler_and_connector()
        job = scheduler.get_job("conviction_weekly")
        assert job is not None

        # Mock IB connection
        class FakeContract:
            def __init__(self, symbol, sec_type="STK"):
                self.symbol = symbol
                self.secType = sec_type

        class FakePosition:
            def __init__(self, symbol, position, sec_type="STK"):
                self.contract = FakeContract(symbol, sec_type)
                self.position = position

        positions = [
            FakePosition("AAPL", 100),
            FakePosition("MSFT", 200),
            FakePosition("SPX", 50, "IND"),   # excluded: not STK
            FakePosition("IBKR", 100),         # excluded: EXCLUDED_TICKERS
            FakePosition("TSLA", 0),            # excluded: zero position
        ]

        class FakeIB:
            async def reqPositionsAsync(self):
                return positions

        async def fake_ensure():
            return FakeIB()

        ib_connector.ensure_connected = fake_ensure

        refresh_calls = []
        def fake_refresh(held, **kwargs):
            refresh_calls.append(held)
            return {"updated": len(held), "failed": 0, "total": len(held), "error": None}

        monkeypatch.setattr(
            "agt_equities.conviction.refresh_conviction_data",
            fake_refresh,
        )

        enqueued = []
        def fake_enqueue(kind, payload, **kwargs):
            enqueued.append((kind, payload))

        monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

        func = job.func
        await func()

        assert len(refresh_calls) == 1
        assert refresh_calls[0] == {"AAPL", "MSFT"}
        assert len(enqueued) == 1
        assert enqueued[0][0] == "CONVICTION_REFRESH"
        assert enqueued[0][1]["updated"] == 2

    @pytest.mark.asyncio
    async def test_conviction_weekly_ib_failure_enqueues_alert(self, monkeypatch):
        """When IB connect fails, job enqueues a warn-level alert."""
        scheduler, ib_connector = _build_scheduler_and_connector()
        job = scheduler.get_job("conviction_weekly")
        assert job is not None

        async def fail_connect():
            raise ConnectionError("Gateway down")

        ib_connector.ensure_connected = fail_connect

        enqueued = []
        def fake_enqueue(kind, payload, **kwargs):
            enqueued.append((kind, payload, kwargs))

        monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

        await job.func()  # must not raise

        assert len(enqueued) == 1
        assert enqueued[0][0] == "CONVICTION_REFRESH"
        assert "Gateway down" in enqueued[0][1]["error"]
        assert enqueued[0][2].get("severity") == "warn"

    @pytest.mark.asyncio
    async def test_conviction_weekly_refresh_exception_enqueues_crit(self, monkeypatch):
        """When refresh_conviction_data raises, job enqueues a crit-level alert."""
        scheduler, ib_connector = _build_scheduler_and_connector()
        job = scheduler.get_job("conviction_weekly")

        class FakeIB:
            async def reqPositionsAsync(self):
                return []

        async def fake_ensure():
            return FakeIB()

        ib_connector.ensure_connected = fake_ensure

        def boom(held, **kwargs):
            raise RuntimeError("yfinance exploded")

        monkeypatch.setattr(
            "agt_equities.conviction.refresh_conviction_data",
            boom,
        )

        enqueued = []
        def fake_enqueue(kind, payload, **kwargs):
            enqueued.append((kind, payload, kwargs))

        monkeypatch.setattr("agt_equities.alerts.enqueue_alert", fake_enqueue)

        await job.func()  # must not raise

        assert len(enqueued) == 1
        assert enqueued[0][0] == "CONVICTION_REFRESH"
        assert enqueued[0][2].get("severity") == "crit"
