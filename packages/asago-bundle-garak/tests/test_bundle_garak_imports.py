"""The adapter runs in the consumer's interpreter, so it must not import Garak."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "asago_bundle_garak"


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", sorted(SOURCE.rglob("*.py")), ids=lambda path: path.name)
def test_module_imports_no_garak(path: Path) -> None:
    assert "garak" not in imported_roots(path)


def test_scan_sees_a_garak_import(tmp_path: Path) -> None:
    module = tmp_path / "planted.py"
    module.write_text("from garak.attempt import Conversation\n", encoding="utf-8")

    assert "garak" in imported_roots(module)
