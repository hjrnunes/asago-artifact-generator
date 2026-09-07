"""Focused consumer tests for the revised observed MCP target profile."""

from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.bundle.loader import _validate_projection_trace
from asago_artifact_generator.garak.capabilities import garak_capabilities
from asago_artifact_generator.garak.default_bindings import complete_garak_runtime_bindings
from asago_artifact_generator.models.execution_case import BoundExecutionCase
from asago_artifact_generator.models.execution_classification import (
    BindingCompleteness,
    EnvironmentBasis,
    ExecutionActionKind,
    ExecutionClaimScope,
    ExecutionClassification,
    ExecutionDeliveryClass,
    ExecutionProfileFit,
    ExecutionResourceKind,
    ExecutionResourcePurpose,
    ExecutionResourceRequirement,
    ExecutionSurface,
    ExecutionTargetProfile,
    SemanticAuthority,
    SemanticExecutionContract,
    SemanticExecutionDelivery,
)
from asago_artifact_generator.models.execution_intent import ExecutionIntent
from asago_artifact_generator.models.runtime_binding import ObservationBinding, RuntimeBindingSet
from asago_artifact_generator.models.semantic_conditions import ActionPresenceCondition
from asago_artifact_generator.planning.bind import bind_and_plan
from asago_artifact_generator.planning.resolve_case import resolve_execution_case
from asago_artifact_generator.trace import ArtifactTrace, ObservationReceipt


def _intent_with_contract(
    contract: SemanticExecutionContract,
    *,
    profile: ExecutionTargetProfile | None = None,
    include_profile_pin: bool = True,
    include_realization_pin: bool = True,
) -> ExecutionIntent:
    from .test_stpa_consumer_core import _intent

    intent = _intent().model_copy(
        update={
            "execution_contract": contract,
            "execution_classification": ExecutionClassification(
                binding_completeness=BindingCompleteness.parameterized,
                environment_basis=EnvironmentBasis.none,
                profile_fit=ExecutionProfileFit.needs_binding,
                claim_scope=ExecutionClaimScope.no_execution_claim,
                target_profile_digest=(profile.semantic_digest if profile is not None else None),
            ),
        }
    )
    if contract.action_kind is ExecutionActionKind.tool_call:
        outcome = intent.unsafe_outcome
        intent = intent.model_copy(
            update={
                "unsafe_outcome": outcome.model_copy(
                    update={
                        "condition": outcome.condition.model_copy(
                            update={"property": "account_id", "expected": "TEST-ACCOUNT-001"}
                        ),
                        "semantic_proposition": (
                            "The operation names the account designated "
                            "by this synthetic test rule."
                        ),
                    }
                )
            }
        )
    if profile is not None:
        pins = dict(intent.trace_refs.source_pins)
        if include_profile_pin:
            pins["execution_target_profile"] = profile.semantic_digest
        if include_profile_pin and include_realization_pin:
            pins["target_realization"] = "e" * 64
        intent = intent.model_copy(
            update={"trace_refs": intent.trace_refs.model_copy(update={"source_pins": pins})}
        )
    return intent


