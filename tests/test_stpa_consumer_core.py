"""Focused acceptance tests for the strict STPA consumer inward seams."""

from __future__ import annotations

import hashlib
import json
import math
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from asago_artifact_generator.authoring import DeterministicPresentationAuthor
from asago_artifact_generator.bundle.loader import (
    BundleValidationError,
    ValidationViolation,
    _classification_has_bindings,
    _validate_categories,
    _validate_condition,
    _validate_entry_order,
    _validate_final_step,
    _validate_integer_placeholder_bounds,
    _validate_one_placeholder_bound,
    _validate_projection,
    _validate_projection_identity,
    _validate_projection_trace,
    _validate_quantity,
    _validate_step_mapping,
    load_execution_bundle,
)
from asago_artifact_generator.garak.capabilities import garak_capabilities
from asago_artifact_generator.garak.compile import compile_execution_artifact
from asago_artifact_generator.models._base import canonical_json_bytes, compute_framed_digest
from asago_artifact_generator.models.execution_case import BoundExecutionCase
from asago_artifact_generator.models.execution_classification import (
    BindingCompleteness,
    EnvironmentBasis,
    ExecutionActionKind,
    ExecutionClaimScope,
    ExecutionClassification,
    ExecutionDeliveryClass,
    ExecutionProfileFit,
    ExecutionTargetProfile,
    RequestedEnvironmentBasis,
    SemanticExecutionContract,
    SemanticExecutionDelivery,
)
from asago_artifact_generator.models.execution_intent import (
    AdversarialStimulusRequirement,
    CausalFactor,
    ExecutionIntent,
    ExecutionRequirements,
    ExecutionStep,
    SemanticBindingPlaceholder,
    TraceReferences,
    UnsafeOutcome,
)
from asago_artifact_generator.models.readiness import ExecutionPlanResult, PlatformCapabilities
from asago_artifact_generator.models.runtime_binding import (
    AdversarialStimulusBinding,
    ClockBinding,
    ControlActionBinding,
    ObservationBinding,
    ReviewEvidence,
    RuntimeBindingSet,
    RuntimeBindingValidationError,
    SemanticBinding,
    SurfaceBinding,
    parse_runtime_binding_set,
)
from asago_artifact_generator.models.semantic_conditions import (
    AbsenceCondition,
    ActionPresenceCondition,
    ActionValueCondition,
    DelayCondition,
    DurationCondition,
    OrderingCondition,
    StateValueCondition,
    WindowCondition,
    _validate_integer_bounds,
    _validate_number_bounds,
    _validate_scalar,
    _validate_time_value,
)
from asago_artifact_generator.planning.bind import (
    _placeholder_bounds_match,
    _replace_placeholder,
    bind_and_plan,
)
from asago_artifact_generator.planning.resolve_case import (
    _classification_has_no_bindings,
    resolve_execution_case,
)

CONTRACT_ROOT = Path(__file__).parents[1] / "contracts" / "stpa-execution"


def test_target_profile_pin_is_provenance_not_an_execution_binding() -> None:
    classification = ExecutionClassification(
        binding_completeness=BindingCompleteness.analytical_only,
        environment_basis=EnvironmentBasis.none,
        profile_fit=ExecutionProfileFit.invalid,
        claim_scope=ExecutionClaimScope.no_execution_claim,
        target_profile_digest="a" * 64,
    )

    assert _classification_has_bindings(classification) is False
    assert _classification_has_no_bindings(classification) is True


def _projection(*, temporal: dict | None = None) -> dict:
    condition = {
        "type": "action_value",
        "control_action_id": "CM-1",
        "property": "semantic_proposition",
        "operator": "equals",
        "expected": True,
    }
    value = {
        "schema_version": "stpa-execution-projection-v2",
        "run_id": "run-1",
        "scenario_id": "SCN-001",
        "candidate_id": "EXEC:CL-1:CM-1:INCORRECT",
        "ica_slot_id": "CL-1:CM-1:INCORRECT",
        "ica_id": "CL-1:CM-1:INCORRECT:1",
        "controller_id": "CL-1",
        "control_action_id": "CM-1",
        "uca_type": "INCORRECT",
        "causal_factors": [
            {
                "factor_id": "CF-1",
                "order": 1,
                "kind": "PROCESS_MODEL_FLAW",
                "structural_source_id": "PM-1-1",
                "description": "the model state diverges",
                "evidence_status": "structural_failure",
                "capability_refs": [],
                "access_refs": [],
                "bounded_assumption": None,
                "temporal_condition": temporal,
            }
        ],
        "steps": [
            {
                "step_id": "S-1",
                "order": 1,
                "kind": "CAUSAL_FACTOR",
                "factor_id": "CF-1",
                "structural_source_id": "PM-1-1",
            },
            {
                "step_id": "S-2",
                "order": 2,
                "kind": "UNSAFE_CONTROL_ACTION",
                "factor_id": None,
                "structural_source_id": "CM-1",
            },
        ],
        "unsafe_outcome": {
            "outcome_id": "OUTCOME-1",
            "control_action_id": "CM-1",
            "uca_type": "INCORRECT",
            "condition": condition,
            "semantic_proposition": "The response exhibits the unsafe semantic behavior.",
            "semantic_binding_required": False,
            "hazard_refs": ["H-1"],
            "constraint_refs": ["SC-1"],
        },
        "stimulus_requirements": [
            {
                "stimulus_id": "STIM-1",
                "intent": "Influence the control decision through its external input.",
                "desired_effect": "Cause the unsafe target action.",
                "delivery_class": "direct_prompt",
                "factor_id": "CF-1",
                "source_role": "direct_user_input",
                "carrier_requirement_id": None,
            }
        ],
        "execution_requirements": {
            "requires_multi_turn": False,
            "requires_tool_execution": True,
            "requires_persistent_state": False,
            "requires_multi_agent": False,
            "requires_real_clock": bool(temporal),
            "requires_state_observation": False,
            "required_surface_categories": ["external_input"],
        },
        "execution_contract": {
            "schema_version": "stpa-execution-contract-v1",
            "disposition": "executable_route",
            "requested_environment_basis": "target_agnostic",
            "delivery": {
                "delivery_class": "direct_prompt",
                "factor_id": "CF-1",
                "source_role": "direct_user_input",
                "carrier_requirement_id": None,
            },
            "action_kind": "model_output",
            "resource_requirements": [],
            "gaps": [],
        },
        "execution_classification": {
            "schema_version": "stpa-execution-classification-v1",
            "binding_completeness": "concrete",
            "environment_basis": "target_agnostic",
            "profile_fit": "not_required",
            "claim_scope": "model_behavior_only",
            "resolved_bindings": [],
            "unresolved_requirement_ids": [],
            "ambiguous_matches": [],
            "unsupported_requirement_ids": [],
            "diagnostics": [],
            "target_profile_digest": None,
        },
        "trace_refs": {
            "obligation_ids": [],
            "risk_ids": [],
            "attack_pattern_ids": [],
            "technique_ids": [],
            "loss_ids": ["L-1"],
            "hazard_ids": ["H-1"],
            "constraint_ids": ["SC-1"],
            "source_pins": {
                "control_structure": "a" * 64,
                "loss_analysis": "b" * 64,
                "ica_enumeration": "c" * 64,
                "scenario_context": "d" * 64,
            },
        },
    }
    value["execution_contract"]["semantic_digest"] = compute_framed_digest(
        "stpa-execution-contract-v1",
        {
            key: item
            for key, item in value["execution_contract"].items()
            if key != "semantic_digest"
        },
    )
    value["execution_classification"]["classification_digest"] = compute_framed_digest(
        "stpa-execution-classification-v1",
        {
            key: item
            for key, item in value["execution_classification"].items()
            if key != "classification_digest"
        },
    )
    value["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in value.items() if key != "semantic_digest"},
    )
    return value


