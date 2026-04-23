"""Trading-day observer daemon — read-only SQLite + NSSM log tail + IB TCP probe.

Usage:
    python scripts/observe_trading_day.py [--date YYYY-MM-DD] [--poll-sec N] [--db PATH]

Writes to reports/trading_day_YYYYMMDD/timeline.jsonl (one JSON event per line).
Emit one stderr heartbeat per minute. Ctrl-C for clean shutdown.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_shutdown = threading.Event()
_lock = threading.Lock()
_stats = {"events": 0, "db_rows": 0, "log_lines": 0, "ib_state": "unknown"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_event(writer, event: dict) -> None:
    with _lock:
        writer.write(json.dumps(event, default=str) + "\n")
        writer.flush()
        _stats["events"] += 1


# ---------------------------------------------------------------------------
# Schema snapshot
# ---------------------------------------------------------------------------
def _dump_schema(db_path: str, out_dir: pathlib.Path) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    conn.close()
    schema_file = out_dir / "schema_snapshot.sql"
    schema_file.write_text(
        "\n\n".join(r[0] for r in rows) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# RO DB connection + polling
# ---------------------------------------------------------------------------
_TABLE_CURSORS: dict[str, int] = {}  # table -> last seen rowid

_TABLES = [
    "pending_orders",
    "pending_order_children",
    "cross_daemon_alerts",       # alert_queue equivalent
    "incidents",
    "daemon_heartbeat",
    "mode_history",
    "glide_paths",
    "el_snapshots",
    "csp_decisions",
    "master_log_trades",
    "master_log_open_positions",
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def _cursor_col(table: str) -> str:
    """Return the column to use as a high-watermark cursor."""
    if table == "daemon_heartbeat":
        return "last_beat_utc"
    return "rowid"


def _poll_db(conn: sqlite3.Connection, writer, known_tables: set[str]) -> None:
    conn.row_factory = sqlite3.Row
    for table in _TABLES:
        if table not in known_tables:
            continue
        col = _cursor_col(table)
        last = _TABLE_CURSORS.get(table)
        try:
            if table == "daemon_heartbeat":
                rows = conn.execute(
                    f"SELECT * FROM {table}"
                ).fetchall()
                for row in rows:
                    d = dict(row)
                    key = (table, d.get("daemon_name", ""))
                    ts = d.get("last_beat_utc", "")
                    if _TABLE_CURSORS.get(key) != ts:
                        _TABLE_CURSORS[key] = ts
                        _write_event(writer, {
                            "observed_at": _now(),
                            "source": "db",
                            "table": table,
                            "row": d,
                        })
                        _stats["db_rows"] += 1
            elif last is None:
                # First poll: grab max rowid as baseline (don't emit existing rows)
                r = conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()
                _TABLE_CURSORS[table] = r[0] or 0
            else:
                # Sprint 6 Mega-MR 6 (observer rowid bug): explicit `rowid`
                # in SELECT — SELECT * on a Row factory-wrapped cursor does
                # not expose `rowid` as a named key (sqlite3.Row only maps
                # declared columns). Prior version failed 85% of poll
                # iterations with "No item with that key".
                rows = conn.execute(
                    f"SELECT rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid",
                    (last,),
                ).fetchall()
                for row in rows:
                    d = dict(row)
                    _TABLE_CURSORS[table] = max(
                        _TABLE_CURSORS[table], row["rowid"]
                    )
                    _write_event(writer, {
                        "observed_at": _now(),
                        "source": "db",
                        "table": table,
                        "row": d,
                    })
                    _stats["db_rows"] += 1
        except sqlite3.Error as exc:
            _write_event(writer, {
                "observed_at": _now(),
                "source": "observer",
                "event": "db_poll_error",
                "table": table,
                "error": str(exc),
            })


def _check_heartbeat_gaps(conn: sqlite3.Connection, writer) -> None:
    try:
        rows = conn.execute(
            "SELECT daemon_name, last_beat_utc FROM daemon_heartbeat"
        ).fetchall()
    except sqlite3.Error:
        return
    now = datetime.now(timezone.utc)
    for name, ts in rows:
        try:
            beat = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            gap = int((now - beat).total_seconds())
            if gap > 120:
                _write_event(writer, {
                    "observed_at": _now(),
                    "source": "observer",
                    "event": "heartbeat_stale",
                    "service": name,
                    "last_ts": ts,
                    "gap_sec": gap,
                })
        except Exception:
            pass


# ---------------------------------------------------------------------------
# IB Gateway probe
# ---------------------------------------------------------------------------
_IB_STATES: dict[int, str] = {}  # port -> last state


def _ib_probe_loop(writer) -> None:
    ports = [4002, 4001]
    # emit initial state immediately
    for port in ports:
        state = _tcp_probe(port)
        _IB_STATES[port] = state
        _write_event(writer, {
            "observed_at": _now(),
            "source": "ib",
            "port": port,
            "state": state,
            "note": "initial",
        })
        if port == 4002:
            _stats["ib_state"] = state

    while not _shutdown.is_set():
        _shutdown.wait(30)
        if _shutdown.is_set():
            break
        for port in ports:
            state = _tcp_probe(port)
            if state != _IB_STATES.get(port):
                _IB_STATES[port] = state
                _write_event(writer, {
                    "observed_at": _now(),
                    "source": "ib",
                    "port": port,
                    "state": state,
                    "note": "transition",
                })
            if port == 4002:
                _stats["ib_state"] = state


def _tcp_probe(port: int) -> str:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        s.close()
        return "connected"
    except OSError:
        return "disconnected"


# ---------------------------------------------------------------------------
# NSSM log tail
# ---------------------------------------------------------------------------
def _discover_log_paths() -> dict[tuple[str, str], str]:
    """Return {(service, stream): path} via nssm or winreg fallback."""
    targets = {
        ("agt-telegram-bot", "stdout"): "AppStdout",
        ("agt-telegram-bot", "stderr"): "AppStderr",
        ("agt-scheduler", "stdout"): "AppStdout",
        ("agt-scheduler", "stderr"): "AppStderr",
    }
    result: dict[tuple[str, str], str] = {}
    for (svc, stream), key in targets.items():
        path = _nssm_get(svc, key)
        if not path:
            path = _winreg_get(svc, key)
        result[(svc, stream)] = path or ""
    return result


def _nssm_get(svc: str, key: str) -> str:
    try:
        r = subprocess.run(
            ["nssm", "get", svc, key],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return ""
        # NSSM outputs UTF-16-LE on Windows
        raw = r.stdout
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            val = raw.decode("utf-16", errors="replace")
        elif b"\x00" in raw:
            val = raw.decode("utf-16-le", errors="replace")
        else:
            val = raw.decode("utf-8", errors="replace")
        return val.strip().strip("\x00").strip()
    except Exception:
        return ""


def _winreg_get(svc: str, key: str) -> str:
    try:
        import winreg
        path = rf"SYSTEM\CurrentControlSet\Services\{svc}\Parameters"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            val, _ = winreg.QueryValueEx(k, key)
            return str(val).strip() if val else ""
    except Exception:
        return ""


def _tail_log_loop(svc: str, stream: str, path: str, writer) -> None:
    if not path:
        _write_event(writer, {
            "observed_at": _now(),
            "source": "observer",
            "event": "log_path_missing",
            "service": svc,
            "stream": stream,
        })
        return

    _write_event(writer, {
        "observed_at": _now(),
        "source": "observer",
        "event": "log_tail_start",
        "service": svc,
        "stream": stream,
        "path": path,
    })

    buf = b""
    fh = None
    last_size = -1

    try:
        fh = open(path, "rb")
        fh.seek(0, 2)  # seek to end
        last_size = fh.tell()
    except OSError as exc:
        _write_event(writer, {
            "observed_at": _now(),
            "source": "observer",
            "event": "log_open_error",
            "service": svc,
            "stream": stream,
            "error": str(exc),
        })
        return

    while not _shutdown.is_set():
        _shutdown.wait(1)
        try:
            cur_size = os.path.getsize(path)
        except OSError:
            cur_size = 0

        if cur_size < last_size:
            # rotation / truncation
            _write_event(writer, {
                "observed_at": _now(),
                "source": "observer",
                "event": "log_rotated",
                "service": svc,
                "stream": stream,
            })
            fh.close()
            fh = open(path, "rb")
            last_size = 0
            buf = b""

        chunk = fh.read(65536)
        if chunk:
            last_size = fh.tell()
            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                if line:
                    _write_event(writer, {
                        "observed_at": _now(),
                        "source": "nssm",
                        "service": svc,
                        "stream": stream,
                        "line": line,
                    })
                    _stats["log_lines"] += 1

    if fh:
        fh.close()


# ---------------------------------------------------------------------------
# Console heartbeat
# ---------------------------------------------------------------------------
def _console_heartbeat_loop() -> None:
    while not _shutdown.is_set():
        _shutdown.wait(60)
        if _shutdown.is_set():
            break
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"[observer {ts}] events_captured={_stats['events']} "
            f"db_rows={_stats['db_rows']} log_lines={_stats['log_lines']} "
            f"ib_state={_stats['ib_state']}",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
def _install_signal_handlers() -> None:
    def _handler(signum, frame):
        _shutdown.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="AGT trading-day observer")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today ET)")
    ap.add_argument("--poll-sec", type=float, default=5.0)
    ap.add_argument("--db", default=r"C:\AGT_Telegram_Bridge\agt_desk.db")
    args = ap.parse_args()

    if args.date:
        date_str = args.date.replace("-", "")
    else:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        date_str = datetime.now(et).strftime("%Y%m%d")

    db_path = str(pathlib.Path(args.db).resolve())
    out_dir = pathlib.Path("reports") / f"trading_day_{date_str}"
    out_dir.mkdir(parents=True, exist_ok=True)

    timeline_path = out_dir / "timeline.jsonl"
    writer = timeline_path.open("a", encoding="utf-8")

    _install_signal_handlers()

    # Schema snapshot
    try:
        _dump_schema(db_path, out_dir)
    except Exception as exc:
        print(f"[observer] schema dump failed: {exc}", file=sys.stderr)

    # Boot event
    _write_event(writer, {
        "observed_at": _now(),
        "source": "observer",
        "event": "boot",
        "pid": os.getpid(),
        "db_path": db_path,
        "poll_sec": args.poll_sec,
        "git_tip": "db288db1",
    })

    # Open RO connection
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True,
            check_same_thread=False, timeout=1.0,
        )
    except sqlite3.Error as exc:
        print(f"[observer] DB open failed: {exc}", file=sys.stderr)
        return 1

    # Discover which tables exist
    known_tables: set[str] = set()
    for t in _TABLES:
        if _table_exists(conn, t):
            known_tables.add(t)
        else:
            _write_event(writer, {
                "observed_at": _now(),
                "source": "observer",
                "event": "table_missing",
                "table": t,
            })

    # Discover NSSM log paths
    log_paths = _discover_log_paths()
    for (svc, stream), path in log_paths.items():
        _write_event(writer, {
            "observed_at": _now(),
            "source": "observer",
            "event": "log_path_discovered",
            "service": svc,
            "stream": stream,
            "path": path or "<not found>",
        })

    # Threads
    threads: list[threading.Thread] = []

    t_ib = threading.Thread(target=_ib_probe_loop, args=(writer,), daemon=True)
    t_ib.start()
    threads.append(t_ib)

    for (svc, stream), path in log_paths.items():
        t = threading.Thread(
            target=_tail_log_loop, args=(svc, stream, path, writer), daemon=True
        )
        t.start()
        threads.append(t)

    t_hb = threading.Thread(target=_console_heartbeat_loop, daemon=True)
    t_hb.start()
    threads.append(t_hb)

    # Main DB poll loop
    try:
        while not _shutdown.is_set():
            try:
                _poll_db(conn, writer, known_tables)
                _check_heartbeat_gaps(conn, writer)
            except Exception as exc:
                _write_event(writer, {
                    "observed_at": _now(),
                    "source": "observer",
                    "event": "poll_error",
                    "error": str(exc),
                })
            _shutdown.wait(args.poll_sec)
    finally:
        _shutdown.set()
        conn.close()
        _write_event(writer, {
            "observed_at": _now(),
            "source": "observer",
            "event": "shutdown",
            "stats": dict(_stats),
        })
        writer.close()
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"[observer {ts}] shutdown — events={_stats['events']} "
            f"db_rows={_stats['db_rows']} log_lines={_stats['log_lines']}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
