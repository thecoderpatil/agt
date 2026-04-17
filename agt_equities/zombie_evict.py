"""MR !90: zombie bot/scheduler eviction on boot.

Problem
-------
When NSSM's tracked outer PID dies while the venv-launcher inner child
survives, the zombie holds the IBKR client slot (clientId=1 for bot,
clientId=2 for scheduler). NSSM restarts the outer; the new daemon
tries to connect to IBKR; IBKR rejects on clientId collision; the new
daemon dies (WIN32_EXIT_CODE 1067); NSSM gives up. This is the
NO_ZOMBIE_BOT_PROCESS scenario surfaced during the MR1.5 crash-restart
test. The invariant flagged it; this module evicts it.

Called from bot + scheduler boot paths BEFORE the singleton lock /
IBKR-connect steps. Pure function surface; no module-level side effects.

Windows note
------------
``psutil.Process.terminate()`` maps to ``TerminateProcess`` on Windows,
which has no graceful-shutdown contract -- the process is killed
immediately. The ``sigterm_grace_s`` window functions as a
wait-and-confirm guard rather than a Unix-style SIGTERM drain. On Unix
the same call sends SIGTERM and the grace window lets the target's
handlers drain. Either way: kill if still running after the window.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Hard cap on total eviction latency. NSSM's AppRestartDelay is 30s; our
# ceiling must stay well below that so eviction + singleton-acquire +
# IBKR-connect completes within NSSM's supervision window. 15s = 5s grace
# + 10s worst-case kill/verify across several zombies.
_MAX_TOTAL_S: float = 15.0

# How long to poll after .terminate() before escalating to .kill().
_POLL_INTERVAL_S: float = 0.25


@dataclass
class EvictionResult:
    """Outcome of one evict_zombie_daemons() call.

    All pid lists are disjoint:
      zombies_found = zombies_evicted (clean terminate) +
                      zombies_survived_sigkill (escalated + verified dead) +
                      any that died between enumeration and terminate (tracked
                      under zombies_evicted for accounting).
    """
    zombies_found: list[int] = field(default_factory=list)
    zombies_evicted: list[int] = field(default_factory=list)
    zombies_survived_sigkill: list[int] = field(default_factory=list)
    evictions_skipped_self_ancestry: list[int] = field(default_factory=list)


def _resolve_self_pair(proc_iter, self_pid: int) -> set[int]:
    """Compute the set of pids representing 'me' in the launcher pair.

    Returns a set containing self_pid, plus the direct parent if its
    cmdline matches self's cmdline (venv launcher), plus any immediate
    child with matching cmdline. Covers both directions of the MR !86
    launcher pair pattern.

    ``proc_iter`` is a pre-collected list of process info dicts with
    keys {pid, ppid, name, cmdline}. Accepts list/tuple, not a generator.
    """
    by_pid = {int(p['pid']): p for p in proc_iter}
    self_info = by_pid.get(self_pid)
    if self_info is None:
        # Strange -- self not in the process snapshot. Only skip self.
        return {self_pid}
    self_cmd = tuple(self_info.get('cmdline') or ())
    self_ppid = self_info.get('ppid')

    pair: set[int] = {self_pid}
    # Upward: immediate parent if its cmdline matches self's
    if self_ppid is not None and self_ppid in by_pid:
        parent_cmd = tuple(by_pid[self_ppid].get('cmdline') or ())
        if parent_cmd == self_cmd:
            pair.add(int(self_ppid))
    # Downward: any child whose ppid is self and cmdline matches
    for pid, info in by_pid.items():
        if info.get('ppid') == self_pid:
            child_cmd = tuple(info.get('cmdline') or ())
            if child_cmd == self_cmd:
                pair.add(int(pid))
    return pair


def _write_incident(
    *,
    cmdline_marker: str,
    evicted: list[int],
    survivors: list[int],
    skipped_self: list[int],
    db_path: str | Path | None,
    logger: logging.Logger,
) -> None:
    """Best-effort incident row. Never raises."""
    try:
        from agt_equities import incidents_repo
    except Exception as exc:
        logger.warning("zombie_evict: incidents_repo import failed (%s)", exc)
        return
    try:
        incidents_repo.register(
            incident_key=f"ZOMBIE_DAEMON_EVICTED:{cmdline_marker}",
            severity="warn",
            scrutiny_tier="low",
            detector="zombie_evict.boot",
            invariant_id="ZOMBIE_DAEMON_EVICTED",
            observed_state={
                "daemon": cmdline_marker,
                "evicted_pids": sorted(evicted),
                "sigkill_survivors": sorted(survivors),
                "skipped_self_ancestry_pids": sorted(skipped_self),
            },
            db_path=db_path,
        )
    except Exception as exc:
        logger.warning("zombie_evict: incident write failed (%s)", exc)


def evict_zombie_daemons(
    *,
    cmdline_marker: str,
    self_pid: int,
    sigterm_grace_s: float = 5.0,
    db_path: str | Path | None = None,
    logger: logging.Logger | None = None,
) -> EvictionResult:
    """Evict any other python process running ``cmdline_marker``.

    Never touches ``self_pid`` or its venv-launcher pair partner(s).
    On Windows, ``.terminate()`` is non-graceful (TerminateProcess); the
    grace window is a wait-and-confirm. On Unix, the grace lets signal
    handlers drain before SIGKILL.

    If no zombies, returns an empty EvictionResult without any side
    effect (no incident row, no process interaction).

    Any exception during enumeration or individual eviction is logged
    and does not propagate -- the boot path must not die inside the
    evictor. The caller checks ``result.zombies_survived_sigkill`` to
    decide whether to refuse to boot.
    """
    log = logger or logging.getLogger(__name__)
    result = EvictionResult()
    t_start = time.monotonic()

    try:
        import psutil  # lazy for monkeypatch-based tests
    except Exception as exc:
        log.warning("zombie_evict: psutil import failed (%s); skipping eviction", exc)
        return result

    # Enumerate candidates matching the marker. Skip AccessDenied rows.
    candidates: list[dict] = []
    try:
        for p in psutil.process_iter(['pid', 'ppid', 'name', 'cmdline']):
            try:
                info = p.info
                name = (info.get('name') or '').lower()
                if 'python' not in name:
                    continue
                cmdline = info.get('cmdline') or []
                cmdstr = ' '.join(cmdline) if isinstance(cmdline, (list, tuple)) else str(cmdline)
                if cmdline_marker not in cmdstr:
                    continue
                candidates.append({
                    'pid': int(info['pid']),
                    'ppid': info.get('ppid'),
                    'name': info.get('name'),
                    'cmdline': list(cmdline),
                    '_proc': p,  # keep for terminate/kill
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                log.info("zombie_evict: skipping process during enumeration (%s)", exc)
                continue
    except Exception as exc:
        log.warning("zombie_evict: process_iter failed (%s); skipping eviction", exc)
        return result

    # Identify the self-pair (self + launcher partner if cmdline matches).
    self_pair = _resolve_self_pair(candidates, self_pid)
    result.evictions_skipped_self_ancestry = sorted(p for p in self_pair if p != self_pid)

    # Target list: candidates minus self-pair.
    targets = [c for c in candidates if c['pid'] not in self_pair]
    result.zombies_found = sorted(c['pid'] for c in targets)

    if not targets:
        log.info(
            "zombie_evict: no zombies found for marker=%r (self=%d, pair=%s)",
            cmdline_marker, self_pid, sorted(self_pair),
        )
        return result

    log.warning(
        "zombie_evict: found %d zombie(s) matching %r: %s",
        len(targets), cmdline_marker, result.zombies_found,
    )

    for tgt in targets:
        pid = tgt['pid']
        proc = tgt['_proc']
        elapsed = time.monotonic() - t_start
        if elapsed >= _MAX_TOTAL_S:
            log.error(
                "zombie_evict: hard ceiling %ss reached after %.1fs; "
                "leaving PID %d un-evicted",
                _MAX_TOTAL_S, elapsed, pid,
            )
            result.zombies_survived_sigkill.append(pid)
            continue

        # Send terminate. On Windows this is TerminateProcess (non-graceful).
        try:
            proc.terminate()
            log.info("zombie_evict: sent terminate to PID %d", pid)
        except psutil.NoSuchProcess:
            log.info("zombie_evict: PID %d already gone before terminate", pid)
            result.zombies_evicted.append(pid)
            continue
        except Exception as exc:
            log.warning("zombie_evict: terminate PID %d failed (%s)", pid, exc)

        # Poll for exit up to sigterm_grace_s.
        deadline = time.monotonic() + sigterm_grace_s
        exited = False
        while time.monotonic() < deadline:
            try:
                if not proc.is_running():
                    exited = True
                    break
            except psutil.NoSuchProcess:
                exited = True
                break
            time.sleep(_POLL_INTERVAL_S)
        if exited:
            log.info("zombie_evict: PID %d exited after terminate", pid)
            result.zombies_evicted.append(pid)
            continue

        # Escalate to kill.
        try:
            proc.kill()
            log.warning("zombie_evict: PID %d survived terminate; sent kill", pid)
        except psutil.NoSuchProcess:
            log.info("zombie_evict: PID %d gone before kill", pid)
            result.zombies_evicted.append(pid)
            continue
        except Exception as exc:
            log.error("zombie_evict: kill PID %d failed (%s)", pid, exc)
            result.zombies_survived_sigkill.append(pid)
            continue

        # Verify after kill.
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        try:
            still = proc.is_running()
        except psutil.NoSuchProcess:
            still = False
        except Exception:
            still = True  # assume worst; caller decides
        if still:
            log.error("zombie_evict: PID %d SURVIVED kill", pid)
            result.zombies_survived_sigkill.append(pid)
        else:
            log.info("zombie_evict: PID %d confirmed dead after kill", pid)
            result.zombies_evicted.append(pid)

    # Emit one incident row (idempotent via stable incident_key per marker).
    if result.zombies_evicted or result.zombies_survived_sigkill:
        _write_incident(
            cmdline_marker=cmdline_marker,
            evicted=result.zombies_evicted,
            survivors=result.zombies_survived_sigkill,
            skipped_self=result.evictions_skipped_self_ancestry,
            db_path=db_path,
            logger=log,
        )

    return result