def _write_bundle(tmp_path: Path, projection: dict | None = None) -> Path:
    projection = projection or _projection()
    scenario = {
        "scenario_id": "SCN-001",
        "target_responsibility": projection["controller_id"],
        "ica_type": projection["uca_type"],
        "scenario_spec": {
            "scenario_id": "SCN-001",
            "target_controller": projection["controller_id"],
            "target_control_action": projection["control_action_id"],
            "ica_type": projection["uca_type"],
            "threat_source": {
                "ica_slot_id": projection["ica_slot_id"],
                "ica_id": projection["ica_id"],
            },
        },
        "narrative": {"summary": "presentation only"},
    }
    scenario_path = tmp_path / "scenarios" / "SCN-001.scenario.json"
    projection_path = tmp_path / "scenarios" / "canonical" / "SCN-001.projection.json"
    scenario_path.parent.mkdir(parents=True)
    projection_path.parent.mkdir(parents=True)
    scenario_bytes = canonical_json_bytes(scenario)
    projection_bytes = canonical_json_bytes(projection)
    scenario_path.write_bytes(scenario_bytes)
    projection_path.write_bytes(projection_bytes)
    index = {
        "schema_version": "stpa-execution-bundle-v1",
        "run_id": "run-1",
        "producer": {"name": "asago-scenario-generator", "version": "test"},
        "entries": [
            {
                "scenario_id": "SCN-001",
                "candidate_id": projection["candidate_id"],
                "ica_slot_id": projection["ica_slot_id"],
                "ica_id": projection["ica_id"],
                "scenario": {
                    "path": "scenarios/SCN-001.scenario.json",
                    "content_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
                },
                "projection": {
                    "path": "scenarios/canonical/SCN-001.projection.json",
                    "schema_version": "stpa-execution-projection-v2",
                    "content_sha256": hashlib.sha256(projection_bytes).hexdigest(),
                    "semantic_digest": projection["semantic_digest"],
                },
                "validation": {
                    "status": "valid",
                    "validator_version": "stpa-execution-projection-v2",
                },
            }
        ],
    }
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path = tmp_path / "execution-bundle.json"
    path.write_bytes(canonical_json_bytes(index))
    return path


def _intent(*, placeholder: bool = False, temporal: bool = False) -> ExecutionIntent:
    temporal_condition = DelayCondition(reference_ref="FB-1-1", delay_ms=100) if temporal else None
    condition = ActionValueCondition(
        control_action_id="CM-1",
        property="semantic_proposition",
        operator="equals",
        expected=True,
    )
    factor = CausalFactor(
        factor_id="CF-1",
        order=1,
        kind="PROCESS_MODEL_FLAW",
        structural_source_id="PM-1-1",
        description="state diverges",
        evidence_status="structural_failure",
        temporal_condition=temporal_condition,
    )
    return ExecutionIntent(
        bundle_digest="a" * 64,
        projection_semantic_digest="b" * 64,
        run_id="run-1",
        scenario_id="SCN-001",
        candidate_id="EXEC:CL-1:CM-1:INCORRECT",
        ica_slot_id="CL-1:CM-1:INCORRECT",
        ica_id="CL-1:CM-1:INCORRECT:1",
        controller_id="CL-1",
        control_action_id="CM-1",
        uca_type="INCORRECT",
        causal_factors=(factor,),
        steps=(
            ExecutionStep(
                step_id="S-1",
                order=1,
                kind="CAUSAL_FACTOR",
                factor_id="CF-1",
                structural_source_id="PM-1-1",
            ),
            ExecutionStep(
                step_id="S-2",
                order=2,
                kind="UNSAFE_CONTROL_ACTION",
                factor_id=None,
                structural_source_id="CM-1",
            ),
        ),
        unsafe_outcome=UnsafeOutcome(
            outcome_id="OUTCOME-1",
            control_action_id="CM-1",
            uca_type="INCORRECT",
            condition=condition,
            semantic_proposition="The response exhibits the unsafe semantic behavior.",
            semantic_binding_required=False,
            hazard_refs=("H-1",),
            constraint_refs=("SC-1",),
        ),
        stimulus_requirements=(
            AdversarialStimulusRequirement(
                stimulus_id="STIM-1",
                intent="Influence the control decision through its external input.",
                desired_effect="Cause the unsafe target action.",
                delivery_class=ExecutionDeliveryClass.direct_prompt,
                factor_id="CF-1",
                source_role="direct_user_input",
            ),
        ),
        execution_requirements=ExecutionRequirements(
            requires_tool_execution=False,
            requires_real_clock=temporal,
            required_surface_categories=("external_input",),
        ),
        execution_contract=SemanticExecutionContract(
            requested_environment_basis=RequestedEnvironmentBasis.target_agnostic,
            delivery=SemanticExecutionDelivery(
                delivery_class=ExecutionDeliveryClass.direct_prompt,
                factor_id="CF-1",
                source_role="direct_user_input",
            ),
            action_kind=ExecutionActionKind.model_output,
        ),
        execution_classification=ExecutionClassification(
            binding_completeness=BindingCompleteness.concrete,
            environment_basis=EnvironmentBasis.target_agnostic,
            profile_fit=ExecutionProfileFit.not_required,
            claim_scope=ExecutionClaimScope.model_behavior_only,
        ),
        trace_refs=TraceReferences(
            loss_ids=("L-1",),
            hazard_ids=("H-1",),
            constraint_ids=("SC-1",),
            source_pins={
                "control_structure": "a" * 64,
                "loss_analysis": "b" * 64,
                "ica_enumeration": "c" * 64,
                "scenario_context": "d" * 64,
            },
        ),
        source_file_digests={"scenario": "e" * 64, "projection": "f" * 64},
    )


def _execution_case(intent: ExecutionIntent) -> BoundExecutionCase:
    """Resolve a test intent before exercising readiness or compilation."""
    result = resolve_execution_case(intent, None)
    assert isinstance(result, BoundExecutionCase)
    return result


def _capabilities(*, clock: bool = False) -> PlatformCapabilities:
    return PlatformCapabilities(
        platform="garak",
        adapter_version="garak-stpa-v1",
        writable_surfaces=("user_turn", "tool_call", "assistant_turn"),
        invocable_operations=("tool_call", "chat_completion"),
        observer_kinds=("tool_argument", "elapsed_time", "output_text"),
        supports_multi_turn=True,
        supports_real_clock=clock,
    )


def _bindings(
    intent: ExecutionIntent,
    *,
    temporal: bool = False,
    adapter_operation: str = "chat_completion",
    target_surface: str = "assistant_turn",
) -> RuntimeBindingSet:
    review = ReviewEvidence(
        reviewed_by="operator@example",
        reviewed_at="2026-09-02T12:00:00Z",
        rationale="test binding",
        evidence_refs=("change-1",),
    )
    semantic = []
    if intent.unsafe_outcome.semantic_binding_required:
        semantic.append(
            SemanticBinding(
                condition_ref="OUTCOME-1",
                binding_ref="SEM-1",
                value="AUTHORIZED-ACCOUNT-001",
                reviewed_by="operator@example",
                rationale="test account",
                evidence_refs=("change-1",),
            )
        )
    observations = [
        ObservationBinding(
            condition_ref="OUTCOME-1",
            observer_kind="output_text"
            if adapter_operation == "chat_completion"
            else "tool_argument",
            event_source="trace.tool_calls",
            semantic_property=intent.unsafe_outcome.condition.property,
            field_path=f"arguments.{intent.unsafe_outcome.condition.property}",
            comparison="equals",
            expected_from="projection",
        )
    ]
    if temporal:
        observations.insert(
            0,
            ObservationBinding(
                condition_ref="CF-1",
                observer_kind="elapsed_time",
                event_source="trace.timestamps",
                semantic_property="delay_ms",
                comparison="less_than_or_equal",
                expected_from="projection",
            ),
        )
    return RuntimeBindingSet.create(
        binding_set_id="BIND-1",
        projection_semantic_digest=intent.projection_semantic_digest,
        target_environment_id="test",
        review=review,
        semantic_bindings=semantic,
        surface_bindings=[
            SurfaceBinding(
                source_ref="PM-1-1",
                surface="user_turn",
                locator="conversation.input",
                writable=True,
            ),
            SurfaceBinding(
                source_ref="CM-1",
                surface=target_surface,
                locator="trace.tool_calls",
                writable=True,
            ),
        ],
        control_action_bindings=[
            ControlActionBinding(
                control_action_id="CM-1",
                adapter_operation=adapter_operation,
                tool_name="authorize_transaction",
                tool_schema={"type": "object"},
                safe_defaults={"account_id": "TEST-ACCOUNT-001"},
            )
        ],
        observation_bindings=observations,
        stimulus_bindings=[
            AdversarialStimulusBinding(
                stimulus_id="STIM-1",
                projection_step_id="S-1",
                factor_id="CF-1",
                content_slot_id="stimulus:STIM-1",
                delivery_class="direct_prompt",
                surface="user_turn",
                source_kind="user_authored",
                review=review,
            )
        ],
        clock_binding=(
            ClockBinding(
                clock_kind="monotonic",
                resolution_ms=10,
                event_timestamp_source="harness.monotonic_timestamp",
            )
            if temporal
            else None
        ),
    )


