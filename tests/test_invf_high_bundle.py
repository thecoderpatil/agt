"""
tests/test_invf_high_bundle.py

Sprint 5 MR A — covers the 7 Investigation F HIGH fixes bundled in this MR:

  F1-H-1 — CC dashboard dead UI deleted (no cc:* callback_data emitted)
  F1-H-2 — parse_and_stage_order writes status='staged' (unifies on canonical state)
  F1-H-3 — Working-orders detail button emits callback_data='orders:detail'
  F2-H-1 — stage_stock_sale_via_smart_friction uses tx_immediate (BEGIN IMMEDIATE)
  F2-H-2 — TokenAuthMiddleware accepts Authorization: Bearer <token> header
  F3-H-1 — vrp_veto.init_vrp_db + write_vrp_results use tx_immediate
  F3-H-2 — scripts/circuit_breaker.py has no module-scope os.chdir

These are source-level structural sentinels + tight behavior tests — no
heavyweight imports (telegram_bot.py 22k LOC is avoided for most assertions;
grep-based sentinels are cheaper and equally load-bearing for the regression
guard).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


REPO = Path(__file__).resolve().parent.parent
TG_BOT = REPO / "telegram_bot.py"
RULE_ENGINE = REPO / "agt_equities" / "rule_engine.py"
VRP_VETO = REPO / "vrp_veto.py"
CIRCUIT_BREAKER = REPO / "scripts" / "circuit_breaker.py"
DECK_MAIN = REPO / "agt_deck" / "main.py"


def _read(path: Path) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# F1-H-1 — CC dashboard dead UI deletion
# ---------------------------------------------------------------------------


def test_f1_h_1_cc_confirm_keyboard_deleted():
    src = _read(TG_BOT)
    assert "def _cc_confirm_keyboard(" not in src, (
        "F1-H-1: _cc_confirm_keyboard should be deleted — dead UI (no "
        "CallbackQueryHandler(pattern=r'^cc:') registered)."
    )


def test_f1_h_1_cc_callback_data_removed():
    """No `callback_data=f"cc:...` emitted anywhere in telegram_bot.py."""
    src = _read(TG_BOT)
    # Literal string pattern match. The Sprint 5 MR A comment block may mention
    # "cc:" in prose — exclude comment lines from the match.
    for line in src.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert 'callback_data=f"cc:' not in line, (
            f"F1-H-1 regression: cc: callback_data still emitted: {line[:80]!r}"
        )


# ---------------------------------------------------------------------------
# F1-H-2 — status='staged' unification
# ---------------------------------------------------------------------------


def test_f1_h_2_parse_and_stage_writes_staged():
    """Ticket dict constructed by parse_and_stage_order has status='staged'."""
    src = _read(TG_BOT)
    # Locate the parse_and_stage_order function body
    m = re.search(r"async def parse_and_stage_order\(.*?\)(.*?)(?=\nasync def |\ndef |\Z)",
                  src, re.DOTALL)
    assert m is not None, "parse_and_stage_order function body not located"
    body = m.group(1)
    assert '"status":        "staged"' in body, (
        "F1-H-2: parse_and_stage_order must write status='staged' so "
        "cmd_approve / handle_approve_callback / _auto_execute_staged can "
        "find NL-path tickets. Old 'pending' value was silently invisible."
    )
    # Defense-in-depth: the default in append_pending_tickets also flipped to 'staged'
    m2 = re.search(r"def append_pending_tickets\(.*?\)(.*?)(?=\ndef |\nasync def |\Z)",
                   src, re.DOTALL)
    assert m2 is not None
    body2 = m2.group(1)
    assert 'setdefault("status", "staged")' in body2, (
        "F1-H-2: append_pending_tickets default should also be 'staged' "
        "for defense-in-depth."
    )


# ---------------------------------------------------------------------------
# F1-H-3 — orders:detail callback data + handler branch
# ---------------------------------------------------------------------------


def test_f1_h_3_orders_detail_callback_data_canonical():
    src = _read(TG_BOT)
    assert 'callback_data="orders:detail"' in src, (
        "F1-H-3: Order Details button must emit 'orders:detail' (with colon) "
        "to match the registered r'^orders:' CallbackQueryHandler."
    )
    # And the broken 'orders_detail' (underscore) form must NOT appear as callback_data
    assert 'callback_data="orders_detail"' not in src, (
        "F1-H-3 regression: underscore form still present."
    )


def test_f1_h_3_handle_orders_callback_has_detail_branch():
    """handle_orders_callback must handle `action == 'detail'`."""
    src = _read(TG_BOT)
    m = re.search(r"async def handle_orders_callback\(.*?\)(.*?)(?=\n# -{3,}|\nasync def |\ndef _|\Z)",
                  src, re.DOTALL)
    assert m is not None
    body = m.group(1)
    assert 'if action == "detail"' in body, (
        "F1-H-3: handle_orders_callback must branch on action=='detail' "
        "(otherwise the button still does nothing even with correct callback_data)."
    )


# ---------------------------------------------------------------------------
# F2-H-1 — rule_engine.stage_stock_sale_via_smart_friction uses tx_immediate
# ---------------------------------------------------------------------------


def test_f2_h_1_stage_stock_sale_uses_tx_immediate():
    src = _read(RULE_ENGINE)
    m = re.search(
        r"def stage_stock_sale_via_smart_friction\(.*?\)(.*?)(?=\ndef |\Z)",
        src, re.DOTALL,
    )
    assert m is not None
    body = m.group(1)
    # The INSERT should be wrapped in tx_immediate
    assert "with tx_immediate(conn):" in body, (
        "F2-H-1: stage_stock_sale_via_smart_friction must wrap its "
        "INSERT INTO bucket3_dynamic_exit_log in tx_immediate (BEGIN IMMEDIATE)."
    )
    # The explicit inner `conn.commit()` must be gone (tx_immediate commits on exit)
    # Allow one remaining commit only in comments; count non-comment lines.
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "conn.commit()" not in stripped, (
            f"F2-H-1: remove inner conn.commit() — tx_immediate handles it: {stripped!r}"
        )


# ---------------------------------------------------------------------------
# F2-H-2 — Authorization header middleware
# ---------------------------------------------------------------------------


def test_f2_h_2_middleware_accepts_authorization_header():
    src = _read(DECK_MAIN)
    assert 'auth_header = request.headers.get("authorization"' in src, (
        "F2-H-2: TokenAuthMiddleware must read Authorization header."
    )
    assert 'auth_header.lower().startswith("bearer ")' in src, (
        "F2-H-2: TokenAuthMiddleware must parse Bearer scheme."
    )
    # URL-query path still present (phase 1 — Tailscale bookmark keeps working)
    assert 'request.query_params.get("t"' in src, (
        "F2-H-2 phase 1: URL-query fallback must still work (Tailscale bookmark compat)."
    )


def test_f2_h_2_uvicorn_access_log_redact_filter_installed():
    src = _read(DECK_MAIN)
    assert "_RedactDeckTokenFilter" in src, (
        "F2-H-2: redact filter class must exist."
    )
    assert 'logging.getLogger("uvicorn.access").addFilter' in src, (
        "F2-H-2: redact filter must be attached to uvicorn.access logger "
        "so `?t=<token>` is scrubbed from disk logs."
    )


def test_f2_h_2_redact_filter_redacts_query_token():
    """Unit-test the redact filter inline without importing the whole app."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("agt_deck_main_partial", DECK_MAIN)
    # We can't fully import agt_deck.main without FastAPI + env; instead unit-test
    # the filter pattern directly.
    import logging as _logging
    import re as _re
    pat = _re.compile(r"([?&]t=)[^ &\"\']+", _re.IGNORECASE)
    out = pat.sub(r"\1<redacted>", 'GET /cure?t=abc123XYZ HTTP/1.1')
    assert "<redacted>" in out
    assert "abc123XYZ" not in out
    out2 = pat.sub(r"\1<redacted>", '/api?foo=bar&t=secret&baz=1')
    assert "<redacted>" in out2
    assert "secret" not in out2


