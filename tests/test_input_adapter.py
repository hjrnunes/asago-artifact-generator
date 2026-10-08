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
)
from tests.support import HANDOFFS

CONTRACT_HANDOFF = HANDOFFS / "refund-bound.json"
CONTRACT_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"


def test_handoff_view_preserves_source_hash_and_authoritative_content() -> None:
    source = CONTRACT_HANDOFF.read_bytes()

    view = load_input(CONTRACT_HANDOFF)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V4
    assert view.source_sha256 == hashlib.sha256(source).hexdigest()
    assert view.narrative == yaml.safe_load(source)["narrative"]
    assert view.gherkin["scenario"] == "Refund command exceeds the remaining balance of the order"


def test_non_handoff_source_is_rejected(tmp_path: Path) -> None:
    source_path = tmp_path / "native.yaml"
    source_path.write_text("scenario_id: native\n", encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path)

    assert str(raised.value) == (
        "authoring source must be a producer scenario-handoff-v4 document; found no schema_version"
    )


def test_tampered_handoff_fails_before_a_view_is_created(tmp_path: Path) -> None:
    payload = CONTRACT_HANDOFF.read_bytes().replace(b"adversarial", b"functional", 1)
    source_path = tmp_path / "tampered.json"
    source_path.write_bytes(payload)

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(source_path)


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

    payload["content_digest"] = _framed_digest("scenario-handoff-v4", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    view = load_input(source_path)

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

    payload["content_digest"] = _framed_digest("scenario-handoff-v4", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    view = load_input(source_path)

    assert view.payload["observation"]["assessment"]["disposition"] == "analytical_only"


CONTRACT_HANDOFF_OBSERVED = HANDOFFS / "adversarial-observed-record.json"


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


def _full_handoff() -> dict:
    """A bound handoff carrying every optional block the hand rules read."""

    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["observation"] = _observation()
    payload["observation"]["criteria"][0]["operation_name"] = "process_refund"
    payload["safe_observable_outcome"] = {
        "observable": True,
        "statement": "The assistant reply preserves the requested action.",
        "claim_level": "reply",
        "evidence": "assistant_message",
        "operation_name": "process_refund",
        "record_refs": [],
        "fact_refs": [],
    }
    payload["deduplication"] = _deduplication(payload["scenario_id"])
    payload["deduplication"]["key"].update(
        operation_name="process_refund", condition="amount exceeds balance"
    )
    return payload


def _all(*mutations):
    def mutate(payload: dict) -> None:
        for mutation in mutations:
            mutation(payload)

    return mutate


_ANALYTICAL_CRITERION = _criterion(
    observable=False, claim_level=_ABSENT, evidence=_ABSENT, operation_name=_ABSENT
)
_ANALYTICAL_SAFE = _all(
    _set("safe_observable_outcome.observable", False),
    _drop("safe_observable_outcome.claim_level"),
    _drop("safe_observable_outcome.evidence"),
    _drop("safe_observable_outcome.operation_name"),
)
_CRIT = "observation.criteria.0"


# The mirrored schema cannot state these rules (blank text, formats, presence
# where the schema has a default, and cross-field pairings), so this reader
# keeps them. The first nine are this reader's alone: the producer accepts those
# documents and no kit case carries them.
@pytest.mark.parametrize(
    ("mutate", "field", "detail"),
    [
        (_set("observation.contract_id", " "), "observation", "observation.contract_id: must not"),
        (
            _set("observation.contract_digest", "abc"),
            "observation",
            "observation.contract_digest: is not a SHA-256 hex digest",
        ),
        (
            _set("observation.contract_schema", "other"),
            "observation",
            "observation.contract_schema: must be observation-contract-v1",
        ),
        (
            _criterion(operation_name=" "),
            "observation",
            f"{_CRIT}.operation_name: must not be blank",
        ),
        (
            _set("observation.assessment.unsupported_criteria", [""]),
            "observation",
            "observation.assessment.unsupported_criteria.0: must not be blank",
        ),
        (
            _set("deduplication.status", "duplicate"),
            "deduplication",
            "deduplication: a duplicate requires duplicate_of",
        ),
        (
            _set("deduplication.duplicate_of", "SCN-0"),
            "deduplication",
            "deduplication: canonical and analytical-only handoffs must omit duplicate_of",
        ),
        (
            _set("deduplication.key.uca_id", " "),
            "deduplication",
            "deduplication.key.uca_id: must not be blank",
        ),
        (
            _set("deduplication.key.condition", " "),
            "deduplication",
            "deduplication.key.condition: must not be blank",
        ),
        (_set("scenario_id", " "), "scenario_id", "scenario_id: must not be blank"),
        (_set("narrative", " "), "narrative", "narrative: must not be blank"),
        (_set("observation.contract_schema", " "), "observation", "contract_schema: must not"),
        (_criterion(reason=" "), "observation", f"{_CRIT}.reason: must not be blank"),
        (
            _criterion(claim_level=_ABSENT),
            "observation",
            f"{_CRIT}: an observable criterion requires claim_level",
        ),
        (
            _criterion(evidence=_ABSENT),
            "observation",
            f"{_CRIT}: an observable criterion requires evidence",
        ),
        (
            _all(_ANALYTICAL_CRITERION, _criterion(claim_level="reply")),
            "observation",
            f"{_CRIT}: an analytical-only criterion must omit claim_level and evidence",
        ),
        (
            _all(_ANALYTICAL_CRITERION, _criterion(operation_name="process_refund")),
            "observation",
            f"{_CRIT}: an analytical-only criterion must omit operation_name",
        ),
        (
            _drop("observation.assessment.supported_criteria"),
            "observation",
            "observation.assessment: requires supported_criteria and unsupported_criteria",
        ),
        (
            _set("observation.assessment.reason", " "),
            "observation",
            "observation.assessment.reason: must not be blank",
        ),
        (
            _set("safe_observable_outcome.statement", " "),
            "safe_observable_outcome",
            "safe_observable_outcome.statement: must not be blank",
        ),
        (
            _set("safe_observable_outcome.evidence", "tool_call"),
            "safe_observable_outcome",
            "safe_observable_outcome: claim_level and evidence do not match",
        ),
        (
            _set("safe_observable_outcome.operation_name", " "),
            "safe_observable_outcome",
            "safe_observable_outcome.operation_name: must not be blank",
        ),
        (
            _all(_ANALYTICAL_SAFE, _set("safe_observable_outcome.claim_level", "reply")),
            "safe_observable_outcome",
            "safe_observable_outcome: an analytical-only safe outcome must omit claim_level",
        ),
        (
            _drop("safe_observable_outcome.record_refs"),
            "safe_observable_outcome",
            "safe_observable_outcome.record_refs: is required",
        ),
        (
            _set("safe_observable_outcome.record_refs", [" "]),
            "safe_observable_outcome",
            "safe_observable_outcome.record_refs.0: must not be blank",
        ),
        (
            _set("safe_observable_outcome.fact_refs", ["F-1", "F-1"]),
            "safe_observable_outcome",
            "safe_observable_outcome.fact_refs: must be unique",
        ),
        (
            _all(_ANALYTICAL_SAFE, _set("safe_observable_outcome.fact_refs", ["F-1"])),
            "safe_observable_outcome",
            "safe_observable_outcome: an analytical-only safe outcome must omit record and fact",
        ),
        (
            _set("tool_call_condition_status.reason", "no_condition"),
            "tool_call_condition_status",
            "tool_call_condition_status: reason must be bound exactly when the status is bound",
        ),
        (
            _set("deduplication.scenario_id", " "),
            "deduplication",
            "deduplication.scenario_id: must not be blank",
        ),
        (
            _all(
                _set("deduplication.status", "duplicate"), _set("deduplication.duplicate_of", " ")
            ),
            "deduplication",
            "deduplication.duplicate_of: must not be blank",
        ),
    ],
)
def test_each_hand_rule_reports_its_top_level_field(
    tmp_path: Path, mutate, field: str, detail: str
) -> None:
    payload = _full_handoff()
    mutate(payload)
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path)

    assert raised.value.codes == (f"schema_violation:{field}",)
    assert str(raised.value).startswith(f"handoff schema invalid: schema_violation:{field} (")
    assert detail in str(raised.value)


def test_the_full_handoff_used_by_the_hand_rule_table_is_valid(tmp_path: Path) -> None:
    payload = _full_handoff()
    payload.pop("content_digest")
    from asago_artifact_generator.input_adapter import _framed_digest

    payload["content_digest"] = _framed_digest("scenario-handoff-v4", payload)
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_input(source_path).payload["deduplication"]["status"] == "canonical"


def test_a_schema_violation_is_reported_before_the_digest_check(tmp_path: Path) -> None:
    payload = _full_handoff()
    payload["gherkin"]["given"] = [1]
    source_path = tmp_path / "handoff.json"
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path)

    assert raised.value.codes == ("schema_violation:gherkin",)
    assert str(raised.value) == (
        "handoff schema invalid: schema_violation:gherkin "
        "(gherkin.given.0: 1 is not of type 'string')"
    )


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
    view = load_input(HANDOFFS / "adversarial-condition-omitted.json")

    assert view.tool_call_condition_status["status"] == "not_executable"
    assert view.tool_call_condition is None


