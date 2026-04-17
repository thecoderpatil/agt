"""
Tests for scripts/precommit_loc_gate.py — LOC-delta pre-commit gate.

Covers the 10 scenarios from loc_delta_gate_dispatch_20260417.md:
  1. Expectation parse happy path
  2. Malformed YAML → GateError raised
  3. diff_stats accurate on +/- mixed content
  4. Allowed shrinkage (within tolerance) passes evaluate_file
  5. Undeclared shrinkage > tolerance fails evaluate_file
  6. Declared shrinking: clause unblocks shrinkage
  7. Missing required symbol fails evaluate_file
  8. All required symbols present passes evaluate_file
  9. Multi-file expectation block (per-file deltas) passes main()
 10. AST walk collects def + class names from truncated-but-valid file
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from precommit_loc_gate import (
    GateError,
    collect_top_level_symbols,
    diff_stats,
    evaluate_file,
    main,
    parse_dispatch_expectation,
    FileExpectation,
    ShrinkingClause,
)

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dispatch_with_block(block: str, tmp_path: Path) -> Path:
    """Write a dispatch markdown file containing the given expected_delta block.

    Fences must be at column 0 for EXPECTED_DELTA_FENCE_RE to match.
    """
    content = "# Test Dispatch\n\nSome preamble.\n\n```yaml expected_delta\n" + block + "\n```\n"
    p = tmp_path / "test_dispatch.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Scenario 1 — Expectation parse happy path
# ---------------------------------------------------------------------------

def test_parse_happy_path(tmp_path):
    block = textwrap.dedent("""\
        files:
          agt_equities/foo.py:
            added: 50
            removed: 5
            net: 45
            tolerance: 10
            required_symbols:
              - my_func
              - MyClass
            required_sentinels:
              - "SENTINEL_VALUE"
        shrinking: []
    """)
    dispatch = _dispatch_with_block(block, tmp_path)
    exp = parse_dispatch_expectation(dispatch)
    assert "agt_equities/foo.py" in exp.files
    fe = exp.files["agt_equities/foo.py"]
    assert fe.added == 50
    assert fe.removed == 5
    assert fe.net == 45
    assert fe.tolerance == 10
    assert fe.required_symbols == ["my_func", "MyClass"]
    assert fe.required_sentinels == ["SENTINEL_VALUE"]
    assert exp.shrinking == {}


# ---------------------------------------------------------------------------
# Scenario 2 — Malformed YAML → GateError raised
# ---------------------------------------------------------------------------

def test_parse_malformed_yaml_raises(tmp_path):
    block = "files:\n  foo: [unclosed bracket"
    dispatch = _dispatch_with_block(block, tmp_path)
    with pytest.raises(GateError, match="YAML parse failed"):
        parse_dispatch_expectation(dispatch)


def test_parse_missing_block_raises(tmp_path):
    p = tmp_path / "no_block.md"
    p.write_text("# Dispatch\n\nNo expected_delta block here.\n", encoding="utf-8")
    with pytest.raises(GateError, match="missing required"):
        parse_dispatch_expectation(p)


def test_parse_missing_files_key_raises(tmp_path):
    block = "shrinking: []"
    dispatch = _dispatch_with_block(block, tmp_path)
    with pytest.raises(GateError, match="missing `files:`"):
        parse_dispatch_expectation(dispatch)


def test_parse_dispatch_not_found_raises(tmp_path):
    with pytest.raises(GateError, match="not found"):
        parse_dispatch_expectation(tmp_path / "nonexistent.md")


# ---------------------------------------------------------------------------
# Scenario 3 — diff_stats accurate on +/- mixed content
# ---------------------------------------------------------------------------

def test_diff_stats_pure_addition():
    old = "line1\nline2\n"
    new = "line1\nline2\nline3\nline4\n"
    added, removed = diff_stats(old, new)
    assert added == 2
    assert removed == 0


def test_diff_stats_pure_deletion():
    old = "line1\nline2\nline3\n"
    new = "line1\n"
    added, removed = diff_stats(old, new)
    assert added == 0
    assert removed == 2


def test_diff_stats_mixed():
    old = "a\nb\nc\n"
    new = "a\nX\nY\nZ\n"
    added, removed = diff_stats(old, new)
    net = added - removed
    # b and c removed (2), X Y Z added (3) → net +1
    assert net == 1
    assert added >= 1
    assert removed >= 1


def test_diff_stats_empty_to_content():
    added, removed = diff_stats("", "line1\nline2\n")
    assert added == 2
    assert removed == 0


def test_diff_stats_identical():
    text = "line1\nline2\n"
    added, removed = diff_stats(text, text)
    assert added == 0
    assert removed == 0


# ---------------------------------------------------------------------------
# Scenario 4 — Allowed shrinkage (< tolerance) passes
# ---------------------------------------------------------------------------

def test_evaluate_file_small_shrinkage_passes():
    # net declared +100, tolerance 10 — actual net +95 is within tolerance
    fe = FileExpectation(
        path="foo.py", added=100, removed=0, net=100, tolerance=10,
        required_symbols=[], required_sentinels=[],
    )
    old = "\n".join(f"line{i}" for i in range(200)) + "\n"
    new = "\n".join(f"line{i}" for i in range(295)) + "\n"  # +95 net ≈ within ±10
    # Use simple texts to get deterministic net
    old_t = "x\n" * 10
    new_t = "x\n" * 105  # net +95
    failures = evaluate_file(fe, old_t, new_t, shrinking=None)
    assert failures == []


# ---------------------------------------------------------------------------
# Scenario 5 — Undeclared shrinkage > tolerance fails
# ---------------------------------------------------------------------------

def test_evaluate_file_undeclared_shrinkage_fails():
    # declared net=+50, tolerance=5 — actual net=-10 → undeclared shrinkage
    fe = FileExpectation(
        path="foo.py", added=50, removed=0, net=50, tolerance=5,
        required_symbols=[], required_sentinels=[],
    )
    old_t = "x\n" * 100
    new_t = "x\n" * 90  # net -10
    failures = evaluate_file(fe, old_t, new_t, shrinking=None)
    assert len(failures) == 1
    assert "undeclared shrinkage" in failures[0]
    assert "shrinking:" in failures[0]


# ---------------------------------------------------------------------------
# Scenario 6 — Declared shrinking: clause unblocks shrinkage
# ---------------------------------------------------------------------------

def test_evaluate_file_shrinking_clause_unblocks():
    fe = FileExpectation(
        path="foo.py", added=0, removed=50, net=-50, tolerance=5,
        required_symbols=[], required_sentinels=[],
    )
    shrink = ShrinkingClause(file="foo.py", reason="Refactor consolidated helpers", expected_net=-50)
    old_t = "x\n" * 100
    new_t = "x\n" * 50  # net -50
    failures = evaluate_file(fe, old_t, new_t, shrinking=shrink)
    assert failures == []


def test_evaluate_file_shrinking_clause_mismatch_fails():
    fe = FileExpectation(
        path="foo.py", added=0, removed=50, net=-50, tolerance=5,
        required_symbols=[], required_sentinels=[],
    )
    # declared expected_net=-50 but actual net=-10 (only minor shrinkage)
    shrink = ShrinkingClause(file="foo.py", reason="Refactor", expected_net=-50)
    old_t = "x\n" * 100
    new_t = "x\n" * 90  # net -10, not -50
    failures = evaluate_file(fe, old_t, new_t, shrinking=shrink)
    assert len(failures) == 1
    assert "shrinking clause mismatch" in failures[0]


# ---------------------------------------------------------------------------
# Scenario 7 — Missing required symbol fails
# ---------------------------------------------------------------------------

def test_evaluate_file_missing_required_symbol_fails():
    fe = FileExpectation(
        path="foo.py", added=5, removed=0, net=5, tolerance=10,
        required_symbols=["missing_func"],
        required_sentinels=[],
    )
    old_t = ""
    new_t = "def present_func():\n    pass\n" * 6
    failures = evaluate_file(fe, old_t, new_t, shrinking=None)
    assert any("required symbols missing" in f for f in failures)
    assert any("missing_func" in f for f in failures)


# ---------------------------------------------------------------------------
# Scenario 8 — All required symbols present passes
# ---------------------------------------------------------------------------

def test_evaluate_file_all_symbols_present_passes():
    fe = FileExpectation(
        path="foo.py", added=10, removed=0, net=10, tolerance=10,
        required_symbols=["my_func", "MyClass", "CONST"],
        required_sentinels=["SENTINEL_STR"],
    )
    old_t = ""
    new_t = textwrap.dedent("""\
        CONST = 42
        SENTINEL_STR = "marker"

        def my_func():
            pass

        class MyClass:
            pass
    """) + "\n" * 10
    failures = evaluate_file(fe, old_t, new_t, shrinking=None)
    assert failures == []


# ---------------------------------------------------------------------------
# Scenario 9 — Multi-file expectation block: main() end-to-end pass
# ---------------------------------------------------------------------------

def test_main_multifile_pass(tmp_path, monkeypatch):
    block = textwrap.dedent("""\
        files:
          scripts/tool.py:
            added: 5
            removed: 0
            net: 5
            tolerance: 10
            required_symbols:
              - run
            required_sentinels:
              - "GATE PASS"
          tests/test_tool.py:
            added: 3
            removed: 0
            net: 3
            tolerance: 10
            required_symbols: []
            required_sentinels:
              - "sprint_a"
    """)
    dispatch = _dispatch_with_block(block, tmp_path)

    # Use relative paths to avoid Windows drive-letter colon ambiguity in
    # the staged argument parser (tok.split(":", 1) splits on first colon).
    monkeypatch.chdir(tmp_path)
    Path("tool.py").write_text(
        "# GATE PASS marker\n\ndef run():\n    pass\n" + "\n" * 5,
        encoding="utf-8",
    )
    Path("test_tool.py").write_text(
        "sprint_a = True\n\ndef test_run():\n    pass\n",
        encoding="utf-8",
    )
    origin_cache = tmp_path / "origin_cache"
    origin_cache.mkdir()

    sys.argv = [
        "precommit_loc_gate.py",
        "--dispatch", str(dispatch),
        "--staged", "tool.py:scripts/tool.py,test_tool.py:tests/test_tool.py",
        "--origin-cache", str(origin_cache),
    ]
    rc = main()
    assert rc == 0


def test_main_extra_staged_file_fails(tmp_path, monkeypatch):
    block = textwrap.dedent("""\
        files:
          scripts/tool.py:
            added: 5
            removed: 0
            net: 5
            tolerance: 10
    """)
    dispatch = _dispatch_with_block(block, tmp_path)
    monkeypatch.chdir(tmp_path)
    Path("tool.py").write_text("def run(): pass\n" * 6, encoding="utf-8")
    Path("extra.py").write_text("pass\n", encoding="utf-8")
    origin_cache = tmp_path / "origin_cache"
    origin_cache.mkdir()

    sys.argv = [
        "precommit_loc_gate.py",
        "--dispatch", str(dispatch),
        "--staged", "tool.py:scripts/tool.py,extra.py:scripts/extra.py",
        "--origin-cache", str(origin_cache),
    ]
    rc = main()
    assert rc != 0


# ---------------------------------------------------------------------------
# Scenario 10 — AST walk collects def + class names from truncated-but-valid file
# ---------------------------------------------------------------------------

def test_collect_symbols_truncated_valid_file():
    # Simulates f4def9f scenario: valid Python but loop body missing.
    # Top-level defs still present — AST walk must find them.
    source = textwrap.dedent("""\
        EXCLUDED_TICKERS = {"SPY", "QQQ"}

        async def scan_csp_harvest_candidates(ib, acct_id):
            pass  # loop body truncated but file is valid Python

        def _should_harvest_csp(credit, ask, dte, days_held=1):
            return False

        def _lookup_days_held(acct_id, ticker, strike, exp, today):
            return -1
    """)
    symbols = collect_top_level_symbols(source)
    assert "scan_csp_harvest_candidates" in symbols
    assert "_should_harvest_csp" in symbols
    assert "_lookup_days_held" in symbols
    assert "EXCLUDED_TICKERS" in symbols


def test_collect_symbols_syntax_error_returns_empty():
    symbols = collect_top_level_symbols("def broken(\n    pass")
    assert symbols == set()


def test_collect_symbols_class_captured():
    source = textwrap.dedent("""\
        class MyGate:
            def method(self):
                pass

        def helper():
            pass
    """)
    symbols = collect_top_level_symbols(source)
    assert "MyGate" in symbols
    assert "helper" in symbols
    assert "method" not in symbols  # nested — not top-level
