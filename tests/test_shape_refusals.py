"""A shape the consumer cannot author stops before any model request, with a typed code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.input_adapter import InputView, ShapeVersionMalformed, load_input

from .support import ScriptedAuthoringTransport, load_failure_evidence, stage_local_orchestrator

_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff" / "handoff-v4"
_RUNTIME: dict[str, Any] = {
    "delivery": ["direct_user_message", "sequential_user_turns"],
    "setup_permissions": [],
    "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
    "limits": {"max_turns": 8, "max_planned_turns": 4},
}
_INVENTORY: dict[str, Any] = {"facts": [], "operations": []}


def _run(tmp_path: Path, name: str, runtime: dict[str, Any] = _RUNTIME):
    transport = ScriptedAuthoringTransport([])
    orchestrator = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    )
    view: InputView = load_input(_KIT / "valid" / name)
    return orchestrator.run(view, _INVENTORY, runtime), transport


def _assert_refused(result: Any, transport: Any, code: str) -> dict[str, Any]:
    assert transport.requests == []
    assert result.status == "failed"
    assert result.package_path is None
    assert [finding.code for finding in result.findings] == [code]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["attempts"] == []
    assert evidence["terminal"] == {"stage": "plan", "attempt_index": None, "reason": code}
    [finding] = evidence["findings"]
    assert finding["code"] == code
    assert finding["path"] == "attack_shape"
    return finding


@pytest.mark.parametrize(
    "name", ["adversarial-indirect-listing.json", "adversarial-indirect-policy-operator.json"]
)
def test_an_indirect_shape_is_refused_when_the_runtime_has_no_planted_item_delivery(
    tmp_path: Path, name: str
) -> None:
    result, transport = _run(tmp_path, name)

    finding = _assert_refused(result, transport, "shape_channel_unsupported")
    assert finding["details"] == {
        "channel": "indirect",
        "runtime_delivery": ["direct_user_message", "sequential_user_turns"],
    }


def test_a_multi_turn_direct_shape_is_refused_when_the_runtime_lists_no_sequential_delivery(
    tmp_path: Path,
) -> None:
    runtime = {**_RUNTIME, "delivery": ["direct_user_message"]}

    result, transport = _run(tmp_path, "adversarial-direct-multi-turn.json", runtime)

    finding = _assert_refused(result, transport, "shape_channel_unsupported")
    assert finding["details"] == {"channel": "direct", "runtime_delivery": ["direct_user_message"]}


def test_a_forged_transcript_shape_is_refused(tmp_path: Path) -> None:
    result, transport = _run(tmp_path, "adversarial-forged-transcript.json")

    finding = _assert_refused(result, transport, "shape_forged_transcript_unsupported")
    assert finding["details"] == {"threat_label": "forged_transcript_threat"}


def test_a_shape_with_more_turns_than_the_runtime_plans_is_refused(tmp_path: Path) -> None:
    runtime = {**_RUNTIME, "limits": {"max_turns": 8, "max_planned_turns": 2}}

    result, transport = _run(tmp_path, "adversarial-direct-multi-turn.json", runtime)

    finding = _assert_refused(result, transport, "shape_turn_count_unsupported")
    assert finding["details"] == {"turn_count": 3, "max_planned_turns": 2}


def test_a_runtime_without_a_planned_turn_limit_allows_four_turns(tmp_path: Path) -> None:
    runtime = {**_RUNTIME, "limits": {"max_turns": 8}}
    view = load_input(_KIT / "valid" / "adversarial-direct-multi-turn.json")
    transport = ScriptedAuthoringTransport([])
    orchestrator = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    )

    orchestrator.run(view, _INVENTORY, runtime)

    assert [request["stage"] for request in transport.requests] == ["call1"]


@pytest.mark.parametrize(
    "name", ["adversarial-direct-single.json", "adversarial-observed-record.json"]
)
def test_a_single_turn_direct_shape_is_not_refused(tmp_path: Path, name: str) -> None:
    runtime = {**_RUNTIME, "delivery": ["direct_user_message"]}
    view = load_input(_KIT / "valid" / name)
    transport = ScriptedAuthoringTransport([])
    orchestrator = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    )

    orchestrator.run(view, _INVENTORY, runtime)

    assert [request["stage"] for request in transport.requests] == ["call1"]


def test_a_refusal_names_the_scenario_as_not_executable(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "adversarial-forged-transcript.json")

    [finding] = result.findings
    assert "not executable" in finding.detail
    assert "forges a transcript" in finding.detail


def test_a_functional_v4_handoff_has_no_shape_to_refuse(tmp_path: Path) -> None:
    runtime = {**_RUNTIME, "delivery": ["direct_user_message"]}
    transport = ScriptedAuthoringTransport([])
    orchestrator = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    )

    orchestrator.run(
        load_input(_KIT / "valid" / "functional-null-shape.json"), _INVENTORY, runtime
    )

    assert [request["stage"] for request in transport.requests] == ["call1"]


def test_the_malformed_shape_refusal_carries_its_code_and_path() -> None:
    with pytest.raises(ShapeVersionMalformed) as raised:
        load_input(_KIT / "invalid" / "shape-turn-count-five.json")

    assert raised.value.code == "shape_version_malformed"
    assert str(raised.value).startswith("shape_version_malformed: ")
    assert raised.value.schema_path.startswith("attack_shape.turn_")


def test_generate_reports_a_malformed_shape_with_its_code_and_spends_nothing(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile.json"
    runtime = tmp_path / "runtime.json"
    profile.write_text("{}", encoding="utf-8")
    runtime.write_text("{}", encoding="utf-8")
    calls: list[object] = []

    result = CliRunner().invoke(
        cli.app,
        [
            "generate",
            str(_KIT / "invalid" / "shape-turn-count-five.json"),
            "--target-profile",
            str(profile),
            "--runtime-contract",
            str(runtime),
            "--profile",
            "unused",
            "--output-dir",
            str(tmp_path / "output"),
        ],
        obj=lambda **options: calls.append(options),
    )

    assert result.exit_code == 2
    assert "shape_version_malformed" in result.output
    assert calls == []
    assert not (tmp_path / "output").exists()
