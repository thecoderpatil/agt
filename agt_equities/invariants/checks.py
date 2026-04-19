"""ADR-007 safety invariant check functions.

Every check:
    - Signature: (conn: sqlite3.Connection, ctx: CheckContext) -> list[Violation]
    - Is pure: read-only against conn and ctx
    - Must not raise on empty tables or missing optional columns
    - May raise on catastrophic errors; the runner catches and records a
      single degraded Violation with evidence={"degraded": True, ...}

Registry: CHECK_REGISTRY at bottom maps invariant_id -> function.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from .types import CheckContext, Violation


# ---- helpers -------------------------------------------------------------------
def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---- 1. NO_LIVE_IN_PAPER -------------------------------------------------------
# MR !84: exclude terminal-status rows. A pending_orders row whose status is
# one of (superseded, rejected, cancelled, failed, filled) is a historical
# artifact, not a live safety hazard -- IBKR rejected/cancelled/superseded
# paths already resolved without a live account hitting the tape, and a
# filled paper row doesn't name a live account in the filled state. The
# invariant exists to catch *stageable* rows still in the execution pipeline.
# Keep 'partially_filled' in scope since partial fills can still route more.
_NO_LIVE_IN_PAPER_TERMINAL_STATUSES = (
    "superseded", "rejected", "cancelled", "failed", "filled",
)

def check_no_live_in_paper(conn: sqlite3.Connection, ctx: CheckContext) -> list[Violation]:
    """When PAPER_MODE is on, no pending_orders row may name a live account."""
    if not ctx.paper_mode:
        return []
    placeholders = ",".join("?" * len(_NO_LIVE_IN_PAPER_TERMINAL_STATUSES))
    sql = (
        f"SELECT id, payload, created_at, status FROM pending_orders "
        f"WHERE status NOT IN ({placeholders})"
    )
    rows = conn.execute(sql, _NO_LIVE_IN_PAPER_TERMINAL_STATUSES).fetchall()
    vios: list[Violation] = []
    for row in rows:
        p = _parse_payload(row["payload"])
        acct = p.get("account_id") or p.get("account")
        if acct in ctx.live_accounts:
            vios.append(Violation(
                invariant_id="NO_LIVE_IN_PAPER",
                description=(
                    f"Live account {acct} found in pending_orders row "
                    f"#{row['id']} under PAPER_MODE (status={row['status']})"
                ),
                severity="critical",
                evidence={
                    "pending_order_id": row["id"],
                    "account_id": acct,
                    "ticker": p.get("ticker"),
                    "status": row["status"],
                    "created_at": row["created_at"],
                },
            ))
    return vios


# ---- 2. NO_UNAPPROVED_LIVE_CSP -------------------------------------------------
def check_no_unapproved_live_csp(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Live CSP orders must carry payload.approval_ref from the Telegram gate."""
    rows = conn.execute(
        "SELECT id, status, payload FROM pending_orders "
        "WHERE status IN ('sent','partially_filled','filled','processing','staged')"
    ).fetchall()
    vios: list[Violation] = []
    for row in rows:
        p = _parse_payload(row["payload"])
        acct = p.get("account_id") or p.get("account")
        mode = p.get("mode") or ""
        if acct in ctx.live_accounts and mode == "CSP_ENTRY":
            if not p.get("approval_ref"):
                vios.append(Violation(
                    invariant_id="NO_UNAPPROVED_LIVE_CSP",
                    description=(
                        f"Live CSP order #{row['id']} on {acct} missing approval_ref"
                    ),
                    severity="critical",
                    evidence={
                        "pending_order_id": row["id"],
                        "account_id": acct,
                        "ticker": p.get("ticker"),
                        "status": row["status"],
                    },
                ))
    return vios


