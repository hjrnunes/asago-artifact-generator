"""The built wheels carry every contract, and an install from them loads each one.

The test builds the root wheel, the two members that ship contract data (``asago-package``
and ``asago-bundle-core``) and the producer packages they depend on into wheels from the
checkouts, installs them into a fresh environment and runs a probe from a directory
outside both checkouts in isolated mode. The probe reads each contract through the package
that ships it. The test then compares what the probe found, file by file, with the
maintained source: this repository's ``contracts/`` and ``asago-package`` data, and the
producer's ``data/contracts/`` for the scenario-handoff kit.

``asago-injection-spec`` and ``asago-bundle-garak`` ship no contract data of their own, and
both need ``midojo-spec``, whose packaging and installed-wheel test follow the pin matrix.
A wheel built from a checkout proves what the checkout packages. It does not prove what a
published wheel carries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DATA = REPO / "packages" / "asago-package" / "src" / "asago_package" / "contracts"
BUNDLE_DATA = REPO / "packages" / "asago-bundle-core" / "src" / "asago_bundle_core" / "contracts"
# The producer packages the installed wheels depend on, built from the checkout the
# repository's own path sources name.
PRODUCER_PACKAGES = ("asago-contracts-core", "asago-handoff", "asago-report-kit")

# Runs inside the installed environment and prints what the wheels carry.
PROBE = """
import hashlib
import json
from pathlib import Path

import asago_artifact_generator
import asago_bundle_core
import asago_contracts_core
import asago_package
from asago_artifact_generator.contract_kit import contract_dir as app_kit
from asago_bundle_core import bundle_contract, receipt_contract
from asago_package.contract_kit import contract_dir as package_kit
from asago_package.runtime_contract_spec import installed_specs as runtime_specs


def tree(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def specs(loaded):
    return sorted(f"{s.contract} {s.version}" for s in loaded.specs)


print(
    json.dumps(
        {
            "origins": {
                module.__name__: module.__file__
                for module in (
                    asago_artifact_generator,
                    asago_bundle_core,
                    asago_contracts_core,
                    asago_package,
                )
            },
            "app": tree(app_kit("kit").parent),
            "package": tree(package_kit("kit").parent),
            "bundle": tree(receipt_contract.CONTRACTS_DIR),
            "scenario-handoff": tree(asago_contracts_core.CONTRACT_DIR),
            "specs": {
                "receipt_contract": specs(receipt_contract.installed_specs()),
                "bundle_contract": specs(bundle_contract.installed_specs()),
                "runtime_contract_spec": specs(runtime_specs()),
            },
        }
    )
)
"""


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _names(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def _drift(source: dict[str, str], installed: dict[str, str]) -> list[str]:
    """Name every file the installed tree lacks, adds or changes against its source."""

    return sorted(
        [f"{name} is missing from the wheel" for name in source.keys() - installed.keys()]
        + [f"{name} is not in the source" for name in installed.keys() - source.keys()]
        + [
            f"{name} differs"
            for name in source.keys() & installed.keys()
            if source[name] != installed[name]
        ]
    )


def _assert_carries(label: str, source: dict[str, str], installed: dict[str, str]) -> None:
    drift = _drift(source, installed)

    assert not drift, f"{label}: {len(drift)} files differ, among them {'; '.join(drift[:5])}"


def _producer_package(name: str) -> Path:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return (REPO / pyproject["tool"]["uv"]["sources"][name]["path"]).resolve()


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    result = subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.fail(f"{' '.join(command)} exited {result.returncode}:\n{result.stderr}")
    return result.stdout


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    uv = shutil.which("uv")
    assert uv, "uv builds and installs the wheels; put it on PATH"
    projects = [
        REPO,
        REPO / "packages" / "asago-package",
        REPO / "packages" / "asago-bundle-core",
        *(_producer_package(name) for name in PRODUCER_PACKAGES),
    ]
    work = tmp_path_factory.mktemp("wheels")
    wheels, venv, outside = work / "wheels", work / "venv", work / "outside"
    outside.mkdir()
    for project in projects:
        _run([uv, "build", "--wheel", "--out-dir", str(wheels), str(project)])
    _run([uv, "venv", "--python", sys.executable, str(venv)])
    python = venv / "bin" / "python"
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            *map(str, sorted(wheels.glob("*.whl"))),
        ]
    )
    return json.loads(_run([str(python), "-I", "-c", PROBE], cwd=outside))


def test_the_install_reads_every_contract_from_outside_the_checkouts(
    installed: dict[str, Any],
) -> None:
    producer = _producer_package("asago-contracts-core").parents[1]
    for module, origin in installed["origins"].items():
        resolved = Path(origin).resolve()
        assert not resolved.is_relative_to(REPO.resolve()), f"{module} was read from {origin}"
        assert not resolved.is_relative_to(producer), f"{module} was read from {origin}"


def test_the_root_wheel_carries_every_root_kit_byte_for_byte(installed: dict[str, Any]) -> None:
    _assert_carries("root wheel, contracts", _tree(REPO / "contracts"), installed["app"])


def test_the_package_wheel_carries_its_kits_byte_for_byte(installed: dict[str, Any]) -> None:
    _assert_carries("asago-package wheel, contracts", _tree(PACKAGE_DATA), installed["package"])


def test_the_bundle_core_wheel_carries_the_root_kits_without_their_locks(
    installed: dict[str, Any],
) -> None:
    authority = {
        f"{name}/{relative}": digest
        for name in _names(BUNDLE_DATA)
        for relative, digest in _tree(REPO / "contracts" / name).items()
        if relative != "CONTRACT.lock"
    }

    _assert_carries("asago-bundle-core wheel, contracts", authority, installed["bundle"])


def test_the_contracts_core_wheel_carries_the_producers_scenario_handoff_kit(
    installed: dict[str, Any],
) -> None:
    producer = _producer_package("asago-contracts-core").parents[1]

    _assert_carries(
        "asago-contracts-core wheel, scenario-handoff",
        _tree(producer / "data" / "contracts" / "scenario-handoff"),
        installed["scenario-handoff"],
    )


def test_the_installed_packages_load_their_specs(installed: dict[str, Any]) -> None:
    both = ["execution-receipt execution-receipt-v1", "tool-bundle tool-bundle-v1"]

    assert installed["specs"] == {
        "receipt_contract": both,
        "bundle_contract": both,
        "runtime_contract_spec": ["runtime-contract artifact-runtime-contract-v1"],
    }


def test_a_missing_data_file_is_named() -> None:
    source = {"a/spec.json": "1", "a/schema.json": "2", "a/README.md": "3"}
    installed = {"a/spec.json": "1", "a/schema.json": "9", "a/extra.json": "4"}

    assert _drift(source, installed) == [
        "a/README.md is missing from the wheel",
        "a/extra.json is not in the source",
        "a/schema.json differs",
    ]
