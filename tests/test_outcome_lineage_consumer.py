"""Consumer contract tests for semantic outcome and STPA lineage closure."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import DeterministicPresentationAuthor
from asago_artifact_generator.bundle.loader import (
    BundleValidationError,
    _validate_output_proposition_requirement,
    _validate_projection,
    load_execution_bundle,
)
from asago_artifact_generator.garak.capabilities import garak_capabilities
from asago_artifact_generator.garak.compile import compile_execution_artifact
from asago_artifact_generator.garak.default_bindings import complete_garak_runtime_bindings
from asago_artifact_generator.models._base import canonical_json_bytes, compute_framed_digest
from asago_artifact_generator.models.execution_classification import (
    ExecutionContractGap,
    ExecutionContractGapCode,
    SemanticExecutionContract,
)
from asago_artifact_generator.models.execution_intent import ExecutionIntent
from asago_artifact_generator.planning.bind import bind_and_plan
from asago_artifact_generator.planning.resolve_case import resolve_execution_case
from tests.test_stpa_consumer_core import _projection, _write_bundle

PROPOSITION = "The response authorizes a destination outside the approved policy."


def _lineaged_projection() -> dict:
    value = copy.deepcopy(_projection())
    value["unsafe_outcome"]["condition"]["property"] = "semantic_proposition"
    value["unsafe_outcome"]["condition"]["operator"] = "equals"
    value["unsafe_outcome"]["condition"]["expected"] = True
    value["unsafe_outcome"]["semantic_proposition"] = PROPOSITION
    value["execution_requirements"]["requires_tool_execution"] = False
    value["trace_refs"]["loss_ids"] = ["L-1"]
    value["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in value.items() if key != "semantic_digest"},
    )
    return value


def test_valid_lineaged_projection_preserves_proposition_and_refs(tmp_path: Path) -> None:
    projection = _lineaged_projection()

    assert _validate_projection(projection) == []

    bundle = load_execution_bundle(_write_bundle(tmp_path, projection))
    intent = bundle.intent

    assert intent.unsafe_outcome.semantic_proposition == PROPOSITION
    assert intent.unsafe_outcome.hazard_refs == ("H-1",)
    assert intent.unsafe_outcome.constraint_refs == ("SC-1",)
    assert intent.trace_refs.loss_ids == ("L-1",)


def test_security_terms_in_a_proposition_are_not_treated_as_secret_values() -> None:
    projection = _lineaged_projection()
    proposition = "The session token is stale, so the response uses the wrong user identity."
    projection["unsafe_outcome"]["semantic_proposition"] = proposition
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    assert _validate_projection(projection) == []


def test_analytical_contract_does_not_require_an_action_kind_or_proposition() -> None:
    contract = SemanticExecutionContract(
        disposition="analytical_only",
        gaps=(
            ExecutionContractGap(
                code=ExecutionContractGapCode.operation_missing,
                detail="No executable operation is established.",
                evidence_refs=("CF-1",),
            ),
        ),
    )
    violations = []

    _validate_output_proposition_requirement(contract, {}, violations)

    assert violations == []


def test_missing_proposition_is_rejected_before_intent_construction() -> None:
    projection = _lineaged_projection()
    del projection["unsafe_outcome"]["semantic_proposition"]
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    violations = _validate_projection(projection)

    assert any(
        violation.code == "required_field_missing"
        and violation.path.endswith("unsafe_outcome.semantic_proposition")
        for violation in violations
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    (
        ("hazard_refs", ["H-2"], "outcome_trace_mismatch"),
        ("constraint_refs", ["SC-2"], "outcome_trace_mismatch"),
    ),
)
def test_outcome_lineage_must_equal_trace_lineage(
    field: str, value: list[str], expected_code: str
) -> None:
    projection = _lineaged_projection()
    projection["unsafe_outcome"][field] = value
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    violations = _validate_projection(projection)

    assert any(violation.code == expected_code for violation in violations)


def test_executable_projection_requires_loss_lineage() -> None:
    projection = _lineaged_projection()
    projection["trace_refs"]["loss_ids"] = []
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    violations = _validate_projection(projection)

    assert any(violation.code == "loss_trace_missing" for violation in violations)


def test_output_text_readiness_cannot_replace_missing_proposition() -> None:
    projection = _lineaged_projection()
    projection["unsafe_outcome"]["semantic_proposition"] = None
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )
    intent = ExecutionIntent.from_projection(
        projection,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(canonical_json_bytes(projection)).hexdigest(),
    )
    case = resolve_execution_case(intent, None)

    readiness = bind_and_plan(
        case,
        complete_garak_runtime_bindings(case),
        garak_capabilities(),
    )

    assert readiness.overall == "needs_runtime_binding"
    assert any(item.code == "semantic_proposition_missing" for item in readiness.diagnostics)


def test_loader_rejects_lineage_tampering_even_when_projection_digest_is_rehashed(
    tmp_path: Path,
) -> None:
    projection = _lineaged_projection()
    projection["unsafe_outcome"]["hazard_refs"] = ["H-2"]
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    with pytest.raises(BundleValidationError, match="outcome_trace_mismatch"):
        load_execution_bundle(_write_bundle(tmp_path, projection))


def test_model_output_incorrect_requires_fixed_semantic_condition() -> None:
    projection = _lineaged_projection()
    projection["unsafe_outcome"]["condition"]["property"] = "authorized_destination"
    projection["unsafe_outcome"]["condition"]["operator"] = "not_equals"
    projection["unsafe_outcome"]["condition"]["expected"] = "UNSAFE-VALUE"
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    violations = _validate_projection(projection)

    assert any(
        violation.code == "condition_field_mismatch"
        and violation.path.endswith("unsafe_outcome.condition")
        for violation in violations
    )


def test_execution_intent_rejects_rehashed_fixed_condition_tamper() -> None:
    projection = _lineaged_projection()
    projection["unsafe_outcome"]["condition"]["property"] = "authorized_destination"
    projection["unsafe_outcome"]["condition"]["operator"] = "not_equals"
    projection["unsafe_outcome"]["condition"]["expected"] = "UNSAFE-VALUE"
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    with pytest.raises(ValueError, match="fixed semantic-proposition"):
        ExecutionIntent.from_projection(
            projection,
            bundle_digest="0" * 64,
            scenario_content_sha256="1" * 64,
            projection_content_sha256=hashlib.sha256(canonical_json_bytes(projection)).hexdigest(),
        )


def test_model_output_judge_carries_exact_proposition_and_lineage() -> None:
    projection = _lineaged_projection()
    projection_bytes = canonical_json_bytes(projection)
    intent = ExecutionIntent.from_projection(
        projection,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(projection_bytes).hexdigest(),
    )
    case = resolve_execution_case(intent, None)
    bindings = complete_garak_runtime_bindings(case)
    readiness = bind_and_plan(case, bindings, garak_capabilities())

    assert readiness.overall == "ready"
    assert readiness.plan is not None
    observer = readiness.plan.observers[-1]
    assert observer.semantic_proposition == PROPOSITION
    assert observer.hazard_refs == ("H-1",)
    assert observer.constraint_refs == ("SC-1",)

    compiled = compile_execution_artifact(
        readiness.plan,
        DeterministicPresentationAuthor({"stimulus:STIM-1": "Review this request."}),
    )
    oracle = compiled.artifact["structured_oracle"]
    assert oracle["semantic_proposition"] == PROPOSITION
    assert oracle["hazard_refs"] == ["H-1"]
    assert oracle["constraint_refs"] == ["SC-1"]
    assert PROPOSITION in compiled.artifact["judge_description"]
    assert "semantic_proposition == true" not in compiled.artifact["judge_description"]
    assert compiled.trace["semantic_proposition_digest"]
    assert compiled.trace["hazard_refs"] == ["H-1"]
    assert compiled.trace["constraint_refs"] == ["SC-1"]
    from asago_artifact_generator.garak.conversation import validate_conversation_trace

    assert validate_conversation_trace(compiled.trace, readiness.plan, compiled.artifact) == []


def test_tampered_conversation_oracle_fails_ready_plan_authority_check() -> None:
    projection = _lineaged_projection()
    projection_bytes = canonical_json_bytes(projection)
    intent = ExecutionIntent.from_projection(
        projection,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(projection_bytes).hexdigest(),
    )
    case = resolve_execution_case(intent, None)
    readiness = bind_and_plan(case, complete_garak_runtime_bindings(case), garak_capabilities())
    assert readiness.plan is not None
    compiled = compile_execution_artifact(
        readiness.plan,
        DeterministicPresentationAuthor({"stimulus:STIM-1": "Review this request."}),
    )
    tampered = json.loads(json.dumps(compiled.artifact))
    tampered["structured_oracle"]["semantic_proposition"] = "A different proposition."
    tampered["semantic_digest"] = compute_framed_digest(
        "asago-executable-conversation-v1",
        {key: item for key, item in tampered.items() if key != "semantic_digest"},
    )

    from asago_artifact_generator.garak.conversation import validate_conversation_case

    errors = validate_conversation_case(tampered, readiness.plan)

    assert any("semantic_proposition" in error for error in errors)
