"""The MiDojo adapter passes the shared conformance round trip on every package shape."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.conformance import AdapterUnderTest, check_round_trip
from asago_bundle_core.testing import sequential_stimulus, write_test_package
from asago_bundle_midojo.compiler import ENTRYPOINT_VALUES, compile_package
from asago_bundle_midojo.instantiate import instantiate_bundle
from asago_bundle_midojo.parse import parse_bundle
from conftest import RECEIPT_SCHEMA, VALUES, write_native


def midojo() -> AdapterUnderTest:
    def write_run(bundle: Path, manifest: dict[str, Any]) -> None:
        write_native(bundle)

    return AdapterUnderTest(
        compile=compile_package,
        instantiate=instantiate_bundle,
        parse=lambda bundle, out: parse_bundle(bundle, out),
        write_native=write_run,
        entrypoint_keys=ENTRYPOINT_VALUES,
    )


def conforms(package: Path, tmp_path: Path) -> list[str]:
    return check_round_trip(midojo(), package, VALUES, tmp_path / "work", RECEIPT_SCHEMA)


def test_a_command_attempt_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", scenario_id="SCN-001")

    assert conforms(package, tmp_path) == []


def test_a_sequential_package_is_a_faithful_gap(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())

    assert conforms(package, tmp_path) == []
    assert not (tmp_path / "work" / "template").exists()


@pytest.mark.parametrize("claim_level", ["reply", "state_effect"])
def test_a_package_the_adapter_cannot_grade_is_a_faithful_gap(
    tmp_path: Path, claim_level: str
) -> None:
    package = write_test_package(tmp_path / "packages", claim_level=claim_level)

    assert conforms(package, tmp_path) == []
    assert not (tmp_path / "work" / "template").exists()
