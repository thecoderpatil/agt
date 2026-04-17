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
def check_no_live_in_paper(conn: sqlite3.Connection, ctx: CheckContext) -> list[Violation]:
    """When PAPER_MODE is on, no pending_orders row may name a live account."""
    if not ctx.paper_mode:
        return []
    rows = conn.execute("SELECT id, payload, created_at FROM pending_orders").fetchall()
    vios: list[Violation] = []
    for row in rows:
        p = _parse_payload(row["payload"])
        acct = p.get("account_id") or p.get("account")
        if acct in ctx.live_accounts:
            vios.append(Violation(
                invariant_id="NO_LIVE_IN_PAPER",
                description=(
                    f"Live account {acct} found in pending_orders row "
                    f"#{row['id']} under PAPER_MODE"
                ),
                severity="critical",
                evidence={
                    "pending_order_id": row["id"],
                    "account_id": acct,
                    "ticker": p.get("ticker"),
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
    """Orders stuck in status='staged' longer than the stranded TTL."""
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
            ))
    return vios


# ---- 6. NO_SILENT_BREAKER_TRIP -------------------------------------------------
def check_no_silent_breaker_trip(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """haiku-watchdog daily_orders flags must emit a cross_daemon_alerts row.

    Checks last 24h of watchdog runs; any run flagging 'daily_orders' without
    a correlated breaker/limit alert in the +/- 1h window is a silent trip.
    """
    if not _table_exists(conn, "autonomous_session_log"):
        return []
    if not _table_exists(conn, "cross_daemon_alerts"):
        return []
    # Pull candidate watchdog runs; do the 24h filter in Python so ISO
    # timestamps with '+00:00' suffix don't break SQLite datetime().
    watchdog_runs = conn.execute(
        """
        SELECT id, run_at, errors FROM autonomous_session_log
        WHERE task_name = 'haiku-watchdog'
          AND errors IS NOT NULL AND errors != '' AND errors != '[]'
        """
    ).fetchall()
    # Pull all breaker/limit/daily_orders alerts once; filter window in Python.
    alert_rows = conn.execute(
        """
        SELECT id, created_ts, kind FROM cross_daemon_alerts
        WHERE kind LIKE '%breaker%'
           OR kind LIKE '%limit%'
           OR kind LIKE '%daily_orders%'
        """
    ).fetchall()
    alert_dts: list[datetime] = []
    for a in alert_rows:
        a_dt = _parse_dt(a["created_ts"])
        if a_dt is not None:
            alert_dts.append(a_dt)
    lookback = ctx.now_utc - timedelta(hours=24)
    window = timedelta(hours=1)
    vios: list[Violation] = []
    for run in watchdog_runs:
        errors_raw = run["errors"] or ""
        if ("daily_orders" not in errors_raw
                and "Daily order limit" not in errors_raw):
            continue
        run_dt = _parse_dt(run["run_at"])
        if run_dt is None:
            continue
        if run_dt < lookback:
            continue
        has_correlated = any(
            abs((alert_dt - run_dt).total_seconds()) <= window.total_seconds()
            for alert_dt in alert_dts
        )
        if not has_correlated:
            vios.append(Violation(
                invariant_id="NO_SILENT_BREAKER_TRIP",
                description=(
                    f"haiku-watchdog flagged daily_orders at {run['run_at']} "
                    "with no cross_daemon_alert emitted"
                ),
                severity="medium",
                evidence={
                    "watchdog_run_id": run["id"],
                    "run_at": run["run_at"],
                    "errors_excerpt": errors_raw[:200],
                },
            ))
    return vios


# ---- 7. NO_ZOMBIE_BOT_PROCESS --------------------------------------------------
def check_no_zombie_bot_process(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Detect >1 running telegram_bot.py process.

    Uses psutil when available; falls back to tasklist/ps. Returns a single
    degraded Violation when process enumeration is unavailable (typical CI).
    """
    pids: list[int] = []
    try:
        import psutil  # type: ignore
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
                name = (proc.info.get("name") or "").lower()
                if "python" in name and "telegram_bot.py" in cmd:
                    pids.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
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
        )]
    return []


# ---- 8. NO_STALE_RED_ALERT -----------------------------------------------------
def check_no_stale_red_alert(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """red_alert_state=ON for more than 2x the stale TTL without recompute."""
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
            vios.append(Violation(
                invariant_id="NO_STALE_RED_ALERT",
                description=(
                    f"red_alert_state ON for {row['household']} since "
                    f"{row['activated_at']} ({age_s/3600:.0f}h, never recomputed)"
                ),
                severity="medium",
                evidence={
                    "household": row["household"],
                    "age_hours": age_s / 3600,
                    "activated_at": row["activated_at"],
                    "last_updated": row["last_updated"],
                },
            ))
    return vios


# ---- 9. NO_STUCK_PROCESSING_ORDER ---------------------------------------------
def check_no_stuck_processing_order(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """Orders in status='processing' past the stuck TTL (default 2h)."""
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
    )]



# ---- Registry ------------------------------------------------------------------
CHECK_REGISTRY: dict[str, Any] = {
    "NO_LIVE_IN_PAPER": check_no_live_in_paper,
    "NO_UNAPPROVED_LIVE_CSP": check_no_unapproved_live_csp,
    "NO_BELOW_BASIS_CC": check_no_below_basis_cc,
    "NO_ORPHAN_CHILDREN": check_no_orphan_children,
    "NO_STRANDED_STAGED_ORDERS": check_no_stranded_staged_orders,
    "NO_SILENT_BREAKER_TRIP": check_no_silent_breaker_trip,
    "NO_ZOMBIE_BOT_PROCESS": check_no_zombie_bot_process,
    "NO_STALE_RED_ALERT": check_no_stale_red_alert,
    "NO_STUCK_PROCESSING_ORDER": check_no_stuck_processing_order,
    "NO_MISSING_DAEMON_HEARTBEAT": check_no_missing_daemon_heartbeat,
    "NO_LOCAL_DRIFT": check_no_local_drift,
}