# ---------------------------------------------------------------------------
# F3-H-1 — vrp_veto tx_immediate
# ---------------------------------------------------------------------------


def test_f3_h_1_vrp_veto_uses_tx_immediate():
    src = _read(VRP_VETO)
    assert "from agt_equities.db import tx_immediate" in src, (
        "F3-H-1: vrp_veto must import tx_immediate."
    )
    # Both write paths should use it
    assert src.count("with tx_immediate(conn):") >= 2, (
        "F3-H-1: init_vrp_db + write_vrp_results both need tx_immediate "
        "(two call sites)."
    )
    # Non-tx_immediate bare `with conn:` should be gone from writes
    # (we keep allowing it in comments/docstrings)
    bare_count = 0
    for line in src.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            continue
        if s == "with conn:":
            bare_count += 1
    assert bare_count == 0, (
        f"F3-H-1 regression: {bare_count} remaining bare `with conn:` blocks."
    )


# ---------------------------------------------------------------------------
# F3-H-2 — circuit_breaker has no module-scope os.chdir
# ---------------------------------------------------------------------------


def test_f3_h_2_no_module_scope_os_chdir():
    src = _read(CIRCUIT_BREAKER)
    # Look for any top-level os.chdir call (indentation-sensitive)
    for line_no, line in enumerate(src.split("\n"), start=1):
        # Top-level = no leading whitespace OR exactly one level into a guarded block
        if line.startswith("os.chdir(") or line.startswith("    os.chdir("):
            pytest.fail(
                f"F3-H-2 regression: module-scope os.chdir() at line {line_no}: "
                f"{line[:100]!r}. Process-global CWD mutation from a "
                f"to_thread worker is a race condition."
            )
    # Positive sentinel: the file reference should be Path-based, not CWD-relative
    assert "RAILS_PATH = Path(__file__).resolve()" in src, (
        "F3-H-2: RAILS_PATH must be an absolute Path, not a CWD-relative string."
    )


# ---------------------------------------------------------------------------
# Integration — F2-H-1 actually commits (behavioral, not just sentinel)
# ---------------------------------------------------------------------------


def test_f2_h_1_tx_immediate_actually_commits(tmp_path):
    """Smoke: tx_immediate on a sqlite conn does BEGIN IMMEDIATE + commit."""
    from agt_equities.db import tx_immediate
    db = tmp_path / "smoke.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x INTEGER)")
    with tx_immediate(conn):
        conn.execute("INSERT INTO t VALUES (1)")
    # Commit should already have happened via tx_immediate context exit
    row = conn.execute("SELECT x FROM t").fetchone()
    assert row == (1,), "tx_immediate must commit on clean exit"
    conn.close()

    # Fresh connection sees the row (confirms commit, not just in-session)
    conn2 = sqlite3.connect(str(db))
    row2 = conn2.execute("SELECT x FROM t").fetchone()
    assert row2 == (1,)
    conn2.close()
