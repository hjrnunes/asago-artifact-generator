"""The three authority contract kits ride inside the library as package data.

Each kit keeps its lock and byte-identical files, so the orchestrator's mirror check and the
consumer's readers see the same digests they saw at the repository root.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import asago_package
from asago_package.contract_kit import contract_dir, verify_contract_lock
from asago_package.package_io import (
    _ALLOWED_MEMBER_NAMES,
    PACKAGE_SCHEMA_VERSION,
    PackageIntegrityError,
    validate_artifact_package_contract,
)

CONTRACTS = Path(asago_package.__file__).resolve().parent / "contracts"
LOCKS: dict[str, dict[str, str]] = {
    "artifact-package": {
        "authority": "asago-artifact-generator",
        "contract": "artifact-package",
        "digest_domain": PACKAGE_SCHEMA_VERSION,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
    },
    "runtime-contract": {
        "authority": "asago-artifact-generator",
        "contract": "runtime-contract",
        "schema_version": "artifact-runtime-contract-v3",
    },
    "capability-gap": {
        "authority": "asago-artifact-generator",
        "contract": "capability-gap",
        "schema_version": "capability-gap-v1",
    },
}


def _verify(root: Path, name: str) -> None:
    verify_contract_lock(
        root,
        PackageIntegrityError,
        lock_label=f"{name} contract lock",
        member_label=f"{name} contract",
        metadata=LOCKS[name],
        metadata_message=f"{name} contract lock metadata is invalid",
    )


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_each_authority_contract_is_package_data_with_a_verified_lock(name: str) -> None:
    root = contract_dir(name)

    assert root.parent == CONTRACTS
    assert (root / "CONTRACT.lock").is_file()
    _verify(root, name)


def test_the_writer_verifies_the_artifact_package_lock() -> None:
    validate_artifact_package_contract()


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_a_tampered_contract_file_fails_its_lock(name: str, tmp_path: Path) -> None:
    copy = tmp_path / name
    shutil.copytree(contract_dir(name), copy)
    lock = json.loads((copy / "CONTRACT.lock").read_text(encoding="utf-8"))
    relative = sorted(lock["files"])[0]
    (copy / relative).write_text("{}\n", encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="digest mismatch"):
        _verify(copy, name)


def test_a_contract_whose_lock_names_another_contract_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "artifact-package"
    shutil.copytree(contract_dir("artifact-package"), copy)
    lock_path = copy / "CONTRACT.lock"
    lock: dict[str, Any] = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["authority"] = "another-repository"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="authority mismatch|metadata is invalid"):
        _verify(copy, "artifact-package")


def test_the_v5_schema_lists_exactly_the_member_paths_the_module_writes() -> None:
    schema = json.loads(
        (contract_dir("artifact-package") / PACKAGE_SCHEMA_VERSION / "schema.json").read_text(
            encoding="utf-8"
        )
    )

    paths = schema["$defs"]["member"]["properties"]["path"]

    assert set(paths["enum"]) == _ALLOWED_MEMBER_NAMES
