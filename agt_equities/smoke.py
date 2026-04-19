"""F.6 nightly integration smoke checks.

Checks daemon heartbeat freshness (<2 hr for both services),
pending_orders, and decisions schema accessibility.
Returns a list of failure strings.  Empty = all checks passed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

_EXPECTED_DAEMONS = ("agt-telegram-bot", "agt-scheduler")
_HEARTBEAT_MAX_AGE_S = 7200  # 2 hours — services beat every 60s

def run_nightly_smoke_checks(db_path: str) -> list[str]:
    """Run health checks against a DB clone.

    Args:
        db_path: absolute path to a CLONED SQLite database.
            MUST NOT equal the production DB path.

    Returns:
        List of human-readable failure strings.  Empty = all OK.
    """
    failures: list[str] = []
    now_utc = datetime.now(timezone.utc)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        return [f"DB open failed: {exc}"]

    try:
        # ── Check 1: daemon heartbeat freshness ──────────────────────────────
        try:
            rows = conn.execute(
                "SELECT daemon_name, last_beat_utc FROM daemon_heartbeat"
            ).fetchall()
        except Exception as exc:
            failures.append(f"daemon_heartbeat query failed: {exc}")
            rows = []

        seen: dict[str, str] = {r["daemon_name"]: r["last_beat_utc"] for r in rows}
        for svc in _EXPECTED_DAEMONS:
            if svc not in seen:
                failures.append(f"daemon_heartbeat: no row for {svc!r}")
                continue
            try:
                raw = seen[svc]
                beat_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if beat_utc.tzinfo is None:
                    beat_utc = beat_utc.replace(tzinfo=timezone.utc)
                age_s = (now_utc - beat_utc).total_seconds()
                if age_s > _HEARTBEAT_MAX_AGE_S:
                    failures.append(
                        f"daemon_heartbeat: {svc!r} stale by {age_s:.0f}s"
                        f" (max {_HEARTBEAT_MAX_AGE_S}s)"
                    )
            except Exception as exc:
                failures.append(f"daemon_heartbeat: {svc!r} parse error: {exc}")

        # ── Check 2: pending_orders schema reachable ─────────────────────────
        try:
            conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()
        except Exception as exc:
            failures.append(f"pending_orders inaccessible: {exc}")

        # ── Check 3: decisions schema reachable (ADR-012) ────────────────────
        try:
            conn.execute("SELECT COUNT(*) FROM decisions").fetchone()
        except Exception as exc:
            failures.append(f"decisions inaccessible: {exc}")

    finally:
        conn.close()

    return failures
