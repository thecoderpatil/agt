#!/usr/bin/env python3
"""
circuit_breaker.py -- Hard safety checks for the autonomous loop.

Called by scheduled tasks before and after operations. Returns a structured
verdict: {ok: bool, halted: bool, violations: [...], warnings: [...]}.

If halted=True, the task MUST stop all order activity immediately.
Violations are logged to autonomous_session_log and (if critical) emailed.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure we're in project root
os.chdir(Path(__file__).resolve().parent.parent)

DB_PATH = "agt_desk.db"
RAILS_PATH = "_SAFETY_RAILS.md"

# Hard-coded limits (mirrors _SAFETY_RAILS.md -- code is the enforcer, file is documentation)
MAX_DAILY_ORDERS = 30
MAX_DAILY_NOTIONAL = 3_000_000
VIX_HALT_THRESHOLD = 35
MAX_CONSECUTIVE_ERRORS = 3
RECONCILIATION_DRIFT_PCT = 0.10  # 10%
ACCOUNT_NLV_DROP_PCT = 0.08  # 8%
DIRECTIVE_MAX_AGE_DAYS = 5


def _get_conn():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def check_daily_order_limit() -> dict:
    """Check if today's order count is within limits."""
    conn = _get_conn()
    try:
        # 'superseded' rows are prior versions of an order after a price
        # modification (price-chase loop, strike retry). They are NOT new
        # IB events; counting them inflates the limit. The /report surface
        # already shows the by-status breakdown -- keep enforcement aligned.
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_orders "
            "WHERE date(created_at) = date('now') "
            "AND status != 'superseded'"
        ).fetchone()
        count = row["cnt"] if row else 0
        if count >= MAX_DAILY_ORDERS:
            return {
                "ok": False, "halted": True,
                "reason": f"Daily order limit reached: {count}/{MAX_DAILY_ORDERS}",
            }
        return {"ok": True, "count": count, "limit": MAX_DAILY_ORDERS}
    finally:
        conn.close()


def check_daily_notional() -> dict:
    """Check if today's total notional is within limits."""
    conn = _get_conn()
    try:
        # Only count committed capital. cancelled/failed/rejected orders
        # never committed; superseded rows are prior versions, not new
        # commitments. Previously counting all rows produced ~12x inflation
        # (2026-04-16: $2.5M phantom vs $204K real).
        rows = conn.execute(
            "SELECT payload FROM pending_orders "
            "WHERE date(created_at) = date('now') "
            "AND status IN ('filled', 'processing', 'partially_filled')"
        ).fetchall()
        total = 0.0
        for r in rows:
            try:
                p = json.loads(r["payload"])
                strike = float(p.get("strike") or 0)
                qty = int(p.get("quantity") or 0)
                total += strike * qty * 100
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        if total >= MAX_DAILY_NOTIONAL:
            return {
                "ok": False, "halted": True,
                "reason": f"Daily notional limit: ${total:,.0f} >= ${MAX_DAILY_NOTIONAL:,.0f}",
            }
        return {"ok": True, "notional": total, "limit": MAX_DAILY_NOTIONAL}
    finally:
        conn.close()


def check_consecutive_errors() -> dict:
    """Check if recent task runs have had consecutive errors."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT errors FROM autonomous_session_log "
            "ORDER BY id DESC LIMIT ?"
        , (MAX_CONSECUTIVE_ERRORS,)).fetchall()

        if len(rows) < MAX_CONSECUTIVE_ERRORS:
            return {"ok": True, "reason": "Not enough history"}

        error_count = 0
        for r in rows:
            errs = r["errors"]
            if errs and errs != "null" and errs != "[]":
                error_count += 1

        if error_count >= MAX_CONSECUTIVE_ERRORS:
            return {
                "ok": False, "halted": True,
                "reason": f"{error_count} consecutive task runs with errors -- CIRCUIT BREAKER",
            }
        return {"ok": True, "consecutive_errors": error_count}
    finally:
        conn.close()


def check_nlv_drop() -> dict:
    """Check if any account has dropped >8% today vs yesterday's close."""
    conn = _get_conn()
    try:
        # Get latest NLV per account
        current = {}
        for r in conn.execute("SELECT account_id, nlv FROM v_available_nlv"):
            current[r["account_id"]] = r["nlv"]

        # Get yesterday's NLV (most recent before today)
        yesterday = {}
        for acct in current:
            row = conn.execute(
                "SELECT nlv FROM el_snapshots "
                "WHERE account_id = ? AND date(timestamp) < date('now') "
                "ORDER BY timestamp DESC LIMIT 1",
                (acct,)
            ).fetchone()
            if row:
                yesterday[acct] = row["nlv"]

        drops = []
        for acct, now_nlv in current.items():
            prev_nlv = yesterday.get(acct)
            if prev_nlv and prev_nlv > 0:
                pct_change = (now_nlv - prev_nlv) / prev_nlv
                if pct_change < -ACCOUNT_NLV_DROP_PCT:
                    drops.append({
                        "account": acct,
                        "prev": prev_nlv,
                        "now": now_nlv,
                        "drop_pct": round(pct_change * 100, 2),
                    })

        if drops:
            return {
                "ok": False, "halted": True,
                "reason": f"NLV drop circuit breaker: {drops}",
                "drops": drops,
            }
        return {"ok": True}
    except Exception as exc:
        return {"ok": True, "warning": f"NLV check failed: {exc}"}
    finally:
        conn.close()