def _mcp_profile(
    *,
    tool_name: str = "process_refund",
    role_ids: tuple[str, ...] = (),
    structural_refs: tuple[str, ...] = (),
) -> ExecutionTargetProfile:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"account_id": {"type": "string"}},
        "required": ["account_id"],
        "additionalProperties": False,
    }
    from asago_artifact_generator.models.execution_classification import (
        DiscoveryProvenance,
        InterpreterVerifierAgreement,
        InventoryAuthority,
        InventoryCompleteness,
        McpInventoryObservation,
        McpToolObservation,
        SemanticAuthority,
        SourceProtocol,
        TargetInterpretationDisposition,
        TargetOperationEffect,
        TargetProfileOperation,
        TargetProfileResource,
        TargetSemanticInterpretation,
        TargetStateEffect,
        mcp_resource_id,
    )

    tool = McpToolObservation(
        name=tool_name,
        source_observation_sha256="0" * 64,
        description="Process a refund for an account.",
        input_schema=schema,
        output_schema="opaque output schema",
        annotations=None,
    )
    inventory = McpInventoryObservation(
        target_id="klarna-test",
        authorization_scope_id="test-customer",
        tools=(tool,),
    )
    resource = TargetProfileResource(
        resource_id=mcp_resource_id("klarna-test", tool_name),
        resource_kind=ExecutionResourceKind.tool,
        target_id="klarna-test",
        tool_name=tool_name,
        description=tool.description,
        input_schema=schema,
        output_schema=tool.output_schema,
        annotations=tool.annotations,
        argument_names=tool.argument_names,
        surfaces=(ExecutionSurface.tool_call, ExecutionSurface.tool_result),
        operations=(
            TargetProfileOperation(
                operation_id=tool_name,
                semantic_operation=tool_name,
                argument_names=tool.argument_names,
            ),
        ),
        evidence_refs=(f"inventory:tool:{tool_name}",),
        role_ids=role_ids,
        structural_refs=structural_refs,
    )
    interpretation = TargetSemanticInterpretation(
        resource_id=resource.resource_id,
        tool_name=tool_name,
        disposition=TargetInterpretationDisposition.supported,
        likely_effect=TargetOperationEffect.update,
        likely_state_effect=TargetStateEffect.changes,
        semantic_roles=("refund_operation",),
        evidence_refs=(f"inventory:tool:{tool_name}:description",),
        rationale="The observed description indicates a refund operation.",
        interpreter_verifier_agreement=InterpreterVerifierAgreement.agree,
    )
    return ExecutionTargetProfile(
        target_id="klarna-test",
        authorization_scope_id="test-customer",
        inventory_authority=InventoryAuthority.observed,
        semantic_authority=SemanticAuthority.inferred,
        inventory_completeness=InventoryCompleteness.observed_complete,
        source_protocol=SourceProtocol.mcp,
        source_inventory_digest=inventory.semantic_digest,
        discovery_provenance=DiscoveryProvenance(
            scanner_id="scanner-v1",
            interpreter_id="interpreter-v1",
            verifier_id="verifier-v1",
        ),
        inventory=inventory,
        resources=(resource,),
        interpretations=(interpretation,),
    )


def _exact_contract(operation: str = "process_refund") -> SemanticExecutionContract:
    resource_id = "mcp:klarna-test:process_refund"
    return SemanticExecutionContract(
        requested_environment_basis="target_profile",
        delivery=SemanticExecutionDelivery(
            delivery_class=ExecutionDeliveryClass.direct_prompt,
            factor_id="CF-1",
            source_role="direct_user_input",
        ),
        action_kind=ExecutionActionKind.tool_call,
        resource_requirements=(
            ExecutionResourceRequirement(
                requirement_id="REQ-target-action",
                purpose=ExecutionResourcePurpose.target_action,
                owner_ref="CM-1",
                acceptable_resource_kinds=(ExecutionResourceKind.tool,),
                role_id="target_control_action",
                operation=operation,
                required_surfaces=(ExecutionSurface.tool_call,),
                exact_resource_id=resource_id,
                late_bindable=False,
                evidence_refs=("CM-1",),
            ),
        ),
    )


def _role_contract() -> SemanticExecutionContract:
    return SemanticExecutionContract(
        requested_environment_basis="target_profile",
        delivery=SemanticExecutionDelivery(
            delivery_class=ExecutionDeliveryClass.direct_prompt,
            factor_id="CF-1",
            source_role="direct_user_input",
        ),
        action_kind=ExecutionActionKind.tool_call,
        resource_requirements=(
            ExecutionResourceRequirement(
                requirement_id="REQ-target-action",
                purpose=ExecutionResourcePurpose.target_action,
                owner_ref="CM-1",
                acceptable_resource_kinds=(ExecutionResourceKind.tool,),
                role_id="target_control_action",
                operation="CM-1",
                required_surfaces=(ExecutionSurface.tool_call,),
                late_bindable=True,
                evidence_refs=("CM-1",),
            ),
        ),
    )


def test_revised_mcp_profile_preserves_exact_observed_and_inferred_axes() -> None:
    profile = _mcp_profile()

    payload = profile.model_dump(mode="json")

    assert payload["inventory_authority"] == "observed"
    assert payload["semantic_authority"] == "inferred"
    assert "authority" not in payload
    assert payload["resources"][0]["tool_name"] == "process_refund"
    assert payload["resources"][0]["input_schema"]["properties"]["account_id"]


