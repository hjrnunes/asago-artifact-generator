"""The injection-spec library stands alone: six runtime dependencies, no adapter, no app."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import asago_injection_spec

PACKAGE = Path(asago_injection_spec.__file__).resolve().parent
PROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
APPLICATION = "asago_artifact_generator"
ADAPTERS = ("asago_bundle_garak",)


def test_no_module_in_the_library_imports_an_adapter_or_the_application() -> None:
    forbidden = (APPLICATION, *ADAPTERS)
    offenders = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in forbidden)
    ]

    assert offenders == []


def test_the_library_declares_only_its_runtime_dependencies() -> None:
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))["project"]

    assert project["name"] == "asago-injection-spec"
    assert {re.split(r"[<>=!~\s]", dependency)[0] for dependency in project["dependencies"]} == {
        "asago-bundle-core",
        "asago-contracts-core",
        "asago-handoff",
        "asago-package",
        "midojo-spec",
        "pyyaml",
    }