def check_vix() -> dict:
    """Check CBOE VIX level; halt if >= VIX_HALT_THRESHOLD.

    Uses yfinance spot quote on ^VIX. Soft-fails open (ok=True with a
    warning) on any fetch error -- refusing to trade because yfinance is
    flaky would be more dangerous than proceeding. The halt branch only
    fires when we have a real number >= threshold.
    """
    try:
        import yfinance as yf  # local import -- circuit_breaker is loaded cheaply
        t = yf.Ticker("^VIX")
        # fast_info is the current snapshot; history(period='1d') is a fallback.
        level = None
        try:
            level = float(t.fast_info.last_price)
        except Exception:
            try:
                hist = t.history(period="1d", interval="1m")
                if hist is not None and not hist.empty:
                    level = float(hist["Close"].iloc[-1])
            except Exception:
                level = None
        if level is None or level <= 0:
            return {"ok": True, "warning": "VIX fetch returned no usable level"}
        if level >= VIX_HALT_THRESHOLD:
            return {
                "ok": False, "halted": True,
                "reason": f"VIX {level:.2f} >= {VIX_HALT_THRESHOLD} halt threshold",
                "vix": level,
            }
        return {"ok": True, "vix": level, "threshold": VIX_HALT_THRESHOLD}
    except Exception as exc:
        return {"ok": True, "warning": f"VIX check failed: {exc}"}


def check_directive_freshness() -> dict:
    """Check if _WEEKLY_ARCHITECT_DIRECTIVE.md exists and is fresh enough."""
    directive_path = Path("_WEEKLY_ARCHITECT_DIRECTIVE.md")
    if not directive_path.exists():
        return {"ok": True, "has_directive": False, "reason": "No directive file"}

    mtime = datetime.fromtimestamp(directive_path.stat().st_mtime)
    age_days = (datetime.now() - mtime).days

    if age_days > DIRECTIVE_MAX_AGE_DAYS:
        return {
            "ok": True, "has_directive": True, "stale": True,
            "reason": f"Directive is {age_days} days old (max {DIRECTIVE_MAX_AGE_DAYS}) -- IGNORING",
        }
    return {"ok": True, "has_directive": True, "stale": False, "age_days": age_days}


def run_all_checks() -> dict:
    """Run all circuit breaker checks. Returns aggregate verdict."""
    checks = {
        "daily_orders": check_daily_order_limit(),
        "daily_notional": check_daily_notional(),
        "consecutive_errors": check_consecutive_errors(),
        "nlv_drop": check_nlv_drop(),
        "vix": check_vix(),
        "directive": check_directive_freshness(),
    }

    halted = any(c.get("halted", False) for c in checks.values())
    violations = [
        {"check": name, **result}
        for name, result in checks.items()
        if not result["ok"]
    ]
    warnings = [
        {"check": name, "warning": result.get("warning") or result.get("reason", "")}
        for name, result in checks.items()
        if result.get("warning") or result.get("stale")
    ]

    return {
        "ok": len(violations) == 0,
        "halted": halted,
        "violations": violations,
        "warnings": warnings,
        "checks": checks,
        "timestamp": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    result = run_all_checks()
    print(json.dumps(result, indent=2, default=str))
    if result["halted"]:
        print("\n*** CIRCUIT BREAKER TRIPPED -- ALL ORDER ACTIVITY HALTED ***")
        sys.exit(1)
    elif not result["ok"]:
        print(f"\n*** {len(result['violations'])} VIOLATIONS -- some actions blocked ***")
        sys.exit(2)
    else:
        print("\n[OK] All checks passed")
        sys.exit(0)
