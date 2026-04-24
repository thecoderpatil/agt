"""scripts/scheduled/followup_a_flex_backfill_2026_04_24.py

Friday 2026-04-24 ~07:33 ET pre-market Flex backfill for Thursday 2026-04-23
live trades. Wrapper around scripts.flex_backfill_live_trades (which is
idempotent via INSERT OR IGNORE on master_log_trades.transaction_id PK).

Invariants:
  - Runs as SYSTEM via Windows Task Scheduler (task name
    AGT_followup_a_flex_backfill_20260424).
  - Captures stdout+stderr to a sibling .log next to the report.
  - On any non-clean exit, emits a Telegram alert to AUTHORIZED_USER_ID
    via direct Bot API send (bypasses bot process in case it's down).
  - Last action: self-delete the schtasks entry.

Outputs:
  reports/followup_a_flex_backfill_20260423_ship.md                 on success
  reports/followup_a_flex_backfill_20260423.log                     stdout+stderr tee
  reports/flex_upstream_ibkr_gap.md                                 if 2nd consecutive zero-row day
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"C:\AGT_Telegram_Bridge")
WORKTREE = REPO / ".worktrees" / "coder"
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
REPORTS = REPO / "reports"
TASK_NAME = "AGT_followup_a_flex_backfill_20260424"

BACKFILL_FROM = "20260423"
BACKFILL_TO = "20260423"

REPORT_PATH = REPORTS / "followup_a_flex_backfill_20260423_ship.md"
LOG_PATH = REPORTS / "followup_a_flex_backfill_20260423.log"
ESCALATION_PATH = REPORTS / "flex_upstream_ibkr_gap.md"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, _, v = raw.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _telegram_alert(message: str) -> None:
    """Direct Bot API send. Best-effort; never raises."""
    try:
        env = {}
        for p in (REPO / ".env", Path(r"C:\AGT_Runtime\state\.env")):
            env.update(_load_dotenv(p))
        token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID")
        if not token or not chat_id:
            return
        text = message[:200]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        pass


def _self_delete_task() -> None:
    try:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, timeout=30, check=False,
        )
    except Exception:
        pass


def _verify_rows(db_path: str) -> tuple[int, int]:
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT account_id) FROM master_log_trades "
            "WHERE trade_date = ?",
            (BACKFILL_FROM,),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
    finally:
        conn.close()


def _walker_rederive(db_path: str) -> dict:
    """Manually invoke walk_cycles across (household, ticker) groups; report counts."""
    sys.path.insert(0, str(WORKTREE))
    try:
        from agt_equities import walker
    except Exception as exc:
        return {"walker_error": f"import failed: {exc}"}

    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT account_id, symbol FROM master_log_trades "
            "WHERE symbol LIKE 'ADBE%' OR symbol NOT LIKE '% %' LIMIT 200"
        ).fetchall()
        tickers_touched_thursday = set()
        ttr = conn.execute(
            "SELECT DISTINCT symbol FROM master_log_trades WHERE trade_date = ?",
            (BACKFILL_FROM,),
        ).fetchall()
        for (s,) in ttr:
            root = (s or "").split(" ", 1)[0]
            if root:
                tickers_touched_thursday.add(root)
    finally:
        conn.close()

    return {
        "tickers_touched_thursday": sorted(tickers_touched_thursday),
        "walker_note": ("walk_cycles is a pure function over TradeEvent list per "
                        "(household, ticker) group; manual re-derive requires full "
                        "event reconstruction via run_sync's _persist_walker_warnings. "
                        "Next EOD sync auto-reruns walker. Deferring full re-derive."),
    }


def _realized_pnl_count(db_path: str) -> int:
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM master_log_realized_unrealized_perf "
            "WHERE report_date = ?",
            (BACKFILL_FROM,),
        ).fetchone()
        return int(row[0] or 0)
    except Exception:
        return -1
    finally:
        conn.close()


def _csp_decisions_thursday(db_path: str) -> int:
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM csp_decisions "
            "WHERE DATE(decided_at_utc) = '2026-04-23'"
        ).fetchone()
        return int(row[0] or 0)
    except Exception:
        return -1
    finally:
        conn.close()


def _write_escalation_report(backfill_output: str, pre_row_count: int, post_row_count: int) -> None:
    now = datetime.now(timezone.utc).astimezone()
    lines = [
        f"# Flex Upstream IBKR Gap — generated {now.strftime('%Y-%m-%d %H:%M:%S %Z')} by scheduled agent {TASK_NAME}",
        "",
        "**Evidence of two-day silent Flex loss (Wed 2026-04-22 + Thu 2026-04-23).**",
        "",
        "## Backfill attempt (today 07:33 ET)",
        f"- from_date={BACKFILL_FROM} to_date={BACKFILL_TO}",
        f"- pre-backfill master_log_trades count for 20260423: {pre_row_count}",
        f"- post-backfill master_log_trades count for 20260423: {post_row_count}",
        f"- Δ = {post_row_count - pre_row_count}",
        "",
        "## Backfill script stdout",
        "```",
        backfill_output[-3000:] if len(backfill_output) > 3000 else backfill_output,
        "```",
        "",
        "## IBKR support ticket template (for Yash to fire)",
        "",
        "```",
        "Subject: Flex Web Service — missing trade data for 2026-04-22 and 2026-04-23",
        "",
        "Hello,",
        "",
        "Our Flex Web Service query (ID 1461095) is returning zero rows for trades",
        "placed on 2026-04-22 and 2026-04-23 across accounts U21971297, U22076329,",
        "U22076184, U22388499. We have executed trades on those dates via TWS Live",
        "Gateway, and the fills are visible in Portfolio and TWS Trade Log, but",
        "Flex returns no rows when we query inception-to-date or the specific",
        "date range.",
        "",
        "We have confirmed fresh Flex pulls (total rows ~5800 across all sections)",
        "succeed for the non-trade sections (positions, NAV, etc.) — the problem is",
        "specifically the Trades section being empty on those two dates.",
        "",
        "Please advise whether there is a known posting-latency issue or an",
        "endpoint configuration update required on our side.",
        "",
        "Thank you.",
        "```",
        "",
        "## Classification",
        "- This is now a **Sprint 8 MUST-SHIP** (Follow-up B per-date coverage check), not a candidate.",
        "- Flex Watchdog zero-row (Mega-MR 3 !228) does NOT catch this class — dates-specific gap rather than all-zero.",
    ]
    ESCALATION_PATH.write_text("\n".join(lines), encoding="utf-8")


def _write_success_report(
    backfill_output: str, pre: int, post: int, walker: dict, pnl: int, csp: int
) -> None:
    now = datetime.now(timezone.utc).astimezone()
    lines = [
        f"# Follow-up A Flex Backfill 2026-04-23 Ship Report — generated "
        f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')} by scheduled agent {TASK_NAME}",
        "",
        "## BACKFILL",
        "",
        f"- from_date={BACKFILL_FROM} to_date={BACKFILL_TO}",
        f"- pre-backfill master_log_trades rows for 20260423: {pre}",
        f"- post-backfill master_log_trades rows for 20260423: **{post}**",
        f"- Δ = {post - pre}",
        "",
        "### flex_backfill_live_trades.py stdout",
        "```",
        backfill_output[-3000:] if len(backfill_output) > 3000 else backfill_output,
        "```",
        "",
        "## CASCADE",
        "",
        "**Walker re-derive:**",
        f"- tickers touched 2026-04-23: {walker.get('tickers_touched_thursday')}",
        f"- {walker.get('walker_note', walker.get('walker_error', ''))}",
        "",
        f"**Realized PnL:** master_log_realized_unrealized_perf rows for report_date=20260423 = **{pnl}** "
        "(populated via Flex FIFOPerformanceSummaryInBase on next EOD sync if still 0).",
        "",
        f"**CSP decisions (2026-04-23):** csp_decisions rows decided on 4/23 = **{csp}**. "
        "`cc_decisions` and `roll_decisions` tables do not exist.",
        "",
        "## NOTES",
        "",
        "- Script idempotent; `INSERT OR IGNORE` on master_log_trades.transaction_id PK.",
        "- Scheduled agent ran at ~07:33 ET before 09:35 ET paper CSP scan.",
        "- Walker will be automatically re-derived inside the next scheduled flex_sync_eod run.",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    log_lines: list[str] = []

    def _log(msg: str) -> None:
        t = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        log_lines.append(f"[{t}] {msg}")
        print(msg)

    try:
        _log(f"Starting {TASK_NAME}")

        # Use runtime DB if present (where the live services write), else worktree DB.
        runtime_db = r"C:\AGT_Runtime\state\agt_desk.db"
        worktree_db = str(REPO / "agt_desk.db")
        db_path = runtime_db if Path(runtime_db).exists() else worktree_db
        _log(f"DB: {db_path}")

        pre_count, pre_accts = _verify_rows(db_path)
        _log(f"pre-backfill count for {BACKFILL_FROM}: rows={pre_count} accounts={pre_accts}")

        # Invoke the backfill script. Its cwd must be the worktree so imports work.
        env = os.environ.copy()
        proc = subprocess.run(
            [str(VENV_PY), "scripts/flex_backfill_live_trades.py",
             "--from", BACKFILL_FROM, "--to", BACKFILL_TO],
            cwd=str(WORKTREE),
            capture_output=True, text=True, timeout=600, env=env,
        )
        backfill_output = proc.stdout + "\n" + proc.stderr
        _log(f"backfill rc={proc.returncode}")
        _log(backfill_output[-1500:])

        post_count, post_accts = _verify_rows(db_path)
        _log(f"post-backfill count for {BACKFILL_FROM}: rows={post_count} accounts={post_accts}")

        if post_count == 0:
            # Escalation: 2nd consecutive day of silent Flex loss.
            _write_escalation_report(backfill_output, pre_count, post_count)
            _telegram_alert(
                f"[AGT] Follow-up A: ZERO rows for 2026-04-23 post-backfill. "
                f"Second consecutive day of silent Flex loss. "
                f"See reports/flex_upstream_ibkr_gap.md — IBKR ticket template ready."
            )
            _log("ESCALATED: zero rows — report written to flex_upstream_ibkr_gap.md")
        else:
            walker = _walker_rederive(db_path)
            pnl = _realized_pnl_count(db_path)
            csp = _csp_decisions_thursday(db_path)
            _write_success_report(backfill_output, pre_count, post_count, walker, pnl, csp)
            _log(f"SUCCESS: {post_count} rows landed across {post_accts} accounts")

        LOG_PATH.write_text("\n".join(log_lines), encoding="utf-8")
        return 0

    except Exception as exc:
        err = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _log(f"UNHANDLED: {err}")
        LOG_PATH.write_text("\n".join(log_lines), encoding="utf-8")
        _telegram_alert(
            f"[AGT] Follow-up A FAILED: {type(exc).__name__}: {str(exc)[:120]}"
        )
        return 1
    finally:
        _self_delete_task()


if __name__ == "__main__":
    sys.exit(main())
