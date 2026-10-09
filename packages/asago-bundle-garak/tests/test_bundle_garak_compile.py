"""compile: a package directory becomes a bundle template and its manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.package_io import load_package
from asago_bundle_core.schema import validate_manifest
from asago_bundle_garak.cli import main
from asago_bundle_garak.compiler import (
    CapabilityGap,
    CompileError,
    compile_package,
)
from asago_bundle_garak.slots import template_slots
from conftest import CONDITION, JUDGE, sequential_stimulus, write_test_package


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_command_attempt_bundle_manifest(command_package: Path, tmp_path: Path) -> None:
    out = tmp_path / "template"

    manifest = compile_package(command_package, out)

    assert manifest == read(out / "bundle.json")
    assert manifest["schema_version"] == "tool-bundle-v1"
    assert manifest["tool"] == "garak"
    assert manifest["tool_revision_range"] == ">=968cc224"
    assert manifest["package_id"] == "SCN-001-SCN-001"
    assert manifest["scenario_id"] == "SCN-001"
    assert manifest["package_digest"] == load_package(command_package).manifest.manifest_digest
    assert manifest["target_mode"] == "orch_hosted"
    assert manifest["claim_level"] == "command_attempt"
    assert manifest["delivery"] == "single"
    assert manifest["plugins"] == ["injection.IndirectInjection", "toolcall.ToolCallCondition"]
    assert manifest["requires"] == [
        "allowed_tools",
        "gateway_url",
        "mcp_url",
        "messages",
        "model",
    ]
    assert manifest["entrypoint"][-2:] == ["--detectors", "toolcall.ToolCallCondition"]
    assert manifest["native_outputs"] == ["reports/SCN-001.report.jsonl"]
    validate_manifest(manifest)


def test_command_attempt_template_carries_the_package_condition(
    command_package: Path, tmp_path: Path
) -> None:
    out = tmp_path / "template"
    manifest = compile_package(command_package, out)

    conversation = read(out / manifest["templates"]["conversation"])
    run = read(out / manifest["templates"]["run"])

    assert conversation["tool_call_condition"] == CONDITION
    assert "judge_rubric" not in conversation
    assert "detectors" not in run["plugins"]
    assert template_slots([conversation, run]) | {"model"} == set(manifest["requires"])


def test_reply_template_carries_the_judge_and_its_runtime_facts_slot(
    reply_package: Path, tmp_path: Path
) -> None:
    out = tmp_path / "template"
    manifest = compile_package(reply_package, out)

    conversation = read(out / manifest["templates"]["conversation"])
    run = read(out / manifest["templates"]["run"])

    assert conversation["judge_rubric"] == {
        **JUDGE,
        "runtime_facts": {"$value": "judge_runtime_facts"},
    }
    assert "tool_call_condition" not in conversation
    assert manifest["plugins"] == ["injection.IndirectInjection", "rubric_judge.RubricJudge"]
    assert manifest["entrypoint"][-1] == "rubric_judge.RubricJudge"
    assert {"judge_url", "judge_model", "judge_runtime_facts"} <= set(manifest["requires"])
    assert template_slots([conversation, run]) | {"model"} == set(manifest["requires"])
    validate_manifest(manifest)


def test_sequential_package_bundle_sends_one_user_turn_per_slot(
    sequential_package: Path, tmp_path: Path
) -> None:
    out = tmp_path / "template"

    manifest = compile_package(sequential_package, out)
    conversation = read(out / manifest["templates"]["conversation"])
    run = read(out / manifest["templates"]["run"])

    assert manifest["delivery"] == "sequential"
    assert manifest["plugins"] == ["injection.SequentialInjection", "toolcall.ToolCallCondition"]
    assert manifest["entrypoint"][-4:-2] == ["--probes", "injection.SequentialInjection"]
    assert conversation["messages"] == [
        {"role": "user", "content": {"$value": "user_turn_1"}},
        {"role": "user", "content": {"$value": "user_turn_2"}},
    ], "one slot per planned user turn, in order"
    assert run["plugins"]["probes"]["injection"] == {
        "SequentialInjection": {"conversation_source": {"$bundle": "conversations.jsonl"}}
    }, "a command attempt is graded after every turn, so it needs no judge_turns"
    assert "messages" not in manifest["requires"]
    assert template_slots([conversation, run]) | {"model"} == set(manifest["requires"])
    validate_manifest(manifest)


def test_sequential_reply_bundle_takes_judge_turns(tmp_path: Path) -> None:
    package = write_test_package(
        tmp_path / "packages", claim_level="reply", stimulus=sequential_stimulus()
    )
    out = tmp_path / "template"

    manifest = compile_package(package, out)
    run = read(out / manifest["templates"]["run"])

    assert run["plugins"]["probes"]["injection"]["SequentialInjection"]["judge_turns"] == {
        "$value": "judge_turns"
    }
    assert {"judge_turns", "user_turn_1", "user_turn_2"} <= set(manifest["requires"])


def test_sequential_history_with_a_reply_is_a_capability_gap(tmp_path: Path) -> None:
    stimulus = {
        **sequential_stimulus(),
        "history": [{"role": "assistant", "content": "Hello."}],
    }
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)
    out = tmp_path / "template"

    with pytest.raises(CapabilityGap) as raised:
        compile_package(package, out)

    record = raised.value.record
    assert record["kind"] == "capability_gap"
    assert record["tool"] == "garak"
    assert record["delivery"] == "sequential"
    assert record["package_id"] == "SCN-001-SCN-001"
    assert record["package_digest"] == load_package(package).manifest.manifest_digest
    assert not out.exists()


@pytest.mark.parametrize("turn_count", [3, 1, "2", None])
def test_sequential_turn_count_must_match_the_turns(tmp_path: Path, turn_count) -> None:
    stimulus = {**sequential_stimulus(), "turn_count": turn_count}
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)

    with pytest.raises(CompileError, match="turn_count"):
        compile_package(package, tmp_path / "template")


def test_unsupported_claim_level_is_refused(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", claim_level="state_change")

    with pytest.raises(CompileError, match="claim level"):
        compile_package(package, tmp_path / "template")


def test_package_content_that_looks_like_a_slot_is_refused(tmp_path: Path) -> None:
    condition = json.loads(json.dumps(CONDITION))
    condition["comparisons"][0]["right"]["value"] = {"$value": "model"}
    package = write_test_package(tmp_path / "packages", condition=condition)

    with pytest.raises(CompileError, match="slot marker"):
        compile_package(package, tmp_path / "template")


def test_cli_compile_writes_the_template(
    command_package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "template"

    code = main(["compile", str(command_package), "--out", str(out)])

    assert code == 0
    assert (out / "bundle.json").is_file()


def test_cli_compile_refuses_a_sequential_gap_with_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stimulus = {
        **sequential_stimulus(),
        "history": [{"role": "assistant", "content": "Hello."}],
    }
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)
    out = tmp_path / "template"

    code = main(["compile", str(package), "--out", str(out)])

    assert code == 3
    record = json.loads(capsys.readouterr().out)
    assert record["kind"] == "capability_gap"
    assert record["delivery"] == "sequential"
    assert not (out / "bundle.json").exists()


def test_cli_compile_reports_an_invalid_package_with_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["compile", str(tmp_path / "missing"), "--out", str(tmp_path / "t")])

    assert code == 1
    assert "package" in capsys.readouterr().err
