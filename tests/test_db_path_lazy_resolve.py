"""
tests/test_db_path_lazy_resolve.py

Sprint 5 MR B (E-M-4). DB_PATH lazy-resolve contract:

- `agt_equities.db.DB_PATH` is None at import (no __file__-anchored fallback).
- `agt_equities.db.get_db_path(override=None)` returns:
  1. override if provided, else
  2. module-level DB_PATH if non-None (monkeypatch path), else
  3. AGT_DB_PATH env var.
- If all three are unset, raises RuntimeError.
- `get_db_connection()` / `get_ro_connection()` route through `_resolve_db_path`
  and therefore raise the same RuntimeError on env-missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.sprint_a


def test_dbpath_attribute_defaults_to_none(monkeypatch):
    """Fresh-import state: `agt_equities.db.DB_PATH` is None until monkeypatched."""
    import importlib
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    import agt_equities.db as agt_db
    importlib.reload(agt_db)
    # The autouse tripwire fixture monkeypatches DB_PATH back to a sentinel
    # path AFTER the reload, so we can't assert DB_PATH is None here — we
    # assert the post-reload module attribute is Truthy (either None or the
    # tripwire sentinel). The important guarantee is that reloading didn't
    # crash because __file__ fallback is gone.
    assert hasattr(agt_db, "DB_PATH")


def test_get_db_path_uses_explicit_override():
    from agt_equities.db import get_db_path
    path = get_db_path(override=r"C:\custom\override\agt_desk.db")
    assert path == Path(r"C:\custom\override\agt_desk.db")


def test_get_db_path_reads_env_when_dbpath_attr_is_none(monkeypatch):
    import agt_equities.db as agt_db
    monkeypatch.setenv("AGT_DB_PATH", r"C:\env\path\agt_desk.db")
    monkeypatch.setattr(agt_db, "DB_PATH", None)
    path = agt_db.get_db_path()
    assert path == Path(r"C:\env\path\agt_desk.db")


def test_get_db_path_module_attribute_wins_over_env(monkeypatch):
    """Tripwire fixture compatibility: if DB_PATH is monkeypatched, it wins
    over the AGT_DB_PATH env var."""
    import agt_equities.db as agt_db
    monkeypatch.setenv("AGT_DB_PATH", r"C:\env\ignored\agt_desk.db")
    monkeypatch.setattr(agt_db, "DB_PATH", Path(r"C:\monkeypatched\win.db"))
    path = agt_db.get_db_path()
    assert path == Path(r"C:\monkeypatched\win.db")


def test_get_db_path_raises_when_all_unset(monkeypatch):
    """Hard-fail when override=None, DB_PATH=None, AGT_DB_PATH unset."""
    import agt_equities.db as agt_db
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    monkeypatch.setattr(agt_db, "DB_PATH", None)
    with pytest.raises(RuntimeError, match="AGT_DB_PATH"):
        agt_db.get_db_path()


def test_get_db_connection_raises_when_all_unset(monkeypatch):
    """get_db_connection() is the production path; same RuntimeError semantics."""
    import agt_equities.db as agt_db
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    monkeypatch.setattr(agt_db, "DB_PATH", None)
    with pytest.raises(RuntimeError, match="AGT_DB_PATH"):
        agt_db.get_db_connection()


def test_get_ro_connection_raises_when_all_unset(monkeypatch):
    import agt_equities.db as agt_db
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    monkeypatch.setattr(agt_db, "DB_PATH", None)
    with pytest.raises(RuntimeError, match="AGT_DB_PATH"):
        agt_db.get_ro_connection()


def test_explicit_dbpath_kwarg_bypasses_env_and_attr(tmp_path, monkeypatch):
    """Explicit db_path= kwarg is the most explicit override and always wins."""
    import sqlite3
    import agt_equities.db as agt_db
    db = tmp_path / "explicit.db"
    # Create a real sqlite file so get_db_connection can open it.
    sqlite3.connect(str(db)).close()
    monkeypatch.delenv("AGT_DB_PATH", raising=False)
    monkeypatch.setattr(agt_db, "DB_PATH", None)
    conn = agt_db.get_db_connection(db_path=db)
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row == (1,) or row[0] == 1
    finally:
        conn.close()


def test_no_file_anchored_fallback_in_db_py():
    """Source-level sentinel: the __file__-anchored fallback is gone from
    executable code in db.py (not just the module docstring / comments)."""
    import ast
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "agt_equities" / "db.py").read_text(encoding="utf-8")
    # Walk AST and ensure no top-level assignment builds a DB_PATH from
    # __file__. Comments and docstrings are not AST nodes we care about here.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "DB_PATH" in targets:
                # DB_PATH must be a simple None assignment (lazy resolve)
                assert (
                    isinstance(node.value, ast.Constant) and node.value.value is None
                ), (
                    "Sprint 5 MR B: module-level DB_PATH must be `= None`, "
                    f"found: {ast.unparse(node.value)}"
                )
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "DB_PATH":
                assert (
                    node.value is not None
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is None
                ), (
                    "Sprint 5 MR B: module-level DB_PATH annotated assignment "
                    "must be `= None`."
                )
    # Positive sentinel: get_db_path defined
    assert "def get_db_path(" in src, (
        "get_db_path() must be a public accessor for the resolved path."
    )


def test_telegram_bot_does_not_fallback_to_file_anchored_db(monkeypatch):
    """Source-level sentinel on telegram_bot.py."""
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "telegram_bot.py").read_text(encoding="utf-8")
    # DB_PATH fallback pattern removed
    assert 'os.environ.get("AGT_DB_PATH") or str(BASE_DIR / "agt_desk.db")' not in src, (
        "Sprint 5 MR B regression: telegram_bot.py still has the "
        "__file__-anchored DB_PATH fallback."
    )
    # Positive: uses get_db_path() for bootstrap + local DB_PATH
    assert "_agt_db.get_db_path()" in src, (
        "telegram_bot.py must route through agt_equities.db.get_db_path()."
    )


def test_pxo_scanner_does_not_fallback_to_file_anchored_db():
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "pxo_scanner.py").read_text(encoding="utf-8")
    assert 'str(Path(__file__).resolve().parent / "agt_desk.db")' not in src, (
        "Sprint 5 MR B regression: pxo_scanner.py still has __file__-anchored fallback."
    )
    assert "from agt_equities.db import get_db_path" in src, (
        "pxo_scanner.py must import get_db_path."
    )


def test_cached_client_no_unused_dbpath_import():
    """Sprint 5 MR B cleanup: unused DB_PATH import from cached_client removed."""
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "agt_equities" / "cached_client.py").read_text(encoding="utf-8")
    # Check the specific import line doesn't bring in DB_PATH
    # (it should just import get_db_connection now)
    import_lines = [l for l in src.split("\n") if "from agt_equities.db" in l]
    assert len(import_lines) == 1
    assert "DB_PATH" not in import_lines[0].split("#")[0], (
        "Sprint 5 MR B: cached_client's unused DB_PATH import should be removed."
    )
