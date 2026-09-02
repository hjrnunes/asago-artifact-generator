from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from asago_artifact_generator.authoring import (
    PresentationRequest,
    PresentationResult,
    PresentationSlot,
    _author_digest,
    _normalize_texts,
)
from asago_artifact_generator.garak.compile import (
    GARAK_COMPILER_VERSION,
    _argument_parts,
    _attack_index_errors,
    _contains_placeholder,
    _emitted_arguments,
    _presentation_result,
    _record_tool_name,
    _source_turn_ids,
    _tool_call_reference_errors,
    _tool_result_name_errors,
    _validate_artifact_digest,
    _validate_call_function,
    _validate_trace_digest,
    _validate_turn_header,
    _validate_turn_records,
)
from asago_artifact_generator.garak.compile import (
    _validate_trace_compiler as validate_compiler_in_artifact,
)
from asago_artifact_generator.garak.schema import (
    _additional_property_errors,
    _alternative_errors,
    _alternative_schema_errors,
    _array_limit_errors,
    _composition_errors,
    _enum_errors,
    _item_schema_value_errors,
    _pattern_schema_errors,
    _required_value_errors,
    _unique_items_value_errors,
    _valid_number,
    _valid_required,
    _valid_type_value,
)
from asago_artifact_generator.models._base import compute_framed_digest, normalize_unicode
from asago_artifact_generator.platforms.base import ArtifactValidationError
from asago_artifact_generator.trace import (
    ArtifactElementTrace,
    ArtifactTrace,
    ObservationReceipt,
    OracleTrace,
    _set_receipt_digest,
    _set_trace_digest,
    _validate_trace_entries,
)
from asago_artifact_generator.trace import (
    _validate_trace_compiler as validate_compiler_in_trace,
)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("string", True),
        (["string", "null"], True),
        ([], False),
        (["unsupported"], False),
        (["string", 1], False),
        (None, False),
    ],
)
def test_schema_type_values_are_closed(value: Any, valid: bool) -> None:
    assert _valid_type_value(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (["account_id"], True),
        ([], True),
        ("account_id", False),
        ([1], False),
        (["account_id", "account_id"], False),
    ],
)
def test_schema_required_values_are_unique_strings(value: Any, valid: bool) -> None:
    assert _valid_required(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [(1, True), (1.5, True), (True, False), (float("inf"), False), ("1", False)],
)
def test_schema_numeric_keywords_accept_only_finite_numbers(value: Any, valid: bool) -> None:
    assert _valid_number(value) is valid


def test_schema_pattern_validation_covers_absent_valid_and_invalid_patterns() -> None:
    assert _pattern_schema_errors({}, "$") == []
    assert _pattern_schema_errors({"pattern": 1}, "$")
    assert _pattern_schema_errors({"pattern": "["}, "$")
    assert _pattern_schema_errors({"pattern": "^account_"}, "$") == []


def test_schema_alternative_declarations_are_closed() -> None:
    assert _alternative_schema_errors({}, "anyOf", "$", set()) == []
    assert _alternative_schema_errors({"anyOf": 1}, "anyOf", "$", set())
    errors = _alternative_schema_errors(
        {"anyOf": [{"type": "string"}, "not-a-schema"]}, "anyOf", "$", set()
    )
    assert any("alternative must be an object" in error for error in errors)
    assert _alternative_schema_errors({"anyOf": [{"type": "string"}]}, "anyOf", "$", set()) == []


@pytest.mark.parametrize(
    ("instance", "schema", "has_errors"),
    [
        (1, {"anyOf": [{"type": "integer"}, {"type": "string"}]}, False),
        (True, {"anyOf": [{"type": "integer"}, {"type": "string"}]}, True),
        (1, {"oneOf": [{"type": "integer"}, {"type": "string"}]}, False),
        (True, {"oneOf": [{"type": "integer"}, {"type": "string"}]}, True),
        (1, {"oneOf": [{}, {"type": "integer"}]}, True),
        (1, {"type": "integer"}, False),
    ],
)
def test_schema_alternatives_validate_match_cardinality(
    instance: Any, schema: dict[str, Any], has_errors: bool
) -> None:
    errors = _alternative_errors(
        instance,
        schema,
        "$",
        "anyOf" if "anyOf" in schema else "oneOf",
        lambda matches: matches > 0 if "anyOf" in schema else matches == 1,
    )
    assert bool(errors) is has_errors


def test_schema_composition_validates_all_of_and_alternatives() -> None:
    schema = {
        "allOf": [{"type": "integer"}],
        "anyOf": [{"type": "integer"}],
        "oneOf": [{"type": "integer"}],
    }
    assert _composition_errors(1, schema, "$") == []


