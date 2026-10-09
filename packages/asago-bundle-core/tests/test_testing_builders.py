"""The package builders adapters use in their tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.package_io import load_package
from asago_bundle_core.testing import (
    CONDITION,
    JUDGE,
    sequential_stimulus,
    single_stimulus,
    write_test_package,
)


def member(package_dir: Path, name: str) -> dict:
    return json.loads(load_package(package_dir).members[name])


def test_a_command_package_carries_the_condition_and_no_judge(tmp_path: Path) -> None:
    root = write_test_package(tmp_path)

    package = load_package(root)

    assert package.manifest.scenario_id == "SCN-001"
    assert package.manifest.package_id == "SCN-001-SCN-001"
    assert member(root, "tool_call_condition.json") == CONDITION
    assert member(root, "plan.json")["observation_claim"]["claim_level"] == "command_attempt"
    assert "judge.json" not in package.members


def test_a_reply_package_carries_the_judge_and_no_condition(tmp_path: Path) -> None:
    root = write_test_package(tmp_path, claim_level="reply")

    package = load_package(root)

    assert member(root, "judge.json") == JUDGE
    assert "tool_call_condition.json" not in package.members


def test_the_scenario_id_names_the_package_directory(tmp_path: Path) -> None:
    root = write_test_package(tmp_path, scenario_id="SCN-007")

    assert root.name == "SCN-007"
    assert load_package(root).manifest.scenario_id == "SCN-007"


def test_a_given_stimulus_replaces_the_default(tmp_path: Path) -> None:
    root = write_test_package(tmp_path, stimulus=sequential_stimulus())

    assert member(root, "stimulus.json")["mode"] == "sequential"


@pytest.mark.parametrize("stimulus", [single_stimulus(), sequential_stimulus()])
def test_the_stimuli_end_with_the_final_user_text(stimulus: dict) -> None:
    assert stimulus["user_text"] == "Show PAT-201."


def test_a_given_condition_replaces_the_default(tmp_path: Path) -> None:
    other = {"comparisons": []}

    root = write_test_package(tmp_path, condition=other)

    assert member(root, "tool_call_condition.json") == other
