"""
CLEANUP-6 Empirical Verification — TOCTOU race in share ledger handlers.

Demonstrates:
  PRE-FIX:  Without BEGIN IMMEDIATE, concurrent read-modify-write loses updates.
  POST-FIX: With BEGIN IMMEDIATE, all updates serialize correctly.

Three tests:
  1. _on_shares_sold pattern: 10 threads sell 10 shares each from 100
  2. _on_shares_bought pattern: 10 threads buy 10 shares each from 0
  3. append_status pattern: 10 threads append unique entries to JSON array
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

NUM_THREADS = 10
SHARES_PER_THREAD = 10

DDL = """
CREATE TABLE premium_ledger (
    household_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    initial_basis REAL,
    total_premium_collected REAL,
    shares_owned INTEGER,
    PRIMARY KEY (household_id, ticker)
);
CREATE TABLE pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'staged',
    status_history TEXT DEFAULT '[]',
    payload TEXT
);
"""


def _run_sell_unfixed(db_path, thread_id, barrier):
    """UNFIXED: deferred transaction — SELECT runs in autocommit."""
    barrier.wait()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        # Deferred BEGIN (default) — no lock until first DML
        with conn:
            row = conn.execute(
                "SELECT shares_owned FROM premium_ledger "
                "WHERE household_id = 'H1' AND ticker = 'TEST'"
            ).fetchone()
            old = int(row["shares_owned"])
            new = max(0, old - SHARES_PER_THREAD)
            time.sleep(0.001)  # Widen race window
            conn.execute(
                "UPDATE premium_ledger SET shares_owned = ? "
                "WHERE household_id = 'H1' AND ticker = 'TEST'",
                (new,),
            )
    finally:
        conn.close()


def _run_sell_fixed(db_path, thread_id, barrier):
    """FIXED: BEGIN IMMEDIATE acquires lock before SELECT."""
    barrier.wait()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT shares_owned FROM premium_ledger "
                "WHERE household_id = 'H1' AND ticker = 'TEST'"
            ).fetchone()
            old = int(row["shares_owned"])
            new = max(0, old - SHARES_PER_THREAD)
            conn.execute(
                "UPDATE premium_ledger SET shares_owned = ? "
                "WHERE household_id = 'H1' AND ticker = 'TEST'",
                (new,),
            )
    finally:
        conn.close()


def _run_buy_unfixed(db_path, thread_id, barrier):
    """UNFIXED: deferred transaction for buy."""
    barrier.wait()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            row = conn.execute(
                "SELECT shares_owned FROM premium_ledger "
                "WHERE household_id = 'H1' AND ticker = 'TEST'"
            ).fetchone()
            old = int(row["shares_owned"])
            new = old + SHARES_PER_THREAD
            time.sleep(0.001)
            conn.execute(
                "UPDATE premium_ledger SET shares_owned = ? "
                "WHERE household_id = 'H1' AND ticker = 'TEST'",
                (new,),
            )
    finally:
        conn.close()


def _run_buy_fixed(db_path, thread_id, barrier):
    """FIXED: BEGIN IMMEDIATE for buy."""
    barrier.wait()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT shares_owned FROM premium_ledger "
                "WHERE household_id = 'H1' AND ticker = 'TEST'"
            ).fetchone()
            old = int(row["shares_owned"])
            new = old + SHARES_PER_THREAD
            conn.execute(
                "UPDATE premium_ledger SET shares_owned = ? "
                "WHERE household_id = 'H1' AND ticker = 'TEST'",
                (new,),
            )
    finally:
        conn.close()


def _run_append_unfixed(db_path, thread_id, barrier):
    """UNFIXED: deferred transaction for JSON append."""
    barrier.wait()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            row = conn.execute(
                "SELECT status_history FROM pending_orders WHERE id = 1"
            ).fetchone()
            history = json.loads(row["status_history"])
            history.append({"thread": thread_id, "ts": time.time()})
            time.sleep(0.001)
            conn.execute(
                "UPDATE pending_orders SET status_history = ? WHERE id = 1",
                (json.dumps(history),),
            )
    finally:
        conn.close()


def _run_append_fixed(db_path, thread_id, barrier):
    """FIXED: BEGIN IMMEDIATE for JSON append."""
    barrier.wait()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status_history FROM pending_orders WHERE id = 1"
            ).fetchone()
            history = json.loads(row["status_history"])
            history.append({"thread": thread_id, "ts": time.time()})
            conn.execute(
                "UPDATE pending_orders SET status_history = ? WHERE id = 1",
                (json.dumps(history),),
            )
    finally:
        conn.close()


def _setup_db(db_path, initial_shares=100):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO premium_ledger VALUES ('H1', 'TEST', 50.0, 0.0, ?)",
        (initial_shares,),
    )
    conn.execute(
        "INSERT INTO pending_orders (status, status_history) VALUES ('staged', '[]')"
    )
    conn.commit()
    conn.close()


def _read_shares(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    val = conn.execute(
        "SELECT shares_owned FROM premium_ledger WHERE household_id='H1' AND ticker='TEST'"
    ).fetchone()["shares_owned"]
    conn.close()
    return int(val)


def _read_history_count(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    h = conn.execute("SELECT status_history FROM pending_orders WHERE id=1").fetchone()["status_history"]
    conn.close()
    return len(json.loads(h))


def _run_test(label, worker_fn, db_path, initial_shares=100):
    barrier = threading.Barrier(NUM_THREADS)
    threads = []
    for i in range(NUM_THREADS):
        t = threading.Thread(target=worker_fn, args=(db_path, i, barrier))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return True


def main():
    results = {}

    # Test 1: Sell
    for label, fn, expected in [
        ("SELL_UNFIXED", _run_sell_unfixed, 0),
        ("SELL_FIXED", _run_sell_fixed, 0),
    ]:
        db = os.path.join(tempfile.gettempdir(), f"c6_{label}.db")
        try:
            _setup_db(db, initial_shares=100)
            _run_test(label, fn, db, initial_shares=100)
            actual = _read_shares(db)
            ok = actual == expected
            results[label] = (actual, expected, ok)
            print(f"[{'OK' if ok else 'BUG'}] {label}: shares_owned={actual} (expected {expected})")
        finally:
            for s in ['', '-wal', '-shm']:
                try: os.unlink(db + s)
                except: pass

    # Test 2: Buy
    for label, fn, expected in [
        ("BUY_UNFIXED", _run_buy_unfixed, 100),
        ("BUY_FIXED", _run_buy_fixed, 100),
    ]:
        db = os.path.join(tempfile.gettempdir(), f"c6_{label}.db")
        try:
            _setup_db(db, initial_shares=0)
            _run_test(label, fn, db, initial_shares=0)
            actual = _read_shares(db)
            ok = actual == expected
            results[label] = (actual, expected, ok)
            print(f"[{'OK' if ok else 'BUG'}] {label}: shares_owned={actual} (expected {expected})")
        finally:
            for s in ['', '-wal', '-shm']:
                try: os.unlink(db + s)
                except: pass

    # Test 3: Append
    for label, fn, expected in [
        ("APPEND_UNFIXED", _run_append_unfixed, 10),
        ("APPEND_FIXED", _run_append_fixed, 10),
    ]:
        db = os.path.join(tempfile.gettempdir(), f"c6_{label}.db")
        try:
            _setup_db(db, initial_shares=0)
            _run_test(label, fn, db)
            actual = _read_history_count(db)
            ok = actual == expected
            results[label] = (actual, expected, ok)
            print(f"[{'OK' if ok else 'BUG'}] {label}: history_count={actual} (expected {expected})")
        finally:
            for s in ['', '-wal', '-shm']:
                try: os.unlink(db + s)
                except: pass

    # Summary
    print("\n=== SUMMARY ===")
    unfixed_bugs = 0
    fixed_bugs = 0
    for k, v in results.items():
        actual, expected, ok = v
        if k.endswith('_UNFIXED') and not ok:
            unfixed_bugs += 1
        elif k.endswith('_FIXED') and not ok:
            fixed_bugs += 1
    print(f"UNFIXED bugs demonstrated: {unfixed_bugs}/3")
    print(f"FIXED bugs remaining: {fixed_bugs}/3")

    if unfixed_bugs == 0:
        print("\nWARNING: unfixed code did NOT demonstrate the bug.")
        print("The race window may be too narrow. Try increasing NUM_THREADS or sleep time.")
    if fixed_bugs > 0:
        print("\nERROR: fixed code still has bugs!")
        sys.exit(1)

    print("\n=== ALL ASSERTIONS PASSED ===")


if __name__ == "__main__":
    main()
