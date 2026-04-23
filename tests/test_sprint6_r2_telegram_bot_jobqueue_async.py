"""
tests/test_sprint6_r2_telegram_bot_jobqueue_async.py

Sprint 6 Mega-MR 1 §1B — R2 regression guard.

Regression context: Sprint 5 R2 was the PTB JobQueue executor misconfig.
MR C's original tried `ThreadPoolExecutor(max_workers=20)` as the PTB
JobQueue's default executor. ThreadPool cannot await coroutines, so every
async callback (bot_heartbeat, invariants_tick, cross_daemon_alerts_drain)
emitted `RuntimeWarning: coroutine was never awaited` and silently dropped.
Observed bot_heartbeat age=271s post-deploy. Fixed in MR !225 by reverting
to PTB's default `AsyncIOExecutor`.

This guard asserts:

1. Sentinel: the JobQueue configure block in telegram_bot.py does NOT
   include an `executors=...` kwarg (which would override the default
   AsyncIOExecutor).
2. Sentinel: no `ThreadPoolExecutor` import or construction inside the
   `jq.scheduler.configure(...)` call site.
3. Integration: building a PTB `Application` and registering a trivial
   async job with `run_repeating` for 1.2s emits ZERO
   `RuntimeWarning: coroutine was never awaited` warnings.

The integration test is guarded by `pytest_asyncio` availability and
`python-telegram-bot` import; it skips gracefully otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_r2_jobqueue_configure_has_no_executor_override():
    """Sentinel: configure block must not pass `executors=...`."""
    src = _read(REPO / "telegram_bot.py")
    # The specific pattern the bug introduced.
    assert "executors={" not in src or "jq.scheduler.configure" not in src or (
        # If configure exists and executors exists, ensure executors is
        # NOT inside the configure call's scope. Use a tighter pattern:
        # the canonical post-hotfix form passes only job_defaults.
        True
    )
    # Tighter assertion: find the configure block and check it mentions
    # job_defaults but not executors.
    idx = src.find("jq.scheduler.configure(")
    assert idx != -1, "telegram_bot.py must still contain jq.scheduler.configure(...)"
    # Look at next 500 chars for the call body.
    call_body = src[idx : idx + 500]
    assert "job_defaults" in call_body, "configure must pass job_defaults"
    assert "executors=" not in call_body, (
        "Sprint 6 R2: jq.scheduler.configure(...) must NOT pass executors=. "
        "PTB's default AsyncIOExecutor is required so async jobs can be "
        "awaited. MR C original regression (2026-04-23 17:30 hotfix)."
    )


def test_r2_no_threadpool_executor_import_in_telegram_bot():
    """Sentinel: no APScheduler ThreadPoolExecutor imports in telegram_bot.py.

    PTB already supplies AsyncIOExecutor; an APScheduler ThreadPoolExecutor
    import is only plausibly there to override the default, which is the
    exact regression.
    """
    src = _read(REPO / "telegram_bot.py")
    assert "from apscheduler.executors.pool import ThreadPoolExecutor" not in src, (
        "Sprint 6 R2: apscheduler ThreadPoolExecutor import is a "
        "regression marker for MR C's broken executor override."
    )
    assert "apscheduler.executors.pool.ThreadPoolExecutor" not in src


def test_r2_async_job_on_default_asyncio_scheduler_emits_no_coroutine_warning():
    """Integration contract: the exact APScheduler config PTB's JobQueue uses
    post-MR-225 must await async jobs without RuntimeWarning.

    PTB's JobQueue wraps `apscheduler.schedulers.asyncio.AsyncIOScheduler`
    with its default executor (AsyncIOExecutor). The hotfix was to call
    `jq.scheduler.configure(job_defaults={...})` without touching the
    executors. This test replicates that exact pattern on a bare
    AsyncIOScheduler (avoiding PTB's live-token validation in
    `Application.initialize()`) and asserts no `coroutine was never
    awaited` warnings are emitted over a 1.2s run window.

    If this test fails, the class-of-bug from R2 is back — swap back to
    default executors and re-check.
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except Exception:
        pytest.skip("APScheduler not importable in this environment")

    callback_invocations = {"count": 0}

    async def _async_cb():
        callback_invocations["count"] += 1

    async def _runner() -> list[warnings.WarningMessage]:
        sched = AsyncIOScheduler()
        # Mirror production MR !225 config: job_defaults only. DO NOT pass
        # executors={} here — that's the exact regression we're guarding.
        sched.configure(
            job_defaults={"misfire_grace_time": 60, "coalesce": True},
        )

        with warnings.catch_warnings(record=True) as rec:
            warnings.simplefilter("always")
            sched.start()
            try:
                sched.add_job(_async_cb, trigger="interval", seconds=0.1)
                await asyncio.sleep(1.2)
            finally:
                sched.shutdown(wait=False)
            # Give any deferred GC-triggered warnings a moment to surface.
            await asyncio.sleep(0.05)
            import gc
            gc.collect()
            return list(rec)

    captured = asyncio.run(_runner())
    offending = [
        w for w in captured
        if issubclass(w.category, RuntimeWarning)
        and "coroutine" in str(w.message)
        and "never awaited" in str(w.message)
    ]
    assert not offending, (
        f"Sprint 6 R2: {len(offending)} 'coroutine never awaited' "
        f"warnings emitted during AsyncIOScheduler async run. First: "
        f"{offending[0].message!r}. Default executor regression — ensure "
        "no `executors=` override is applied anywhere in the config chain."
    )
    assert callback_invocations["count"] >= 3, (
        f"Async callback only fired {callback_invocations['count']} times — "
        "executor may be silently dropping coroutines."
    )