def test_schema_enum_validation_covers_const_and_enum_outcomes() -> None:
    assert _enum_errors(1, {}, "$") == []
    assert _enum_errors(1, {"const": 1}, "$") == []
    assert _enum_errors(2, {"const": 1}, "$")
    assert _enum_errors(2, {"enum": [1, 2]}, "$") == []
    assert _enum_errors(3, {"enum": [1, 2]}, "$")


def test_schema_array_limits_and_uniqueness_cover_all_outcomes() -> None:
    assert _array_limit_errors([], {}, "$") == []
    assert _array_limit_errors([], {"minItems": 1}, "$")
    assert _array_limit_errors([1, 2], {"maxItems": 1}, "$")
    assert _unique_items_value_errors([1, 2], {}, "$") == []
    assert _unique_items_value_errors([1, 2], {"uniqueItems": True}, "$") == []
    assert _unique_items_value_errors([1, 1], {"uniqueItems": True}, "$")


def test_schema_item_and_additional_property_validation_is_closed() -> None:
    assert _item_schema_value_errors([1], {}, "$") == []
    assert _item_schema_value_errors([1], {"items": True}, "$") == []
    assert _item_schema_value_errors([1], {"items": {"type": "integer"}}, "$") == []
    assert _item_schema_value_errors(["bad"], {"items": {"type": "integer"}}, "$")
    instance = {"known": 1, "extra": "ok"}
    schema = {"additionalProperties": False}
    assert _additional_property_errors("extra", "ok", schema, "$")
    assert _additional_property_errors(
        "extra", 1, {"additionalProperties": {"type": "string"}}, "$"
    )
    assert (
        _additional_property_errors(
            "extra", "ok", {"additionalProperties": {"type": "string"}}, "$"
        )
        == []
    )
    assert _additional_property_errors("extra", "ok", {}, "$") == []
    assert _required_value_errors(instance, {"required": ["known"]}, "$") == []
    assert _required_value_errors(instance, {"required": ["missing"]}, "$")


def test_emitted_argument_resolution_rejects_malformed_transcripts() -> None:
    valid = {"tool_calls": [{"function": {"arguments": {"account_id": "A"}}}]}
    assert _emitted_arguments(valid) == {"account_id": "A"}
    for turn in (
        {},
        {"tool_calls": []},
        {"tool_calls": [None]},
        {"tool_calls": [{}]},
        {"tool_calls": [{"function": {}}]},
        {"tool_calls": [{"function": {"arguments": []}}]},
    ):
        with pytest.raises(ArtifactValidationError):
            _emitted_arguments(turn)


def test_presentation_result_enforces_authority_and_result_type() -> None:
    request = PresentationRequest(slots=(PresentationSlot("slot-1", "test text", "user"),))
    with pytest.raises(ArtifactValidationError):
        _presentation_result(request, object(), {"slot-1": "text"})
    with pytest.raises(ArtifactValidationError):
        _presentation_result(request, None, None)

    class InvalidAuthor:
        def author(self, request: PresentationRequest) -> object:
            return object()

    with pytest.raises(ArtifactValidationError):
        _presentation_result(request, InvalidAuthor(), None)

    class ValidAuthor:
        def author(self, request: PresentationRequest) -> PresentationResult:
            return PresentationResult({"slot-1": "text"})

    result = _presentation_result(request, ValidAuthor(), None)
    assert result.texts == {"slot-1": "text"}


