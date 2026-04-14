"""CI compliance: ast.parse every .py file, fail on syntax error."""
from __future__ import annotations
import ast
import pathlib
import sys

SKIP_DIRS = {"venv", "__pycache__", "node_modules", ".venv"}

def main() -> int:
    fails: list[str] = []
    count = 0
    for p in pathlib.Path(".").rglob("*.py"):
        if any(part.startswith(".") for part in p.parts):
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        count += 1
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            fails.append(f"{p}: {e}")
    if fails:
        for f in fails:
            print(f, file=sys.stderr)
        return 1
    print(f"AST OK across {count} .py files")
    return 0

if __name__ == "__main__":
    sys.exit(main())