# ---- 3. NO_BELOW_BASIS_CC ------------------------------------------------------
def check_no_below_basis_cc(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Covered calls must be struck at or above per-account paper basis.

    Uses trade_repo.get_active_cycles + Cycle.paper_basis_for_account when
    available. If walker/trade_repo are unreachable, returns a single degraded
    Violation so Step-4 alerting can distinguish "clean" from "skipped".
    """
    try:
        from agt_equities import trade_repo  # type: ignore
    except Exception as exc:
        return [Violation(
            invariant_id="NO_BELOW_BASIS_CC",
            description="trade_repo import failed; check degraded",
            severity="low",
            evidence={"degraded": True, "error": str(exc)},
        )]
    rows = conn.execute(
        "SELECT id, status, payload FROM pending_orders "
        "WHERE status NOT IN ('cancelled','superseded','rejected','failed')"
    ).fetchall()
    # Group open CC orders by (household, ticker) for efficient cycle lookup
    candidates: list[tuple[int, str, str, str, float]] = []
    for row in rows:
        p = _parse_payload(row["payload"])
        if p.get("action") != "SELL" or p.get("right") != "C":
            continue
        acct = p.get("account_id")
        ticker = p.get("ticker")
        household = p.get("household")
        strike = p.get("strike")
        if not (acct and ticker and household and strike is not None):
            continue
        try:
            candidates.append((row["id"], household, ticker, acct, float(strike)))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return []
    vios: list[Violation] = []
    cycle_cache: dict[tuple[str, str], Any] = {}
    for oid, household, ticker, acct, strike in candidates:
        key = (household, ticker)
        if key not in cycle_cache:
            try:
                cycles = trade_repo.get_active_cycles(
                    household=household, ticker=ticker, db_path=ctx.db_path
                )
                cycle_cache[key] = cycles[0] if cycles else None
            except Exception:
                cycle_cache[key] = None
        cycle = cycle_cache[key]
        if cycle is None:
            continue
        try:
            basis = cycle.paper_basis_for_account(acct)
        except Exception:
            basis = None
        if basis is None:
            continue
        if float(strike) < float(basis):
            vios.append(Violation(
                invariant_id="NO_BELOW_BASIS_CC",
                description=(
                    f"CC on {ticker} struck at {strike} below per-account "
                    f"basis {basis} ({acct})"
                ),
                severity="high",
                evidence={
                    "pending_order_id": oid,
                    "account_id": acct,
                    "ticker": ticker,
                    "strike": float(strike),
                    "basis": float(basis),
                },
            ))
    return vios


# ---- 4. NO_ORPHAN_CHILDREN -----------------------------------------------------
def check_no_orphan_children(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Children whose parent filled/superseded but whose own status never settled."""
    if not _table_exists(conn, "pending_order_children"):
        return []
    rows = conn.execute(
        """
        SELECT c.id AS child_id, c.parent_order_id,
               c.status AS child_status, p.status AS parent_status
        FROM pending_order_children c
        JOIN pending_orders p ON c.parent_order_id = p.id
        WHERE p.status IN ('filled','superseded')
          AND c.status NOT IN ('filled','cancelled','superseded','rejected')
        """
    ).fetchall()
    return [
        Violation(
            invariant_id="NO_ORPHAN_CHILDREN",
            description=(
                f"Child order #{r['child_id']} still '{r['child_status']}' while "
                f"parent #{r['parent_order_id']} is '{r['parent_status']}'"
            ),
            severity="high",
            evidence={
                "child_id": r["child_id"],
                "parent_order_id": r["parent_order_id"],
                "child_status": r["child_status"],
                "parent_status": r["parent_status"],
            },
        )
        for r in rows
    ]


# ---- 5. NO_STRANDED_STAGED_ORDERS ---------------------------------------------
def check_no_stranded_staged_orders(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Orders stuck in status='staged' longer than the stranded TTL.

    MR !85: incident_key stabilized to ``NO_STRANDED_STAGED_ORDERS:<id>`` so
    repeated ticks on the same stranded order bump ``consecutive_breaches``
    on one canonical row instead of INSERTing a fresh row every 60s when
    the time-varying ``age_hours`` field bumps the evidence fingerprint.
    Latent bug; no rows tripping today but prophylactic.
    """
    rows = conn.execute(
        "SELECT id, payload, created_at FROM pending_orders WHERE status='staged'"
    ).fetchall()
    vios: list[Violation] = []
    for row in rows:
        created = _parse_dt(row["created_at"])
        if created is None:
            continue
        age_s = (ctx.now_utc - created).total_seconds()
        if age_s > ctx.stranded_staged_ttl_s:
            p = _parse_payload(row["payload"])
            vios.append(Violation(
                invariant_id="NO_STRANDED_STAGED_ORDERS",
                description=(
                    f"Order #{row['id']} has been 'staged' for "
                    f"{age_s/3600:.1f}h with no placement"
                ),
                severity="high",
                evidence={
                    "pending_order_id": row["id"],
                    "age_hours": age_s / 3600,
                    "ticker": p.get("ticker"),
                    "mode": p.get("mode"),
                    "account_id": p.get("account_id"),
                },
                stable_key=f"NO_STRANDED_STAGED_ORDERS:{row['id']}",
            ))
    return vios


# ---- 7. NO_ZOMBIE_BOT_PROCESS --------------------------------------------------
def _collapse_venv_launcher_pairs(
    candidates: list[tuple[int, int | None, str]],
) -> list[int]:
    """Drop Windows venv-launcher (parent, child) duplicates from a candidate list.

    On Windows, running ``python.exe telegram_bot.py`` under a ``.venv`` spawns
    an inner interpreter with an identical command line (the launcher wraps the
    real process). Both show up in ``psutil.process_iter`` with a parent/child
    ppid relationship but the SAME cmdline. That is a single logical bot
    instance, not a singleton-lock violation.

    Algorithm:
        Build a ``{pid: (ppid, cmdline)}`` map of candidates. A candidate is a
        launcher child iff its ``ppid`` is also a candidate AND the two
        cmdlines are byte-equal. Drop the child, keep the parent.

    Evidence trail: MR !84 restart showed PID 5756 (ppid 11292) and PID 4508
    (ppid 5756) both running the same ``.venv\\Scripts\\python.exe
    telegram_bot.py`` command; the child was spawned ~70ms after the parent.
    That pair triggered NO_ZOMBIE_BOT_PROCESS falsely (incident 412).

    Args:
        candidates: list of ``(pid, ppid, cmdline)`` tuples. ``ppid`` may be
            ``None`` when psutil could not resolve a parent.

    Returns:
        List of pids for distinct logical bot instances (parents kept, launcher
        children dropped). Order preserved from input for stability.
    """
    pid_info: dict[int, tuple[int | None, str]] = {
        pid: (ppid, cmd) for pid, ppid, cmd in candidates
    }
    keep: list[int] = []
    for pid, ppid, cmd in candidates:
        if (
            ppid is not None
            and ppid in pid_info
            and pid_info[ppid][1] == cmd
        ):
            # launcher child of a same-cmdline parent in the candidate set; drop
            continue
        keep.append(pid)
    return keep


def check_no_zombie_bot_process(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Detect >1 running telegram_bot.py process.

    Uses psutil when available; falls back to tasklist/ps. Returns a single
    degraded Violation when process enumeration is unavailable (typical CI).

    MR !85: stable_key added so repeat ticks on the same host condition
    (zombie OR degraded) bump consecutive_breaches on one canonical row.
    """
    pids: list[int] = []
    try:
        import psutil  # type: ignore
        candidates: list[tuple[int, int | None, str]] = []
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
                name = (proc.info.get("name") or "").lower()
                if "python" in name and "telegram_bot.py" in cmd:
                    candidates.append(
                        (proc.info["pid"], proc.info.get("ppid"), cmd)
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # MR !86: collapse Windows venv-launcher (parent, child) duplicates;
        # they share the same cmdline and represent one logical bot instance.
        pids = _collapse_venv_launcher_pairs(candidates)
    except ImportError:
        cmd_line = (
            ["tasklist", "/FO", "CSV", "/V"]
            if sys.platform.startswith("win")
            else ["ps", "-eo", "pid,args"]
        )
        try:
            out = subprocess.run(
                cmd_line, capture_output=True, text=True, timeout=5
            ).stdout
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return [Violation(
                invariant_id="NO_ZOMBIE_BOT_PROCESS",
                description="process enumeration unavailable; check degraded",
                severity="low",
                evidence={"degraded": True, "reason": "no_psutil_no_ps"},
                stable_key="NO_ZOMBIE_BOT_PROCESS:degraded",
            )]
        for line in out.splitlines():
            if "telegram_bot.py" in line:
                pids.append(len(pids))  # count-only fallback
    if len(pids) > 1:
        return [Violation(
            invariant_id="NO_ZOMBIE_BOT_PROCESS",
            description=(
                f"{len(pids)} telegram_bot.py processes running; "
                "singleton lock is broken"
            ),
            severity="high",
            evidence={"pid_count": len(pids), "pids": pids[:10]},
            stable_key="NO_ZOMBIE_BOT_PROCESS",
        )]
    return []


# ---- 8. NO_STALE_RED_ALERT -----------------------------------------------------
def check_no_stale_red_alert(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """red_alert_state=ON for more than 2x the stale TTL without recompute.

    MR !84: incident_key stabilized to ``NO_STALE_RED_ALERT:<household>`` via
    ``Violation.stable_key`` so repeated ticks on the same stale alert bump
    ``consecutive_breaches`` on a single canonical row instead of INSERTing
    a fresh row every 60s. Pre-fix: ~2 rows/min unbounded growth driven by
    time-varying ``age_hours`` in the evidence fingerprint. The age info is
    still carried in the description string for operator readability.
    """
    if not _table_exists(conn, "red_alert_state"):
        return []
    rows = conn.execute(
        "SELECT household, current_state, activated_at, last_updated "
        "FROM red_alert_state WHERE current_state = 'ON'"
    ).fetchall()
    vios: list[Violation] = []
    threshold_s = ctx.red_alert_stale_ttl_s * 2  # default = 48h
    for row in rows:
        activated = _parse_dt(row["activated_at"])
        if activated is None:
            continue
        age_s = (ctx.now_utc - activated).total_seconds()
        if age_s > threshold_s:
            household = row["household"]
            vios.append(Violation(
                invariant_id="NO_STALE_RED_ALERT",
                description=(
                    f"red_alert_state ON for {household} since "
                    f"{row['activated_at']} ({age_s/3600:.0f}h, never recomputed)"
                ),
                severity="medium",
                evidence={
                    "household": household,
                    "activated_at": row["activated_at"],
                    "age_hours": age_s / 3600,
                    "last_updated": row["last_updated"],
                },
                stable_key=f"NO_STALE_RED_ALERT:{household}",
            ))
    return vios


# ---- 9. NO_STUCK_PROCESSING_ORDER ---------------------------------------------
def check_no_stuck_processing_order(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Orders in status='processing' past the stuck TTL (default 2h).

    MR !85: incident_key stabilized to ``NO_STUCK_PROCESSING_ORDER:<id>``
    so repeated ticks on the same stuck order bump ``consecutive_breaches``
    on one canonical row. age_hours churn would otherwise INSERT a new
    row every 60s. Latent bug; prophylactic fix.
    """
    rows = conn.execute(
        "SELECT id, payload, created_at, ib_order_id FROM pending_orders "
        "WHERE status = 'processing'"
    ).fetchall()
    vios: list[Violation] = []
    for row in rows:
        created = _parse_dt(row["created_at"])
        if created is None:
            continue
        age_s = (ctx.now_utc - created).total_seconds()
        if age_s > ctx.stuck_processing_ttl_s:
            p = _parse_payload(row["payload"])
            vios.append(Violation(
                invariant_id="NO_STUCK_PROCESSING_ORDER",
                description=(
                    f"Order #{row['id']} stuck in 'processing' for "
                    f"{age_s/3600:.1f}h"
                ),
                severity="high",
                evidence={
                    "pending_order_id": row["id"],
                    "age_hours": age_s / 3600,
                    "ticker": p.get("ticker"),
                    "account_id": p.get("account_id"),
                    "ib_order_id": row["ib_order_id"],
                },
                stable_key=f"NO_STUCK_PROCESSING_ORDER:{row['id']}",
            ))
    return vios


# ---- 10. NO_MISSING_DAEMON_HEARTBEAT ------------------------------------------
def check_no_missing_daemon_heartbeat(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Every daemon in ctx.expected_daemons must be heartbeating within TTL."""
    if not _table_exists(conn, "daemon_heartbeat"):
        return [Violation(
            invariant_id="NO_MISSING_DAEMON_HEARTBEAT",
            description="daemon_heartbeat table does not exist",
            severity="high",
            evidence={"reason": "no_table"},
        )]
    rows = conn.execute(
        "SELECT daemon_name, last_beat_utc FROM daemon_heartbeat"
    ).fetchall()
    seen: dict[str, datetime | None] = {
        r["daemon_name"]: _parse_dt(r["last_beat_utc"]) for r in rows
    }
    vios: list[Violation] = []
    for daemon in ctx.expected_daemons:
        beat = seen.get(daemon)
        if beat is None:
            vios.append(Violation(
                invariant_id="NO_MISSING_DAEMON_HEARTBEAT",
                description=f"Expected daemon '{daemon}' has no heartbeat row",
                severity="high",
                evidence={"daemon_name": daemon, "reason": "missing"},
                stable_key=f"NO_MISSING_DAEMON_HEARTBEAT:{daemon}",
            ))
            continue
        age_s = (ctx.now_utc - beat).total_seconds()
        if age_s > ctx.daemon_heartbeat_ttl_s:
            vios.append(Violation(
                invariant_id="NO_MISSING_DAEMON_HEARTBEAT",
                description=(
                    f"Daemon '{daemon}' heartbeat stale by {age_s:.0f}s"
                ),
                severity="high",
                evidence={
                    "daemon_name": daemon,
                    "stale_seconds": age_s,
                    "last_beat_utc": beat.isoformat(),
                },
                stable_key=f"NO_MISSING_DAEMON_HEARTBEAT:{daemon}",
            ))
    return vios


# ---- 11. NO_LOCAL_DRIFT --------------------------------------------------------
def check_no_local_drift(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Working tree must be clean modulo the TRIPWIRE_EXEMPT_REGISTRY.

    Any tracked file that appears in ``git status --porcelain`` output and
    is not on the exempt list is a drift incident. Severity is medium
    (hygiene, not a safety rail) and consecutive_violations=3 in the YAML
    to tolerate the brief mid-edit window.

    Repo path resolves from ``AGT_REPO_PATH`` env var, default
    ``C:\\AGT_Telegram_Bridge`` (Windows production box). On Linux CI the
    env is unset and the ``.git`` probe fails cleanly -> returns [].

    MR !85: stable_key="NO_LOCAL_DRIFT" (singleton). Pre-fix, the
    drift_sample list mutated on every mtime bump -> evidence fingerprint
    churned -> new incident row every 60s. Post-fix one canonical row
    UPDATEs consecutive_breaches; drift_sample still observable for
    operator triage but no longer busts dedup. Degraded paths share
    a sibling key ``NO_LOCAL_DRIFT:degraded`` so the real-drift row and
    the degraded row don't alias.
    """
    import os
    repo_path = os.environ.get("AGT_REPO_PATH", r"C:\AGT_Telegram_Bridge")
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return []  # no git repo here; degraded-not-violating
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return [Violation(
            invariant_id="NO_LOCAL_DRIFT",
            description=f"git status failed; check degraded: {exc}",
            severity="low",
            evidence={"degraded": True, "error": str(exc)},
            stable_key="NO_LOCAL_DRIFT:degraded",
        )]
    if result.returncode != 0:
        return [Violation(
            invariant_id="NO_LOCAL_DRIFT",
            description=f"git status returned {result.returncode}",
            severity="low",
            evidence={
                "degraded": True,
                "stderr": (result.stderr or "")[:400],
            },
            stable_key="NO_LOCAL_DRIFT:degraded",
        )]
    # TRIPWIRE_EXEMPT_REGISTRY active drift allowlist (4 files per v23 handoff).
    exempt_paths = frozenset({
        "boot_desk.bat",
        "cure_lifecycle.html",
        "cure_smart_friction.html",
        "tests/test_command_prune.py",
    })
    ignored_prefixes = ("reports/", "tmp/", ".venv/", "__pycache__/")
    drift_lines: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path in exempt_paths:
            continue
        if any(path.startswith(p) for p in ignored_prefixes):
            continue
        drift_lines.append({"status": status, "path": path})
    if not drift_lines:
        return []
    return [Violation(
        invariant_id="NO_LOCAL_DRIFT",
        description=(
            f"Working tree has {len(drift_lines)} drifted file(s) "
            "beyond the exempt registry"
        ),
        severity="medium",
        evidence={
            "drift_count": len(drift_lines),
            "drift_sample": drift_lines[:10],
            "repo_path": repo_path,
        },
        stable_key="NO_LOCAL_DRIFT",
    )]





# ---- 12. NO_SHADOW_ON_PROD_DB ---------------------------------------------------
def check_no_shadow_on_prod_db(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """No ``scripts/shadow_scan.py`` process may run against ``PROD_DB_PATH``.

    Primary enforcement is the runtime assert at entry of
    ``scripts/shadow_scan.py`` (``NO_SHADOW_ON_PROD_DB`` +
    ``NO_LIVE_CTX_IN_SHADOW_SCRIPT`` per ADR-008 section 6). This periodic
    check is a belt-and-suspenders scan of ``psutil.process_iter`` for any
    Python process whose cmdline references ``scripts/shadow_scan.py`` AND
    the production DB path.

    Returns a degraded Violation when psutil is unavailable (typical CI
    container). Returns the empty list when no offending shadow process is
    running, which is the steady state well over 99% of the time -
    shadow runs are ephemeral CLI invocations.
    """
    try:
        from agt_equities.runtime import PROD_DB_PATH  # local import: avoids cycle
    except ImportError:
        return [Violation(
            invariant_id="NO_SHADOW_ON_PROD_DB",
            description="agt_equities.runtime import failed; check degraded",
            severity="low",
            evidence={"degraded": True, "reason": "no_runtime_module"},
            stable_key="NO_SHADOW_ON_PROD_DB:degraded",
        )]
    try:
        import psutil  # type: ignore
    except ImportError:
        return [Violation(
            invariant_id="NO_SHADOW_ON_PROD_DB",
            description="psutil unavailable; shadow-scan process scan degraded",
            severity="low",
            evidence={"degraded": True, "reason": "no_psutil"},
            stable_key="NO_SHADOW_ON_PROD_DB:degraded",
        )]
    offenders: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd_parts = proc.info.get("cmdline") or []
            cmd = " ".join(str(p) for p in cmd_parts)
            if "shadow_scan.py" not in cmd:
                continue
            if PROD_DB_PATH in cmd:
                offenders.append({
                    "pid": proc.info["pid"],
                    "cmdline": cmd[:400],
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not offenders:
        return []
    return [Violation(
        invariant_id="NO_SHADOW_ON_PROD_DB",
        description=(
            f"{len(offenders)} shadow_scan.py process(es) running against "
            "PROD_DB_PATH; runtime guard was bypassed"
        ),
        severity="high",
        evidence={
            "offender_count": len(offenders),
            "offenders": offenders[:10],
        },
        stable_key="NO_SHADOW_ON_PROD_DB",
    )]


# ---- Registry ------------------------------------------------------------------

def check_self_healing_write_path_canonical(
    conn: sqlite3.Connection, ctx: CheckContext,
) -> list[Violation]:
    """ADR-007 Addendum §2.2 — audit the incidents write path.

    Two conditions must both hold for a clean return:
      (a) Path(agt_equities.db.DB_PATH).resolve() == Path(PROD_DB_PATH).resolve()
      (b) A daemon_heartbeat row exists within _STALE_HEARTBEAT_S seconds
          when read through a FRESH connection opened via db.get_ro_connection()
          (NOT via `conn`, which was opened by the runner against the
          canonical read path).
    """
    from contextlib import closing
    from pathlib import Path

    from agt_equities import db as agt_db
    from agt_equities.runtime import PROD_DB_PATH

    _STALE_HEARTBEAT_S = 180

    write_path = Path(agt_db.DB_PATH).resolve()
    canonical = Path(PROD_DB_PATH).resolve()
    if write_path != canonical:
        return [Violation(
            invariant_id="SELF_HEALING_WRITE_PATH_CANONICAL",
            description=(
                f"Incident write path {write_path} != canonical {canonical}"
            ),
            severity="crit",
            evidence={
                "write_path": str(write_path),
                "canonical": str(canonical),
            },
        )]

    try:
        with closing(agt_db.get_ro_connection()) as wconn:
            row = wconn.execute(
                "SELECT MAX(last_heartbeat_at) AS ts FROM daemon_heartbeat"
            ).fetchone()
    except Exception as exc:
        return [Violation(
            invariant_id="SELF_HEALING_WRITE_PATH_CANONICAL",
            description=f"Write-path heartbeat query failed: {exc}",
            severity="crit",
            evidence={"write_path": str(write_path), "error": str(exc)},
        )]

    last_ts = _parse_dt(row["ts"] if row else None)
    if last_ts is None:
        return [Violation(
            invariant_id="SELF_HEALING_WRITE_PATH_CANONICAL",
            description="Write-path daemon_heartbeat table is empty or unparseable",
            severity="crit",
            evidence={"write_path": str(write_path), "last_ts": None},
        )]

    age_s = (ctx.now_utc - last_ts).total_seconds()
    if age_s > _STALE_HEARTBEAT_S:
        return [Violation(
            invariant_id="SELF_HEALING_WRITE_PATH_CANONICAL",
            description=(
                f"Write-path daemon_heartbeat stale: {age_s:.0f}s > "
                f"{_STALE_HEARTBEAT_S}s threshold"
            ),
            severity="crit",
            evidence={
                "write_path": str(write_path),
                "age_seconds": round(age_s, 1),
                "last_heartbeat_at": row["ts"],
            },
        )]

    return []

CHECK_REGISTRY: dict[str, Any] = {
    "NO_LIVE_IN_PAPER": check_no_live_in_paper,
    "NO_UNAPPROVED_LIVE_CSP": check_no_unapproved_live_csp,
    "NO_BELOW_BASIS_CC": check_no_below_basis_cc,
    "NO_ORPHAN_CHILDREN": check_no_orphan_children,
    "NO_STRANDED_STAGED_ORDERS": check_no_stranded_staged_orders,
    "NO_ZOMBIE_BOT_PROCESS": check_no_zombie_bot_process,
    "NO_STALE_RED_ALERT": check_no_stale_red_alert,
    "NO_STUCK_PROCESSING_ORDER": check_no_stuck_processing_order,
    "NO_MISSING_DAEMON_HEARTBEAT": check_no_missing_daemon_heartbeat,
    "NO_LOCAL_DRIFT": check_no_local_drift,
    "NO_SHADOW_ON_PROD_DB": check_no_shadow_on_prod_db,
    "SELF_HEALING_WRITE_PATH_CANONICAL": check_self_healing_write_path_canonical,
}