def test_presentation_texts_cover_empty_type_slot_length_and_digest_boundaries() -> None:
    assert _normalize_texts({}) == {}
    assert _normalize_texts({"slot-1": "text"}) == {"slot-1": "text"}
    with pytest.raises(TypeError):
        _normalize_texts(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _normalize_texts({1: "text"})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        _normalize_texts({"slot-1": 1})  # type: ignore[dict-item]

    digest = _author_digest("", {"slot-1": "text"})
    assert len(digest) == 64
    assert _author_digest("a" * 64, {"slot-1": "text"}) == "a" * 64
    with pytest.raises(ValueError):
        _author_digest("not-a-digest", {"slot-1": "text"})

    empty_request = PresentationRequest(slots=())
    PresentationResult().validate_for(empty_request)
    request = PresentationRequest(slots=(PresentationSlot("slot-1", "test", "user", 1),))
    with pytest.raises(ValueError, match="omitted"):
        PresentationResult().validate_for(request)
    with pytest.raises(ValueError, match="unknown"):
        PresentationResult({"slot-1": "x", "extra": "y"}).validate_for(request)
    with pytest.raises(ValueError, match="exceeds"):
        PresentationResult({"slot-1": "too long"}).validate_for(request)


def test_unicode_normalization_rejects_invalid_object_keys_and_nonfinite_values() -> None:
    assert normalize_unicode("e\u0301") == "é"
    assert normalize_unicode(["e\u0301"]) == ["é"]
    assert normalize_unicode(("e\u0301",)) == ["é"]
    assert normalize_unicode(1.25) == 1.25
    assert normalize_unicode(None) is None

    class SampleModel(BaseModel):
        value: str

    assert normalize_unicode(SampleModel(value="e\u0301")) == {"value": "é"}
    with pytest.raises(TypeError, match="keys must be strings"):
        normalize_unicode({1: "value"})
    with pytest.raises(ValueError, match="collide"):
        normalize_unicode({"é": "first", "e\u0301": "second"})
    with pytest.raises(ValueError, match="NaN"):
        normalize_unicode(float("nan"))


@pytest.mark.parametrize(
    ("field_path", "expected"),
    [
        ("arguments.account_id", ["account_id"]),
        ("function.arguments.account_id", ["account_id"]),
        ("tool_call.arguments.account_id", ["account_id"]),
    ],
)
def test_argument_paths_accept_only_closed_prefixes(field_path: str, expected: list[str]) -> None:
    assert _argument_parts(field_path) == expected


@pytest.mark.parametrize("field_path", ["account_id", "arguments"])
def test_argument_paths_reject_missing_or_unknown_paths(field_path: str) -> None:
    with pytest.raises(ArtifactValidationError):
        _argument_parts(field_path)


def test_tool_result_reference_and_name_validation_cover_all_outcomes() -> None:
    assert _tool_call_reference_errors({"tool_call_id": "call-1"}, 1, {"call-1": 0}) == []
    assert _tool_call_reference_errors({}, 1, {})
    assert _tool_call_reference_errors({"tool_call_id": "call-2"}, 1, {"call-2": 2})
    assert _tool_result_name_errors({"name": "tool"}, 1) == []
    assert _tool_result_name_errors({"tool_name": "tool"}, 1) == []
    assert _tool_result_name_errors({}, 1)


def test_record_tool_name_prefers_tool_name_and_handles_missing_name() -> None:
    used: set[str] = set()
    _record_tool_name({"tool_name": "preferred", "name": "fallback"}, used)
    _record_tool_name({"name": "fallback"}, used)
    _record_tool_name({}, used)
    assert used == {"preferred", "fallback"}


def test_turn_header_and_call_function_validation_cover_malformed_values() -> None:
    assert _validate_turn_header(
        {"source_step_id": "S-1", "turn_id": "turn-1", "role": "user", "content": "x"}, 0
    ) == ([], "S-1", "turn-1", "user")
    errors, source_id, turn_id, role = _validate_turn_header(
        {"source_step_id": 1, "role": "unknown", "content": None}, 1
    )
    assert source_id is None
    assert turn_id is not None
    assert role == "unknown"
    assert len(errors) == 3
    errors, source_id, turn_id, role = _validate_turn_header({"role": "system", "content": "x"}, 2)
    assert errors == []
    assert source_id is None and turn_id is None and role == "system"

    errors, function = _validate_call_function({}, 0, set())
    assert errors and function is None
    errors, function = _validate_call_function({"function": {"name": 1}}, 1, set())
    assert errors and function is not None
    used: set[str] = set()
    errors, function = _validate_call_function({"function": {"name": "tool"}}, 2, used)
    assert errors == [] and function is not None and used == {"tool"}


def test_turn_record_and_attack_index_validation_cover_invalid_records() -> None:
    source_ids: list[str] = []
    source_turn_ids: list[str] = []
    used_tools: set[str] = set()
    errors = _validate_turn_records(
        [
            {"turn_id": "turn-1", "source_step_id": "S-1", "role": "user", "content": "x"},
            {"turn_id": "turn-2", "source_step_id": "S-1", "role": "user", "content": "x"},
            None,
        ],
        source_ids,
        source_turn_ids,
        used_tools,
    )
    assert any("exactly once" in error for error in errors)
    assert any("turn 2 must be an object" in error for error in errors)
    assert source_ids == ["S-1", "S-1"]
    assert _attack_index_errors({"attack_turn_index": 0}, [{"adversarial": True}]) == []
    assert _attack_index_errors({"attack_turn_index": None}, [{"adversarial": True}])
    assert _attack_index_errors({"attack_turn_index": 2}, [{"adversarial": True}])
    assert _attack_index_errors({"attack_turn_index": 0}, [{"adversarial": False}])
    assert _attack_index_errors({"attack_turn_index": 0}, [None])


def test_source_turn_ids_and_placeholders_cover_empty_and_nested_values() -> None:
    assert _source_turn_ids(None) == []
    assert _source_turn_ids(
        [{"source_step_id": "S-1", "turn_id": "turn-1"}, {"source_step_id": None}, None]
    ) == ["turn-1"]
    assert _contains_placeholder({"binding_ref": "B"}) is True
    assert _contains_placeholder({"nested": [{"binding_ref": "B"}]}) is True
    assert _contains_placeholder({"nested": []}) is False
    assert _contains_placeholder([]) is False
    assert _contains_placeholder("plain") is False


def test_trace_compiler_and_digest_validators_cover_mismatch_and_canonical_errors() -> None:
    data = {"platform": "garak", "adapter_version": "garak-stpa-v1"}
    valid_compiler = {
        "compiler": {
            "platform": "garak",
            "adapter_version": "garak-stpa-v1",
            "compiler_version": GARAK_COMPILER_VERSION,
        }
    }
    assert validate_compiler_in_artifact(valid_compiler, data) == []
    assert validate_compiler_in_artifact({}, data)
    assert validate_compiler_in_artifact(
        {
            "compiler": {
                "platform": "other",
                "adapter_version": "garak-stpa-v1",
                "compiler_version": GARAK_COMPILER_VERSION,
            }
        },
        data,
    )
    assert validate_compiler_in_artifact(
        {
            "compiler": {
                "platform": "garak",
                "adapter_version": "other",
                "compiler_version": GARAK_COMPILER_VERSION,
            }
        },
        data,
    )
    assert validate_compiler_in_artifact(
        {
            "compiler": {
                "platform": "garak",
                "adapter_version": "garak-stpa-v1",
                "compiler_version": "other",
            }
        },
        data,
    )

    artifact_body = {"schema_version": "garak-execution-artifact-v1", "value": 1}
    artifact = {
        **artifact_body,
        "artifact_digest": compute_framed_digest("garak-execution-artifact-v1", artifact_body),
    }
    assert _validate_artifact_digest({}) == []
    assert _validate_artifact_digest(artifact) == []
    assert _validate_artifact_digest({**artifact, "value": 2})
    assert _validate_artifact_digest({"artifact_digest": "a" * 64, "value": {1, 2}})

    trace_body = {"schema_version": "artifact-trace-v1", "value": 1}
    trace = {
        **trace_body,
        "trace_digest": compute_framed_digest("artifact-trace-v1", trace_body),
    }
    assert _validate_trace_digest({"trace_digest": "bad"})
    assert _validate_trace_digest(trace) == []
    assert _validate_trace_digest({**trace, "value": 2})
    assert _validate_trace_digest(
        {"schema_version": "artifact-trace-v1", "trace_digest": "a" * 64, "value": {1, 2}}
    )


def test_trace_entry_and_digest_helpers_cover_invalid_values() -> None:
    step = ArtifactElementTrace("turn-1", "S-1")
    oracle = OracleTrace("detector-1", "OUTCOME-1", "tool_argument")
    _validate_trace_entries((step,), (oracle,))
    with pytest.raises(TypeError):
        _validate_trace_entries(("bad",), (oracle,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _validate_trace_entries((step,), ("bad",))  # type: ignore[arg-type]
    source = {
        "bundle_digest": "a" * 64,
        "projection_semantic_digest": "b" * 64,
        "scenario_content_sha256": "c" * 64,
        "projection_content_sha256": "d" * 64,
        "run_id": "run-1",
        "scenario_id": "scenario-1",
        "candidate_id": "candidate-1",
        "ica_slot_id": "slot-1",
        "ica_id": "ica-1",
    }
    trace = ArtifactTrace(
        source={"projection_schema_version": "stpa-execution-projection-v2", **source},
        binding={"binding_set_id": "bind-1", "semantic_digest": "e" * 64},
        compiler={"platform": "garak", "adapter_version": "v1", "compiler_version": "v1"},
        step_map=(step,),
        oracle_map=(oracle,),
    )
    _set_trace_digest(trace)
    object.__setattr__(trace, "trace_digest", "bad")
    with pytest.raises(ValueError):
        _set_trace_digest(trace)
    receipt = ObservationReceipt(
        artifact_digest="a" * 64,
        projection_semantic_digest="b" * 64,
        binding_set_digest="c" * 64,
        execution_id="execution-1",
    )
    _set_receipt_digest(receipt)
    object.__setattr__(receipt, "receipt_digest", "bad")
    with pytest.raises(ValueError):
        _set_receipt_digest(receipt)
    assert (
        validate_compiler_in_trace(
            {"platform": "garak", "adapter_version": "v1", "compiler_version": "v1"}
        )
        is None
    )
    with pytest.raises(TypeError):
        validate_compiler_in_trace(None)