def test_simulation_classification_requires_reviewed_semantic_authority() -> None:
    with pytest.raises(ValueError, match="reviewed semantic_authority"):
        ExecutionClassification(
            binding_completeness=BindingCompleteness.parameterized,
            environment_basis=EnvironmentBasis.simulation_profile,
            profile_fit=ExecutionProfileFit.needs_binding,
            claim_scope=ExecutionClaimScope.no_execution_claim,
            semantic_authority=SemanticAuthority.inferred,
        )


def test_mcp_operation_preserves_exact_argument_names() -> None:
    from asago_artifact_generator.models.execution_classification import TargetProfileOperation

    operation = TargetProfileOperation(
        operation_id="process-refund",
        argument_names=("order-id",),
        observable_properties=("state_changes",),
    )

    assert operation.argument_names == ("order-id",)


def test_resource_requirement_preserves_exact_mcp_operation_names() -> None:
    requirement = ExecutionResourceRequirement(
        requirement_id="REQ-target-action",
        purpose=ExecutionResourcePurpose.target_action,
        owner_ref="CM-1",
        acceptable_resource_kinds=(ExecutionResourceKind.tool,),
        role_id="target_control_action",
        operation="process-refund",
        required_surfaces=(ExecutionSurface.tool_call,),
        exact_resource_id="mcp:klarna-test:process-refund",
        late_bindable=False,
        evidence_refs=("CM-1",),
    )

    assert requirement.operation == "process-refund"


def test_profile_resource_preserves_producer_role_identifiers() -> None:
    from asago_artifact_generator.models.execution_classification import TargetProfileResource

    resource = TargetProfileResource(
        resource_id="TOOL-1",
        role_ids=("role-1",),
        evidence_refs=("inventory:tool",),
    )

    assert resource.role_ids == ("role-1",)


def test_exact_mcp_operation_binds_without_semantic_remapping() -> None:
    profile = _mcp_profile()
    case = resolve_execution_case(
        _intent_with_contract(_exact_contract(), profile=profile), profile
    )

    assert isinstance(case, BoundExecutionCase)
    assert case.resolved_bindings[0].resource_id == "mcp:klarna-test:process_refund"
    assert case.resolved_bindings[0].operation_id == "process_refund"
    assert case.inventory_authority == "observed"
    assert case.semantic_authority == "inferred"
    assert case.target_realization_digest == "e" * 64


def test_target_role_match_does_not_replace_exact_realization_selection() -> None:
    profile = _mcp_profile(
        tool_name="CM-1",
        role_ids=("target_control_action",),
        structural_refs=("CM-1",),
    )
    result = resolve_execution_case(
        _intent_with_contract(_role_contract(), profile=profile), profile
    )

    assert not isinstance(result, BoundExecutionCase)
    assert result.code == "needs_target_binding"


def test_exact_resource_binding_requires_both_realized_source_pins() -> None:
    profile = _mcp_profile()
    result = resolve_execution_case(_intent_with_contract(_exact_contract()), profile)

    assert not isinstance(result, BoundExecutionCase)
    assert result.code == "invalid_source_binding"
    assert "target_realization" in result.diagnostics[0].detail


def test_profile_source_pin_mismatch_is_rejected_before_binding() -> None:
    profile = _mcp_profile()
    intent = _intent_with_contract(_exact_contract(), profile=profile)
    pins = dict(intent.trace_refs.source_pins)
    pins["execution_target_profile"] = "f" * 64
    intent = intent.model_copy(
        update={"trace_refs": intent.trace_refs.model_copy(update={"source_pins": pins})}
    )

    result = resolve_execution_case(intent, profile)

    assert not isinstance(result, BoundExecutionCase)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "target_profile_digest_mismatch"


def test_supplied_profile_requires_classification_profile_digest() -> None:
    profile = _mcp_profile()
    intent = _intent_with_contract(_role_contract(), profile=profile, include_profile_pin=False)
    classification_data = intent.execution_classification.model_dump(mode="json")
    classification_data["target_profile_digest"] = None
    classification_data.pop("classification_digest")
    classification = ExecutionClassification.model_validate(classification_data)
    intent = intent.model_copy(update={"execution_classification": classification})

    result = resolve_execution_case(intent, profile)

    assert not isinstance(result, BoundExecutionCase)
    assert result.code == "invalid_source_binding"
    assert "classification target_profile_digest is required" in result.diagnostics[0].detail


def test_operation_name_drift_is_rejected_even_when_semantics_match() -> None:
    profile = _mcp_profile()
    result = resolve_execution_case(
        _intent_with_contract(_exact_contract(operation="refund_payment"), profile=profile),
        profile,
    )

    assert not isinstance(result, BoundExecutionCase)
    assert result.code == "invalid_source_binding"


