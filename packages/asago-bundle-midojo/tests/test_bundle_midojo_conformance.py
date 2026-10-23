"""The MiDojo adapter passes the shared conformance round trip on every package shape."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from asago_bundle_core.conformance import AdapterUnderTest, check_round_trip
from asago_bundle_core.testing import MOCK_TARGET, sequential_stimulus, write_test_package
from asago_bundle_midojo.compiler import ENTRYPOINT_VALUES, compile_package
from asago_bundle_midojo.instantiate import instantiate_bundle
from asago_bundle_midojo.parse import parse_bundle
from conftest import (
    RECEIPT_SCHEMA,
    REPLY_VALUES,
    SEQUENTIAL_REPLY_VALUES,
    SEQUENTIAL_VALUES,
    VALUES,
    compile_options,
    reply_verdict_dump,
    turns_evaluation_dump,
    write_native,
)


def midojo() -> AdapterUnderTest:
    def write_run(bundle: Path, manifest: dict[str, Any]) -> None:
        reply = manifest["claim_level"] == "reply"
        sequential = manifest["delivery"] == "sequential"
        write_native(
            bundle,
            evaluation=turns_evaluation_dump() if sequential else None,
            verdict=reply_verdict_dump() if reply else None,
        )

    return AdapterUnderTest(
        compile=partial(compile_package, **compile_options()),
        instantiate=partial(instantiate_bundle, target=MOCK_TARGET),
        parse=lambda bundle, out: parse_bundle(bundle, out),
        write_native=write_run,
        entrypoint_keys=ENTRYPOINT_VALUES,
    )


def conforms(package: Path, tmp_path: Path, values: dict[str, Any] = VALUES) -> list[str]:
    return check_round_trip(midojo(), package, values, tmp_path / "work", RECEIPT_SCHEMA)


def test_a_command_attempt_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", scenario_id="SCN-001")

    assert conforms(package, tmp_path) == []


def test_a_sequential_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())

    assert conforms(package, tmp_path, SEQUENTIAL_VALUES) == []


def test_a_single_reply_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", claim_level="reply")

    assert conforms(package, tmp_path, REPLY_VALUES) == []


def test_a_sequential_reply_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(
        tmp_path / "packages", claim_level="reply", stimulus=sequential_stimulus()
    )

    assert conforms(package, tmp_path, SEQUENTIAL_REPLY_VALUES) == []


def test_a_package_the_adapter_cannot_grade_is_a_faithful_gap(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", claim_level="state_effect")

    assert conforms(package, tmp_path) == []
    assert not (tmp_path / "work" / "template").exists()
