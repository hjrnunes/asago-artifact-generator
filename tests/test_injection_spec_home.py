"""The injection-spec library owns the compiler, the document and the sealed readers.

The implementation lives in the ``asago-injection-spec`` workspace member. ``bundle-midojo``
and ``bundle-garak`` import it directly, and ``bundle-midojo`` reads the judge-turn constants
from the member; ``bundle-garak`` declares the member instead of ``bundle-midojo``. The one
remaining reference to the adapter in Garak's tests is the marked parity test, which reads one
seal through both parsers.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

import asago_injection_spec

_REPO = Path(__file__).resolve().parents[1]
_PACKAGES = _REPO / "packages"
_MIDOJO_SOURCE = _PACKAGES / "asago-bundle-midojo" / "src" / "asago_bundle_midojo"
_GARAK_SOURCE = _PACKAGES / "asago-bundle-garak" / "src" / "asago_bundle_garak"
_GARAK_TESTS = _PACKAGES / "asago-bundle-garak" / "tests"


def _project(package: str) -> dict[str, object]:
    return tomllib.loads((_PACKAGES / package / "pyproject.toml").read_text(encoding="utf-8"))


def _dependency_names(package: str) -> set[str]:
    project = _project(package)["project"]
    assert isinstance(project, dict)
    return {
        re.split(r"[<>=!~\s\[]", requirement)[0].lower() for requirement in project["dependencies"]
    }


def test_the_injection_spec_is_a_workspace_member() -> None:
    project = _project("asago-injection-spec")
    workspace = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "uv"
    ]["workspace"]

    assert project["project"] == {
        "name": "asago-injection-spec",
        "version": "0.1.0",
        "description": (
            "The injection-spec compiler: a planted_item artifact package becomes one "
            "MiDojo injection spec, and its sealed result reads back as evidence"
        ),
        "requires-python": ">=3.12",
        "dependencies": [
            "asago-bundle-core",
            "asago-contracts-core>=0.1,<0.2",
            "asago-handoff>=0.1,<0.2",
            "asago-package>=0.1,<0.2",
            "pyyaml>=6.0",
            "midojo-sdk",
        ],
    }
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/asago_injection_spec"
    ]
    assert "packages/*" in workspace["members"]
    assert (_PACKAGES / "asago-injection-spec" / "src" / "asago_injection_spec").is_dir()


@pytest.mark.parametrize("name", ("FINAL_TURN", "JUDGE_TURNS", "TIMEOUT_SECONDS", "judged_count"))
def test_the_package_root_exports_the_judge_turn_vocabulary(name: str) -> None:
    assert getattr(asago_injection_spec, name) is getattr(asago_injection_spec.judge_turns, name)


def _imported_from(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def test_the_judge_service_reads_the_judge_turn_constants_from_the_member() -> None:
    names = _imported_from(_GARAK_SOURCE / "judge_service.py", "asago_injection_spec")

    assert names >= {"FINAL_TURN", "JUDGE_TURNS", "TIMEOUT_SECONDS", "judged_count"}


def test_the_judge_service_binds_the_members_constants() -> None:
    from asago_bundle_garak import judge_service

    assert judge_service.FINAL_TURN is asago_injection_spec.FINAL_TURN
    assert judge_service.JUDGE_TURNS is asago_injection_spec.JUDGE_TURNS
    assert judge_service.TIMEOUT_SECONDS is asago_injection_spec.TIMEOUT_SECONDS
    assert judge_service.judged_count is asago_injection_spec.judged_count


def test_the_garak_adapter_names_no_midojo_adapter_module() -> None:
    naming = sorted(
        path.relative_to(_REPO).as_posix()
        for path in _GARAK_SOURCE.rglob("*.py")
        if "asago_bundle_midojo" in path.read_text(encoding="utf-8")
    )

    assert naming == []


def test_the_garak_adapter_declares_the_injection_spec_not_the_midojo_adapter() -> None:
    project = _project("asago-bundle-garak")
    sources = project["tool"]["uv"]["sources"]

    assert _dependency_names("asago-bundle-garak") >= {
        "asago-bundle-core",
        "asago-injection-spec",
        "asago-package",
    }
    assert "asago-bundle-midojo" not in _dependency_names("asago-bundle-garak")
    assert "asago-bundle-midojo" not in sources
    assert sources["asago-injection-spec"] == {"workspace": True}


def test_only_the_parity_test_reads_the_midojo_adapter() -> None:
    naming = sorted(
        path.name
        for path in _GARAK_TESTS.glob("*.py")
        if "asago_bundle_midojo" in path.read_text(encoding="utf-8")
    )

    assert naming == ["test_bundle_garak_parity.py"]


def test_the_parity_test_is_marked_and_the_marker_is_registered() -> None:
    parity = (_GARAK_TESTS / "test_bundle_garak_parity.py").read_text(encoding="utf-8")
    settings = _project("asago-bundle-garak")["tool"]["pytest"]["ini_options"]
    markers = settings["markers"]

    assert "pytest.mark.parity" in parity
    assert any(marker.startswith("parity:") for marker in markers)
