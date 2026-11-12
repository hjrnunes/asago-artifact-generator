"""The handoff contract lives in the shared core; the consumer keeps no copy of it.

The producer's ``data/contracts/scenario-handoff/`` is the authority and
``scripts/sync_handoff_package_data.py`` copies it byte for byte into
``asago_contracts_core``'s package data, so the input adapter reads
``asago_contracts_core.CONTRACT_DIR`` and needs no repository mirror. The consumer still
holds two kinds of root kit: the three contracts it owns (``execution-receipt``,
``runtime-context`` and ``tool-bundle``), and the producer mirrors no
package ships yet (``model-profile``, ``target-profile`` and ``tool-call-condition``).
Each mirror is the producer's kit byte for byte: the producer's ``data/contracts/`` is the
only maintained source, and a test names every file that differs.

Every dependency on an Asago package from another repository names a version range, so an
installed environment cannot drift outside the version this repository supports.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import asago_contracts_core
import pytest

from asago_artifact_generator import input_adapter

_REPO = Path(__file__).resolve().parents[1]
_CONTRACTS = _REPO / "contracts"
# The contracts this repository owns and publishes from its root kit.
_AUTHORITIES = ("execution-receipt", "runtime-context", "tool-bundle")
# The producer contracts the consumer mirrors because no package ships them yet.
_MIRRORS = ("model-profile", "target-profile", "tool-call-condition")
# The Asago packages that live in another repository.
_CROSS_REPOSITORY = (
    "asago-contracts-core",
    "asago-handoff",
    "asago-injection-spec",
    "asago-package",
    "asago-report-kit",
    "asago-target-discovery",
)
_PYPROJECTS = (_REPO / "pyproject.toml", *sorted((_REPO / "packages").glob("*/pyproject.toml")))


def test_the_handoff_kit_lives_only_in_the_shared_core() -> None:
    contract = asago_contracts_core.CONTRACT_DIR

    assert input_adapter._HANDOFF_ROOT == contract
    assert (contract / "CONTRACT.lock").is_file()
    assert not (_CONTRACTS / "scenario-handoff").exists()


def test_the_consumer_holds_its_own_kits_and_the_mirrors_no_package_ships() -> None:
    names = {path.name for path in _CONTRACTS.iterdir() if path.is_dir()}

    assert names == set(_AUTHORITIES) | set(_MIRRORS)
    for name in (*_AUTHORITIES, *_MIRRORS):
        assert (_CONTRACTS / name / "CONTRACT.lock").is_file(), name


def _tree_drift(copy: Path, source: Path) -> list[str]:
    """Name every file the copy lacks, adds or changes against its source."""

    def files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    wanted, held = files(source), files(copy)
    return sorted(
        [f"{name} is missing from the copy" for name in wanted.keys() - held.keys()]
        + [f"{name} is not in the source" for name in held.keys() - wanted.keys()]
        + [f"{name} differs" for name in wanted.keys() & held.keys() if wanted[name] != held[name]]
    )


@pytest.mark.parametrize("name", _MIRRORS)
def test_each_mirror_is_the_producers_kit_byte_for_byte(name: str) -> None:
    paths = pytest.importorskip(
        "asago_scenario_generator.data.paths",
        reason=(
            "requires the producer checkout ../asago-scenario-generator and the "
            "producer-pair dependency group (uv sync --locked --group producer-pair)"
        ),
    )
    authority = paths.DATA_ROOT / "contracts" / name

    assert authority.is_dir(), f"the producer holds no {name} kit at {authority}"
    assert _tree_drift(_CONTRACTS / name, authority) == []


def test_a_mirror_that_differs_is_named_file_by_file(tmp_path: Path) -> None:
    source, copy = tmp_path / "source", tmp_path / "copy"
    for root in (source, copy):
        (root / "v1").mkdir(parents=True)
    (source / "CONTRACT.lock").write_text("lock")
    (source / "v1" / "schema.json").write_text("{}")
    (source / "v1" / "README.md").write_text("docs")
    (copy / "v1" / "schema.json").write_text('{"changed": 1}')
    (copy / "v1" / "README.md").write_text("docs")
    (copy / "v1" / "stray.json").write_text("{}")

    assert _tree_drift(copy, source) == [
        "CONTRACT.lock is missing from the copy",
        "v1/schema.json differs",
        "v1/stray.json is not in the source",
    ]


def test_every_cross_repository_asago_dependency_declares_a_version_range() -> None:
    checked: list[tuple[Path, str]] = []
    for path in _PYPROJECTS:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        project = document.get("project", {})
        groups = [project.get("dependencies", [])]
        groups.extend(project.get("optional-dependencies", {}).values())
        groups.extend(document.get("dependency-groups", {}).values())
        for group in groups:
            for requirement in group:
                name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
                if name in _CROSS_REPOSITORY:
                    checked.append((path, requirement))
                    assert ">=" in requirement and "<" in requirement, (path, requirement)

    assert len(checked) >= 10


def _requirement_names(document: dict) -> dict[str, str]:
    """Return the package's requirements by name, across every dependency group."""
    project = document.get("project", {})
    groups = [project.get("dependencies", [])]
    groups.extend(project.get("optional-dependencies", {}).values())
    groups.extend(document.get("dependency-groups", {}).values())
    return {
        re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower(): requirement
        for group in groups
        for requirement in group
    }


def _imports_core(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "asago_contracts_core" for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").split(".")[0] == "asago_contracts_core"
        ):
            return True
    return False


def test_every_package_that_imports_the_contracts_core_declares_it() -> None:
    missing: list[str] = []
    for path in _PYPROJECTS:
        package = path.parent
        sources = package / "src"
        if not sources.is_dir():
            continue
        users = [module for module in sorted(sources.rglob("*.py")) if _imports_core(module)]
        if not users:
            continue
        requirement = _requirement_names(tomllib.loads(path.read_text(encoding="utf-8"))).get(
            "asago-contracts-core", ""
        )
        if ">=" not in requirement or "<" not in requirement:
            names = ", ".join(str(user.relative_to(package)) for user in users)
            missing.append(f"{package.name} (imports in {names})")

    assert missing == []


def test_the_scan_finds_the_adapters_core_imports() -> None:
    names = {
        path.name
        for path in (_REPO / "packages" / "asago-bundle-core" / "src").rglob("*.py")
        if _imports_core(path)
    }

    assert names