def _vendored_intent(fixture_name: str) -> ExecutionIntent:
    fixture_path = CONTRACT_ROOT / "projection-v2" / "valid" / fixture_name
    projection_bytes = fixture_path.read_bytes()
    projection = json.loads(projection_bytes)
    return ExecutionIntent.from_projection(
        projection,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(projection_bytes).hexdigest(),
    )


def test_consumer_rejects_outcome_ordered_before_itself() -> None:
    payload = _vendored_intent("ordering.json").model_dump(mode="json")
    payload["unsafe_outcome"]["condition"]["reference_step_id"] = payload["steps"][-1]["step_id"]
    with pytest.raises(ValueError, match="ordering cannot compare.*itself"):
        ExecutionIntent.model_validate(payload)


def _vendored_runtime_bindings(intent: ExecutionIntent) -> RuntimeBindingSet:
    answer = intent.execution_contract.action_kind.value == "model_output"
    review = ReviewEvidence(
        reviewed_by="matrix@example",
        reviewed_at="2026-09-02T12:00:00Z",
        rationale="Coordinated deterministic acceptance case.",
        evidence_refs=("matrix-case-1",),
    )
    surfaces_by_ref: dict[str, SurfaceBinding] = {}
    for step in intent.steps:
        surface = (
            ("assistant_turn" if answer else "tool_call")
            if step.kind == "UNSAFE_CONTROL_ACTION"
            else "user_turn"
        )
        surfaces_by_ref[step.structural_source_id] = SurfaceBinding(
            source_ref=step.structural_source_id,
            surface=surface,
            locator=f"trace.{step.step_id}",
            writable=True,
        )

    condition_items = [
        (factor.factor_id, factor.temporal_condition)
        for factor in intent.causal_factors
        if factor.temporal_condition is not None
    ]
    condition_items.append((intent.unsafe_outcome.outcome_id, intent.unsafe_outcome.condition))
    semantic_bindings: list[SemanticBinding] = []
    observations: list[ObservationBinding] = []
    seen_placeholders: set[tuple[str, str]] = set()
    observer_kinds = {
        "ordering": "event_order",
        "delay": "elapsed_time",
        "duration": "duration",
        "window": "elapsed_time",
        "absence": "event_absence",
        "action_presence": "tool_call",
        "action_value": "tool_argument",
        "state_value": "state_value",
    }
    for condition_ref, condition in condition_items:
        placeholders = condition.placeholders()
        for placeholder in placeholders:
            key = (condition_ref, placeholder.binding_ref)
            if key in seen_placeholders:
                continue
            seen_placeholders.add(key)
            value: str | int | float | bool
            if placeholder.value_type == "string":
                value = "BOUND-VALUE"
            elif placeholder.value_type == "integer":
                value = placeholder.minimum if placeholder.minimum is not None else 1
            elif placeholder.value_type == "number":
                value = placeholder.minimum if placeholder.minimum is not None else 1.0
            else:
                value = True
            semantic_bindings.append(
                SemanticBinding(
                    condition_ref=condition_ref,
                    binding_ref=placeholder.binding_ref,
                    value=value,
                    reviewed_by="matrix@example",
                    rationale="Deterministic test value.",
                    evidence_refs=("matrix-case-1",),
                )
            )
        observer_kind = (
            "output_text"
            if answer and condition.type in {"action_value", "action_presence"}
            else observer_kinds[condition.type]
        )
        observations.append(
            ObservationBinding(
                condition_ref=condition_ref,
                observer_kind=observer_kind,
                event_source="trace.events",
                semantic_property=getattr(condition, "property", condition.type),
                field_path="arguments.value" if observer_kind == "tool_argument" else "",
                comparison=getattr(condition, "operator", "less_than_or_equal"),
                expected_from=(
                    f"semantic_binding:{placeholders[0].binding_ref}"
                    if placeholders
                    else "projection"
                ),
            )
        )

    outcome_condition = intent.unsafe_outcome.condition
    outcome_expected = getattr(outcome_condition, "expected", "approved")
    if isinstance(outcome_expected, SemanticBindingPlaceholder):
        outcome_expected = "BOUND-VALUE"
    if getattr(outcome_condition, "operator", "") == "not_equals":
        outcome_expected = "UNSAFE-VALUE"
    action = ControlActionBinding(
        control_action_id=intent.control_action_id,
        adapter_operation="chat_completion" if answer else "tool_call",
        tool_name="bound_action",
        tool_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        safe_defaults={"value": str(outcome_expected)},
    )
    return RuntimeBindingSet.create(
        binding_set_id="matrix-bind",
        projection_semantic_digest=intent.projection_semantic_digest,
        target_environment_id="matrix",
        review=review,
        semantic_bindings=semantic_bindings,
        surface_bindings=list(surfaces_by_ref.values()),
        control_action_bindings=[action],
        observation_bindings=observations,
        stimulus_bindings=[
            AdversarialStimulusBinding(
                stimulus_id="STIM-1",
                projection_step_id="S-1",
                factor_id="CF-1",
                content_slot_id="stimulus:STIM-1",
                delivery_class="direct_prompt",
                surface="user_turn",
                source_kind="user_authored",
                review=review,
            )
        ],
        clock_binding=(
            ClockBinding(
                clock_kind="monotonic",
                resolution_ms=10,
                event_timestamp_source="harness.monotonic_timestamp",
            )
            if intent.execution_requirements.requires_real_clock
            else None
        ),
    )


