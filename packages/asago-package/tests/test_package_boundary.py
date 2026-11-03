"""The library stands alone: it declares two runtime dependencies and imports no application."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import asago_package

PACKAGE = Path(asago_package.__file__).resolve().parent
PROJECT = PACKAGE.parents[1] / "pyproject.toml"


def test_no_module_in_the_library_imports_the_authoring_application() -> None:
    offenders = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "asago_artifact_generator" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_the_library_declares_only_its_two_runtime_dependencies() -> None:
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))["project"]

    assert project["name"] == "asago-package"
    assert {re.split(r"[<>=!~\s]", dependency)[0] for dependency in project["dependencies"]} == {
        "jsonschema",
        "pydantic",
    }
