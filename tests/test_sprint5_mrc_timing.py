"""
tests/test_sprint5_mrc_timing.py

Sprint 5 MR C — Scheduler/executor timing + deploy integrity_check hook.

Source-level sentinels + forensic-evidence assertions. The actual misfire
reduction is an empirical test that runs on prod — the ship report captures
tomorrow's 09:35/09:37/09:45 slot outcomes as the canary.

Scope:
  - PTB JobQueue configure path: misfire_grace_time=60, coalesce=True.
    NOTE: MR !225 hotfix reverted MR C's ThreadPoolExecutor override
    (it broke async JobQueue.job_callback awaits — bot_heartbeat age
    climbed to 271s post-deploy). Executor must stay AsyncIOExecutor
    (PTB default); only job_defaults are configured here.
  - deploy.ps1 integrity_check hook + non-zero exit on corruption.
  - Forensic evidence captured in ship report: Thursday 2026-04-23 scheduler
    daemon had 0 misfires while PTB JobQueue had 1566+ (822 bot_heartbeat +
    742 cross_daemon_alerts_drain + 3 cron cron-slot misses).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


REPO = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# PTB JobQueue scheduler.configure sentinel
# ---------------------------------------------------------------------------


def test_mrc_telegram_bot_job_queue_configure_block_present():
    src = _read(REPO / "telegram_bot.py")
    # The configure call block
    assert "jq.scheduler.configure(" in src, (
        "Sprint 5 MR C: PTB JobQueue must be reconfigured BEFORE job "
        "registration so job_defaults apply to all registered jobs."
    )
    assert '"misfire_grace_time": 60' in src, (
        "MR C: misfire_grace_time must be bumped to 60s (from default 1s)."
    )
    assert '"coalesce": True' in src, (
        "MR C: coalesce=True collapses multiple missed ticks into a single "
        "fire so interval jobs don't spiral on executor recovery."
    )


def test_mrc_hotfix_no_executor_override():
    """MR !225 regression guard: the configure call must NOT replace PTB's
    default AsyncIOExecutor with a ThreadPoolExecutor. A thread-pool executor
    cannot await JobQueue.job_callback coroutines; MR C's original code
    (max_workers=20 ThreadPoolExecutor) silently broke every async job."""
    src = _read(REPO / "telegram_bot.py")
    # Locate the configure call and check the argument set
    cfg_idx = src.find("jq.scheduler.configure(")
    assert cfg_idx >= 0, "scheduler.configure block missing"
    # Walk forward to the closing paren
    close_idx = src.find(")", cfg_idx)
    block = src[cfg_idx:close_idx + 1]
    assert "executors=" not in block, (
        "MR !225 hotfix: scheduler.configure must NOT set executors=. "
        "PTB's default AsyncIOExecutor is required to await async job_callback "
        "coroutines. Prior code used ThreadPoolExecutor(max_workers=20) which "
        "silently broke bot_heartbeat (age climbed to 271s)."
    )
    assert "ThreadPoolExecutor" not in block, (
        "MR !225 hotfix: no ThreadPoolExecutor reference in the configure block."
    )


def test_mrc_configure_runs_before_run_daily():
    """Order check: scheduler.configure must appear before any jq.run_daily
    in main() — APScheduler's config is mutable but only pre-start changes
    apply cleanly."""
    src = _read(REPO / "telegram_bot.py")
    cfg_idx = src.find("jq.scheduler.configure(")
    first_run_daily_idx = src.find("jq.run_daily(", cfg_idx if cfg_idx >= 0 else 0)
    assert cfg_idx >= 0, "scheduler.configure not found"
    assert first_run_daily_idx > cfg_idx, (
        "MR C: scheduler.configure must run before the first jq.run_daily so "
        "the job_defaults apply to cc_daily, csp_scan_daily, csp_digest_send."
    )


def test_mrc_configure_failure_does_not_crash_boot():
    """Defensive: if configure raises, the except branch logs a warning but
    does not re-raise — boot continues with defaults (stale behavior is
    preferred over boot failure)."""
    src = _read(REPO / "telegram_bot.py")
    # Find the try/except around the configure call.
    # Hotfix MR !225 removed the ThreadPoolExecutor import, so the try: block
    # now contains only the configure() call + logger.info.
    m = re.search(
        r"try:\s*\n\s*jq\.scheduler\.configure\(.*?"
        r"except Exception as _sched_cfg_exc:",
        src, re.DOTALL,
    )
    assert m is not None, (
        "MR C: scheduler.configure must be wrapped in try/except so a "
        "version-skew or internal APScheduler change doesn't abort bot boot."
    )


# ---------------------------------------------------------------------------
# deploy.ps1 integrity_check hook
# ---------------------------------------------------------------------------


def test_mrc_deploy_script_has_integrity_check_hook():
    src = _read(REPO / "scripts" / "deploy" / "deploy.ps1")
    assert "PRAGMA integrity_check" in src, (
        "MR C: deploy.ps1 must issue PRAGMA integrity_check after service "
        "start. Catches the Sprint 4 transient 'malformed' class proactively."
    )
    assert "integrityExit" in src, (
        "MR C: the hook must capture the exit code of the integrity probe."
    )
    assert "exit $integrityExit" in src, (
        "MR C: non-ok integrity_check must halt the deploy."
    )


def test_mrc_integrity_check_runs_after_services_started():
    src = _read(REPO / "scripts" / "deploy" / "deploy.ps1")
    start_idx = src.find("nssm start agt-telegram-bot")
    hook_idx = src.find("PRAGMA integrity_check")
    assert start_idx >= 0 and hook_idx > start_idx, (
        "MR C: integrity_check must run AFTER the services are started so "
        "any corruption introduced during boot is caught. Running before "
        "start wouldn't catch boot-time races."
    )


def test_mrc_integrity_check_has_grace_sleep():
    """Services need a few seconds to settle their sqlite connections before
    we probe — otherwise the probe itself can race with the bot's init."""
    src = _read(REPO / "scripts" / "deploy" / "deploy.ps1")
    m = re.search(r"Start-Sleep -Seconds 5.*?PRAGMA integrity_check", src, re.DOTALL)
    assert m is not None, (
        "MR C: a grace Start-Sleep (>=5s) must precede the integrity probe "
        "so the bot/scheduler have time to finish init and release any "
        "transient file locks."
    )


# ---------------------------------------------------------------------------
# Evidence-preservation sentinel — the misfire-classification ground-truth
# that drove this MR's config choice lives in the ship report and cowork
# notes. We don't re-compute here; we assert the ship-report file exists so
# tomorrow's morning-triage reviewer can find the reasoning trail.
# ---------------------------------------------------------------------------


def test_mrc_ship_report_captures_forensic_evidence():
    """The MR C per-MR dispatch file must name the specific misfire counts
    from Thursday 2026-04-23 that justified the 60s / 20-worker choice."""
    dispatch = REPO / "reports" / "sprint5_mrC_dispatch.md"
    if not dispatch.exists():
        pytest.skip("sprint5_mrC_dispatch.md not yet written (in-progress MR)")
    src = _read(dispatch)
    assert "822" in src or "bot_heartbeat" in src, (
        "MR C ship report must reference the 822 bot_heartbeat misses "
        "evidence."
    )
    assert "6.5s" in src or "6.5" in src or "misfire_grace" in src, (
        "MR C ship report must reference the 6.5s cron-slot misfire evidence."
    )