def _assert_unready_cannot_compile(result: object) -> None:
    calls = 0

    class FailingAuthor:
        def author(self, request: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("unready matrix case reached authoring")

    with pytest.raises(TypeError, match="ReadyExecutionPlan"):
        compile_execution_artifact(result, FailingAuthor())  # type: ignore[arg-type]
    assert calls == 0


def test_valid_bundle_produces_immutable_intent(tmp_path: Path) -> None:
    bundle = load_execution_bundle(_write_bundle(tmp_path))

    assert bundle.intent.scenario_id == "SCN-001"
    assert bundle.intent.unsafe_outcome.condition.type == "action_value"
    with pytest.raises(TypeError):
        bundle.entries[0].scenario_document["scenario_id"] = "tampered"  # type: ignore[index]


def test_vendored_contract_lock_and_minimal_bundle_are_authoritative() -> None:
    lock = json.loads((CONTRACT_ROOT / "CONTRACT.lock").read_text())
    for relative_path, expected_digest in lock["files"].items():
        path = CONTRACT_ROOT / relative_path
        assert path.is_file(), relative_path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest

    upstream = json.loads((CONTRACT_ROOT / "UPSTREAM.lock").read_text())
    assert upstream["repository"] == "asago-scenario-generator"
    assert upstream["revision"] == "a68897dca57af31eade17be0e5672f5ca41e1569"
    assert upstream["source"] == "data/contracts/stpa-execution/CONTRACT.lock"
    assert upstream["source_state"] == "committed"
    assert upstream["content_lock_status"] == "pinned"
    assert (
        upstream["contract_lock_sha256"]
        == hashlib.sha256((CONTRACT_ROOT / "CONTRACT.lock").read_bytes()).hexdigest()
    )

    bundle_path = CONTRACT_ROOT / "bundle-v1" / "valid" / "minimal-run" / "execution-bundle.json"
    verified = load_execution_bundle(bundle_path)
    assert verified.run_id == "fixture-run"
    assert verified.entries[0].candidate_id == "EXEC:RESP-1:CA-1-1:INCORRECT"
    projection = json.loads(verified.entries[0].projection_bytes)
    assert verified.entries[0].intent.projection_semantic_digest == projection["semantic_digest"]


def test_vendored_unspecified_environment_fixture_stays_pending() -> None:
    fixture_path = CONTRACT_ROOT / "projection-v2" / "valid" / "unspecified-basis.json"
    projection = json.loads(fixture_path.read_bytes())

    assert _validate_projection(projection) == []

    intent = _vendored_intent("unspecified-basis.json")

    assert intent.execution_contract.requested_environment_basis is None
    assert intent.execution_contract.resource_requirements
    assert intent.execution_classification.diagnostics[0].code == (
        "environment_profile_not_supplied"
    )

    result = resolve_execution_case(intent, None)

    assert result.code == "needs_environment_binding"
    assert result.requirement_ids == ("REQ-agent-channel",)


def test_coordinated_cross_repo_eight_case_acceptance_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_names = {
        "fully_declared_projection_garak_executable",
        "semantic_binding_needed",
        "runtime_binding_needed",
        "unsupported_by_garak",
        "malformed_projection",
        "scenario_projection_pair_mismatch",
        "binding_digest_mismatch",
        "unknown_schema_version",
    }
    executed: set[str] = set()
    assert CONTRACT_ROOT == Path(__file__).parents[1] / "contracts" / "stpa-execution"
    assert "asago-scenario-generator" not in str(CONTRACT_ROOT)

    def fail_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("coordinated consumer acceptance must not use the network")

    monkeypatch.setattr(socket, "socket", fail_network)

    # 1. A producer-published, fully declared bundle is ready and compiles.
    bundle_path = CONTRACT_ROOT / "bundle-v1" / "valid" / "minimal-run" / "execution-bundle.json"
    verified = load_execution_bundle(bundle_path)
    ready_result = bind_and_plan(
        _execution_case(verified.intent),
        _vendored_runtime_bindings(verified.intent),
        garak_capabilities(),
    )
    assert ready_result.overall == "ready"
    assert ready_result.plan is not None
    compiled = compile_execution_artifact(
        ready_result.plan,
        DeterministicPresentationAuthor(
            {slot: "Use the deployment-bound action." for slot in ready_result.plan.content_slots}
        ),
    )
    assert compiled.validation["ok"] is True
    executed.add("fully_declared_projection_garak_executable")

    # 2. The producer temporal placeholder fixture remains semantic-binding gated.
    semantic_result = bind_and_plan(
        _execution_case(_vendored_intent("delay-placeholder.json")),
        None,
        garak_capabilities(),
    )
    assert semantic_result.overall == "needs_semantic_binding"
    _assert_unready_cannot_compile(semantic_result)
    executed.add("semantic_binding_needed")

    # 3. A valid temporal projection without runtime bindings is runtime gated.
    runtime_intent = _vendored_intent("delay.json")
    # Isolate temporal readiness from the old fixture's unrelated tool flag.
    # The selected action is model_output; no tool step is declared here.
    runtime_intent = runtime_intent.model_copy(
        update={
            "execution_requirements": runtime_intent.execution_requirements.model_copy(
                update={"requires_tool_execution": False}
            )
        }
    )
    runtime_result = bind_and_plan(_execution_case(runtime_intent), None, garak_capabilities())
    assert runtime_result.overall == "needs_runtime_binding"
    _assert_unready_cannot_compile(runtime_result)
    executed.add("runtime_binding_needed")

    # 4. The same valid temporal projection is unsupported by Garak once bound.
    unsupported_result = bind_and_plan(
        _execution_case(runtime_intent),
        _vendored_runtime_bindings(runtime_intent),
        garak_capabilities(),
    )
    assert unsupported_result.overall == "unsupported"
    _assert_unready_cannot_compile(unsupported_result)
    executed.add("unsupported_by_garak")

    # 5. Producer malformed fixtures fail before inward model construction.
    malformed = json.loads(
        (CONTRACT_ROOT / "projection-v2" / "invalid" / "unknown-field.json").read_text()
    )
    malformed_codes = {violation.code for violation in _validate_projection(malformed)}
    expected_malformed = json.loads(
        (CONTRACT_ROOT / "projection-v2" / "expected-violations.json").read_text()
    )["unknown-field.json"]
    assert set(expected_malformed) <= malformed_codes
    executed.add("malformed_projection")

    # 6. Producer pair mismatch fixtures fail closed at the bundle seam.
    pair_path = CONTRACT_ROOT / "bundle-v1" / "invalid" / "pair-mismatch" / "execution-bundle.json"
    with pytest.raises(BundleValidationError, match="pair_identity_mismatch"):
        load_execution_bundle(pair_path)
    executed.add("scenario_projection_pair_mismatch")

    # 7. A consumer-derived binding digest mismatch is invalid and uncompileable.
    binding_data = _vendored_runtime_bindings(verified.intent).model_dump(mode="python")
    binding_data["semantic_digest"] = "0" * 64
    tampered_bindings = RuntimeBindingSet.model_validate(binding_data)
    binding_result = bind_and_plan(
        _execution_case(verified.intent), tampered_bindings, garak_capabilities()
    )
    assert binding_result.overall == "invalid"
    assert any(item.code == "binding_digest_mismatch" for item in binding_result.diagnostics)
    _assert_unready_cannot_compile(binding_result)
    executed.add("binding_digest_mismatch")

    # 8. A consumer-derived unknown schema version fails before file access.
    unknown_dir = tmp_path / "unknown-schema"
    unknown_dir.mkdir()
    unknown_path = _write_bundle(unknown_dir)
    unknown_index = json.loads(unknown_path.read_text())
    unknown_index["schema_version"] = "stpa-execution-bundle-v999"
    unknown_path.write_bytes(canonical_json_bytes(unknown_index))
    with pytest.raises(BundleValidationError, match="schema_version_mismatch"):
        load_execution_bundle(unknown_path)
    executed.add("unknown_schema_version")
    assert executed == case_names


@pytest.mark.parametrize("fixture_name", ("hash-mismatch", "pair-mismatch"))
def test_vendored_invalid_bundle_fixtures_fail_closed(fixture_name: str) -> None:
    path = CONTRACT_ROOT / "bundle-v1" / "invalid" / fixture_name / "execution-bundle.json"
    expected_codes = json.loads(
        (CONTRACT_ROOT / "bundle-v1" / "expected-violations.json").read_text()
    )[f"invalid/{fixture_name}"]

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(path)

    actual_codes = {violation.code for violation in error.value.violations}
    assert set(expected_codes) <= actual_codes


def test_vendored_target_profile_fixture_matches_consumer_wire() -> None:
    path = CONTRACT_ROOT / "target-profile-v1" / "valid" / "minimal.json"
    document = json.loads(path.read_text())
    profile = ExecutionTargetProfile.model_validate(document)

    assert profile.semantic_digest == document["semantic_digest"]
    assert profile.model_dump(mode="json") == document

    invalid = json.loads(
        (CONTRACT_ROOT / "target-profile-v1" / "invalid" / "unknown-field.json").read_text()
    )
    with pytest.raises(ValidationError, match="unexpected_field"):
        ExecutionTargetProfile.model_validate(invalid)


@pytest.mark.parametrize(
    ("fixture_name", "expected_codes"),
    tuple(
        (
            name,
            tuple(codes),
        )
        for name, codes in json.loads(
            (CONTRACT_ROOT / "projection-v2" / "expected-violations.json").read_text()
        ).items()
    ),
)
def test_vendored_projection_conformance_fixtures(
    fixture_name: str, expected_codes: tuple[str, ...]
) -> None:
    document = json.loads((CONTRACT_ROOT / "projection-v2" / "invalid" / fixture_name).read_text())

    actual_codes = {violation.code for violation in _validate_projection(document)}
    assert set(expected_codes) <= actual_codes


@pytest.mark.parametrize(
    "fixture_path",
    sorted((CONTRACT_ROOT / "projection-v2" / "valid").glob("*.json")),
)
def test_vendored_valid_projection_fixtures(fixture_path: Path) -> None:
    document = json.loads(fixture_path.read_text())

    assert _validate_projection(document) == []


@pytest.mark.parametrize(
    "fixture_path",
    sorted((CONTRACT_ROOT / "projection-v2" / "valid").glob("*.json")),
)
def test_vendored_valid_projections_normalize_to_inward_intents(fixture_path: Path) -> None:
    projection_bytes = fixture_path.read_bytes()
    document = json.loads(projection_bytes)

    intent = ExecutionIntent.from_projection(
        document,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(projection_bytes).hexdigest(),
    )

    assert intent.projection_semantic_digest == document["semantic_digest"]
    assert intent.source_file_digests["projection"] == hashlib.sha256(projection_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Vendored turns/ordering kit revision (producer a68897d)


def test_turn_revision_invalid_fixtures_carry_exactly_the_published_codes() -> None:
    expected = json.loads(
        (CONTRACT_ROOT / "projection-v2" / "expected-violations.json").read_text()
    )
    for name in (
        "turns-on-direct-prompt.json",
        "turns-duplicate-ids.json",
        "ordering-reference-without-argument.json",
    ):
        document = json.loads((CONTRACT_ROOT / "projection-v2" / "invalid" / name).read_text())
        actual = {violation.code for violation in _validate_projection(document)}
        assert actual == set(expected[name]), name


def test_vendored_projection_semantic_digests_recompute_from_documents() -> None:
    """Every vendored valid fixture keeps its embedded semantic digest."""

    recorded = json.loads(
        (CONTRACT_ROOT / "projection-v2" / "canonical-digests.json").read_text()
    )["semantic_digests"]
    computed_at_test_start = dict(recorded)

    for fixture_path in sorted((CONTRACT_ROOT / "projection-v2" / "valid").glob("*.json")):
        document = json.loads(fixture_path.read_text())
        body = {key: value for key, value in document.items() if key != "semantic_digest"}
        computed = compute_framed_digest("stpa-execution-projection-v2", body)
        assert computed == document["semantic_digest"], fixture_path.name
        assert computed == computed_at_test_start[f"valid/{fixture_path.name}"], fixture_path.name


@pytest.mark.parametrize(
    "fixture_name",
    ("conversation-user-turns.json", "ordering-reference-tool.json"),
)
def test_turn_revision_valid_fixtures_normalize_to_inward_intents(fixture_name: str) -> None:
    fixture_path = CONTRACT_ROOT / "projection-v2" / "valid" / fixture_name
    document = json.loads(fixture_path.read_text())

    intent = ExecutionIntent.from_projection(
        document,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    )

    assert intent.projection_semantic_digest == document["semantic_digest"]


def test_vendored_ordering_reference_fixture_stays_pending_without_profile() -> None:
    """An unbound producer route names the semantic operation, never the action id."""

    intent = _vendored_intent("ordering-reference-tool.json")
    requirement = intent.execution_contract.resource_requirements[0]

    assert requirement.purpose.value == "target_action"
    assert requirement.owner_ref == intent.unsafe_outcome.control_action_id == "CM-1"
    assert requirement.operation == "lookup_order"
    assert requirement.exact_resource_id is None

    result = resolve_execution_case(intent, None)

    assert result.code == "needs_environment_binding"
    assert result.requirement_ids == ("REQ-target-action",)


def test_turn_revision_fixtures_keep_legacy_fixture_digests_unchanged() -> None:
    """The additive fields are absent from legacy fixtures, so digests hold."""

    legacy = ("absence.json", "delay.json", "duration.json", "ordering.json", "window.json")
    for name in legacy:
        document = json.loads((CONTRACT_ROOT / "projection-v2" / "valid" / name).read_text())
        stimulus = document["stimulus_requirements"][0]
        assert "turns" not in stimulus, name
        condition = document["unsafe_outcome"]["condition"]
        assert "reference_tool" not in condition and "reference_argument" not in condition, name


def test_ordering_reference_argument_placeholders_reach_the_condition_view() -> None:
    from asago_artifact_generator.models.semantic_conditions import ReferenceArgument

    placeholder = SemanticBindingPlaceholder(
        binding_ref="SEM-REF-ORDER",
        value_type="string",
        description="The order id whose verification was skipped.",
    )
    condition = OrderingCondition(
        reference_step_id="S-1",
        relation="before",
        reference_tool="lookup_order",
        reference_argument=ReferenceArgument(
            property="order_id",
            operator="equals",
            expected=placeholder,
        ),
    )

    assert condition.placeholders() == (placeholder,)
    assert condition.model_dump(mode="json")["reference_tool"] == "lookup_order"
    bare = OrderingCondition(reference_step_id="S-1", relation="before")
    assert "reference_tool" not in bare.model_dump(mode="json")
    assert "reference_argument" not in bare.model_dump(mode="json")
    assert bare.placeholders() == ()
    with pytest.raises(ValueError, match="reference_tool and reference_argument together"):
        OrderingCondition(
            reference_step_id="S-1",
            relation="before",
            reference_tool="lookup_order",
        )


def test_stimulus_turns_round_trip_and_reject_broken_shapes() -> None:
    from asago_artifact_generator.models.semantic_conditions import StimulusTurn

    turns = (
        StimulusTurn(turn_id="T-1", text="First turn.", intent="context"),
        StimulusTurn(turn_id="T-2", text="Second turn."),
    )
    stimulus = AdversarialStimulusRequirement(
        stimulus_id="STIM-1",
        intent="Influence the control decision.",
        desired_effect="Cause the unsafe target action.",
        delivery_class="conversation_context",
        factor_id="CF-1",
        source_role="conversation_history",
        carrier_requirement_id=None,
        turns=turns,
    )
    dumped = stimulus.model_dump(mode="json")
    assert [turn["turn_id"] for turn in dumped["turns"]] == ["T-1", "T-2"]
    assert "intent" not in dumped["turns"][1]
    without_turns = stimulus.model_copy(update={"turns": None})
    assert "turns" not in without_turns.model_dump(mode="json")

    # model_copy bypasses validators; revalidate dumped payloads instead.
    direct_payload = dict(dumped)
    direct_payload["delivery_class"] = "direct_prompt"
    direct_payload["source_role"] = "direct_user_input"
    with pytest.raises(ValueError, match="only conversation_context stimuli may carry turns"):
        AdversarialStimulusRequirement.model_validate(direct_payload)

    single_payload = dict(dumped)
    single_payload["turns"] = [dumped["turns"][0]]
    with pytest.raises(ValueError, match="two to three entries"):
        AdversarialStimulusRequirement.model_validate(single_payload)

    duplicate_payload = dict(dumped)
    duplicate_payload["turns"] = [
        {"turn_id": "T-1", "text": "First turn."},
        {"turn_id": "T-1", "text": "Second turn."},
    ]
    with pytest.raises(ValueError, match="unique turn_id"):
        AdversarialStimulusRequirement.model_validate(duplicate_payload)

    with pytest.raises(ValidationError, match="Extra inputs"):
        StimulusTurn(turn_id="T-1", text="text", role="assistant")


@pytest.mark.parametrize(
    "condition",
    (
        OrderingCondition(reference_step_id="S-1", relation="before"),
        DelayCondition(reference_ref="PM-1-1", delay_ms=100),
        DurationCondition(reference_ref="CM-1", duration_ms=100),
        WindowCondition(reference_ref="PM-1-1", window_from_ms=100, window_to_ms=200),
        AbsenceCondition(reference_ref="PM-1-1", until_step_id="S-2"),
        ActionPresenceCondition(control_action_id="CM-1"),
        ActionValueCondition(
            control_action_id="CM-1",
            property="authorized_destination",
            operator="equals",
            expected="AUTHORIZED-ACCOUNT-001",
        ),
        StateValueCondition(
            subject_ref="PM-1-1",
            property="authorization_state",
            operator="equals",
            expected=True,
        ),
    ),
)
def test_semantic_condition_union_accepts_each_producer_variant(condition: object) -> None:
    assert condition.type in {
        "ordering",
        "delay",
        "duration",
        "window",
        "absence",
        "action_presence",
        "action_value",
        "state_value",
    }


def test_semantic_bound_and_time_validation_covers_strict_branches() -> None:
    numeric = SemanticBindingPlaceholder(
        binding_ref="SEM-N", value_type="number", description="delay", minimum=0, maximum=10
    )
    assert numeric.minimum == 0
    with pytest.raises(ValidationError):
        SemanticBindingPlaceholder(
            binding_ref="SEM-S", value_type="string", description="text", minimum=1
        )
    with pytest.raises(ValidationError):
        SemanticBindingPlaceholder(
            binding_ref="SEM-I", value_type="integer", description="count", minimum=1.5
        )
    with pytest.raises(ValidationError):
        SemanticBindingPlaceholder(
            binding_ref="SEM-T", value_type="number", description="delay", minimum=5, maximum=1
        )
    with pytest.raises(ValidationError):
        DelayCondition(reference_ref="PM-1-1", delay_ms=-1)
    with pytest.raises(ValidationError):
        DurationCondition(reference_ref="CM-1", duration_ms=True)
    with pytest.raises(ValidationError):
        WindowCondition(reference_ref="PM-1-1", window_from_ms=5, window_to_ms=1)


def test_loader_and_planner_private_edges_are_closed() -> None:
    violations: list[ValidationViolation] = []
    _validate_integer_placeholder_bounds({"minimum": 1.5, "maximum": True}, "$.p", violations)
    assert len(violations) == 2
    violations.clear()
    _validate_one_placeholder_bound(None, "$.p.minimum", violations)
    _validate_one_placeholder_bound(1, "$.p.minimum", violations)
    _validate_one_placeholder_bound(-1, "$.p.minimum", violations)
    assert len(violations) == 1

    condition_violations: list[ValidationViolation] = []
    _validate_condition(
        {
            "type": "action_value",
            "control_action_id": "CM-1",
            "property": "value",
            "operator": "equals",
            "expected": {"binding_ref": "SEM-1", "value_type": "string"},
        },
        "$.condition",
        condition_violations,
    )
    assert any(item.code == "required_field_missing" for item in condition_violations)

    ordered = [{"scenario_id": "SCN-2"}, {"scenario_id": "SCN-1"}]
    order_violations: list[ValidationViolation] = []
    _validate_entry_order(ordered, order_violations)
    assert any(item.code == "schema_field_invalid" for item in order_violations)


def test_parser_and_binding_value_edges_are_verified() -> None:
    intent = _intent()
    binding = _bindings(intent)
    parsed = parse_runtime_binding_set(binding.model_dump(mode="json"))
    assert parsed.binding_set_id == binding.binding_set_id
    with pytest.raises(RuntimeBindingValidationError):
        parse_runtime_binding_set([])  # type: ignore[arg-type]
    raw = binding.model_dump(mode="json")
    raw["extra"] = True
    with pytest.raises(RuntimeBindingValidationError):
        parse_runtime_binding_set(raw)

    placeholder = SemanticBindingPlaceholder(
        binding_ref="SEM-N", value_type="number", description="number", minimum=1, maximum=3
    )
    assert _placeholder_bounds_match(2, placeholder)
    assert not _placeholder_bounds_match(0, placeholder)
    assert not _placeholder_bounds_match(4, placeholder)
    string_placeholder = SemanticBindingPlaceholder(
        binding_ref="SEM-S", value_type="string", description="text"
    )
    assert _placeholder_bounds_match("text", string_placeholder)
    assert _replace_placeholder({"items": [placeholder]}, {("CF-1", "SEM-N"): 2}, "CF-1") == {
        "items": [2]
    }


def test_projection_digest_tampering_fails_closed(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    index["entries"][0]["projection"]["semantic_digest"] = "0" * 64
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="semantic_digest_mismatch"):
        load_execution_bundle(path)


def test_traversal_path_fails_before_file_access(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    index["entries"][0]["scenario"]["path"] = "../outside.json"
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="bundle_path_invalid"):
        load_execution_bundle(path)


def test_validator_version_is_part_of_closed_entry(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    del index["entries"][0]["validation"]["validator_version"]
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="validator_version"):
        load_execution_bundle(path)


def test_derived_unknown_bundle_schema_case_fails_closed(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    index["schema_version"] = "stpa-execution-bundle-v999"
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="schema_version_mismatch"):
        load_execution_bundle(path)


def test_repacked_json_bytes_fail_canonical_bundle_contract(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    scenario_path = tmp_path / index["entries"][0]["scenario"]["path"]
    scenario = json.loads(scenario_path.read_text())
    scenario_path.write_text(json.dumps(scenario, indent=2) + "\n")
    index["entries"][0]["scenario"]["content_sha256"] = hashlib.sha256(
        scenario_path.read_bytes()
    ).hexdigest()
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="canonical_bytes_mismatch"):
        load_execution_bundle(path)


def test_nested_scenario_spec_identity_tampering_fails_closed(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    scenario_path = tmp_path / index["entries"][0]["scenario"]["path"]
    scenario = json.loads(scenario_path.read_text())
    scenario["scenario_spec"] = {
        "scenario_id": "SCN-001",
        "target_controller": "CL-1",
        "target_control_action": "CM-1",
        "ica_type": "UNSAFE",
        "threat_source": {
            "ica_slot_id": "CL-1:CM-1:UNSAFE",
            "ica_id": "CL-1:CM-1:UNSAFE:1",
        },
    }
    scenario_bytes = canonical_json_bytes(scenario)
    scenario_path.write_bytes(scenario_bytes)
    index["entries"][0]["scenario"]["content_sha256"] = hashlib.sha256(scenario_bytes).hexdigest()
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="pair_identity_mismatch"):
        load_execution_bundle(path)


def test_nested_scenario_identity_fields_are_mandatory(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    index = json.loads(path.read_text())
    scenario_path = tmp_path / index["entries"][0]["scenario"]["path"]
    scenario = json.loads(scenario_path.read_text())
    del scenario["scenario_spec"]["threat_source"]["ica_id"]
    scenario_bytes = canonical_json_bytes(scenario)
    scenario_path.write_bytes(scenario_bytes)
    index["entries"][0]["scenario"]["content_sha256"] = hashlib.sha256(scenario_bytes).hexdigest()
    index["bundle_digest"] = compute_framed_digest(
        "stpa-execution-bundle-v1",
        {key: item for key, item in index.items() if key != "bundle_digest"},
    )
    path.write_bytes(canonical_json_bytes(index))

    with pytest.raises(BundleValidationError, match="pair_identity_mismatch"):
        load_execution_bundle(path)


def test_loader_closes_factor_registry_evidence_and_outcome_identity() -> None:
    bad_kind = _projection()
    bad_kind["causal_factors"][0]["kind"] = "FEEDBACK_DELAY"
    assert any(item.code == "factor_reference_mismatch" for item in _validate_projection(bad_kind))

    reachable = _projection()
    reachable["causal_factors"][0]["evidence_status"] = "reachable_capability"
    reachable["causal_factors"][0]["capability_refs"] = ["CAP-1"]
    reachable["causal_factors"][0]["access_refs"] = ["ACCESS-1"]
    reachable["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in reachable.items() if key != "semantic_digest"},
    )
    assert _validate_projection(reachable) == []

    bad_evidence = _projection()
    bad_evidence["causal_factors"][0]["evidence_status"] = "reachable_capability"
    assert any(item.code == "schema_field_invalid" for item in _validate_projection(bad_evidence))

    refs_without_capability_evidence = _projection()
    refs_without_capability_evidence["causal_factors"][0]["capability_refs"] = ["CAP-1"]
    assert any(
        item.code == "schema_field_invalid"
        for item in _validate_projection(refs_without_capability_evidence)
    )

    bounded = _projection()
    bounded["causal_factors"][0]["evidence_status"] = "bounded_assumption"
    bounded["causal_factors"][0]["bounded_assumption"] = "bounded test assumption"
    bounded["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in bounded.items() if key != "semantic_digest"},
    )
    assert _validate_projection(bounded) == []

    bad_bounded = _projection()
    bad_bounded["causal_factors"][0]["evidence_status"] = "bounded_assumption"
    assert any(item.code == "schema_field_invalid" for item in _validate_projection(bad_bounded))

    bad_assumption_status = _projection()
    bad_assumption_status["causal_factors"][0]["bounded_assumption"] = "unsupported assumption"
    assert any(
        item.code == "schema_field_invalid" for item in _validate_projection(bad_assumption_status)
    )

    bad_outcome = _projection()
    bad_outcome["unsafe_outcome"]["outcome_id"] = "OUT-1"
    assert any(item.code == "schema_field_invalid" for item in _validate_projection(bad_outcome))


def test_loader_requires_contract_delivery_factor_to_resolve() -> None:
    projection = _projection()
    projection["causal_factors"] = []

    violations = _validate_projection(projection)

    assert any(
        item.path == "$.projection.execution_contract.delivery.factor_id" for item in violations
    )


def test_loader_rechecks_target_action_requirement_against_unsafe_outcome() -> None:
    projection = _projection()
    contract = projection["execution_contract"]
    contract["requested_environment_basis"] = "target_profile"
    contract["delivery"] = {
        "delivery_class": "indirect_content",
        "factor_id": "CF-1",
        "source_role": "attacker_influenced_content",
        "carrier_requirement_id": "REQ-1",
    }
    contract["action_kind"] = "tool_call"
    contract["resource_requirements"] = [
        {
            "requirement_id": "REQ-1",
            "purpose": "stimulus_carrier",
            "factor_id": "CF-1",
            "owner_ref": "PM-1-1",
            "acceptable_resource_kinds": ["tool"],
            "role_id": "attacker_influenced_content_source",
            "operation": "retrieve_content",
            "required_surfaces": ["tool_result"],
            "required_properties": ["content_reaches_model_context"],
            "required_attacker_influence": "direct",
            "exact_resource_id": None,
            "late_bindable": True,
            "evidence_refs": ["CF-1"],
        },
        {
            "requirement_id": "REQ-2",
            "purpose": "target_action",
            "factor_id": None,
            "owner_ref": "CM-9",
            "acceptable_resource_kinds": ["tool"],
            "role_id": "target_control_action",
            "operation": "CM-9",
            "required_surfaces": ["tool_call"],
            "required_properties": [],
            "required_attacker_influence": "none",
            "exact_resource_id": "TOOL-action",
            "late_bindable": False,
            "evidence_refs": ["CM-1"],
        },
    ]
    contract.pop("semantic_digest", None)
    contract["semantic_digest"] = compute_framed_digest(
        "stpa-execution-contract-v1",
        {key: item for key, item in contract.items() if key != "semantic_digest"},
    )
    projection["semantic_digest"] = compute_framed_digest(
        "stpa-execution-projection-v2",
        {key: item for key, item in projection.items() if key != "semantic_digest"},
    )

    violations = _validate_projection(projection)

    assert any(
        item.code == "identity_mismatch" and item.path.endswith("resource_requirements[1]")
        for item in violations
    )


def test_loader_requires_classification_to_match_contract_result() -> None:
    projection = _projection()
    classification = projection["execution_classification"]
    classification["binding_completeness"] = "parameterized"
    classification["classification_digest"] = compute_framed_digest(
        "stpa-execution-classification-v1",
        {key: item for key, item in classification.items() if key != "classification_digest"},
    )

    violations = _validate_projection(projection)

    assert any(
        item.path == "$.projection.execution_classification"
        and item.code == "schema_field_invalid"
        for item in violations
    )


def test_bind_and_plan_rejects_raw_mappings() -> None:
    with pytest.raises(TypeError, match="BoundExecutionCase"):
        bind_and_plan({}, None, _capabilities())  # type: ignore[arg-type]


def test_missing_semantic_value_wins_readiness_precedence() -> None:
    intent = _vendored_intent("delay-placeholder.json")
    result = bind_and_plan(_execution_case(intent), None, _capabilities())

    assert result.overall == "needs_semantic_binding"
    assert result.semantic_binding_status == "incomplete"
    assert result.runtime_binding_status == "incomplete"
    assert any(item.code == "semantic_binding_missing" for item in result.diagnostics)
    assert any(item.code == "surface_binding_missing" for item in result.diagnostics)


def test_reviewed_semantic_binding_and_runtime_bindings_make_ready_plan() -> None:
    intent = _vendored_intent("delay-placeholder.json")
    intent = intent.model_copy(
        update={
            "execution_requirements": intent.execution_requirements.model_copy(
                update={"requires_tool_execution": False}
            )
        }
    )
    result = bind_and_plan(
        _execution_case(intent), _vendored_runtime_bindings(intent), _capabilities(clock=True)
    )

    assert result.overall == "ready"
    assert result.plan is not None
    assert [step.projection_step_id for step in result.plan.steps] == ["S-1", "S-2"]
    assert result.plan.observers[0].condition_ref == "OUTCOME-1"
    assert result.plan.trace_map["plan-2"]["projection_step_id"] == "S-2"
    assert result.plan.steps[-1].content_slot_id is None
    assert result.plan.scenario_content_sha256 == intent.source_file_digests["scenario"]
    assert result.plan.projection_content_sha256 == intent.source_file_digests["projection"]


@pytest.mark.parametrize(
    "update, expected_code",
    (
        ({"projection_step_id": "S-999"}, "stimulus_projection_mismatch"),
        ({"surface": "tool_result"}, "stimulus_surface_mismatch"),
    ),
)
def test_stimulus_binding_mismatches_remain_unready(
    update: dict[str, str], expected_code: str
) -> None:
    intent = _intent()
    bindings = _bindings(intent)
    stimulus = bindings.stimulus_bindings[0].model_copy(update=update)
    tampered = bindings.model_copy(
        update={"stimulus_bindings": (stimulus,), "semantic_digest": None}
    ).with_computed_digest()

    result = bind_and_plan(_execution_case(intent), tampered, _capabilities())

    assert result.overall != "ready"
    assert any(item.code == expected_code for item in result.diagnostics)


def test_inward_models_reject_non_hex_digest_values() -> None:
    intent_data = _intent().model_dump(mode="python")
    intent_data["bundle_digest"] = "p" * 64
    with pytest.raises(ValidationError):
        ExecutionIntent.model_validate(intent_data)

    review = ReviewEvidence(
        reviewed_by="operator@example",
        reviewed_at="2026-09-02T12:00:00Z",
        rationale="test binding",
        evidence_refs=("change-1",),
    )
    with pytest.raises(ValidationError):
        RuntimeBindingSet.create(
            binding_set_id="BIND-1",
            projection_semantic_digest="p" * 64,
            target_environment_id="test",
            review=review,
        )


def test_tool_execution_requirement_checks_target_operation() -> None:
    intent = _intent()
    intent = intent.model_copy(
        update={
            "execution_requirements": intent.execution_requirements.model_copy(
                update={"requires_tool_execution": True}
            )
        }
    )
    bindings = _bindings(intent, adapter_operation="emit_text")
    capabilities = PlatformCapabilities(
        platform="test",
        adapter_version="test-v1",
        writable_surfaces=("user_turn", "tool_call"),
        invocable_operations=("emit_text",),
        observer_kinds=("tool_argument",),
    )

    result = bind_and_plan(_execution_case(intent), bindings, capabilities)

    assert intent.execution_requirements.requires_tool_execution is True
    assert result.overall == "needs_runtime_binding"
    assert any(item.code == "tool_execution_operation_mismatch" for item in result.diagnostics)


def test_tool_action_requires_a_compatible_target_surface() -> None:
    intent = _intent()
    result = bind_and_plan(
        _execution_case(intent),
        _bindings(intent, adapter_operation="tool_call", target_surface="user_turn"),
        _capabilities(),
    )

    assert result.overall == "needs_runtime_binding"
    assert any(item.code == "surface_action_incompatible" for item in result.diagnostics)


def test_derived_binding_digest_case_is_invalid() -> None:
    intent = _intent()
    binding_data = _bindings(intent).model_dump(mode="python")
    binding_data["semantic_digest"] = "0" * 64
    tampered = RuntimeBindingSet.model_validate(binding_data)

    result = bind_and_plan(_execution_case(intent), tampered, _capabilities())

    assert result.overall == "invalid"
    assert result.source_status == "invalid"
    assert any(item.code == "binding_digest_mismatch" for item in result.diagnostics)


def test_temporal_factor_requires_its_own_observer_and_platform_support() -> None:
    intent = _intent(temporal=True)
    result = bind_and_plan(
        _execution_case(intent), _bindings(intent, temporal=False), _capabilities()
    )

    assert result.overall == "needs_runtime_binding"
    assert any(
        item.code == "observation_binding_missing" and item.path == "CF-1"
        for item in result.diagnostics
    )

    result = bind_and_plan(
        _execution_case(intent),
        _bindings(intent, temporal=True),
        _capabilities(clock=False),
    )
    assert result.overall == "unsupported"
    assert any(item.code == "real_clock_unsupported" for item in result.diagnostics)


def test_remaining_loader_and_semantic_validation_branches_are_explicit() -> None:
    bounds: list[ValidationViolation] = []
    _validate_integer_bounds((("minimum", None), ("maximum", 3)))
    with pytest.raises(ValueError):
        _validate_integer_bounds((("minimum", True),))
    with pytest.raises(ValueError):
        _validate_integer_bounds((("minimum", 1.5),))
    _validate_number_bounds((("minimum", None), ("maximum", 3)))
    with pytest.raises(ValueError):
        _validate_number_bounds((("minimum", True),))
    with pytest.raises(ValueError):
        _validate_number_bounds((("minimum", math.nan),))
    _validate_one_placeholder_bound(math.nan, "$.p.minimum", bounds)
    assert bounds

    _validate_scalar("literal", "$.value")
    _validate_scalar(
        SemanticBindingPlaceholder(binding_ref="SEM-V", value_type="string", description="text"),
        "$.value",
    )
    with pytest.raises(ValueError):
        _validate_scalar(math.nan, "$.value")
    with pytest.raises(ValueError):
        _validate_scalar(None, "$.value")
    _validate_time_value(
        SemanticBindingPlaceholder(binding_ref="SEM-T", value_type="number", description="delay"),
        "$.delay_ms",
    )
    with pytest.raises(ValueError):
        _validate_time_value(
            SemanticBindingPlaceholder(
                binding_ref="SEM-T", value_type="string", description="delay"
            ),
            "$.delay_ms",
        )
    with pytest.raises(ValueError):
        _validate_time_value("100", "$.delay_ms")

    quantity_violations: list[ValidationViolation] = []
    _validate_quantity(
        {"binding_ref": "SEM-T", "value_type": "integer", "description": "delay"},
        "$.quantity",
        quantity_violations,
    )
    _validate_quantity(
        {"binding_ref": "SEM-S", "value_type": "string", "description": "delay"},
        "$.quantity",
        quantity_violations,
    )
    _validate_quantity(0, "$.quantity", quantity_violations)
    _validate_quantity(-1, "$.quantity", quantity_violations)
    assert any(item.code == "condition_value_invalid" for item in quantity_violations)

    incomplete_identity: list[ValidationViolation] = []
    _validate_projection_identity({}, incomplete_identity)
    assert incomplete_identity == []
    bad_identity = _projection()
    bad_identity.update(
        {
            "candidate_id": "wrong",
            "ica_slot_id": "wrong",
            "ica_id": "wrong",
        }
    )
    _validate_projection_identity(bad_identity, incomplete_identity)
    assert len(incomplete_identity) == 3

    final_violations: list[ValidationViolation] = []
    _validate_final_step([None], "CM-1", final_violations)
    _validate_final_step(
        [{"kind": "CAUSAL_FACTOR", "factor_id": "CF-1", "structural_source_id": "PM-1-1"}],
        "CM-1",
        final_violations,
    )
    assert final_violations

    mapping_violations: list[ValidationViolation] = []
    _validate_step_mapping(
        {"kind": "CAUSAL_FACTOR", "factor_id": "CF-1", "structural_source_id": "PM-1-1"},
        1,
        None,
        "$.step",
        mapping_violations,
    )
    _validate_step_mapping(
        {"kind": "UNSAFE_CONTROL_ACTION", "factor_id": None, "structural_source_id": "wrong"},
        1,
        "PM-1-1",
        "$.step",
        mapping_violations,
    )
    assert len(mapping_violations) == 2

    category_violations: list[ValidationViolation] = []
    _validate_categories(["external_input"], "$.categories", category_violations)
    _validate_categories(["unknown", "unknown"], "$.categories", category_violations)
    _validate_categories(None, "$.categories", category_violations)
    assert len(category_violations) == 4

    trace_violations: list[ValidationViolation] = []
    trace_projection = _projection()
    trace_projection["trace_refs"]["source_pins"] = {"other": "0" * 64}
    _validate_projection_trace(trace_projection, trace_violations)
    trace_projection["trace_refs"]["source_pins"] = []
    _validate_projection_trace(trace_projection, trace_violations)
    trace_projection["trace_refs"] = {}
    _validate_projection_trace(trace_projection, trace_violations)
    assert any(item.code == "source_pin_mismatch" for item in trace_violations)

    canonical_json_bytes({"number": 1.25, "tuple": ("a",)})
    with pytest.raises(ValueError):
        canonical_json_bytes({"number": math.nan})


def test_remaining_model_invariants_reject_inconsistent_values() -> None:
    intent = _intent()
    condition = ActionValueCondition(
        control_action_id="CM-1",
        property="authorized_destination",
        operator="equals",
        expected="account",
    )
    with pytest.raises(ValidationError):
        UnsafeOutcome(
            outcome_id="OUTCOME-1",
            control_action_id="CM-1",
            uca_type="WRONG_TIMING",
            condition=condition,
            semantic_binding_required=False,
        )
    with pytest.raises(ValidationError):
        UnsafeOutcome(
            outcome_id="OUTCOME-1",
            control_action_id="CM-1",
            uca_type="INCORRECT",
            condition=condition,
            semantic_binding_required=True,
        )
    with pytest.raises(ValidationError):
        UnsafeOutcome(
            outcome_id="OUTCOME-1",
            control_action_id="CM-1",
            uca_type="INCORRECT",
            condition=ActionValueCondition(
                control_action_id="CM-2",
                property="authorized_destination",
                operator="equals",
                expected="account",
            ),
            semantic_binding_required=False,
        )
    UnsafeOutcome(
        outcome_id="OUTCOME-1",
        control_action_id="CM-1",
        uca_type="WRONG_TIMING",
        condition=OrderingCondition(reference_step_id="S-1", relation="before"),
        semantic_binding_required=False,
    )
    with pytest.raises(ValidationError):
        UnsafeOutcome(
            outcome_id="OUT-1",
            control_action_id="CM-1",
            uca_type="INCORRECT",
            condition=condition,
            semantic_binding_required=False,
        )
    with pytest.raises(ValidationError):
        CausalFactor(
            factor_id="CF-1",
            order=1,
            kind="FEEDBACK_DELAY",
            structural_source_id="PM-1-1",
            description="state diverges",
            evidence_status="structural_failure",
        )
    with pytest.raises(ValidationError):
        CausalFactor(
            factor_id="CF-1",
            order=1,
            kind="PROCESS_MODEL_FLAW",
            structural_source_id="FB-1-1",
            description="state diverges",
            evidence_status="reachable_capability",
        )
    with pytest.raises(ValidationError):
        CausalFactor(
            factor_id="CF-1",
            order=1,
            kind="FEEDBACK_DELAY",
            structural_source_id="FB-1-1",
            description="state diverges",
            evidence_status="bounded_assumption",
        )
    with pytest.raises(ValidationError):
        CausalFactor(
            factor_id="CF-1",
            order=1,
            kind="PROCESS_MODEL_FLAW",
            structural_source_id="PM-1-1",
            description="state diverges",
            evidence_status="structural_failure",
            bounded_assumption="unsupported assumption",
        )
    CausalFactor(
        factor_id="CF-1",
        order=1,
        kind="PROCESS_MODEL_FLAW",
        structural_source_id="PM-1-1",
        description="state diverges",
        evidence_status="reachable_capability",
        capability_refs=("CAP-1",),
        access_refs=("ACCESS-1",),
    )
    with pytest.raises(ValidationError):
        CausalFactor(
            factor_id="CF-1",
            order=1,
            kind="PROCESS_MODEL_FLAW",
            structural_source_id="PM-1-1",
            description="state diverges",
            evidence_status="reachable_capability",
        )
    with pytest.raises(ValidationError):
        CausalFactor(
            factor_id="CF-1",
            order=1,
            kind="PROCESS_MODEL_FLAW",
            structural_source_id="PM-1-1",
            description="state diverges",
            evidence_status="structural_failure",
            capability_refs=("CAP-1",),
        )

    ready = bind_and_plan(_execution_case(intent), _bindings(intent), _capabilities())
    assert ready.plan is not None
    result_data = ready.model_dump(mode="python")
    result_data["plan"] = None
    with pytest.raises(ValidationError):
        ExecutionPlanResult.model_validate(result_data)
    result_data = ready.model_dump(mode="python")
    result_data["overall"] = "unsupported"
    with pytest.raises(ValidationError):
        ExecutionPlanResult.model_validate(result_data)

    duplicate_surface = _bindings(intent).surface_bindings[0]
    with pytest.raises(ValidationError):
        RuntimeBindingSet.create(
            binding_set_id="BIND-DUP",
            projection_semantic_digest=intent.projection_semantic_digest,
            target_environment_id="test",
            review=_bindings(intent).review,
            surface_bindings=[duplicate_surface, duplicate_surface],
        )