def test_profile_inventory_schema_drift_cannot_be_revalidated_as_intact() -> None:
    profile = _mcp_profile()
    payload = profile.model_dump(mode="json")
    payload["resources"][0]["input_schema"]["properties"]["account_id"]["type"] = "integer"

    with pytest.raises(ValueError, match="input_schema drifted"):
        ExecutionTargetProfile.model_validate(payload)


def test_loader_requires_realized_pin_pair_for_exact_resource_contract() -> None:
    from .test_stpa_consumer_core import _projection

    projection = _projection()
    projection["execution_contract"]["resource_requirements"] = [
        {"exact_resource_id": "mcp:klarna-test:process_refund"}
    ]
    violations: list[Any] = []

    _validate_projection_trace(projection, violations)

    assert any(
        item.code == "source_pin_mismatch" and "exact resource requirements" in item.detail
        for item in violations
    )


def test_chat_action_does_not_supply_a_tool_definition() -> None:
    from .test_stpa_consumer_core import _bindings, _execution_case, _intent

    intent = _intent()
    intent = intent.model_copy(
        update={
            "execution_requirements": intent.execution_requirements.model_copy(
                update={
                    "required_surface_categories": ("external_input", "tool_definition"),
                }
            ),
        }
    )
    result = bind_and_plan(_execution_case(intent), _bindings(intent), garak_capabilities())
    assert result.overall == "needs_runtime_binding"
    assert any(item.code == "required_surface_category_missing" for item in result.diagnostics)


@pytest.mark.parametrize("require_declaration", [False, True])
def test_default_runtime_binding_and_ready_plan_keep_exact_operation_and_authority(
    require_declaration: bool,
) -> None:
    profile = _mcp_profile()
    intent = _intent_with_contract(_exact_contract(), profile=profile)
    if require_declaration:
        intent = intent.model_copy(
            update={
                "execution_requirements": intent.execution_requirements.model_copy(
                    update={
                        "requires_tool_execution": True,
                        "required_surface_categories": ("external_input", "tool_definition"),
                    }
                ),
            }
        )
    case = resolve_execution_case(intent, profile)
    assert isinstance(case, BoundExecutionCase)

    defaults = complete_garak_runtime_bindings(case, target_profile=profile)
    assert not any(item.surface == "tool_definition" for item in defaults.surface_bindings)
    action = defaults.control_action_bindings[0]
    assert action.tool_name == "process_refund"
    assert action.tool_schema == profile.resources[0].input_schema

    observation = ObservationBinding(
        condition_ref="OUTCOME-1",
        observer_kind="tool_argument",
        event_source="trace.tool_calls",
        semantic_property="account_id",
        field_path="arguments.account_id",
        comparison="equals",
        expected_from="projection",
    )
    bindings = RuntimeBindingSet.create(
        binding_set_id=defaults.binding_set_id,
        projection_semantic_digest=defaults.projection_semantic_digest,
        target_environment_id=defaults.target_environment_id,
        review=defaults.review,
        semantic_bindings=defaults.semantic_bindings,
        surface_bindings=defaults.surface_bindings,
        control_action_bindings=defaults.control_action_bindings,
        observation_bindings=(observation,),
        stimulus_bindings=defaults.stimulus_bindings,
        clock_binding=defaults.clock_binding,
    )
    readiness = bind_and_plan(case, bindings, garak_capabilities())

    assert readiness.overall == "ready"
    assert readiness.plan is not None
    assert readiness.plan.inventory_authority == "observed"
    assert readiness.plan.semantic_authority == "inferred"
    assert readiness.plan.target_realization_digest == "e" * 64
    assert readiness.plan.steps[-1].tool_name == "process_refund"
    assert readiness.plan.steps[-1].tool_schema == profile.resources[0].input_schema

    from asago_artifact_generator.garak.compile import _source_metadata

    trace = ArtifactTrace(
        source=_source_metadata(readiness.plan),
        binding={
            "binding_set_id": defaults.binding_set_id,
            "semantic_digest": defaults.semantic_digest,
        },
        compiler={
            "platform": "garak",
            "adapter_version": readiness.plan.adapter_version,
            "compiler_version": "garak-compiler-v1",
        },
        step_map=(),
        oracle_map=(),
    )
    assert trace.source["target_realization_digest"] == "e" * 64
    trace.verify()

    receipt = ObservationReceipt(
        artifact_digest="a" * 64,
        projection_semantic_digest=readiness.plan.projection_semantic_digest,
        binding_set_digest=defaults.semantic_digest,
        execution_id="execution-1",
        selected_profile_id=case.selected_profile_id,
        selected_profile_basis=case.selected_profile_basis,
        target_environment_id=case.target_environment_id,
        target_profile_digest=case.target_profile_digest,
        target_realization_digest=case.target_realization_digest,
        inventory_authority=case.inventory_authority.value,
        semantic_authority=case.semantic_authority.value,
    )
    assert receipt.payload()["target_realization_digest"] == "e" * 64
    receipt.verify()


