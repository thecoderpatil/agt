"""scripts/scheduled/sprint7_first_fire_observation_2026_04_24.py

Friday 2026-04-24 ~18:42 ET — ADR-017 §8 observation window Day 1
first-fire verification. Runs 7 min after the scheduled 18:35 ET
oversight_digest_send fire.

Writes raw classification-ready data — Architect classifies flags
(real / baseline / suspicious) at next session. Scripts only captures.

Invariants:
  - Runs as SYSTEM via Windows Task Scheduler (task name
    AGT_sprint7_first_fire_20260424).
  - Read-only across all upstream tables (incidents, cross_daemon_alerts,
    daemon_heartbeat, master_log_sync, decisions).
  - On any non-clean exit, emits Telegram alert to AUTHORIZED_USER_ID.
  - Last action: self-delete the schtasks entry.

Outputs:
  reports/sprint7_first_fire_observation.md                 primary report
  reports/sprint7_first_fire_observation.log                stdout+stderr tee
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(r"C:\AGT_Telegram_Bridge")
WORKTREE = REPO / ".worktrees" / "coder"
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
REPORTS = REPO / "reports"
TASK_NAME = "AGT_sprint7_first_fire_20260424"

REPORT_PATH = REPORTS / "sprint7_first_fire_observation.md"
LOG_PATH = REPORTS / "sprint7_first_fire_observation.log"


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
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
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


def _db_path() -> str:
    rt = r"C:\AGT_Runtime\state\agt_desk.db"
    return rt if Path(rt).exists() else str(REPO / "agt_desk.db")


def _heartbeats(db_path: str):
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT daemon_name, MAX(last_beat_utc), MAX(pid) FROM daemon_heartbeat "
            "GROUP BY daemon_name"
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    out = []
    for name, last, pid in rows:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (now - dt).total_seconds()
        except Exception:
            age = None
        out.append({"daemon_name": name, "last_beat_utc": last, "pid": pid, "age_s": age})
    return out


def _query_failure_alerts(db_path: str, window_start_epoch: float, window_end_epoch: float):
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, kind, severity, payload_json, created_ts FROM cross_daemon_alerts "
            "WHERE kind IN ('OVERSIGHT_DIGEST_FAILED', 'SCHEDULED_DIGEST_FAILED') "
            "AND created_ts >= ? AND created_ts < ?",
            (window_start_epoch, window_end_epoch),
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(("id", "kind", "severity", "payload_json", "created_ts"), r)) for r in rows]


def _grep_bot_log_for_fire(window_start: datetime) -> dict:
    log_paths = [
        Path(r"C:\AGT_Runtime\bridge-current\telegram_ui.log"),
        Path(r"C:\AGT_Telegram_Bridge\logs\telegram_ui.log"),
        Path(r"C:\AGT_Telegram_Bridge\bot.log"),
    ]
    # Read the last ~500 lines of each log, grep for oversight_digest_send lines.
    findings: list[str] = []
    searched: list[str] = []
    for p in log_paths:
        if not p.exists():
            continue
        searched.append(str(p))
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-2000:]
            for ln in tail:
                if "oversight_digest_send" in ln:
                    findings.append(ln)
        except Exception as exc:
            findings.append(f"ERROR reading {p}: {exc}")
    return {"searched": searched, "findings": findings[-50:]}


def _build_snapshot_and_flags() -> dict:
    """Invoke the observability helpers in a subprocess to isolate import side effects."""
    script = r"""
import sys, json
sys.path.insert(0, r'C:\AGT_Telegram_Bridge\.worktrees\coder')
from agt_equities.observability.digest import build_observability_snapshot, render_observability_card
from agt_equities.observability.thresholds import compute_threshold_flags, ThresholdFlag
import dataclasses

s = build_observability_snapshot()
f = compute_threshold_flags()
card = render_observability_card(s, threshold_flags=f)

def serialize_flag(fl):
    d = dataclasses.asdict(fl) if dataclasses.is_dataclass(fl) else dict(fl.__dict__)
    return d

flags_raw = [serialize_flag(fl) for fl in (f or [])]

section_errors = {
    'architect_only': s.architect_only_error,
    'authorable': s.authorable_error,
    'heartbeats': s.heartbeats_error,
    'flex': s.flex_error,
    'promotion': s.promotion_error,
}
section_ok_count = sum(1 for v in section_errors.values() if not v)

