"""The package library owns package IO; the consumer's module re-exports the same objects.

The implementation lives in the ``asago-package`` workspace member, which imports nothing
from this application and declares only pydantic and jsonschema. The consumer keeps its old
import path as a re-export, and the three authority contract kits moved with the module, so
each one exists in exactly one place in this checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import asago_artifact_generator.metadata_policy as application_metadata_policy
import asago_artifact_generator.package_io as application_package_io
import asago_package.metadata_policy as package_metadata_policy
import asago_package.package_io as package_package_io
from asago_artifact_generator.contract_kit import contract_dir as application_contract_dir
from asago_package.contract_kit import contract_dir

_REPO = Path(__file__).resolve().parents[1]
_CONTRACTS = _REPO / "packages" / "asago-package" / "src" / "asago_package" / "contracts"
_MOVED = ("artifact-package", "runtime-contract", "capability-gap")


def test_the_consumer_module_reexports_the_whole_public_surface() -> None:
    assert application_package_io.__all__ == package_package_io.__all__


@pytest.mark.parametrize("name", package_package_io.__all__)
def test_the_consumer_module_reexports_the_same_object(name: str) -> None:
    assert getattr(application_package_io, name) is getattr(package_package_io, name)


def test_the_consumer_metadata_policy_reexports_the_same_walker() -> None:
    assert (
        application_metadata_policy.secret_metadata_paths
        is package_metadata_policy.secret_metadata_paths
    )


@pytest.mark.parametrize("name", _MOVED)
def test_each_moved_contract_kit_lives_only_in_the_package(name: str) -> None:
    assert contract_dir(name) == _CONTRACTS / name
    assert contract_dir(name).is_dir()
    assert not (_REPO / "contracts" / name).exists()


def test_the_consumer_keeps_its_own_contract_kits() -> None:
    assert application_contract_dir("scenario-handoff") == _REPO / "contracts" / "scenario-handoff"
    assert application_contract_dir("scenario-handoff").is_dir()
