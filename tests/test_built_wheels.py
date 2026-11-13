"""The built wheels carry every contract, and an install from them loads each one.

The test builds the root wheel, the four members it ships (``asago-package``,
``asago-bundle-core``, ``asago-injection-spec`` and ``asago-bundle-garak``) and the producer
packages they depend on into wheels from the checkouts. It installs them into a fresh
environment together with ``midojo-spec``, which uv builds from the revision that
``pyproject.toml`` pins, and runs a probe from a directory outside both checkouts in
isolated mode. The probe reads each contract through the package that ships it and loads
the injection-spec document v1 schema from the installed ``midojo-spec``. The test then
compares what the probe found, file by file, with the maintained source: this repository's
``contracts/`` and ``asago-package`` data, and the producer's ``data/contracts/`` for the
scenario-handoff kit. It also checks the installed schema against the pinned copies of
MiDojo's injection-spec documents.

``asago-injection-spec`` and ``asago-bundle-garak`` ship no contract data of their own; the
probe imports both from the install, which shows that their modules install and import
beside the other wheels. It does not show that either manifest declares its dependencies,
because the test installs every wheel itself. A wheel built from a checkout proves what the
checkout packages. It does not prove what a published wheel carries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DATA = REPO / "packages" / "asago-package" / "src" / "asago_package" / "contracts"
BUNDLE_DATA = REPO / "packages" / "asago-bundle-core" / "src" / "asago_bundle_core" / "contracts"
# The producer packages the installed wheels depend on, built from the checkout the
# repository's own path sources name.
PRODUCER_PACKAGES = ("asago-contracts-core", "asago-handoff", "asago-report-kit")
# The members the root wheel does not carry, built from this repository.
MEMBERS = ("asago-package", "asago-bundle-core", "asago-injection-spec", "asago-bundle-garak")
# MiDojo's injection-spec documents, pinned byte for byte beside the injection-spec library.
VECTORS = (
    REPO
    / "packages"
    / "asago-injection-spec"
    / "tests"
    / "fixtures"
    / "midojo"
    / "injection_spec_document"
)
# Refused documents the schema accepts: MiDojo's own reader refuses them by a rule the
# schema does not state, so the installed contracts cannot.
SEMANTIC_REFUSALS = frozenset({"refused_openshell_predicate_on_unmanaged.json"})

# Runs inside the installed environment and prints what the wheels carry.
PROBE = """
import hashlib
import json
import sys
from importlib import metadata
from pathlib import Path

if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2])

import asago_artifact_generator
import asago_bundle_core
import asago_bundle_garak.cli
import asago_contracts_core
import asago_injection_spec
import asago_package
import jsonschema
import midojo_spec
from asago_artifact_generator.contract_kit import contract_dir as app_kit
from asago_bundle_core import bundle_contract, receipt_contract
from asago_package.contract_kit import contract_dir as package_kit
from asago_package.runtime_contract_spec import installed_specs as runtime_specs
from midojo_spec.injection_spec_schema import SCHEMA_FILE, load_schema


def tree(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def specs(loaded):
    return sorted(f"{s.contract} {s.version}" for s in loaded.specs)


def direct_url():
    text = metadata.distribution("midojo-spec").read_text("direct_url.json")
    return json.loads(text) if text else None


def verdicts(directory):
    schema = load_schema()
    found = {}
    for path in sorted(directory.glob("*.json")):
        try:
            jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), schema)
        except jsonschema.ValidationError as error:
            found[path.name] = error.message
        else:
            found[path.name] = None
    return found


