"""compile: what MiDojo cannot deliver is a capability gap, written before anything else."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.conformance import GAP_FIELDS, check_gap
from asago_bundle_core.gap import CapabilityGap
from asago_bundle_core.testing import sequential_stimulus, single_stimulus, write_test_package
from asago_bundle_midojo.compiler import CompileError
from asago_bundle_midojo.compiler import compile_package as _compile
from conftest import compile_options


def compile_package(package: Path, out: Path) -> dict[str, Any]:
    return _compile(package, out, **compile_options())


def gap_of(package: Path, tmp_path: Path) -> dict[str, Any]:
    with pytest.raises(CapabilityGap) as raised:
        compile_package(package, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert check_gap(raised.value.record, package) == []
    return raised.value.record


def test_a_sequential_package_compiles(sequential_package: Path, tmp_path: Path) -> None:
    manifest = compile_package(sequential_package, tmp_path / "out")

    assert manifest["delivery"] == "sequential"


def test_a_sequential_package_with_a_non_user_message_is_a_gap(tmp_path: Path) -> None:
    stimulus = {
        **sequential_stimulus(),
        "history": [
            {"role": "user", "content": "Hello."},
            {"role": "assistant", "content": "Hi."},
        ],
        "turn_count": 3,
    }
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)

    record = gap_of(package, tmp_path)

    assert set(record) == GAP_FIELDS
    assert record["tool"] == "midojo"
    assert record["delivery"] == "sequential"
    assert "non-user message" in record["reason"]


@pytest.mark.parametrize("stated", [3, 1, None, "2"])
def test_a_sequential_turn_count_that_disagrees_is_a_compile_error(
    tmp_path: Path, stated: Any
) -> None:
    package = write_test_package(
        tmp_path / "packages", stimulus={**sequential_stimulus(), "turn_count": stated}
    )

    with pytest.raises(CompileError, match="turn_count"):
        compile_package(package, tmp_path / "out")


def test_a_single_package_with_a_non_user_message_is_a_gap(tmp_path: Path) -> None:
    stimulus = {**single_stimulus(), "history": [{"role": "assistant", "content": "Earlier."}]}
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)

    record = gap_of(package, tmp_path)

    assert record["delivery"] == "single"
    assert "non-user message" in record["reason"]


def test_a_single_package_with_earlier_user_turns_is_a_gap(tmp_path: Path) -> None:
    stimulus = {**single_stimulus(), "history": [{"role": "user", "content": "Hello."}]}
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)

    record = gap_of(package, tmp_path)

    assert record["delivery"] == "single"
    assert "one prompt" in record["reason"]
    assert "own turn" in record["reason"]
    assert "non-user" not in record["reason"]


def test_a_reply_package_with_sequential_turns_compiles(tmp_path: Path) -> None:
    package = write_test_package(
        tmp_path / "packages", claim_level="reply", stimulus=sequential_stimulus()
    )

    manifest = compile_package(package, tmp_path / "out")

    assert (manifest["claim_level"], manifest["delivery"]) == ("reply", "sequential")


def test_a_reply_package_with_earlier_user_turns_is_a_gap_for_the_turns_alone(
    tmp_path: Path,
) -> None:
    stimulus = {**single_stimulus(), "history": [{"role": "user", "content": "Hello."}]}
    package = write_test_package(tmp_path / "packages", claim_level="reply", stimulus=stimulus)

    record = gap_of(package, tmp_path)

    assert record["delivery"] == "single"
    assert "rubric" not in record["reason"]


def test_a_state_effect_package_is_a_gap(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", claim_level="state_effect")

    record = gap_of(package, tmp_path)

    assert "state_effect" in record["reason"]


@pytest.mark.parametrize("claim", [None, "tool_call", "", 3])
def test_an_unknown_claim_level_is_a_compile_error(tmp_path: Path, claim: Any) -> None:
    package = write_test_package(tmp_path / "packages")
    plan = package / "plan.json"
    plan.write_text(json.dumps({"observation_claim": {"claim_level": claim}}), encoding="utf-8")

    with pytest.raises(CompileError):
        compile_package(package, tmp_path / "out")
