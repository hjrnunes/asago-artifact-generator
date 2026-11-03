"""The three adapters run without the authoring application.

``bundle-core``, ``bundle-garak`` and ``bundle-midojo`` read the package values, the
binding rules and the condition evaluator from the ``asago-package`` member. Each
adapter declares that library rather than this application, names no
``asago_artifact_generator`` module in its sources, and CI installs the adapter alone
and proves the isolated environment cannot import the authoring application.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_PACKAGES = _REPO / "packages"
_ADAPTERS = ("asago-bundle-core", "asago-bundle-garak", "asago-bundle-midojo")
_APPLICATION = "asago-artifact-generator"
_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"


def _project(adapter: str) -> dict[str, Any]:
    text = (_PACKAGES / adapter / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def _dependency_names(adapter: str) -> list[str]:
    return [
        re.split(r"[<>=!~\s\[]", requirement)[0].lower()
        for requirement in _project(adapter)["project"]["dependencies"]
    ]


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_the_adapter_declares_the_package_library_not_the_application(adapter: str) -> None:
    names = _dependency_names(adapter)

    assert "asago-package" in names
    assert _APPLICATION not in names


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_the_adapter_pyproject_does_not_name_the_application(adapter: str) -> None:
    text = (_PACKAGES / adapter / "pyproject.toml").read_text(encoding="utf-8")

    assert _APPLICATION not in text


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_the_adapter_sources_import_no_authoring_application_module(adapter: str) -> None:
    naming = [
        path.relative_to(_REPO).as_posix()
        for path in (_PACKAGES / adapter / "src").rglob("*.py")
        if "asago_artifact_generator" in path.read_text(encoding="utf-8")
    ]

    assert naming == []


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _isolation_libraries() -> dict[str, str]:
    """Return ``{adapter: libraries}`` from the CI isolation matrix entries."""

    libraries: dict[str, str] = {}
    for job in _workflow()["jobs"].values():
        include = job.get("strategy", {}).get("matrix", {}).get("include", [])
        for entry in include:
            adapter = entry.get("adapter")
            extra = entry.get("libraries")
            if isinstance(adapter, str) and isinstance(extra, str):
                libraries[adapter] = extra
    return libraries


def _isolation_steps() -> list[str]:
    return [
        step["run"]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if "--isolated" in step.get("run", "")
    ]


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_ci_installs_the_adapter_without_the_application(adapter: str) -> None:
    libraries = _isolation_libraries().get(adapter)

    assert libraries is not None, f"CI has no isolation matrix entry for {adapter}"
    assert _APPLICATION not in libraries
    assert "./packages/asago-package" in libraries


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_ci_runs_the_adapter_tests_in_an_isolated_environment(adapter: str) -> None:
    steps = _isolation_steps()
    running = [
        step
        for step in steps
        if "--with ./packages/${{ matrix.adapter }}" in step
        and "pytest packages/${{ matrix.adapter }}/tests" in step
    ]

    assert running, f"CI has no isolated test run for {adapter}"
    assert all("--no-project" in step for step in running)
    assert all(_APPLICATION not in step for step in steps)


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_ci_proves_the_isolated_environment_cannot_import_the_application(
    adapter: str,
) -> None:
    steps = _isolation_steps()

    assert any("import asago_artifact_generator" in step for step in steps)
    assert all("uv run" in step for step in steps)
