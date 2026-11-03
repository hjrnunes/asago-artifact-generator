"""Every workspace member runs without the authoring application.

``bundle-core``, ``bundle-garak``, ``bundle-midojo`` and ``asago-injection-spec`` read the
package values, the binding rules and the condition evaluator from the ``asago-package``
member. Each adapter declares that library rather than this application, names no
``asago_artifact_generator`` module in its sources, and CI installs the adapter alone
and proves the isolated environment cannot import the authoring application. The
injection-spec library is neither adapter's dependency: ``bundle-garak`` declares it, and
the parity test that reads a seal through ``bundle-midojo`` too is marked and deselected
in the isolated run.
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
_MEMBERS = tuple(sorted(path.parent.name for path in _PACKAGES.glob("*/pyproject.toml")))
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


def _isolation_matrix() -> dict[str, Any]:
    """Return the isolation job's matrix."""

    for job in _workflow()["jobs"].values():
        matrix = job.get("strategy", {}).get("matrix", {})
        if any(entry.get("libraries") for entry in matrix.get("include", [])):
            return matrix
    raise AssertionError("CI has no adapter-isolation matrix")


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


def test_ci_isolates_every_workspace_member() -> None:
    # asago-package is the shared library every entry installs, not a member with its own run.
    isolated = tuple(member for member in _MEMBERS if member != "asago-package")

    assert tuple(sorted(_isolation_libraries())) == isolated


def test_the_garak_isolation_environment_omits_the_midojo_adapter() -> None:
    libraries = _isolation_libraries()["asago-bundle-garak"]

    assert "./packages/asago-injection-spec" in libraries
    assert "asago-bundle-midojo" not in libraries


def test_the_isolated_garak_run_deselects_the_marked_parity_test() -> None:
    entries = [
        entry
        for entry in _isolation_matrix()["include"]
        if entry.get("adapter") == "asago-bundle-garak"
    ]

    assert entries, "CI has no isolation matrix entry for asago-bundle-garak"
    assert "not parity" in entries[0]["pytest_args"]
    assert any("matrix.pytest_args" in step for step in _isolation_steps())


def test_ci_runs_the_isolation_matrix_on_every_linted_python() -> None:
    linted = _workflow()["jobs"]["lint-test"]["strategy"]["matrix"]["python-version"]

    assert _isolation_matrix()["python-version"] == linted