print(
    json.dumps(
        {
            "origins": {
                module.__name__: module.__file__
                for module in (
                    asago_artifact_generator,
                    asago_bundle_core,
                    asago_bundle_garak,
                    asago_contracts_core,
                    asago_injection_spec,
                    asago_package,
                    midojo_spec,
                )
            },
            "midojo-spec": {
                "direct_url": direct_url(),
                "schema_file": SCHEMA_FILE,
                "verdicts": verdicts(Path(sys.argv[1])),
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


def _midojo_spec_pin() -> dict[str, str]:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["uv"]["sources"]["midojo-spec"]


def _midojo_spec_requirement() -> str:
    """The requirement that has uv build midojo-spec at the revision the repository pins."""

    pin = _midojo_spec_pin()
    return f"midojo-spec @ git+{pin['git']}@{pin['rev']}#subdirectory={pin['subdirectory']}"


def _attempt(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    return subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
    )


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    result = _attempt(command, cwd=cwd)
    if result.returncode != 0:
        pytest.fail(f"{' '.join(command)} exited {result.returncode}:\n{result.stderr}")
    return result.stdout


@dataclass(frozen=True)
class Environment:
    """The fresh environment the wheels are installed into, and where its probe runs."""

    python: Path
    outside: Path


def _probe_command(environment: Environment, *, first_on_path: Path | None = None) -> list[str]:
    """The probe, in isolated mode; ``first_on_path`` puts a directory ahead of the install."""

    command = [str(environment.python), "-I", "-c", PROBE, str(VECTORS)]
    if first_on_path is not None:
        command.append(str(first_on_path))
    return command


@pytest.fixture(scope="module")
def environment(tmp_path_factory: pytest.TempPathFactory) -> Environment:
    uv = shutil.which("uv")
    assert uv, "uv builds and installs the wheels; put it on PATH"
    projects = [
        REPO,
        *(REPO / "packages" / name for name in MEMBERS),
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
            _midojo_spec_requirement(),
        ]
    )
    return Environment(python=python, outside=outside)


@pytest.fixture(scope="module")
def installed(environment: Environment) -> dict[str, Any]:
    return json.loads(_run(_probe_command(environment), cwd=environment.outside))


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


def test_midojo_spec_is_installed_from_the_revision_the_repository_pins(
    installed: dict[str, Any],
) -> None:
    pin = _midojo_spec_pin()
    direct_url = installed["midojo-spec"]["direct_url"]

    assert direct_url, "midojo-spec was not installed from a git source"
    assert direct_url["vcs_info"]["vcs"] == "git"
    assert direct_url["vcs_info"]["commit_id"] == pin["rev"]
    assert direct_url["subdirectory"] == pin["subdirectory"]


def test_the_installed_schema_reads_the_pinned_midojo_documents(installed: dict[str, Any]) -> None:
    verdicts = installed["midojo-spec"]["verdicts"]
    names = sorted(path.name for path in VECTORS.glob("*.json"))
    kinds = {name.split("_", 1)[0] for name in names}

    assert sorted(verdicts) == names
    assert kinds == {"v1", "refused"}, f"the pinned documents are of the kinds {sorted(kinds)}"
    refused = [name for name in names if name.startswith("v1_") and verdicts[name] is not None]
    accepted = [
        name
        for name in names
        if name.startswith("refused_") and name not in SEMANTIC_REFUSALS and verdicts[name] is None
    ]
    assert not refused, f"the installed schema refuses {refused}"
    assert not accepted, f"the installed schema accepts {accepted}"


def test_a_removed_midojo_spec_data_file_is_named(
    environment: Environment, installed: dict[str, Any], tmp_path: Path
) -> None:
    """The install's own ``midojo_spec`` is copied; one copy goes first on the path intact."""

    schema = installed["midojo-spec"]["schema_file"]
    package = tmp_path / "midojo_spec"
    shutil.copytree(
        Path(installed["origins"]["midojo_spec"]).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    command = _probe_command(environment, first_on_path=tmp_path)

    intact = _attempt(command, cwd=environment.outside)
    (package / schema).unlink()
    damaged = _attempt(command, cwd=environment.outside)

    assert intact.returncode == 0, intact.stderr
    assert damaged.returncode != 0
    assert Path(schema).name in damaged.stderr


def test_a_missing_data_file_is_named() -> None:
    source = {"a/spec.json": "1", "a/schema.json": "2", "a/README.md": "3"}
    installed = {"a/spec.json": "1", "a/schema.json": "9", "a/extra.json": "4"}

    assert _drift(source, installed) == [
        "a/README.md is missing from the wheel",
        "a/extra.json is not in the source",
        "a/schema.json differs",
    ]
