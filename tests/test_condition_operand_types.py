"""Comparison semantics reach the authoring prompt and operand types are checked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from asago_artifact_generator.authoring.condition_types import operand_type_findings
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2
from asago_artifact_generator.input_adapter import _framed_digest, load_input

from .support import ScriptedAuthoringTransport, load_failure_evidence, stage_local_orchestrator
from .test_run_created_records import _runtime

_NOT_CALLED = (
    Path(__file__).resolve().parents[1]
    / "contracts/scenario-handoff/handoff-v3/valid/functional-not-called.json"
)
_RUNTIME = {
    "delivery": ["direct_user_message"],
    "setup_permissions": [],
    "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
    "limits": {"max_turns": 2},
}


def _inventory(**argument_types: object) -> dict:
    return {
        "facts": [],
        "operations": [
            {
                "name": "transfer_funds",
                "arguments": {
                    "type": "object",
                    "properties": {name: {"type": kind} for name, kind in argument_types.items()},
                },
            }
        ],
    }


def _argument(name: str) -> dict:
    return {"source": "argument", "operation": "transfer_funds", "argument": name}


def _literal(value: object) -> dict:
    return {"source": "literal", "value": value}


def _condition(left: dict, op: str, right: dict) -> dict:
    return {"comparisons": [{"kind": "value", "left": left, "op": op, "right": right}]}


def _codes(condition: dict, inventory: dict) -> list[str]:
    return [finding.code for finding in operand_type_findings(condition, inventory)]


@pytest.mark.parametrize(
    ("left", "op", "right", "inventory"),
    [
        (_argument("record"), "eq", _literal([1]), _inventory(record="object")),
        (_argument("amount"), "ne", _literal("500"), _inventory(amount="integer")),
        (_argument("amount"), "in", _literal(500), _inventory(amount="integer")),
        (
            _argument("amount"),
            "not_in",
            _argument("limit"),
            _inventory(amount="integer", limit="string"),
        ),
        (_argument("record"), "in", _literal(["a", "b"]), _inventory(record="object")),
        (_argument("flags"), "gt", _literal(1), _inventory(flags="array")),
        (_argument("amount"), "le", _literal("many"), _inventory(amount="integer")),
    ],
)
def test_operand_types_that_cannot_match_are_reported(left, op, right, inventory) -> None:
    findings = operand_type_findings(_condition(left, op, right), inventory)

    [finding] = findings
    assert finding.code == "condition_operand_type_mismatch"
    assert finding.path == "tool_call_condition.comparisons[0]"
    assert op in finding.detail


@pytest.mark.parametrize(
    ("left", "op", "right", "inventory"),
    [
        (_argument("name"), "eq", _literal("Ada"), _inventory(name="string")),
        (_argument("amount"), "eq", _literal(1.5), _inventory(amount="integer")),
        (_argument("amount"), "gt", _literal(500), _inventory(amount="number")),
        (_argument("amount"), "gt", _literal("12"), _inventory(amount="integer")),
        (_argument("name"), "in", _literal(["Ada", "Bob"]), _inventory(name="string")),
        (_argument("tags"), "in", _literal(["a", "b"]), _inventory(tags="array")),
        (_argument("name"), "in", _argument("names"), _inventory(name="string", names="array")),
        (_argument("name"), "eq", _literal(None), _inventory(name="string")),
        (_argument("missing"), "eq", _literal(["x"]), _inventory(name="string")),
        (_argument("a"), "eq", _argument("b"), _inventory(a="string", b="string")),
    ],
)
def test_operand_types_that_can_match_pass(left, op, right, inventory) -> None:
    assert _codes(_condition(left, op, right), inventory) == []


def test_only_value_comparisons_are_type_checked_and_each_is_located() -> None:
    condition = {
        "comparisons": [
            {"kind": "not_called", "operation": "transfer_funds", "where": []},
            {"kind": "order", "operation": "transfer_funds", "requires_prior": "check"},
            {"kind": "value", "left": _argument("amount"), "op": "in", "right": _literal(5)},
        ]
    }

    [finding] = operand_type_findings(condition, _inventory(amount="integer"))

    assert finding.path == "tool_call_condition.comparisons[2]"


def test_a_missing_or_malformed_condition_has_nothing_to_check() -> None:
    assert operand_type_findings(None, _inventory()) == []
    assert operand_type_findings({"comparisons": "x"}, _inventory()) == []
    assert operand_type_findings({"comparisons": [7]}, _inventory()) == []


def _signed_handoff(tmp_path: Path, comparison: dict) -> Path:
    payload = json.loads(_NOT_CALLED.read_text(encoding="utf-8"))
    payload["tool_call_condition"]["comparisons"][0] = comparison
    payload = {key: value for key, value in payload.items() if key != "content_digest"}
    payload["content_digest"] = _framed_digest("scenario-handoff-v3", payload)
    path = tmp_path / "handoff.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_command_attempt_with_an_undecidable_condition_stops_before_any_model_call(
    tmp_path: Path,
) -> None:
    comparison = {
        "kind": "value",
        "left": _argument("amount"),
        "op": "in",
        "right": _literal(500),
    }
    handoff = _signed_handoff(tmp_path, comparison)
    transport = ScriptedAuthoringTransport([])
    orchestrator = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    )

    result = orchestrator.run(load_input(handoff), _inventory(amount="integer"), _RUNTIME)

    assert transport.requests == []
    assert result.status == "failed"
    assert [finding.code for finding in result.findings] == ["condition_operand_type_mismatch"]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["terminal"]["reason"] == "condition_operand_type_mismatch"
    assert evidence["terminal"]["attempt_index"] is None


def _call1_user() -> str:
    view = load_input(_NOT_CALLED)
    inventory = {"facts": [], "operations": [], "source_handles": []}
    return build_call1_packet_v2(view, inventory, _runtime()).user


def test_prompt_states_that_comparisons_combine_with_and() -> None:
    assert "Comparisons combine with AND" in _call1_user()


def test_prompt_states_exact_matching_with_one_example() -> None:
    user = _call1_user()

    assert "in, not_in, eq, and ne match exactly" in user
    assert "never as a substring or by meaning" in user
    assert "one by one when the left value is a list" in user
    example = 'topic in [\\"refund\\", \\"billing\\"]'
    assert user.count(example) == 1


def test_prompt_states_the_not_called_semantics_without_a_trigger() -> None:
    user = _call1_user()

    assert "after any trigger the scenario states" not in user
    assert "a captured call to the operation after any trigger" not in user
    assert "matching call at any position" in user
    assert "has no trigger" in user


def test_prompt_states_the_order_semantics_of_the_contract() -> None:
    user = _call1_user()

    assert "Check order comparisons on captured call order: operation called with" not in user
    assert "the call to operation has no earlier call to requires_prior" in user
    assert "only an earlier requires_prior call with the same value" in user
