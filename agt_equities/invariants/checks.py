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


# ---- 6. NO_SILENT_BREAKER_TRIP -------------------------------------------------
# MR !85: the old haiku-watchdog task was retired in MR !66 (see
# `project_autonomous_pipeline_launched` memory). Its replacement is the
# Windows schtask ``AGT_Bot_Liveness_Watchdog`` running
# ``scripts/bot_liveness_watchdog.ps1`` every ~5 minutes during RTH, which
# reads ``daemon_heartbeat`` for ``agt_bot`` and writes a BOT_STALE row
# into ``cross_daemon_alerts`` when the heartbeat goes stale.
#
# The invariant migrates from autonomous_session_log (dead source) to a
# DB-native join between daemon_heartbeat and cross_daemon_alerts:
#
#   Silent trip = daemon_heartbeat for 'agt_bot' is observably stale
#   (> 5 min) AND no BOT_STALE cross_daemon_alerts row exists inside the
#   watchdog cadence window (10 min = 2x schtask interval).
#
# If the heartbeat is stale but a recent BOT_STALE alert exists, the
# watchdog fired -- the bot drain is responsible for surfacing it to
# Telegram/Gmail. If the heartbeat is stale AND no alert, the watchdog
# itself isn't running (schtask disabled, PowerShell policy blocking,
# sqlite3.exe missing, etc.) and the user will not be notified of the
# bot outage. That's the "silent breaker trip" failure mode.
#
# NO_MISSING_DAEMON_HEARTBEAT already fires on stale heartbeat at the
# 120s TTL; this invariant is independent and surfaces a different
# failure mode (detection-pipeline health). They can co-fire.
_SILENT_TRIP_HEARTBEAT_STALE_S = 300      # 5 min
_SILENT_TRIP_WATCHDOG_WINDOW_S = 600      # 10 min (schtask 5 min * 2)
_SILENT_TRIP_MONITORED_DAEMON = "agt_bot"


def check_no_silent_breaker_trip(
    conn: sqlite3.Connection, ctx: CheckContext
) -> list[Violation]:
    """AGT_Bot_Liveness_Watchdog must emit BOT_STALE when daemon_heartbeat is stale."""
    if not _table_exists(conn, "daemon_heartbeat"):
        return []
    if not _table_exists(conn, "cross_daemon_alerts"):
        return []
    row = conn.execute(
        "SELECT last_beat_utc FROM daemon_heartbeat WHERE daemon_name=?",
        (_SILENT_TRIP_MONITORED_DAEMON,),
    ).fetchone()
    # No row at all = NO_MISSING_DAEMON_HEARTBEAT's concern, not ours.
    if row is None:
        return []
    last_beat = _parse_dt(row["last_beat_utc"])
    if last_beat is None:
        return []
    age_s = (ctx.now_utc - last_beat).total_seconds()
    if age_s <= _SILENT_TRIP_HEARTBEAT_STALE_S:
        return []
    # Heartbeat stale. Did the watchdog fire recently?
    now_epoch = ctx.now_utc.timestamp()
    cutoff_epoch = now_epoch - _SILENT_TRIP_WATCHDOG_WINDOW_S
    recent_alert = conn.execute(
        "SELECT id FROM cross_daemon_alerts "
        "WHERE kind = 'BOT_STALE' "
        "  AND CAST(created_ts AS REAL) >= ? "
        "ORDER BY id DESC LIMIT 1",
        (cutoff_epoch,),
    ).fetchone()
    if recent_alert is not None:
        return []  # watchdog fired within cadence; NOT silent
    return [Violation(
        invariant_id="NO_SILENT_BREAKER_TRIP",
        description=(
            f"daemon_heartbeat '{_SILENT_TRIP_MONITORED_DAEMON}' stale by "
            f"{age_s:.0f}s but no BOT_STALE cross_daemon_alert in the last "
            f"{_SILENT_TRIP_WATCHDOG_WINDOW_S}s watchdog window "
            "(AGT_Bot_Liveness_Watchdog likely not running)"
        ),
        severity="medium",
        evidence={
            "daemon_name": _SILENT_TRIP_MONITORED_DAEMON,
            "stale_seconds": age_s,
            "watchdog_window_seconds": _SILENT_TRIP_WATCHDOG_WINDOW_S,
            "last_beat_utc": last_beat.isoformat(),
        },
        stable_key=f"NO_SILENT_BREAKER_TRIP:{_SILENT_TRIP_MONITORED_DAEMON}",
    )]


# ---- 7. NO_ZOMBIE_BOT_PROCESS --------------------------------------------------
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
