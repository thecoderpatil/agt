"""
tests/test_sprint5_mrd_bundle.py

Sprint 5 MR D — MED + LOW bundle. Reduced scope vs dispatch (6 of 11 fixes
ship this sprint; 5 punt to Sprint 6 per timebox discipline). Shipped:

  F1-L-1 — _check_and_track_tokens(0,0) skips DB write (no api_calls inflation)
  F2-M-1 — SSE error event sanitized (no str(exc) leaked to browser)
  F2-M-2 — _vix_cache + _spot_cache lock-guarded
  F3-M-1 — pxo_scanner._load_scan_universe logs exception (not silent pass)
  F3-L-1 — yf.option_chain wrapped with 5s timeout

Punted to Sprint 6:
  F1-M-1 (LLM max_rounds user message), F1-M-2 (Gate-1/2 in NL staging —
  invasive), F1-M-3 (update_live_order ACTIVE_ACCOUNTS check — caller audit),
  F2-L-1 (CSRF, depends on F2-H-2 phase 2), F2-L-2 (Windows ACL on .deck_token).
  F3-M-2 (vrp_veto __file__ fallback) — folded into MR B partially; vrp has
  its OWN _VRP_DB_PATH (separate DB file), out of scope for MR B.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


REPO = Path(__file__).resolve().parent.parent
TG_BOT = REPO / "telegram_bot.py"
PXO = REPO / "pxo_scanner.py"
DECK = REPO / "agt_deck" / "main.py"


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# F1-L-1 — _check_and_track_tokens skips DB on (0, 0)
# ---------------------------------------------------------------------------


def test_f1_l_1_check_and_track_tokens_skips_dbwrite_on_zero():
    src = _read(TG_BOT)
    m = re.search(
        r"def _check_and_track_tokens\(.*?\)(.*?)(?=\ndef |\Z)",
        src, re.DOTALL,
    )
    assert m is not None
    body = m.group(1)
    assert "Sprint 5 MR D F1-L-1" in body, "F1-L-1 sentinel comment missing"
    assert "if input_tokens == 0 and output_tokens == 0:" in body, (
        "F1-L-1: must early-return when both token counts are zero."
    )
    # The early return must precede the INSERT INTO api_usage
    early_ret = body.find("if input_tokens == 0 and output_tokens == 0:")
    insert = body.find("INSERT INTO api_usage")
    assert early_ret < insert, (
        "F1-L-1: the zero-guard must run BEFORE the DB write so the insert "
        "is skipped — not after."
    )


# ---------------------------------------------------------------------------
# F2-M-1 — SSE error no longer leaks str(exc)
# ---------------------------------------------------------------------------


def test_f2_m_1_sse_error_event_is_opaque():
    src = _read(DECK)
    # Find the SSE streaming block
    assert "Sprint 5 MR D F2-M-1" in src, "F2-M-1 sentinel comment missing"
    # 'internal_error' code is shipped
    assert "'error': 'internal_error'" in src, (
        "F2-M-1: SSE error must stream opaque 'internal_error' not str(exc)."
    )
    # Literal `str(exc)` in the SSE yield is gone
    sse_yields = [
        line for line in src.split("\n")
        if "event: error" in line and "yield" in line
    ]
    for line in sse_yields:
        assert "str(exc)" not in line, (
            f"F2-M-1 regression: SSE yield still contains str(exc): {line[:120]!r}"
        )


# ---------------------------------------------------------------------------
# F2-M-2 — cache locks on _vix_cache + _spot_cache
# ---------------------------------------------------------------------------


def test_f2_m_2_vix_cache_lock_guarded():
    src = _read(DECK)
    assert "_VIX_CACHE_LOCK" in src, "F2-M-2: _VIX_CACHE_LOCK must exist."
    assert "_VIX_CACHE_LOCK = _threading_mod.Lock()" in src
    # get_vix must acquire the lock at least twice (read check + write-back)
    m = re.search(r"def get_vix\(.*?\)(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    assert m is not None
    body = m.group(1)
    assert body.count("with _VIX_CACHE_LOCK:") >= 2, (
        "F2-M-2: get_vix must acquire the lock on both read-check and "
        "write-back — 2+ `with _VIX_CACHE_LOCK:` blocks."
    )


def test_f2_m_2_spot_cache_lock_guarded():
    src = _read(DECK)
    assert "_SPOT_CACHE_LOCK" in src
    m = re.search(r"def get_spots\(.*?\)(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    assert m is not None
    body = m.group(1)
    assert body.count("with _SPOT_CACHE_LOCK:") >= 2, (
        "F2-M-2: get_spots must acquire the lock on cache-read and write-back."
    )


# ---------------------------------------------------------------------------
# F3-M-1 — pxo_scanner silent exception replaced with logger.exception
# ---------------------------------------------------------------------------


def test_f3_m_1_load_scan_universe_logs_on_db_failure():
    src = _read(PXO)
    m = re.search(
        r"def _load_scan_universe\(.*?\)(.*?)(?=\ndef |\Z)",
        src, re.DOTALL,
    )
    assert m is not None
    body = m.group(1)
    assert "logger.exception" in body, (
        "F3-M-1: DB-read failure must log exception with traceback, not silent pass."
    )
    assert "Sprint 5 MR D F3-M-1" in body, "F3-M-1 sentinel missing."
    # The bare `except Exception: pass` must be gone
    assert "except Exception:" not in body or "except Exception as exc:" in body, (
        "F3-M-1: must bind exception and log, not silently pass."
    )


# ---------------------------------------------------------------------------
# F3-L-1 — option_chain wrapped with 5s timeout
# ---------------------------------------------------------------------------


def test_f3_l_1_option_chain_has_timeout():
    src = _read(PXO)
    m = re.search(
        r"def scan_single_ticker\(.*?\)(.*?)(?=\ndef |\Z)",
        src, re.DOTALL,
    )
    assert m is not None
    body = m.group(1)
    assert "Sprint 5 MR D F3-L-1" in body, "F3-L-1 sentinel missing."
    # ThreadPoolExecutor + timeout on the option_chain call
    assert "option_chain" in body
    assert ".result(timeout=5.0)" in body, (
        "F3-L-1: option_chain must be wrapped in a 5-second timeout "
        "(ThreadPoolExecutor.submit + .result(timeout=5.0))."
    )
    assert "FuturesTimeout" in body, (
        "F3-L-1: must catch concurrent.futures.TimeoutError."
    )
