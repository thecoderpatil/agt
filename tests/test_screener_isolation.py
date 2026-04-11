"""
tests/test_screener_isolation.py

LOAD-BEARING SAFETY CONTRACT for the Act 60 Fortress CSP Screener.

The screener is a read-only side project. It MUST NOT import, reference, or
call any execution-path symbol from telegram_bot.py, the V2 router, or the
pre-trade gate machinery. This test walks every .py file under
agt_equities/screener/ and asserts via AST inspection that none of them
contain a forbidden identifier as an Import, ImportFrom, Name, or Attribute
node.

Forbidden identifiers (any usage = test failure):
  - _pre_trade_gates
  - placeOrder
  - assert_execution_enabled
  - execution_gate
  - _HALTED
  - _scan_and_stage_defensive_rolls
  - append_pending_tickets
  - _log_cc_cycle

Forbidden modules (any import = test failure):
  - telegram_bot
  - ib_async        (Phase 5 chain_walker.py is the ONE exception — see below)
  - agt_equities.rule_engine

Architectural rule: only `agt_equities/screener/chain_walker.py` is allowed
to import ib_async (the C5 commit will introduce that file). Until C5 lands,
no file in the screener package may import ib_async at all. The whitelist
mechanism below is forward-compatible: it permits chain_walker.py to import
ib_async once it exists, while keeping every other file blocked.

This test runs on every commit. If the screener package grows to N files,
all N must pass.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENER_DIR = REPO_ROOT / "agt_equities" / "screener"

# Forbidden bare identifiers (matched against ast.Name.id and ast.Attribute.attr)
FORBIDDEN_IDENTIFIERS: frozenset[str] = frozenset({
    "_pre_trade_gates",
    "placeOrder",
    "assert_execution_enabled",
    "execution_gate",
    "_HALTED",
    "_scan_and_stage_defensive_rolls",
    "append_pending_tickets",
    "_log_cc_cycle",
})

# Forbidden module imports (matched against ast.Import / ast.ImportFrom names)
FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "telegram_bot",
    "agt_equities.rule_engine",
    "agt_equities.order_state",
    "agt_equities.mode_engine",
})

# ib_async whitelist: two files are allowed to import ib_async.
#
# vol_event_armor.py (Phase 4): needs reqHistoricalDataAsync with
#   whatToShow="OPTION_IMPLIED_VOLATILITY" for the IVR gate. Added
#   C4 after the IBKR subscription probe verified Option D viability
#   on 2026-04-11 (AAPL/MSFT/SPY all returned 249-250 bars with zero
#   subscription errors).
#
# chain_walker.py (Phase 5): will need full option chain access via
#   reqSecDefOptParams + reqMktData. Added pre-emptively in C1 before
#   Phase 5 was scoped. Not yet implemented.
#
# Every OTHER file in the screener package remains blocked from
# importing ib_async. The guard test below will fail if this
# whitelist is bypassed by any other file.
IBKR_WHITELIST: frozenset[str] = frozenset({
    "chain_walker.py",
    "vol_event_armor.py",
})


def _all_screener_py_files() -> list[Path]:
    """Return every .py file under agt_equities/screener/ (recursive)."""
    if not SCREENER_DIR.exists():
        return []
    return sorted(SCREENER_DIR.rglob("*.py"))


def _check_file(path: Path) -> list[str]:
    """Return a list of violations in the given file. Empty list = clean."""
    violations: list[str] = []
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path.name}: cannot read file: {exc}"]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path.name}: syntax error: {exc}"]

    relname = path.name

    for node in ast.walk(tree):
        # ImportFrom: `from telegram_bot import X` / `from agt_equities.rule_engine import Y`
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # Block by module
            if module in FORBIDDEN_MODULES:
                violations.append(
                    f"{relname}:{node.lineno} forbidden module import: from {module} import ..."
                )
            # Block by ib_async (Phase 5 whitelist)
            if module == "ib_async" and relname not in IBKR_WHITELIST:
                violations.append(
                    f"{relname}:{node.lineno} ib_async import not whitelisted "
                    f"(only {sorted(IBKR_WHITELIST)} may import ib_async)"
                )
            # Block by forbidden identifier on the import alias
            for alias in node.names:
                if alias.name in FORBIDDEN_IDENTIFIERS:
                    violations.append(
                        f"{relname}:{node.lineno} forbidden symbol import: "
                        f"from {module} import {alias.name}"
                    )

        # Import: `import telegram_bot` / `import ib_async`
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    violations.append(
                        f"{relname}:{node.lineno} forbidden module import: import {alias.name}"
                    )
                if alias.name == "ib_async" and relname not in IBKR_WHITELIST:
                    violations.append(
                        f"{relname}:{node.lineno} ib_async import not whitelisted"
                    )

        # Name reference: `_pre_trade_gates(...)` etc.
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_IDENTIFIERS:
                violations.append(
                    f"{relname}:{node.lineno} forbidden identifier reference: {node.id}"
                )

        # Attribute access: `telegram_bot._pre_trade_gates` / `mod.placeOrder`
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_IDENTIFIERS:
                violations.append(
                    f"{relname}:{node.lineno} forbidden attribute access: .{node.attr}"
                )

    return violations


def test_screener_directory_exists():
    """Sanity check: the screener package exists and has at least __init__.py."""
    assert SCREENER_DIR.exists(), (
        f"agt_equities/screener/ does not exist at {SCREENER_DIR}"
    )
    init_py = SCREENER_DIR / "__init__.py"
    assert init_py.exists(), f"agt_equities/screener/__init__.py missing"


def test_screener_no_forbidden_imports_or_references():
    """AST guard: no .py file under screener/ may import or reference any
    execution-path symbol or module.
    """
    py_files = _all_screener_py_files()
    assert len(py_files) >= 1, "No .py files found under agt_equities/screener/"

    all_violations: list[str] = []
    for path in py_files:
        all_violations.extend(_check_file(path))

    if all_violations:
        msg = "Screener isolation violations:\n  " + "\n  ".join(all_violations)
        pytest.fail(msg)


def test_screener_universe_csv_present():
    """The static universe seed CSV must exist and have a sensible row count."""
    csv_path = SCREENER_DIR / "sp500_nasdaq100.csv"
    assert csv_path.exists(), f"Universe seed CSV missing: {csv_path}"

    lines = csv_path.read_text(encoding="utf-8").splitlines()
    # Header + ~520 rows expected (S&P 500 ~503 + ~13 NDX-only after dedup)
    data_rows = len(lines) - 1
    assert 480 <= data_rows <= 560, (
        f"Universe CSV row count {data_rows} outside expected band [480, 560]. "
        "Either the source ETF holdings drifted, or the dedup logic broke."
    )

    # Header must contain the canonical columns
    header = lines[0]
    for col in ("ticker", "name", "sector", "source"):
        assert col in header, f"Universe CSV header missing column: {col}"