def test_garak_maps_exact_environment_action_to_profile_tool_call() -> None:
    profile = _mcp_profile(tool_name="escalate_to_human")
    contract_payload = _exact_contract(operation="escalate_to_human").model_dump(
        mode="json", exclude={"semantic_digest"}
    )
    contract_payload["action_kind"] = ExecutionActionKind.environment_action
    contract_payload["resource_requirements"][0]["exact_resource_id"] = (
        "mcp:klarna-test:escalate_to_human"
    )
    contract = SemanticExecutionContract.model_validate(contract_payload)
    intent = _intent_with_contract(contract, profile=profile)
    condition = ActionPresenceCondition(control_action_id=intent.control_action_id)
    intent = intent.model_copy(
        update={
            "unsafe_outcome": intent.unsafe_outcome.model_copy(update={"condition": condition}),
            "execution_requirements": intent.execution_requirements.model_copy(
                update={
                    "requires_tool_execution": True,
                    "required_surface_categories": (
                        "external_input",
                        "environment_event",
                    ),
                }
            ),
        }
    )

    case = resolve_execution_case(intent, profile)
    defaults = complete_garak_runtime_bindings(case, target_profile=profile)

    assert defaults.control_action_bindings[0].adapter_operation == "tool_call"
    assert defaults.control_action_bindings[0].tool_name == "escalate_to_human"
    assert any(item.surface == "environment_event" for item in defaults.surface_bindings)
    assert defaults.observation_bindings[0].observer_kind == "tool_call"
    assert defaults.observation_bindings[0].event_source == "trace.tool_calls"

    readiness = bind_and_plan(case, defaults, garak_capabilities())
    assert readiness.overall == "ready"
    assert readiness.plan is not None
    assert readiness.plan.steps[-1].tool_name == "escalate_to_human"

    from asago_artifact_generator.authoring import DeterministicPresentationAuthor
    from asago_artifact_generator.garak.compile import compile_execution_artifact
    from asago_artifact_generator.garak.plan import build_garak_plan

    garak_plan = build_garak_plan(readiness.plan)
    assert garak_plan.steps[-1].surface == "environment_event"
    compiled = compile_execution_artifact(
        readiness.plan,
        DeterministicPresentationAuthor(
            {"stimulus:STIM-1": "Ask about a regulated financial decision."}
        ),
    )
    assert compiled.validation["ok"] is True
    assert compiled.artifact["tool_choice"] == "auto"


def test_explicit_runtime_binding_cannot_substitute_profile_tool() -> None:
    profile = _mcp_profile()
    case = resolve_execution_case(
        _intent_with_contract(_exact_contract(), profile=profile), profile
    )
    assert isinstance(case, BoundExecutionCase)
    defaults = complete_garak_runtime_bindings(case, target_profile=profile)
    substituted = defaults.control_action_bindings[0].model_copy(
        update={"tool_name": "refund_payment"}
    )
    explicit = RuntimeBindingSet.create(
        binding_set_id=defaults.binding_set_id,
        projection_semantic_digest=defaults.projection_semantic_digest,
        target_environment_id=defaults.target_environment_id,
        review=defaults.review,
        semantic_bindings=defaults.semantic_bindings,
        surface_bindings=defaults.surface_bindings,
        control_action_bindings=(substituted,),
        observation_bindings=defaults.observation_bindings,
        stimulus_bindings=defaults.stimulus_bindings,
        clock_binding=defaults.clock_binding,
    )

    with pytest.raises(ValueError, match="substitutes"):
        complete_garak_runtime_bindings(case, explicit, target_profile=profile)