@pytest.mark.parametrize(
    ("relative", "version"),
    [
        ("handoff-v1/valid/adversarial-refund.json", "scenario-handoff-v1"),
        ("handoff-v2/valid/adversarial-observed-record.json", "scenario-handoff-v2"),
        ("handoff-v3/valid/refund-bound.json", "scenario-handoff-v3"),
    ],
)
def test_retired_handoff_versions_are_rejected_for_authoring(relative: str, version: str) -> None:
    with pytest.raises(InputSourceError) as raised:
        load_input(CONTRACT_KIT / relative)

    assert str(raised.value) == (
        f"authoring source must be a producer scenario-handoff-v4 document; found {version}"
    )


def test_handoff_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    source_path = tmp_path / "handoff.json"
    source_path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path)

    assert str(raised.value) == (
        "authoring source must be a producer scenario-handoff-v4 document; found no schema_version"
    )


def test_gherkin_companion_file_replaces_the_rendered_text(tmp_path: Path) -> None:
    source_path = tmp_path / "handoff.json"
    source_path.write_bytes(CONTRACT_HANDOFF.read_bytes())
    companion = b"Feature: written by the producer\n  Scenario: exact bytes\n"
    source_path.with_suffix(".feature").write_bytes(companion)

    view = load_input(source_path)

    assert view.gherkin_text == companion.decode("utf-8")
    assert view.gherkin_bytes == companion
    assert view.source_digests["gherkin"] == hashlib.sha256(companion).hexdigest()
    assert view.source_digests["input"] == view.source_sha256


def test_gherkin_companion_must_be_utf8(tmp_path: Path) -> None:
    source_path = tmp_path / "handoff.json"
    source_path.write_bytes(CONTRACT_HANDOFF.read_bytes())
    companion = source_path.with_suffix(".feature")
    companion.write_bytes(b"Feature: \xff\xfe\n")

    with pytest.raises(InputSourceError) as raised:
        load_input(source_path)

    assert str(raised.value) == f"Gherkin companion is not UTF-8: {companion}"


@pytest.mark.parametrize("schema_text", [None, "{not json"])
def test_unreadable_vendored_handoff_schema_is_a_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_text: str | None
) -> None:
    from asago_artifact_generator import input_adapter

    monkeypatch.setattr(input_adapter, "_HANDOFF_ROOT", tmp_path)
    if schema_text is not None:
        schema = tmp_path / "handoff-v4" / "schema.json"
        schema.parent.mkdir()
        schema.write_text(schema_text, encoding="utf-8")

    with pytest.raises(InputSourceError, match="^cannot read vendored handoff-v4 schema: "):
        input_adapter._handoff_schema()
