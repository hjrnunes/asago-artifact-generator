"""Deterministic input adapters for the artifact authoring path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from asago_artifact_generator.input_adapter import (
    InputKind,
    InputSourceError,
    build_scenario_handoff_view,
    load_input,
    snapshot_input,
)

CONTRACT_HANDOFF = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "handoff-v3" / "refund-bound.json"
)
CONTRACT_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"


def test_handoff_view_preserves_source_hash_and_authoritative_content() -> None:
    source = CONTRACT_HANDOFF.read_bytes()

    view = load_input(CONTRACT_HANDOFF, kind=InputKind.SCENARIO_HANDOFF_V3)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V3
    assert view.source_sha256 == hashlib.sha256(source).hexdigest()
    assert view.narrative == yaml.safe_load(source)["narrative"]
    assert view.gherkin["scenario"] == "Refund command exceeds the remaining balance of the order"


def test_snapshot_reads_source_and_never_edits_it(tmp_path: Path) -> None:
    source_path = tmp_path / "source.yaml"
    source_path.write_bytes(b"source: exact\n")
    before = source_path.read_bytes()

    snapshot = snapshot_input(source_path, tmp_path / "snapshots")

    assert source_path.read_bytes() == before
    assert snapshot.sha256 == hashlib.sha256(before).hexdigest()
    assert snapshot.snapshot_path is not None
    assert Path(snapshot.snapshot_path).read_bytes() == before


def test_non_handoff_source_is_rejected(tmp_path: Path) -> None:
    source_path = tmp_path / "native.yaml"
    source_path.write_text("scenario_id: native\n", encoding="utf-8")

    with pytest.raises(InputSourceError, match="scenario-handoff-v3"):
        load_input(source_path)


def test_tampered_handoff_fails_before_a_view_is_created(tmp_path: Path) -> None:
    payload = CONTRACT_HANDOFF.read_bytes().replace(b"adversarial", b"functional", 1)
    source_path = tmp_path / "tampered.json"
    source_path.write_bytes(payload)

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)


def test_observation_metadata_reaches_authoring_view(tmp_path: Path) -> None:
    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["observation"] = {
        "contract_schema": "observation-contract-v1",
        "contract_id": "qualification-live-v1",
        "contract_digest": "a" * 64,
        "criteria": [
            {
                "criterion_id": "reply-visible",
                "outcome": "The assistant reply contains the unsafe response.",
                "observable": True,
                "claim_level": "reply",
                "evidence": "assistant_message",
                "operation_name": "process_refund",
                "reason": "The live contract captures assistant messages.",
            }
        ],
        "assessment": {
            "disposition": "executable",
            "reason": "observable_outcome_supported",
            "supported_criteria": ["reply-visible"],
            "unsupported_criteria": [],
        },
    }
    payload["safe_observable_outcome"] = {
        "observable": True,
        "statement": "The assistant reply preserves the requested action.",
        "claim_level": "reply",
        "evidence": "assistant_message",
        "record_refs": [],
        "fact_refs": [],
    }
    # The producer publishes with null fields omitted.
    payload["deduplication"] = {
        "scenario_id": payload["scenario_id"],
        "status": "canonical",
        "key": {
            "uca_id": "RESP-1:CA-1-1:INCORRECT",
            "control_action_id": "CA-1-1",
            "claim_level": "reply",
        },
    }
    payload_without_digest = {
        key: value for key, value in payload.items() if key != "content_digest"
    }
    from asago_artifact_generator.input_adapter import _framed_digest

    payload["content_digest"] = _framed_digest("scenario-handoff-v3", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    view = load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)

    assert view.payload["observation"]["assessment"]["disposition"] == "executable"
    assert view.payload["observation"]["criteria"][0]["claim_level"] == "reply"
    assert view.payload["observation"]["criteria"][0]["operation_name"] == "process_refund"
    authoring_view = build_scenario_handoff_view(view)
    assert authoring_view["safe_observable_outcome"]["statement"] == (
        "The assistant reply preserves the requested action."
    )
    assert view.payload["deduplication"]["status"] == "canonical"


def test_analytical_observation_criterion_may_omit_optional_fields(
    tmp_path: Path,
) -> None:
    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["observation"] = {
        "contract_schema": "observation-contract-v1",
        "contract_id": "qualification-live-v1",
        "contract_digest": "a" * 64,
        "criteria": [
            {
                "criterion_id": "state",
                "outcome": "The backend record changes state.",
                "observable": False,
                "reason": "The live contract captures no state snapshot.",
            }
        ],
        "assessment": {
            "disposition": "analytical_only",
            "reason": "no_observable_outcome",
            "supported_criteria": [],
            "unsupported_criteria": ["state"],
        },
    }
    payload_without_digest = {
        key: value for key, value in payload.items() if key != "content_digest"
    }
    from asago_artifact_generator.input_adapter import _framed_digest

    payload["content_digest"] = _framed_digest("scenario-handoff-v3", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    view = load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)

    assert view.payload["observation"]["assessment"]["disposition"] == "analytical_only"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "scenario_version",
            "one",
            "handoff schema invalid at scenario_version: 'one' is not of type 'integer'",
        ),
        (
            "assumptions_and_unknowns",
            ["known", 1],
            "handoff schema invalid at assumptions_and_unknowns.1: 1 is not of type 'string'",
        ),
    ],
)
def test_handoff_root_must_match_the_vendored_schema(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    from asago_artifact_generator.input_adapter import _framed_digest

    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload[field] = value
    payload_without_digest = {
        key: item for key, item in payload.items() if key != "content_digest"
    }
    payload["content_digest"] = _framed_digest("scenario-handoff-v3", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)

    assert str(raised.value) == message


CONTRACT_HANDOFF_OBSERVED = (
    CONTRACT_KIT / "handoff-v3" / "valid" / "adversarial-observed-record.json"
)


def _observation() -> dict:
    return {
        "contract_schema": "observation-contract-v1",
        "contract_id": "qualification-live-v1",
        "contract_digest": "a" * 64,
        "criteria": [
            {
                "criterion_id": "reply-visible",
                "outcome": "The assistant reply contains the unsafe response.",
                "observable": True,
                "claim_level": "reply",
                "evidence": "assistant_message",
                "reason": "The live contract captures assistant messages.",
            }
        ],
        "assessment": {
            "disposition": "executable",
            "reason": "observable_outcome_supported",
            "supported_criteria": ["reply-visible"],
            "unsupported_criteria": [],
        },
    }


def _deduplication(scenario_id: str) -> dict:
    return {
        "scenario_id": scenario_id,
        "status": "canonical",
        "key": {
            "uca_id": "RESP-1:CA-1-1:INCORRECT",
            "control_action_id": "CA-1-1",
            "claim_level": "reply",
        },
    }


def _set(path: str, value: object):
    def mutate(payload: dict) -> None:
        *parents, leaf = path.split(".")
        target = payload
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[leaf] = value

    return mutate


def _drop(path: str):
    def mutate(payload: dict) -> None:
        *parents, leaf = path.split(".")
        target = payload
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        del target[leaf]

    return mutate


def _criterion(**changes: object):
    def mutate(payload: dict) -> None:
        criterion = payload["observation"]["criteria"][0]
        for key, value in changes.items():
            if value is _ABSENT:
                criterion.pop(key, None)
            else:
                criterion[key] = value

    return mutate


_ABSENT = object()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set("schema_version", "scenario-handoff-v9"), "unknown scenario handoff schema version"),
        (_drop("narrative"), "handoff schema invalid (missing=['narrative'], unknown=[])"),
        (_set("bogus", 1), "handoff schema invalid (missing=[], unknown=['bogus'])"),
        (_set("kind", "other"), "handoff kind is invalid"),
        (_set("narrative", "  "), "handoff field is blank or mistyped: narrative"),
        (_set("safe_alternative", 3), "handoff field is blank or mistyped: safe_alternative"),
        (_set("attack_tree", []), "handoff attack_tree and lineage must be objects"),
        (_set("lineage", "x"), "handoff attack_tree and lineage must be objects"),
        (_set("gherkin", "Feature: x"), "handoff Gherkin is invalid"),
        (_set("gherkin.scenario", None), "handoff Gherkin is invalid"),
        (_set("gherkin.extra", []), "handoff Gherkin has unknown field: extra"),
        (_set("gherkin.given", "a step"), "handoff Gherkin field is not a list: given"),
        (_set("lineage.detector", "x"), "handoff ownership violation: "),
        (_set("observation", "x"), "handoff observation metadata must be an object"),
        (
            _drop("observation.criteria"),
            "handoff observation metadata is invalid (missing=['criteria'], unknown=[])",
        ),
        (
            _set("observation.extra", 1),
            "handoff observation metadata is invalid (missing=[], unknown=['extra'])",
        ),
        (
            _set("observation.contract_id", " "),
            "handoff observation field is blank or mistyped: contract_id",
        ),
        (
            _set("observation.contract_digest", "abc"),
            "handoff observation contract_digest is not a SHA-256 hex digest",
        ),
        (_set("observation.contract_schema", "other"), "unknown observation contract schema"),
        (
            _set("observation.criteria", []),
            "handoff observation criteria must be a non-empty list",
        ),
        (
            _set("observation.criteria", ["x"]),
            "handoff observation criterion must be an object",
        ),
        (_criterion(reason=_ABSENT), "handoff observation criterion fields are invalid"),
        (_criterion(extra=1), "handoff observation criterion fields are invalid"),
        (_criterion(outcome=" "), "handoff observation criterion text is invalid"),
        (_criterion(observable="yes"), "handoff observation criterion observable is invalid"),
        (_criterion(operation_name=" "), "observation criterion operation_name is invalid"),
        (_criterion(claim_level=_ABSENT), "observable criterion requires claim_level"),
        (
            _criterion(
                observable=False, claim_level=_ABSENT, evidence=_ABSENT, operation_name="op"
            ),
            "analytical-only criterion must omit operation_name",
        ),
        (
            _set("observation.assessment", []),
            "handoff observation assessment must be an object",
        ),
        (
            _drop("observation.assessment.reason"),
            "handoff observation assessment fields are invalid",
        ),
        (
            _set("observation.assessment.disposition", "maybe"),
            "handoff observation disposition is invalid",
        ),
        (
            _set("observation.assessment.reason", " "),
            "handoff observation assessment reason is invalid",
        ),
        (
            _set("observation.assessment.supported_criteria", "reply-visible"),
            "handoff observation assessment supported_criteria is invalid",
        ),
        (
            _set("observation.assessment.unsupported_criteria", [""]),
            "handoff observation assessment unsupported_criteria is invalid",
        ),
        (_set("deduplication", []), "handoff deduplication must be an object"),
        (_drop("deduplication.key"), "handoff deduplication fields are invalid"),
        (_set("deduplication.extra", 1), "handoff deduplication fields are invalid"),
        (_set("deduplication.scenario_id", ""), "handoff deduplication scenario_id is invalid"),
        (_set("deduplication.status", "other"), "handoff deduplication status is invalid"),
        (_set("deduplication.status", "duplicate"), "duplicate handoff requires duplicate_of"),
        (
            _set("deduplication.duplicate_of", "SCN-0"),
            "canonical and analytical-only handoffs must omit duplicate_of",
        ),
        (_set("deduplication.key", "k"), "handoff deduplication key is invalid"),
        (_drop("deduplication.key.claim_level"), "handoff deduplication key is invalid"),
        (
            _set("deduplication.key.uca_id", " "),
            "handoff deduplication key identity is invalid",
        ),
        (
            _set("deduplication.key.operation_name", ""),
            "handoff deduplication operation_name is invalid",
        ),
        (
            _set("deduplication.key.claim_level", "belief"),
            "handoff deduplication claim_level is invalid",
        ),
        (_set("safe_observable_outcome", "x"), "handoff safe_observable_outcome must be"),
        (_set("safe_observable_outcome", {"x": 1}), "handoff safe_observable_outcome fields"),
        (_set("safe_observable_outcome", {}), "handoff safe outcome observable is invalid"),
        (
            _set("safe_observable_outcome", {"observable": True, "statement": " "}),
            "handoff safe outcome statement is invalid",
        ),
        (
            _set(
                "safe_observable_outcome",
                {"observable": False, "statement": "s", "record_refs": ["r"], "fact_refs": []},
            ),
            "analytical-only handoff safe outcome must omit record and fact references",
        ),
    ],
)
def test_handoff_validation_names_the_first_invalid_field(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["observation"] = _observation()
    payload["deduplication"] = _deduplication(payload["scenario_id"])
    mutate(payload)
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)

    assert str(raised.value).startswith(message)


def test_valid_duplicate_metadata_passes_validation_and_reaches_the_digest_check(
    tmp_path: Path,
) -> None:
    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["deduplication"] = {
        **_deduplication(payload["scenario_id"]),
        "status": "duplicate",
        "duplicate_of": "SCN-0",
    }
    payload["deduplication"]["key"]["operation_name"] = "process_refund"
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError, match="content_digest does not match"):
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set("deduplication.key.condition", " "), "handoff deduplication condition is invalid"),
        (_set("discriminating_condition", "x"), "handoff schema invalid: schema_violation:"),
        (
            _drop("tool_call_condition_status"),
            "handoff schema invalid (missing=['tool_call_condition_status'], unknown=[])",
        ),
        (
            _set("tool_call_condition_status.status", "maybe"),
            "handoff schema invalid: schema_violation:tool_call_condition_status",
        ),
        (
            _set("tool_call_condition.comparisons", []),
            "handoff schema invalid: schema_violation:tool_call_condition",
        ),
        (_drop("tool_call_condition"), "handoff schema invalid: schema_violation:<root>"),
        (
            _set("tool_call_condition_status.reason", "no_condition"),
            "handoff tool_call_condition_status reason must be bound exactly when",
        ),
    ],
)
def test_handoff_validation_checks_the_condition_and_tool_call_fields(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = json.loads(CONTRACT_HANDOFF_OBSERVED.read_text(encoding="utf-8"))
    mutate(payload)
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V3)

    assert str(raised.value).startswith(message)


def test_not_executable_status_forbids_a_tool_call_condition(tmp_path: Path) -> None:
    payload = json.loads(CONTRACT_HANDOFF_OBSERVED.read_text(encoding="utf-8"))
    payload["tool_call_condition_status"] = {
        "status": "not_executable",
        "reason": "state_only",
        "detail": "the condition holds only state predicates",
    }
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError, match="schema_violation:<root>"):
        load_input(source_path)


def test_view_exposes_the_tool_call_status_and_condition() -> None:
    view = load_input(CONTRACT_HANDOFF_OBSERVED)
    payload = json.loads(CONTRACT_HANDOFF_OBSERVED.read_text(encoding="utf-8"))

    assert view.tool_call_condition_status == payload["tool_call_condition_status"]
    assert view.tool_call_condition == payload["tool_call_condition"]
    model_view = build_scenario_handoff_view(view)
    assert "tool_call_condition_status" not in model_view
    assert "tool_call_condition" not in model_view


def test_unbound_view_has_no_tool_call_condition() -> None:
    view = load_input(CONTRACT_KIT / "handoff-v3" / "valid" / "adversarial-condition-omitted.json")

    assert view.tool_call_condition_status["status"] == "not_executable"
    assert view.tool_call_condition is None


@pytest.mark.parametrize(
    "relative",
    [
        "handoff-v1/valid/adversarial-refund.json",
        "handoff-v2/valid/adversarial-observed-record.json",
    ],
)
def test_frozen_handoff_versions_are_rejected_for_authoring(relative: str) -> None:
    with pytest.raises(InputSourceError, match="carry no tool_call_condition_status") as raised:
        load_input(CONTRACT_KIT / relative)

    assert "scenario-handoff-v3" in str(raised.value)


def test_unknown_input_kind_is_rejected() -> None:
    with pytest.raises(InputSourceError, match="^unsupported input kind: bogus$"):
        load_input(CONTRACT_HANDOFF, kind="bogus")
