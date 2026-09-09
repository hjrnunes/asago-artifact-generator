"""Public acceptance tests for deterministic semantic case resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from asago_artifact_generator.garak.compile import compile_garak_artifact
from asago_artifact_generator.garak.plan import build_garak_plan
from asago_artifact_generator.models._base import compute_framed_digest
from asago_artifact_generator.models.execution_case import (
    BoundExecutionCase,
    ExecutionCaseExclusion,
    _validate_target_agnostic_result,
)
from asago_artifact_generator.models.execution_classification import (
    AmbiguousExecutionMatch,
    BindingCompleteness,
    EnvironmentBasis,
    ExecutionActionKind,
    ExecutionClaimScope,
    ExecutionClassification,
    ExecutionClassificationDiagnostic,
    ExecutionContractGap,
    ExecutionContractGapCode,
    ExecutionDeliveryClass,
    ExecutionDiagnosticCode,
    ExecutionProfileFit,
    ExecutionResourceKind,
    ExecutionResourcePurpose,
    ExecutionResourceRequirement,
    ExecutionTargetProfile,
    InventoryAuthority,
    InventoryCompleteness,
    ProfileBasis,
    RequestedEnvironmentBasis,
    ResolvedExecutionBinding,
    SemanticAuthority,
    SemanticExecutionContract,
    SemanticExecutionDelivery,
    TargetProfileOperation,
    TargetProfileResource,
)
from asago_artifact_generator.models.execution_intent import (
    AdversarialStimulusRequirement,
    ExecutionIntent,
)
from asago_artifact_generator.models.readiness import PlatformCapabilities
from asago_artifact_generator.models.runtime_binding import RuntimeBindingSet
from asago_artifact_generator.planning.bind import bind_and_plan
from asago_artifact_generator.planning.resolve_case import resolve_execution_case

from .test_stpa_consumer_core import _bindings, _capabilities, _intent


def _direct_contract() -> SemanticExecutionContract:
    return SemanticExecutionContract(
        requested_environment_basis=RequestedEnvironmentBasis.target_agnostic,
        delivery=SemanticExecutionDelivery(
            delivery_class=ExecutionDeliveryClass.direct_prompt,
            factor_id="CF-1",
            source_role="direct_user_input",
        ),
        action_kind=ExecutionActionKind.model_output,
    )


def _tool_contract(
    *,
    exact_resource_id: str | None = None,
    exact_retrieval: bool = False,
    exact_action: bool = True,
) -> SemanticExecutionContract:
    return SemanticExecutionContract(
        requested_environment_basis=RequestedEnvironmentBasis.target_profile,
        delivery=SemanticExecutionDelivery(
            delivery_class=ExecutionDeliveryClass.indirect_content,
            factor_id="CF-1",
            source_role="attacker_influenced_content",
            carrier_requirement_id="REQ-1",
        ),
        action_kind=ExecutionActionKind.tool_call,
        resource_requirements=(
            ExecutionResourceRequirement(
                requirement_id="REQ-1",
                purpose=ExecutionResourcePurpose.stimulus_carrier,
                factor_id="CF-1",
                owner_ref="PM-1-1",
                acceptable_resource_kinds=(ExecutionResourceKind.tool,),
                role_id="attacker_influenced_content_source",
                operation="retrieve-1",
                required_surfaces=("tool_result",),
                required_properties=(),
                exact_resource_id=(
                    exact_resource_id
                    if exact_resource_id is not None
                    else ("mcp:target-1:retrieve-1" if exact_retrieval else None)
                ),
                late_bindable=exact_resource_id is None and not exact_retrieval,
                evidence_refs=("CF-1",),
                required_attacker_influence="direct",
            ),
            ExecutionResourceRequirement(
                requirement_id="REQ-2",
                purpose=ExecutionResourcePurpose.target_action,
                owner_ref="CM-1",
                acceptable_resource_kinds=(ExecutionResourceKind.tool,),
                role_id="target_control_action",
                operation="action-1" if exact_action else "CM-1",
                required_surfaces=("tool_call",),
                required_properties=(),
                exact_resource_id=("mcp:target-1:action-1" if exact_action else None),
                late_bindable=not exact_action,
                evidence_refs=("CM-1",),
                required_attacker_influence="none",
            ),
        ),
    )


def _profile(
    *,
    inventory: InventoryCompleteness = InventoryCompleteness.observed_complete,
    resources: tuple[TargetProfileResource, ...] | None = None,
) -> ExecutionTargetProfile:
    from asago_artifact_generator.models.execution_classification import (
        DiscoveryProvenance,
        InventoryAuthority,
        McpInventoryObservation,
        McpToolObservation,
        SemanticAuthority,
        SourceProtocol,
        TargetInterpretationDisposition,
        TargetOperationEffect,
        TargetSemanticInterpretation,
        TargetStateEffect,
        mcp_resource_id,
    )

    retrieval_schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }
    action_schema = {
        "type": "object",
        "properties": {"request": {"type": "string"}},
        "required": ["request"],
        "additionalProperties": False,
    }
    default_resources = (
        TargetProfileResource(
            resource_id=mcp_resource_id("target-1", "retrieve-1"),
            target_id="target-1",
            tool_name="retrieve-1",
            resource_kind=ExecutionResourceKind.tool,
            description="Retrieve attacker-influenced content.",
            input_schema=retrieval_schema,
            output_schema="opaque",
            role_ids=("attacker_influenced_content_source",),
            structural_refs=("PM-1-1",),
            attacker_influence="direct",
            surfaces=("tool_call", "tool_result"),
            operations=(
                TargetProfileOperation(
                    operation_id="retrieve-1",
                    semantic_operation="retrieve-1",
                    argument_names=("content",),
                ),
            ),
            evidence_refs=("inventory:tool:retrieve-1",),
        ),
        TargetProfileResource(
            resource_id=mcp_resource_id("target-1", "action-1"),
            target_id="target-1",
            tool_name="action-1",
            resource_kind=ExecutionResourceKind.tool,
            description="Perform the selected control action.",
            input_schema=action_schema,
            output_schema="opaque",
            role_ids=("target_control_action",),
            structural_refs=("CM-1",),
            attacker_influence="none",
            surfaces=("tool_call", "tool_result"),
            operations=(
                TargetProfileOperation(
                    operation_id="action-1",
                    semantic_operation="action-1",
                    argument_names=("request",),
                ),
            ),
            evidence_refs=("inventory:tool:action-1",),
        ),
    )
    resources = tuple(resources or default_resources)
    tools = tuple(
        McpToolObservation(
            name=resource.tool_name or resource.operations[0].operation_id,
            source_observation_sha256=("0" if index == 0 else "1") * 64,
            title=resource.title,
            description=resource.description,
            input_schema=resource.input_schema,
            output_schema=resource.output_schema,
            annotations=resource.annotations,
        )
        for index, resource in enumerate(resources)
    )
    inventory_value = McpInventoryObservation(
        target_id="target-1",
        authorization_scope_id="test-scope",
        tools=tools,
        pagination_complete=inventory is not InventoryCompleteness.observed_partial,
    )
    interpretations = tuple(
        TargetSemanticInterpretation(
            resource_id=resource.resource_id,
            tool_name=resource.tool_name or resource.operations[0].operation_id,
            disposition=TargetInterpretationDisposition.supported,
            likely_effect=TargetOperationEffect.execute,
            likely_state_effect=TargetStateEffect.may_change,
            semantic_roles=("observed_operation",),
            evidence_refs=(
                f"inventory:tool:{resource.tool_name or resource.operations[0].operation_id}",
            ),
            rationale="The observed tool is retained as exact interface evidence.",
        )
        for resource in resources
    )
    return ExecutionTargetProfile(
        target_id="target-1",
        authorization_scope_id="test-scope",
        basis=ProfileBasis.target,
        inventory_authority=InventoryAuthority.observed,
        semantic_authority=SemanticAuthority.inferred,
        inventory_completeness=inventory,
        source_protocol=SourceProtocol.mcp,
        source_inventory_digest=inventory_value.semantic_digest,
        discovery_provenance=DiscoveryProvenance(
            scanner_id="test-scanner",
            interpreter_id="test-interpreter",
            verifier_id="test-verifier",
        ),
        inventory=inventory_value,
        resources=resources,
        interpretations=interpretations,
    )


def _simulation_contract() -> SemanticExecutionContract:
    payload = _tool_contract(exact_action=False).model_dump(mode="json")
    payload.pop("semantic_digest", None)
    payload["requested_environment_basis"] = "simulation_profile"
    return SemanticExecutionContract.model_validate(payload)


def _simulation_profile() -> ExecutionTargetProfile:
    from asago_artifact_generator.models.execution_classification import (
        SemanticAuthority,
        SimulationBehavior,
        SourceProtocol,
    )

    resources = []
    for item in _profile().resources:
        operation = item.operations[0]
        if (
            item.resource_kind is ExecutionResourceKind.tool
            and "target_control_action" in item.role_ids
        ):
            operation = operation.model_copy(update={"semantic_operation": "CM-1"})
        resources.append(
            item.model_copy(
                update={
                    "target_id": None,
                    "tool_name": None,
                    "operations": (operation,),
                    "simulation_behavior": SimulationBehavior(
                        inputs={"request": "safe"},
                        outputs={"response": "safe"},
                        observation_points=("response",),
                    ),
                }
            )
        )
    return ExecutionTargetProfile(
        target_id="simulation-target",
        authorization_scope_id="test-scope",
        basis=ProfileBasis.simulation,
        semantic_authority=SemanticAuthority.reviewed,
        inventory_completeness=InventoryCompleteness.unknown,
        source_protocol=SourceProtocol.simulation,
        resources=tuple(resources),
    )


def _intent_with_contract(
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    *,
    profile: ExecutionTargetProfile | None = None,
    include_profile_pin: bool = True,
    include_realization_pin: bool = True,
) -> ExecutionIntent:
    delivery = contract.delivery
    stimuli = (
        ()
        if delivery is None
        else (
            AdversarialStimulusRequirement(
                stimulus_id="STIM-1",
                intent="Influence the control decision through its external input.",
                desired_effect="Cause the unsafe target action.",
                delivery_class=delivery.delivery_class,
                factor_id=delivery.factor_id,
                source_role=delivery.source_role,
                carrier_requirement_id=delivery.carrier_requirement_id,
            ),
        )
    )
    if profile is not None:
        classification_data = classification.model_dump(mode="json")
        classification_data["target_profile_digest"] = profile.semantic_digest
        classification_data.pop("classification_digest", None)
        classification = ExecutionClassification.model_validate(classification_data)
    intent = _intent().model_copy(
        update={
            "execution_contract": contract,
            "execution_classification": classification,
            "stimulus_requirements": stimuli,
        }
    )
    if contract.action_kind is ExecutionActionKind.tool_call:
        outcome = intent.unsafe_outcome
        intent = intent.model_copy(
            update={
                "unsafe_outcome": outcome.model_copy(
                    update={
                        "condition": outcome.condition.model_copy(
                            update={"property": "request", "expected": "TEST-REQUEST"}
                        ),
                        "semantic_proposition": (
                            "The operation carries the request designated "
                            "by this synthetic test rule."
                        ),
                    }
                )
            }
        )
    if profile is not None and profile.basis is ProfileBasis.target:
        pins = dict(intent.trace_refs.source_pins)
        if include_profile_pin:
            pins["execution_target_profile"] = profile.semantic_digest
        if include_profile_pin and include_realization_pin:
            pins["target_realization"] = "e" * 64
        intent = intent.model_copy(
            update={"trace_refs": intent.trace_refs.model_copy(update={"source_pins": pins})}
        )
    return intent


def _classification(
    *,
    completeness: BindingCompleteness,
    environment: EnvironmentBasis,
    fit: ExecutionProfileFit,
    claim: ExecutionClaimScope,
    target_profile_digest: str | None = None,
    resolved_bindings: tuple[ResolvedExecutionBinding, ...] = (),
) -> ExecutionClassification:
    inventory_authority = (
        InventoryAuthority.observed if environment is EnvironmentBasis.target_profile else None
    )
    semantic_authority = (
        SemanticAuthority.reviewed
        if environment is EnvironmentBasis.simulation_profile
        else SemanticAuthority.inferred
        if environment is EnvironmentBasis.target_profile
        else None
    )
    return ExecutionClassification(
        binding_completeness=completeness,
        environment_basis=environment,
        profile_fit=fit,
        claim_scope=claim,
        inventory_authority=inventory_authority,
        semantic_authority=semantic_authority,
        target_profile_digest=target_profile_digest,
        resolved_bindings=resolved_bindings,
    )


def _parameterized_intent(contract: SemanticExecutionContract) -> ExecutionIntent:
    return _intent_with_contract(
        contract,
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )


def _agent_requirement() -> ExecutionResourceRequirement:
    return ExecutionResourceRequirement(
        requirement_id="REQ-AGENT",
        purpose=ExecutionResourcePurpose.agent_channel,
        owner_ref="agent",
        acceptable_resource_kinds=(ExecutionResourceKind.agent_channel,),
        role_id="agent_channel",
        operation="send_message",
        required_surfaces=("agent_message",),
        late_bindable=True,
        evidence_refs=("review:agent",),
    )


def test_direct_case_is_concrete_without_profile() -> None:
    intent = _intent_with_contract(
        _direct_contract(),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_agnostic,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.model_behavior_only,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, BoundExecutionCase)
    assert result.selected_profile_id is None
    assert result.claim_scope is ExecutionClaimScope.model_behavior_only


def test_inconsistent_target_agnostic_classification_is_not_repaired() -> None:
    intent = _intent_with_contract(
        _direct_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    result = resolve_execution_case(intent, _profile())

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "producer_binding_mismatch"


def test_target_agnostic_case_ignores_a_global_profile() -> None:
    intent = _intent_with_contract(
        _direct_contract(),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_agnostic,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.model_behavior_only,
        ),
    )

    result = resolve_execution_case(intent, _profile())

    assert isinstance(result, BoundExecutionCase)
    assert result.selected_profile_id is None
    assert result.binding_completeness is BindingCompleteness.concrete
    assert result.environment_basis is EnvironmentBasis.target_agnostic
    assert result.profile_fit is ExecutionProfileFit.not_required
    assert result.claim_scope is ExecutionClaimScope.model_behavior_only


def test_missing_source_classification_is_rejected_without_fallback() -> None:
    intent = _intent().model_copy(
        update={"execution_contract": None, "execution_classification": None}
    )

    with pytest.raises(ValueError, match="execution_contract and execution_classification"):
        resolve_execution_case(intent, None)


def test_parameterized_case_without_profile_is_excluded() -> None:
    intent = _intent_with_contract(
        _tool_contract(exact_action=False),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "needs_target_binding"
    assert result.requirement_ids == ("REQ-1", "REQ-2")


def test_parameterized_contract_preserves_an_unspecified_environment_choice() -> None:
    payload = _tool_contract().model_dump(mode="json")
    payload["requested_environment_basis"] = None
    payload.pop("semantic_digest", None)

    contract = SemanticExecutionContract.model_validate(payload)

    assert contract.requested_environment_basis is None
    assert contract.resource_requirements


def test_unspecified_parameterized_case_without_profile_needs_environment_binding() -> None:
    payload = _tool_contract(exact_action=False).model_dump(mode="json")
    payload["requested_environment_basis"] = None
    payload.pop("semantic_digest", None)
    intent = _intent_with_contract(
        SemanticExecutionContract.model_validate(payload),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "needs_environment_binding"
    assert result.requirement_ids == ("REQ-1", "REQ-2")
    assert result.diagnostics[0].code == "environment_profile_not_supplied"


def test_explicit_simulation_request_without_profile_needs_simulation_binding() -> None:
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "needs_simulation_binding"
    assert result.diagnostics[0].code == "simulation_contract_missing"


def test_unspecified_parameterized_case_uses_supplied_target_profile_as_basis() -> None:
    profile = _profile()
    payload = _tool_contract(exact_retrieval=True).model_dump(mode="json")
    payload["requested_environment_basis"] = None
    payload.pop("semantic_digest", None)
    intent = _intent_with_contract(
        SemanticExecutionContract.model_validate(payload),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    assert result.environment_basis is EnvironmentBasis.target_profile
    assert result.selected_profile_basis == "target"


def test_unspecified_parameterized_case_uses_supplied_simulation_profile_as_basis() -> None:
    profile = _simulation_profile()
    payload = _tool_contract(exact_action=False).model_dump(mode="json")
    payload["requested_environment_basis"] = None
    payload.pop("semantic_digest", None)
    intent = _intent_with_contract(
        SemanticExecutionContract.model_validate(payload),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    assert result.environment_basis is EnvironmentBasis.simulation_profile
    assert result.selected_profile_basis == "simulation"


def test_explicit_environment_basis_must_match_supplied_profile() -> None:
    simulation_profile = _simulation_profile()
    target_profile = _profile()
    target_intent = _intent_with_contract(
        _tool_contract(exact_action=False),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
            target_profile_digest=simulation_profile.semantic_digest,
        ),
    )
    simulation_intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
            target_profile_digest=target_profile.semantic_digest,
        ),
    )

    target_result = resolve_execution_case(target_intent, simulation_profile)
    simulation_result = resolve_execution_case(simulation_intent, target_profile)

    assert isinstance(target_result, ExecutionCaseExclusion)
    assert target_result.code == "invalid_profile"
    assert isinstance(simulation_result, ExecutionCaseExclusion)
    assert simulation_result.code == "invalid_profile"


def test_mixed_bundle_keeps_each_environment_basis_independent() -> None:
    pending_payload = _tool_contract(exact_action=False).model_dump(mode="json")
    pending_payload["requested_environment_basis"] = None
    pending_payload.pop("semantic_digest", None)
    pending = _parameterized_intent(SemanticExecutionContract.model_validate(pending_payload))
    target_profile = _profile()
    target_contract = _tool_contract(exact_retrieval=True)
    target = _intent_with_contract(
        target_contract,
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=target_profile,
    )
    simulation_profile = _simulation_profile()
    simulation = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=simulation_profile,
    )
    target_agnostic = _intent_with_contract(
        _direct_contract(),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_agnostic,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.model_behavior_only,
        ),
    )
    results = (
        resolve_execution_case(target_agnostic, _profile()),
        resolve_execution_case(pending, None),
        resolve_execution_case(target, target_profile),
        resolve_execution_case(simulation, simulation_profile),
    )

    assert isinstance(results[0], BoundExecutionCase)
    assert results[0].environment_basis is EnvironmentBasis.target_agnostic
    assert results[0].selected_profile_id is None
    assert isinstance(results[1], ExecutionCaseExclusion)
    assert results[1].code == "needs_environment_binding"
    assert isinstance(results[2], BoundExecutionCase)
    assert results[2].environment_basis is EnvironmentBasis.target_profile
    assert results[2].selected_profile_basis == "target"
    assert isinstance(results[3], BoundExecutionCase)
    assert results[3].environment_basis is EnvironmentBasis.simulation_profile
    assert results[3].selected_profile_basis == "simulation"


def test_reviewed_profile_binds_exact_semantic_operation() -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _tool_contract(exact_retrieval=True),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    assert result.selected_profile_id == "target-1"
    assert result.target_profile_digest == profile.semantic_digest
    assert result.resolved_bindings[0].operation_id == "retrieve-1"
    assert result.binding_completeness is BindingCompleteness.concrete
    assert result.environment_basis is EnvironmentBasis.target_profile
    assert result.profile_fit is ExecutionProfileFit.matched
    assert result.claim_scope is ExecutionClaimScope.target_specific_intent


def test_required_surfaces_are_part_of_exact_profile_matching() -> None:
    profile = _profile()
    payload = _tool_contract(exact_retrieval=True).model_dump(mode="json")
    payload["resource_requirements"][0]["required_surfaces"] = ["tool_definition"]
    payload.pop("semantic_digest", None)
    intent = _intent_with_contract(
        SemanticExecutionContract.model_validate(payload),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.requirement_ids == ("REQ-1",)
    assert result.diagnostics[0].code == "explicit_target_ref_dangling"


def test_concrete_producer_binding_must_match_the_pinned_profile() -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _tool_contract(exact_retrieval=True),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_profile,
            fit=ExecutionProfileFit.matched,
            claim=ExecutionClaimScope.target_specific_intent,
            target_profile_digest=profile.semantic_digest,
            resolved_bindings=(
                ResolvedExecutionBinding(
                    requirement_id="REQ-1",
                    resource_id="mcp:target-1:retrieve-1",
                    operation_id="forged-operation",
                ),
                ResolvedExecutionBinding(
                    requirement_id="REQ-2",
                    resource_id="mcp:target-1:action-1",
                    operation_id="action-1",
                ),
            ),
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "producer_binding_mismatch"


def test_exact_resource_must_exist_and_be_reviewed() -> None:
    base_profile = _profile()
    retrieval = next(
        resource
        for resource in base_profile.resources
        if resource.resource_id == "mcp:target-1:retrieve-1"
    )
    partial_resources = (retrieval,)
    profile = _profile(resources=partial_resources)
    intent = _intent_with_contract(
        _tool_contract(exact_retrieval=True),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )
    result = resolve_execution_case(intent, profile)
    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "explicit_target_ref_dangling"

    action = next(
        resource
        for resource in base_profile.resources
        if resource.resource_id == "mcp:target-1:action-1"
    )
    exact_profile = _profile(resources=(retrieval, action))
    exact_intent = _intent_with_contract(
        _tool_contract(exact_retrieval=True),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=exact_profile,
    )
    result = resolve_execution_case(exact_intent, exact_profile)
    assert isinstance(result, BoundExecutionCase)
    assert result.resolved_bindings[0].resource_id == "mcp:target-1:retrieve-1"


def test_profile_digest_mismatch_is_rejected_before_resolution() -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _tool_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
            target_profile_digest="0" * 64,
        ),
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "target_profile_digest_mismatch"


@pytest.mark.parametrize(
    ("environment", "fit", "claim"),
    (
        (
            EnvironmentBasis.none,
            ExecutionProfileFit.matched,
            ExecutionClaimScope.target_specific_intent,
        ),
        (
            EnvironmentBasis.target_profile,
            ExecutionProfileFit.needs_binding,
            ExecutionClaimScope.target_specific_intent,
        ),
        (
            EnvironmentBasis.target_profile,
            ExecutionProfileFit.matched,
            ExecutionClaimScope.model_behavior_only,
        ),
    ),
)
def test_concrete_producer_claim_must_match_profile(
    environment: EnvironmentBasis,
    fit: ExecutionProfileFit,
    claim: ExecutionClaimScope,
) -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _tool_contract(exact_retrieval=True),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=environment,
            fit=fit,
            claim=claim,
            target_profile_digest=profile.semantic_digest,
            resolved_bindings=(
                ResolvedExecutionBinding(
                    requirement_id="REQ-1",
                    resource_id="mcp:target-1:retrieve-1",
                    operation_id="retrieve-1",
                ),
                ResolvedExecutionBinding(
                    requirement_id="REQ-2",
                    resource_id="mcp:target-1:action-1",
                    operation_id="action-1",
                ),
            ),
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "producer_binding_mismatch"


def test_partial_inventory_does_not_make_role_match_concrete() -> None:
    profile = _profile(inventory=InventoryCompleteness.inferred_partial)
    intent = _intent_with_contract(
        _tool_contract(exact_action=False),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "needs_target_binding"


def test_two_exact_matches_are_ambiguous() -> None:
    simulation = _simulation_profile()
    first = next(
        item for item in simulation.resources if item.resource_id == "mcp:target-1:retrieve-1"
    )
    second = first.model_copy(update={"resource_id": "mcp:target-1:retrieve-1-2"})
    action = next(
        item for item in simulation.resources if item.resource_id == "mcp:target-1:action-1"
    )
    profile = simulation.model_copy(
        update={"resources": (first, second, action), "semantic_digest": None}
    )
    profile = profile.model_copy(update={"semantic_digest": profile.compute_semantic_digest()})
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "ambiguous"
    assert result.diagnostics[0].candidate_resource_ids == (
        "mcp:target-1:retrieve-1",
        "mcp:target-1:retrieve-1-2",
    )


def test_exact_resource_can_bind_in_partial_inventory() -> None:
    profile = _profile(inventory=InventoryCompleteness.inferred_partial)
    intent = _intent_with_contract(
        _tool_contract(exact_resource_id="mcp:target-1:retrieve-1"),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_profile,
            fit=ExecutionProfileFit.matched,
            claim=ExecutionClaimScope.target_specific_intent,
            target_profile_digest=profile.semantic_digest,
            resolved_bindings=(
                ResolvedExecutionBinding(
                    requirement_id="REQ-1",
                    resource_id="mcp:target-1:retrieve-1",
                    operation_id="retrieve-1",
                ),
                ResolvedExecutionBinding(
                    requirement_id="REQ-2",
                    resource_id="mcp:target-1:action-1",
                    operation_id="action-1",
                ),
            ),
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    assert result.resolved_bindings[0].resource_id == "mcp:target-1:retrieve-1"


def test_profile_digest_tampering_is_rejected() -> None:
    profile = _profile()

    with pytest.raises(ValueError, match="semantic_digest"):
        ExecutionTargetProfile.model_validate(
            profile.model_dump(mode="json") | {"semantic_digest": "0" * 64}
        )


def test_classification_digest_is_a_distinct_wire_field() -> None:
    classification = _classification(
        completeness=BindingCompleteness.concrete,
        environment=EnvironmentBasis.target_agnostic,
        fit=ExecutionProfileFit.not_required,
        claim=ExecutionClaimScope.model_behavior_only,
    )

    payload = classification.model_dump(mode="json")
    assert "classification_digest" in payload
    assert "semantic_digest" not in payload
    with pytest.raises(ValueError, match="classification_digest"):
        ExecutionClassification.model_validate(payload | {"classification_digest": "0" * 64})


def test_classification_candidate_diagnostics_are_canonicalized() -> None:
    ambiguous = AmbiguousExecutionMatch(
        requirement_id="REQ-1",
        candidate_resource_ids=["RESOURCE-2", "RESOURCE-1"],
    )
    diagnostic = ExecutionClassificationDiagnostic(
        code=ExecutionDiagnosticCode.target_resource_ambiguous,
        detail="more than one resource matches",
        candidate_resource_ids=["RESOURCE-2", "RESOURCE-1"],
    )

    assert ambiguous.candidate_resource_ids == ("RESOURCE-1", "RESOURCE-2")
    assert diagnostic.candidate_resource_ids == ("RESOURCE-1", "RESOURCE-2")


def test_agent_channel_contract_has_one_exact_requirement() -> None:
    requirement = _agent_requirement()
    contract = SemanticExecutionContract(
        requested_environment_basis=RequestedEnvironmentBasis.target_profile,
        delivery=SemanticExecutionDelivery(
            delivery_class=ExecutionDeliveryClass.direct_prompt,
            factor_id="CF-1",
            source_role="direct_user_input",
        ),
        action_kind=ExecutionActionKind.agent_message,
        resource_requirements=(requirement,),
    )

    assert contract.resource_requirements == (requirement,)

    with pytest.raises(ValueError, match="one agent_channel"):
        SemanticExecutionContract(
            requested_environment_basis=RequestedEnvironmentBasis.target_profile,
            delivery=contract.delivery,
            action_kind=ExecutionActionKind.agent_message,
        )
    with pytest.raises(ValueError, match="agent_channel requirements are unused"):
        SemanticExecutionContract(
            requested_environment_basis=RequestedEnvironmentBasis.target_profile,
            delivery=contract.delivery,
            action_kind=ExecutionActionKind.model_output,
            resource_requirements=(requirement,),
        )


def test_external_action_contracts_require_one_target_action_requirement() -> None:
    payload = _tool_contract().model_dump(mode="json")
    payload["resource_requirements"] = [payload["resource_requirements"][0]]
    payload.pop("semantic_digest", None)
    with pytest.raises(ValueError, match="one target_action"):
        SemanticExecutionContract.model_validate(payload)

    action_requirement = _tool_contract().resource_requirements[1]
    with pytest.raises(ValueError, match="target_action requirements are unused"):
        SemanticExecutionContract(
            requested_environment_basis=RequestedEnvironmentBasis.target_profile,
            delivery=SemanticExecutionDelivery(
                delivery_class=ExecutionDeliveryClass.direct_prompt,
                factor_id="CF-1",
                source_role="direct_user_input",
            ),
            action_kind=ExecutionActionKind.model_output,
            resource_requirements=(action_requirement,),
        )


def test_target_action_requirement_must_match_unsafe_outcome() -> None:
    payload = _tool_contract().model_dump(mode="json")
    payload["resource_requirements"][1]["owner_ref"] = "CM-9"
    payload.pop("semantic_digest", None)
    contract = SemanticExecutionContract.model_validate(payload)
    intent = _intent_with_contract(
        contract,
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    with pytest.raises(ValidationError, match="target_action requirement"):
        ExecutionIntent.model_validate(intent.model_dump(mode="json"))

    with pytest.raises(ValueError, match="target_action requirement"):
        resolve_execution_case(intent, _profile())


def _producer_shaped_contract(
    *,
    operation: str = "action-1",
    exact_resource_id: str | None = None,
    exact_retrieval: bool = False,
    requested_environment_basis: str | None = None,
) -> SemanticExecutionContract:
    """Build the target_action requirement as the producer emits it.

    ``operation`` is the semantic operation name and ``owner_ref`` is the
    control action; producer output never makes the two coincide.
    """

    payload = _tool_contract(exact_retrieval=exact_retrieval, exact_action=False).model_dump(
        mode="json"
    )
    payload["requested_environment_basis"] = requested_environment_basis
    payload.pop("semantic_digest", None)
    action = payload["resource_requirements"][1]
    action["operation"] = operation
    action["exact_resource_id"] = exact_resource_id
    action["late_bindable"] = exact_resource_id is None
    return SemanticExecutionContract.model_validate(payload)


def _semantic_operation_simulation_profile() -> ExecutionTargetProfile:
    """Reviewed simulation profile whose CM-1 resource exposes ``action-1`` only."""

    payload = _simulation_profile().model_dump(mode="json")
    payload.pop("semantic_digest", None)
    for resource in payload["resources"]:
        for operation in resource["operations"]:
            operation["semantic_operation"] = operation["operation_id"]
    return ExecutionTargetProfile.model_validate(payload)


def test_producer_shaped_unbound_target_action_stays_pending_without_profile() -> None:
    intent = _parameterized_intent(_producer_shaped_contract())
    requirement = intent.execution_contract.resource_requirements[1]
    assert requirement.owner_ref == intent.unsafe_outcome.control_action_id == "CM-1"
    assert requirement.operation == "action-1"
    assert requirement.exact_resource_id is None

    ExecutionIntent.model_validate(intent.model_dump(mode="json"))
    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "needs_environment_binding"
    assert result.requirement_ids == ("REQ-1", "REQ-2")
    assert result.diagnostics[0].code == "environment_profile_not_supplied"


def test_reviewed_simulation_binds_producer_operation_through_owner_ref() -> None:
    profile = _semantic_operation_simulation_profile()
    intent = _intent_with_contract(
        _producer_shaped_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    binding = next(item for item in result.resolved_bindings if item.requirement_id == "REQ-2")
    assert binding.resource_id == "mcp:target-1:action-1"
    assert binding.operation_id == "action-1"


def test_reviewed_simulation_rejects_owned_resource_with_wrong_operation() -> None:
    profile = _semantic_operation_simulation_profile()
    intent = _intent_with_contract(
        _producer_shaped_contract(operation="action-2"),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "unsupported"
    assert result.requirement_ids == ("REQ-2",)
    assert result.diagnostics[0].code == "operation_unsupported"


def test_reviewed_simulation_rejects_operation_owned_by_another_action() -> None:
    payload = _semantic_operation_simulation_profile().model_dump(mode="json")
    payload.pop("semantic_digest", None)
    for resource in payload["resources"]:
        if "target_control_action" in resource["role_ids"]:
            resource["structural_refs"] = ["CM-9"]
    profile = ExecutionTargetProfile.model_validate(payload)
    intent = _intent_with_contract(
        _producer_shaped_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "unsupported"
    assert result.requirement_ids == ("REQ-2",)
    assert result.diagnostics[0].code == "operation_unsupported"


def test_target_profile_rejects_exact_resource_with_wrong_operation() -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _producer_shaped_contract(
            operation="action-2",
            exact_resource_id="mcp:target-1:action-1",
            exact_retrieval=True,
            requested_environment_basis="target_profile",
        ),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.requirement_ids == ("REQ-2",)
    assert result.diagnostics[0].code == "explicit_target_ref_dangling"


def test_resource_requirement_late_binding_flags_are_consistent() -> None:
    requirement = _tool_contract().resource_requirements[0]
    exact_payload = requirement.model_dump(mode="json")
    exact_payload["exact_resource_id"] = "TOOL-retrieval"
    exact_payload["late_bindable"] = True
    with pytest.raises(ValueError, match="cannot be late_bindable"):
        ExecutionResourceRequirement.model_validate(exact_payload)

    unresolved_payload = requirement.model_dump(mode="json")
    unresolved_payload["exact_resource_id"] = None
    unresolved_payload["late_bindable"] = False
    with pytest.raises(ValueError, match="must be late_bindable"):
        ExecutionResourceRequirement.model_validate(unresolved_payload)


@pytest.mark.parametrize(
    ("delivery_update", "requirement_update", "message"),
    (
        ({"carrier_requirement_id": None}, {}, "requires a carrier"),
        ({"carrier_requirement_id": "REQ-MISSING"}, {}, "does not resolve"),
        (
            {},
            {"purpose": "target_action"},
            "must have stimulus_carrier purpose",
        ),
        (
            {},
            {"required_attacker_influence": "none"},
            "requires direct or indirect attacker influence",
        ),
    ),
)
def test_indirect_carrier_contract_facts_are_required(
    delivery_update: dict[str, str | None],
    requirement_update: dict[str, str],
    message: str,
) -> None:
    payload = _tool_contract().model_dump(mode="json")
    payload["delivery"].update(delivery_update)
    payload["resource_requirements"][0].update(requirement_update)
    payload.pop("semantic_digest", None)

    with pytest.raises(ValueError, match=message):
        SemanticExecutionContract.model_validate(payload)


def test_analytical_contract_rejects_route_requirements_and_environment() -> None:
    gap = ExecutionContractGap(
        code=ExecutionContractGapCode.operation_missing,
        detail="No executable operation is established.",
        evidence_refs=("CF-1",),
    )
    with pytest.raises(ValueError, match="at least one gap"):
        SemanticExecutionContract(disposition="analytical_only")
    with pytest.raises(ValueError, match="executable route fields"):
        SemanticExecutionContract(
            disposition="analytical_only",
            delivery=_direct_contract().delivery,
            gaps=(gap,),
        )
    with pytest.raises(ValueError, match="resource requirements"):
        SemanticExecutionContract(
            disposition="analytical_only",
            resource_requirements=(_agent_requirement(),),
            gaps=(gap,),
        )
    with pytest.raises(ValueError, match="environment basis"):
        SemanticExecutionContract(
            disposition="analytical_only",
            requested_environment_basis=RequestedEnvironmentBasis.target_profile,
            gaps=(gap,),
        )


@pytest.mark.parametrize("bad_value", (float("nan"), float("inf"), float("-inf")))
def test_profile_interface_rejects_nonfinite_json(bad_value: float) -> None:
    with pytest.raises(ValueError, match="interface JSON"):
        TargetProfileResource(
            resource_id="RESOURCE-1",
            resource_kind=ExecutionResourceKind.tool,
            attacker_influence="none",
            input_schema={"type": "object", "properties": {"invalid": bad_value}},
            evidence_refs=("inventory:tool:test",),
        )


def test_profile_validation_rejects_missing_and_forbidden_simulation_behavior() -> None:
    missing_behavior = _simulation_profile().model_dump(mode="json")
    missing_behavior["resources"][0]["simulation_behavior"] = None
    missing_behavior.pop("semantic_digest", None)
    with pytest.raises(ValueError, match="simulation_behavior"):
        ExecutionTargetProfile.model_validate(missing_behavior)

    forbidden_behavior = _profile().model_dump(mode="json")
    forbidden_behavior["resources"][0]["simulation_behavior"] = {
        "inputs": {"request": "safe"},
        "outputs": {"response": "safe"},
        "observation_points": ["response"],
    }
    forbidden_behavior.pop("semantic_digest", None)
    with pytest.raises(ValueError, match="cannot contain simulation_behavior"):
        ExecutionTargetProfile.model_validate(forbidden_behavior)


@pytest.mark.parametrize(
    ("update", "message"),
    (
        ({"profile_fit": "matched"}, "profile_fit not_required"),
        ({"claim_scope": "target_specific_intent"}, "model_behavior_only"),
        (
            {
                "selected_profile_id": "forged",
                "selected_profile_basis": "target",
                "target_environment_id": "env",
                "target_profile_digest": "0" * 64,
                "inventory_authority": "observed",
                "semantic_authority": "inferred",
            },
            "cannot retain a profile",
        ),
    ),
)
def test_target_agnostic_bound_case_rejects_profile_backed_result(
    update: dict[str, str], message: str
) -> None:
    intent = _intent_with_contract(
        _direct_contract(),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_agnostic,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.model_behavior_only,
        ),
    )
    case = resolve_execution_case(intent, None)
    assert isinstance(case, BoundExecutionCase)
    payload = case.model_dump(mode="json") | update

    with pytest.raises(ValueError, match=message):
        BoundExecutionCase.model_validate(payload)


def test_target_agnostic_bound_case_rejects_domain_resources() -> None:
    value = SimpleNamespace(
        intent=SimpleNamespace(
            execution_contract=SimpleNamespace(resource_requirements=("REQ-1",))
        )
    )

    with pytest.raises(ValueError, match="domain resources"):
        _validate_target_agnostic_result(value)


def test_bound_case_profile_identity_is_required() -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _tool_contract(exact_retrieval=True),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )
    case = resolve_execution_case(intent, profile)
    assert isinstance(case, BoundExecutionCase)
    payload = case.model_dump(mode="json")
    payload.pop("target_environment_id")

    with pytest.raises(ValueError, match="profile pin is incomplete"):
        BoundExecutionCase.model_validate(payload)


def test_execution_intent_stimulus_must_match_contract_delivery() -> None:
    payload = _intent().model_dump(mode="json")
    contract = _direct_contract().model_dump(mode="json")
    contract["delivery"]["source_role"] = "other_input"
    contract["semantic_digest"] = compute_framed_digest(
        "stpa-execution-contract-v1",
        {key: item for key, item in contract.items() if key != "semantic_digest"},
    )
    payload["execution_contract"] = contract

    with pytest.raises(ValueError, match="stimulus route must match"):
        ExecutionIntent.model_validate(payload)


def test_analytical_contract_digest_tampering_is_rejected() -> None:
    contract = SemanticExecutionContract(
        disposition="analytical_only",
        delivery=None,
        action_kind=None,
        resource_requirements=(),
        gaps=(
            ExecutionContractGap(
                code=ExecutionContractGapCode.operation_missing,
                detail="No executable operation is established.",
                evidence_refs=("CF-1",),
            ),
        ),
    )
    payload = contract.model_dump(mode="json") | {"semantic_digest": "0" * 64}

    with pytest.raises(ValueError, match="semantic_digest"):
        SemanticExecutionContract.model_validate(payload)


def test_analytical_contract_is_excluded_without_route_inference() -> None:
    contract = SemanticExecutionContract(
        disposition="analytical_only",
        delivery=None,
        action_kind=None,
        resource_requirements=(),
        gaps=(
            ExecutionContractGap(
                code=ExecutionContractGapCode.operation_missing,
                detail="No executable operation is established.",
                evidence_refs=("CF-1",),
            ),
        ),
    )
    intent = _intent_with_contract(
        contract,
        _classification(
            completeness=BindingCompleteness.analytical_only,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.invalid,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    result = resolve_execution_case(intent, _profile())

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "analytical_only"
    assert result.diagnostics[0].code == "operation_missing"


def test_analytical_classification_requires_invalid_profile_fit() -> None:
    contract = SemanticExecutionContract(
        disposition="analytical_only",
        delivery=None,
        action_kind=None,
        resource_requirements=(),
        gaps=(
            ExecutionContractGap(
                code=ExecutionContractGapCode.operation_missing,
                detail="No executable operation is established.",
                evidence_refs=("CF-1",),
            ),
        ),
    )
    intent = _intent_with_contract(
        contract,
        _classification(
            completeness=BindingCompleteness.analytical_only,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "producer_binding_mismatch"


def test_inconsistent_analytical_classification_is_not_repaired() -> None:
    contract = SemanticExecutionContract(
        disposition="analytical_only",
        delivery=None,
        action_kind=None,
        resource_requirements=(),
        gaps=(
            ExecutionContractGap(
                code=ExecutionContractGapCode.operation_missing,
                detail="No executable operation is established.",
                evidence_refs=("CF-1",),
            ),
        ),
    )
    intent = _intent_with_contract(
        contract,
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_agnostic,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.model_behavior_only,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"
    assert result.diagnostics[0].code == "producer_binding_mismatch"


def test_exact_resource_without_profile_remains_unresolved() -> None:
    intent = _intent_with_contract(
        _tool_contract(exact_resource_id="mcp:target-1:retrieve-1"),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_profile,
            fit=ExecutionProfileFit.matched,
            claim=ExecutionClaimScope.target_specific_intent,
        ),
    )

    result = resolve_execution_case(intent, None)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "invalid_source_binding"


def test_complete_inventory_without_matching_resource_is_unsupported() -> None:
    profile = _simulation_profile()
    action = next(item for item in profile.resources if "target_control_action" in item.role_ids)
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
    )

    reduced = profile.model_copy(update={"resources": (action,), "semantic_digest": None})
    reduced = reduced.model_copy(update={"semantic_digest": reduced.compute_semantic_digest()})
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=reduced,
    )
    result = resolve_execution_case(intent, reduced)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "unsupported"


def test_inferred_profile_cannot_establish_target_claim() -> None:
    profile = _profile()
    intent = _intent_with_contract(
        _tool_contract(exact_action=False),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, ExecutionCaseExclusion)
    assert result.code == "needs_target_binding"


def test_explicit_simulation_profile_produces_simulated_claim() -> None:
    profile = _simulation_profile()
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    assert result.selected_profile_basis == "simulation"
    assert result.claim_scope is ExecutionClaimScope.agent_behavior_with_simulated_tools
    assert tuple(item.resource_id for item in result.selected_simulation_resources) == (
        "mcp:target-1:action-1",
        "mcp:target-1:retrieve-1",
    )
    assert all(
        item.simulation_behavior.observation_points == ("response",)
        for item in result.selected_simulation_resources
    )


def test_complete_inferred_simulation_profile_can_resolve_concretely() -> None:
    profile = _simulation_profile()
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )

    result = resolve_execution_case(intent, profile)

    assert isinstance(result, BoundExecutionCase)
    assert result.binding_completeness is BindingCompleteness.concrete
    assert result.environment_basis is EnvironmentBasis.simulation_profile
    assert result.profile_fit is ExecutionProfileFit.matched
    assert result.claim_scope is ExecutionClaimScope.agent_behavior_with_simulated_tools


def test_simulation_evidence_reaches_ready_plan_and_garak_metadata() -> None:
    profile = _simulation_profile()
    intent = _intent_with_contract(
        _simulation_contract(),
        _classification(
            completeness=BindingCompleteness.parameterized,
            environment=EnvironmentBasis.none,
            fit=ExecutionProfileFit.needs_binding,
            claim=ExecutionClaimScope.no_execution_claim,
        ),
        profile=profile,
    )
    case = resolve_execution_case(intent, profile)
    assert isinstance(case, BoundExecutionCase)
    base = _bindings(intent, adapter_operation="tool_call", target_surface="tool_call")
    stimulus = base.stimulus_bindings[0].model_copy(
        update={
            "delivery_class": "indirect_content",
            "surface": "tool_result",
            "source_kind": "tool_output",
            "carrier_tool_name": "retrieve_content",
            "carrier_tool_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            "carrier_tool_arguments": {"limit": 1},
        }
    )
    causal_surface = base.surface_bindings[0].model_copy(update={"surface": "tool_result"})
    bindings = RuntimeBindingSet.create(
        binding_set_id=base.binding_set_id,
        projection_semantic_digest=base.projection_semantic_digest,
        target_environment_id=case.target_environment_id,
        review=base.review,
        semantic_bindings=base.semantic_bindings,
        surface_bindings=(causal_surface, base.surface_bindings[1]),
        control_action_bindings=base.control_action_bindings,
        observation_bindings=base.observation_bindings,
        stimulus_bindings=(stimulus,),
        clock_binding=base.clock_binding,
    )
    capabilities_data = _capabilities().model_dump(mode="json")
    capabilities_data["writable_surfaces"].append("tool_result")
    readiness = bind_and_plan(
        case, bindings, PlatformCapabilities.model_validate(capabilities_data)
    )

    assert readiness.ready
    assert readiness.plan is not None
    assert readiness.plan.selected_simulation_resources == case.selected_simulation_resources
    garak_plan = build_garak_plan(readiness.plan)
    metadata = garak_plan.to_dict()["selected_simulation_resources"]
    assert metadata == [
        item.model_dump(mode="json") for item in case.selected_simulation_resources
    ]


def test_ready_plan_carries_bound_case_identity() -> None:
    contract = _direct_contract()
    classification = _classification(
        completeness=BindingCompleteness.concrete,
        environment=EnvironmentBasis.target_agnostic,
        fit=ExecutionProfileFit.not_required,
        claim=ExecutionClaimScope.model_behavior_only,
    )
    intent = _intent_with_contract(contract, classification)
    case = resolve_execution_case(intent, None)
    readiness = bind_and_plan(case, _bindings(intent), _capabilities())

    assert readiness.ready
    assert readiness.plan is not None
    assert readiness.plan.case_id == case.case_id
    assert readiness.plan.case_digest == case.case_digest
    assert readiness.plan.binding_completeness == case.binding_completeness.value
    assert readiness.plan.environment_basis == case.environment_basis.value
    assert readiness.plan.profile_fit == case.profile_fit.value
    assert (
        readiness.plan.execution_classification_digest
        == case.execution_classification.classification_digest
    )
    assert readiness.plan.source_binding_completeness == (
        case.execution_classification.binding_completeness.value
    )
    assert readiness.plan.claim_scope == "model_behavior_only"


def test_readiness_rejects_a_plan_without_case_provenance() -> None:
    contract = _direct_contract()
    classification = _classification(
        completeness=BindingCompleteness.concrete,
        environment=EnvironmentBasis.target_agnostic,
        fit=ExecutionProfileFit.not_required,
        claim=ExecutionClaimScope.model_behavior_only,
    )
    intent = _intent_with_contract(contract, classification)
    case = resolve_execution_case(intent, None)
    readiness = bind_and_plan(case, _bindings(intent), _capabilities())

    assert readiness.plan is not None
    plan_data = readiness.plan.model_dump(mode="python")
    plan_data.pop("case_id")
    with pytest.raises(ValidationError, match="case_id"):
        type(readiness.plan).model_validate(plan_data)


def test_readiness_rejects_a_bare_intent() -> None:
    intent = _intent_with_contract(
        _direct_contract(),
        _classification(
            completeness=BindingCompleteness.concrete,
            environment=EnvironmentBasis.target_agnostic,
            fit=ExecutionProfileFit.not_required,
            claim=ExecutionClaimScope.model_behavior_only,
        ),
    )

    with pytest.raises(TypeError, match="BoundExecutionCase"):
        bind_and_plan(intent, _bindings(intent), _capabilities())  # type: ignore[arg-type]


def test_garak_artifact_carries_bound_case_provenance() -> None:
    from asago_artifact_generator.garak.capabilities import garak_capabilities
    from asago_artifact_generator.garak.default_bindings import (
        complete_garak_runtime_bindings,
    )

    contract = _direct_contract()
    classification = _classification(
        completeness=BindingCompleteness.concrete,
        environment=EnvironmentBasis.target_agnostic,
        fit=ExecutionProfileFit.not_required,
        claim=ExecutionClaimScope.model_behavior_only,
    )
    intent = _intent_with_contract(contract, classification).model_copy(
        update={
            "execution_requirements": _intent().execution_requirements.model_copy(
                update={"requires_tool_execution": False}
            )
        }
    )
    case = resolve_execution_case(intent, None)
    readiness = bind_and_plan(case, complete_garak_runtime_bindings(case), garak_capabilities())

    assert readiness.plan is not None
    plan = build_garak_plan(readiness.plan)
    compiled = compile_garak_artifact(
        plan,
        prebound_texts={"stimulus:STIM-1": "Review the supplied request."},
    )

    assert compiled.artifact["case_id"] == case.case_id
    assert compiled.artifact["case_digest"] == case.case_digest
    assert (
        compiled.artifact["execution_classification_digest"]
        == classification.classification_digest
    )
    assert compiled.artifact["claim_scope"] == "model_behavior_only"
    assert compiled.artifact["artifact_trace"]["source"]["case_id"] == case.case_id