summary = {
    'sections_ok_count': section_ok_count,
    'section_errors': {k: v for k, v in section_errors.items() if v},
    'flags_raw': flags_raw,
    'flag_count': len(flags_raw),
    'architect_only_rows': len(s.architect_only),
    'authorable_rows': len(s.authorable),
    'heartbeat_rows': len(s.heartbeats),
    'flex_sync_id': s.flex.sync_id if s.flex else None,
    'flex_stale': bool(s.flex.stale) if s.flex else None,
    'flex_zero_row_suspicion': bool(s.flex.zero_row_suspicion) if s.flex else None,
    'promotion_rows': len(s.promotion),
    'card_preview': card[:2000],
}
print(json.dumps(summary, default=str))
"""
    proc = subprocess.run(
        [str(VENV_PY), "-c", script],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr[-2000:]}
    try:
        return json.loads(proc.stdout.strip().split("\n")[-1])
    except Exception as exc:
        return {"error": f"json decode failed: {exc}", "stdout": proc.stdout[-1000:]}


def _on_demand_parity_reinvoke() -> dict:
    """Invoke a SECOND snapshot+render call to assert structural parity with the first."""
    # We snapshot twice and compare section_ok_count + section_errors keys + flag_count.
    # Values will differ slightly (timestamps, heartbeat ages) but structure should match.
    r1 = _build_snapshot_and_flags()
    r2 = _build_snapshot_and_flags()
    if "error" in r1 or "error" in r2:
        return {"parity": False, "r1_error": r1.get("error"), "r2_error": r2.get("error")}
    ok = (
        r1.get("sections_ok_count") == r2.get("sections_ok_count")
        and set(r1.get("section_errors", {}).keys()) == set(r2.get("section_errors", {}).keys())
    )
    return {"parity": ok, "r1_sections": r1.get("sections_ok_count"),
            "r2_sections": r2.get("sections_ok_count"),
            "r1_errors": r1.get("section_errors"), "r2_errors": r2.get("section_errors")}


def _write_report(data: dict) -> None:
    now = datetime.now(timezone.utc).astimezone()
    fire_et = now - timedelta(minutes=7)  # target +7 min post-fire
    card_arrived = data.get("card_arrived", "unknown")
    snap = data.get("snap", {})
    failure_alerts = data.get("failure_alerts", [])
    parity = data.get("parity", {})
    hb = data.get("heartbeats", [])
    log_find = data.get("log_findings", {})

    flags_raw = snap.get("flags_raw") or []

    sections_ok = snap.get("sections_ok_count", -1)
    section_errs = snap.get("section_errors", {})

    overnight_day_1 = "CONFIRMED" if (
        card_arrived == "yes"
        and sections_ok == 5
        and len(failure_alerts) == 0
        and parity.get("parity") is True
    ) else "DEFERRED"

    lines = [
        f"# Sprint 7 First-Fire Observation — generated {now.strftime('%Y-%m-%d %H:%M:%S %Z')} by scheduled agent {TASK_NAME}",
        "",
        f"FIRE_TIME: ~{fire_et.strftime('%Y-%m-%d %H:%M ET')} (targeted 18:35 ET)",
        f"CARD_ARRIVED: {card_arrived}",
        f"SECTIONS_OK: {sections_ok}/5"
        + (f" | section_errors={json.dumps(section_errs)}" if section_errs else ""),
        f"SCHEDULED_DIGEST_FAILED_ROWS: {sum(1 for a in failure_alerts if a['kind']=='SCHEDULED_DIGEST_FAILED')}",
        f"OVERSIGHT_DIGEST_FAILED_ROWS: {sum(1 for a in failure_alerts if a['kind']=='OVERSIGHT_DIGEST_FAILED')}",
        f"JOB_STILL_REGISTERED: {data.get('job_still_registered', 'unknown')}",
        f"ON_DEMAND_PARITY: {parity.get('parity')}",
        f"FLAGS_FIRED: {len(flags_raw)} total (classification deferred to Architect)",
        f"OBSERVATION_WINDOW_DAY_1: {overnight_day_1}",
        "",
        "## HEARTBEATS",
    ]
    for h in hb:
        lines.append(f"- {h['daemon_name']}: age_s={h['age_s']} pid={h['pid']} at={h['last_beat_utc']}")
    lines.append("")

    lines.append("## FAILURE ALERTS (18:30-18:45 ET window)")
    if failure_alerts:
        for a in failure_alerts:
            lines.append(f"- id={a['id']} kind={a['kind']} severity={a['severity']} "
                         f"payload={a['payload_json']}")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## BOT LOG GREP — 'oversight_digest_send' lines")
    lines.append(f"_searched: {log_find.get('searched')}_")
    lines.append("```")
    for ln in (log_find.get("findings") or [])[-30:]:
        lines.append(ln)
    lines.append("```")
    lines.append("")

    lines.append("## FLAGS_FIRED_RAW (for Architect classification)")
    if flags_raw:
        for fl in flags_raw:
            kind = fl.get("kind")
            src = fl.get("source")
            inv = fl.get("invariant_id")
            msg = fl.get("message")
            ev = fl.get("evidence", {})
            lines.append(f"- kind={kind} source={src} invariant_id={inv}")
            lines.append(f"  message=\"{msg}\"")
            lines.append(f"  evidence={json.dumps(ev, default=str)}")
    else:
        lines.append("_no flags fired_")
    lines.append("")

    lines.append("## CARD PREVIEW (first 2000 chars from renderer)")
    lines.append("```")
    lines.append(snap.get("card_preview", "(no card rendered)"))
    lines.append("```")
    lines.append("")

    lines.append("## NOTES")
    lines.append("- Script runs at 18:42 ET — 7 min after scheduled 18:35 ET fire.")
    lines.append("- Classification (real / baseline / suspicious) deferred to Architect next session.")
    lines.append("- `ON_DEMAND_PARITY=True` means a second snapshot+render invocation matches")
    lines.append("  the first on sections_ok_count + section_errors keys.")
    lines.append("- `card_arrived` is inferred from bot-log grep; a missing line does not")
    lines.append("  definitively mean the card failed to send — log may have rotated.")
    if overnight_day_1 == "DEFERRED":
        lines.append("- **Day 1 DEFERRED**: do not count toward the 10-trading-day ADR-017 §8 window.")
        lines.append("  Telegram alert + cowork-notes entry filed at runtime.")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _update_cowork_notes_top(message: str) -> None:
    p = WORKTREE / ".claude-cowork-notes.md"
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    header = "# AGT Cowork Session Notes"
    if header in src:
        new_block = f"{header}\n\n**🚨 SPRINT 7 OBSERVATION DAY 1 DEFERRED (scheduled agent):** {message}\n\n"
        src = src.replace(header, new_block[:-2], 1)  # keep structure
    try:
        p.write_text(src, encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    log_lines: list[str] = []

    def _log(msg: str) -> None:
        t = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        log_lines.append(f"[{t}] {msg}")
        print(msg)

    try:
        _log(f"Starting {TASK_NAME}")
        now = datetime.now(timezone.utc).astimezone()
        # Window: 18:30-18:45 ET today (local offset via .astimezone())
        et_today = now.replace(hour=18, minute=30, second=0, microsecond=0)
        window_start = et_today
        window_end = et_today + timedelta(minutes=15)
        window_start_epoch = window_start.timestamp()
        window_end_epoch = window_end.timestamp()
        _log(f"window UTC epoch: {window_start_epoch} → {window_end_epoch}")

        db = _db_path()
        _log(f"DB: {db}")

        hb = _heartbeats(db)
        _log(f"heartbeats: {hb}")

        bot_hb = next((h for h in hb if h["daemon_name"] == "agt_bot"), None)
        job_still_registered = "yes" if (bot_hb and bot_hb["age_s"] is not None and bot_hb["age_s"] < 120) else "no"

        failure_alerts = _query_failure_alerts(db, window_start_epoch, window_end_epoch)
        _log(f"failure alerts in window: {len(failure_alerts)}")

        log_find = _grep_bot_log_for_fire(window_start)
        _log(f"bot-log findings: {len(log_find['findings'])} lines mentioning oversight_digest_send")

        # Detect fire success from log — any success line in today's window counts as "arrived".
        card_arrived = "no"
        today_str = now.strftime("%Y-%m-%d")
        for ln in (log_find.get("findings") or []):
            if today_str in ln and "oversight_digest_send: ok" in ln:
                card_arrived = "yes"
                break
            if today_str in ln and "Telegram send failed" in ln:
                card_arrived = "no (send failed)"
                break

        snap = _build_snapshot_and_flags()
        if "error" in snap:
            _log(f"snapshot build ERROR: {snap['error']}")

        parity = _on_demand_parity_reinvoke()
        _log(f"parity: {parity}")

        data = {
            "card_arrived": card_arrived,
            "snap": snap,
            "failure_alerts": failure_alerts,
            "parity": parity,
            "heartbeats": hb,
            "log_findings": log_find,
            "job_still_registered": job_still_registered,
        }
        _write_report(data)
        _log(f"report written: {REPORT_PATH}")

        # Day-1 verdict side-effects.
        sections_ok = snap.get("sections_ok_count", -1)
        day_1 = (
            card_arrived == "yes"
            and sections_ok == 5
            and len(failure_alerts) == 0
            and parity.get("parity") is True
        )
        if not day_1:
            msg = (
                f"Day 1 DEFERRED: card_arrived={card_arrived} sections_ok={sections_ok}/5 "
                f"failure_alerts={len(failure_alerts)} parity={parity.get('parity')}"
            )
            _log(msg)
            _telegram_alert(f"[AGT] Sprint 7 first-fire NOT CLEAN. {msg[:150]}")
            _update_cowork_notes_top(msg)
        else:
            _log("Day 1 CONFIRMED — no alerts needed")

        LOG_PATH.write_text("\n".join(log_lines), encoding="utf-8")
        return 0

    except Exception as exc:
        err = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _log(f"UNHANDLED: {err}")
        LOG_PATH.write_text("\n".join(log_lines), encoding="utf-8")
        _telegram_alert(f"[AGT] Sprint 7 first-fire script FAILED: {type(exc).__name__}: {str(exc)[:120]}")
        return 1
    finally:
        _self_delete_task()


if __name__ == "__main__":
    sys.exit(main())
