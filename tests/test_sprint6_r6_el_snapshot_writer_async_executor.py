"""
tests/test_sprint6_r6_el_snapshot_writer_async_executor.py

Sprint 6 Mega-MR 1 §1F — R6 regression guard + fix verification.

Regression context: `agt_scheduler._el_snapshot_writer_job` is `async
def` (line 388) but was registered on the scheduler's default
`ThreadPoolExecutor(max_workers=10)` (set in `build_scheduler()`
executors dict). A ThreadPool worker cannot await coroutines, so every
30-second fire emitted `RuntimeWarning: coroutine was never awaited`.
Writes eventually landed via asyncio GC paths (41443 rows present) but
the path was fragile — any change to event-loop GC semantics or a
deploy-time environment shift could have silently disabled the writer.

Same class as Sprint 5 R2 (PTB JobQueue executor).

**Fix choice (Coder judgment per dispatch §1F):** Option (d) — register
`_el_snapshot_writer_job` with an explicit `executor="asyncio"` and add
an `AsyncIOExecutor` instance to the scheduler's executor map alongside
the existing ThreadPool default.

Why not:
- (a) `asyncio.run(...)` wrapper: creates a new event loop per fire,
  breaks the ib_connector binding (connection is attached to the main
  loop) — unsafe.
- (b) Convert to sync def: the body `await ib_connector.ensure_connected()`
  + `await ib.accountSummaryAsync()` means (b) doesn't apply.
- (c) Register via PTB JobQueue: wrong coupling — scheduler-only work
  shouldn't depend on the bot's event loop.

Why (d):
- Minimal diff (1 import + 1 dict entry + 1 kwarg).
- Preserves ThreadPool concurrency for the 3 sync jobs
  (heartbeat_writer, orphan_sweep, attested_sweeper).
- AsyncIOExecutor natively awaits coroutines on the main event loop
  (the one `ib_connector` is bound to).
- Idiomatic APScheduler pattern for mixed sync/async job sets.

This guard asserts:

1. `build_scheduler()` declares an `AsyncIOExecutor` alongside the
   ThreadPool in its executors dict, under a non-default key.
2. The `scheduler.add_job(_el_snapshot_writer_job, ...)` callsite
   passes an `executor=` kwarg pointing at the asyncio executor key.
3. AST: the `async def _el_snapshot_writer_job` remains async (not
   converted to sync — that would mean option (b) was taken, which
   our investigation rejected).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO = Path(__file__).resolve().parent.parent
SCHEDULER = REPO / "agt_scheduler.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_r6_build_scheduler_adds_asyncio_executor():
    src = _read(SCHEDULER)
    assert "AsyncIOExecutor" in src, (
        "Sprint 6 R6: agt_scheduler.build_scheduler() must import "
        "APScheduler's AsyncIOExecutor so async jobs can be awaited."
    )
    assert (
        "from apscheduler.executors.asyncio import AsyncIOExecutor" in src
        or "from apscheduler.executors.asyncio import AsyncIOExecutor as" in src
    ), (
        "Sprint 6 R6: AsyncIOExecutor must be imported from "
        "apscheduler.executors.asyncio in agt_scheduler.py."
    )
    # Executor map mentions 'asyncio' key
    assert '"asyncio"' in src or "'asyncio'" in src, (
        "Sprint 6 R6: build_scheduler() must register AsyncIOExecutor "
        "under the 'asyncio' key in the executors dict."
    )


def test_r6_el_snapshot_writer_registered_with_asyncio_executor():
    src = _read(SCHEDULER)
    # Find the _el_snapshot_writer_job add_job call and inspect its kwargs.
    idx = src.find("scheduler.add_job(\n        _el_snapshot_writer_job")
    if idx == -1:
        # Fallback: search for any add_job with _el_snapshot_writer_job.
        idx = src.find("_el_snapshot_writer_job,\n        trigger=")
        if idx == -1:
            pytest.fail("Could not locate _el_snapshot_writer_job add_job call")
    # Inspect the next 400 chars for executor kwarg.
    block = src[idx : idx + 500]
    assert 'executor="asyncio"' in block or "executor='asyncio'" in block, (
        "Sprint 6 R6: scheduler.add_job(_el_snapshot_writer_job, ...) "
        "must pass executor=\"asyncio\" so the coroutine is awaited. "
        "Default ThreadPoolExecutor silently drops coroutine returns."
    )


def test_r6_el_snapshot_writer_remains_async_def():
    """Sanity check: we did NOT take dispatch option (b) and convert to sync.

    Option (b) would have been incorrect because the body awaits
    `ib_connector.ensure_connected()` and `ib.accountSummaryAsync()`.
    """
    src = _read(SCHEDULER)
    tree = ast.parse(src)
    found_async = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_el_snapshot_writer_job"
        ):
            found_async = True
            break
    assert found_async, (
        "Sprint 6 R6: `_el_snapshot_writer_job` must remain `async def` "
        "(its body awaits ib_connector / accountSummaryAsync). If you took "
        "dispatch option (b) and converted to sync, you broke IB coupling."
    )
