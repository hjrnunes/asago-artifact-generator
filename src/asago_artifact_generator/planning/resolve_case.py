"""Pure exact resolution of semantic execution cases to selected environments."""

from __future__ import annotations

from collections.abc import Iterable

from ..models.execution_case import (
    BoundExecutionCase,
    ExecutionCaseDiagnostic,
    ExecutionCaseExclusion,
    ExecutionCaseResolution,
    SelectedSimulationResource,
)
from ..models.execution_classification import (
    BindingCompleteness,
    EnvironmentBasis,
    ExecutionClaimScope,
    ExecutionClassification,
    ExecutionProfileFit,
    ExecutionResourceRequirement,
    ExecutionTargetProfile,
    ProfileBasis,
    RequestedEnvironmentBasis,
    ResolvedExecutionBinding,
    SemanticExecutionContract,
    TargetProfileResource,
)
from ..models.execution_intent import ExecutionIntent


def resolve_execution_case(
    intent: ExecutionIntent,
    profile: ExecutionTargetProfile | None,
) -> ExecutionCaseResolution:
    """Resolve one verified semantic intent against one explicit profile.

    The resolver is deliberately before runtime readiness.  It selects no
    surface, safe value, observer, endpoint, credential, provider, or
    platform operation.  A profile is used only for exact semantic role
    matching; missing or ambiguous facts remain typed exclusions.
    """

    _require_typed_inputs(intent, profile)
    contract, classification = _execution_sources(intent)
    source_error = _source_integrity_error(contract, classification)
    if source_error is not None:
        return _source_integrity_exclusion(intent, classification, source_error)
    source_lineage_error = _source_profile_lineage_error(intent, contract, classification, profile)
    if source_lineage_error is not None:
        return _source_integrity_exclusion(intent, classification, source_lineage_error)
    return _resolve_verified_case(intent, contract, classification, profile)


def _source_integrity_exclusion(
    intent: ExecutionIntent,
    classification: ExecutionClassification,
    detail: str,
) -> ExecutionCaseExclusion:
    return _exclusion(
        intent,
        classification,
        "invalid_source_binding",
        (),
        _diagnostic("target_profile_digest_mismatch", detail),
    )


def _resolve_verified_case(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile | None,
) -> ExecutionCaseResolution:
    target_action_error = _target_action_requirement_error(intent)
    if target_action_error is not None:
        detail, requirement_ids = target_action_error
        raise ValueError(f"{detail}: {', '.join(requirement_ids)}")
    if contract.disposition == "analytical_only":
        return _resolve_analytical_case(intent, contract, classification)
    if profile is None:
        return _without_profile(intent, contract, classification)
    return _resolve_supplied_profile(intent, contract, classification, profile)


def _execution_sources(
    intent: ExecutionIntent,
) -> tuple[SemanticExecutionContract, ExecutionClassification]:
    contract = intent.execution_contract
    classification = intent.execution_classification
    if contract is None or classification is None:
        raise ValueError(
            "ExecutionIntent requires execution_contract and execution_classification"
        )
    return contract, classification


def _resolve_supplied_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    profile_error = _profile_integrity_error(profile)
    if profile_error is not None:
        return _exclusion(
            intent,
            classification,
            "invalid_profile",
            (),
            _diagnostic("target_profile_digest_mismatch", profile_error),
        )
    if _profile_digest_mismatch(classification, profile):
        return _exclusion(
            intent,
            classification,
            "invalid_profile",
            (),
            _diagnostic(
                "target_profile_digest_mismatch",
                "classification target_profile_digest does not match the supplied profile",
            ),
        )
    return _with_profile(intent, contract, classification, profile)


def _source_integrity_error(
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> str | None:
    try:
        contract.assert_integrity()
        classification.assert_integrity()
    except ValueError as exc:
        return str(exc)
    return None


def _source_profile_lineage_error(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile | None,
) -> str | None:
    """Verify target-profile pins without reconstructing producer authority.

    The producer owns target-realization contents and row semantics.  The
    consumer only checks that an explicitly supplied profile is the one named
    by the projection and retains the two source pins for downstream output.
    """

    pins = intent.trace_refs.source_pins
    profile_pin = pins.get("execution_target_profile")
    realization_pin = pins.get("target_realization")
    exact_resource_required = any(
        requirement.exact_resource_id is not None for requirement in contract.resource_requirements
    )
    profile_consumed = (
        profile is not None
        and contract.disposition != "analytical_only"
        and contract.requested_environment_basis is not RequestedEnvironmentBasis.target_agnostic
    )
    if exact_resource_required and (profile_pin is None or realization_pin is None):
        return (
            "exact resource requirements require execution_target_profile and "
            "target_realization source pins"
        )
    if profile_pin is not None:
        if profile is None:
            return (
                "execution_target_profile source pin cannot be verified without a supplied profile"
            )
        if profile.basis is not ProfileBasis.target:
            return "target source pins cannot be attached to a simulation profile"
        if profile_pin != profile.semantic_digest:
            return (
                "execution_target_profile source pin does not match the supplied "
                "profile semantic_digest"
            )
        if classification.target_profile_digest is None:
            return (
                "classification target_profile_digest is required with an "
                "execution_target_profile source pin"
            )
        if classification.target_profile_digest != profile.semantic_digest:
            return (
                "classification target_profile_digest does not match the supplied "
                "profile semantic_digest"
            )
    if realization_pin is not None and profile is None:
        return "target_realization source pin cannot be retained without a supplied profile"
    if profile_consumed and classification.target_profile_digest is None:
        return (
            "classification target_profile_digest is required when a supplied profile is consumed"
        )
    if profile_consumed and classification.target_profile_digest != profile.semantic_digest:
        return (
            "classification target_profile_digest does not match the supplied "
            "profile semantic_digest"
        )
    return None


def _target_action_requirement_error(
    intent: ExecutionIntent,
) -> tuple[str, tuple[str, ...]] | None:
    """Recheck the UCA identity at the consumer boundary."""

    action_id = intent.unsafe_outcome.control_action_id
    mismatches = tuple(
        requirement.requirement_id
        for requirement in intent.execution_contract.resource_requirements
        if requirement.purpose.value == "target_action"
        and (
            requirement.owner_ref != action_id
            or (requirement.exact_resource_id is None and requirement.operation != action_id)
        )
    )
    if not mismatches:
        return None
    return (
        "target_action requirement must name the unsafe outcome control action",
        mismatches,
    )


def _resolve_analytical_case(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> ExecutionCaseResolution:
    source_error = _validate_analytical_classification(classification)
    if source_error is not None:
        code, requirement_ids, diagnostics = source_error
        return _exclusion(intent, classification, code, requirement_ids, *diagnostics)
    return _exclusion(
        intent,
        classification,
        "analytical_only",
        (),
        *_contract_gap_diagnostics(contract),
    )


def _profile_integrity_error(profile: ExecutionTargetProfile) -> str | None:
    try:
        profile.assert_integrity()
    except ValueError as exc:
        return str(exc)
    return None


def _profile_digest_mismatch(
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> bool:
    return (
        classification.target_profile_digest is not None
        and classification.target_profile_digest != profile.semantic_digest
    )


def _require_typed_inputs(intent: ExecutionIntent, profile: ExecutionTargetProfile | None) -> None:
    if not isinstance(intent, ExecutionIntent):
        raise TypeError("resolve_execution_case requires an ExecutionIntent")
    if profile is not None and not isinstance(profile, ExecutionTargetProfile):
        raise TypeError("resolve_execution_case requires an ExecutionTargetProfile")


def _without_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> ExecutionCaseResolution:
    requested = contract.requested_environment_basis
    if requested is RequestedEnvironmentBasis.target_agnostic:
        return _without_profile_target_agnostic(intent, contract, classification)
    if requested is None:
        return _without_profile_unselected_environment(intent, contract, classification)
    if requested is RequestedEnvironmentBasis.simulation_profile:
        return _without_profile_simulation(intent, contract, classification)
    return _without_profile_target(intent, contract, classification)


def _without_profile_target_agnostic(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> ExecutionCaseResolution:
    source_error = _validate_target_agnostic_classification(classification)
    if source_error is not None:
        code, requirement_ids, diagnostics = source_error
        return _exclusion(intent, classification, code, requirement_ids, *diagnostics)
    if contract.resource_requirements:
        return _exclusion(
            intent,
            classification,
            "invalid_source_binding",
            tuple(item.requirement_id for item in contract.resource_requirements),
            _diagnostic(
                "execution_route_missing",
                "target_agnostic contract unexpectedly contains resource requirements",
            ),
        )
    return _bound(
        intent,
        classification,
        claim_scope=ExecutionClaimScope.model_behavior_only,
    )


def _without_profile_unselected_environment(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> ExecutionCaseResolution:
    return _exclusion(
        intent,
        classification,
        "needs_environment_binding",
        tuple(item.requirement_id for item in contract.resource_requirements),
        _diagnostic(
            "environment_profile_not_supplied",
            "the execution contract has no requested environment basis and "
            "no profile was supplied",
        ),
    )


def _without_profile_simulation(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> ExecutionCaseResolution:
    return _exclusion(
        intent,
        classification,
        "needs_simulation_binding",
        tuple(item.requirement_id for item in contract.resource_requirements),
        _diagnostic(
            "simulation_contract_missing",
            "the execution contract requests a simulation profile but none was supplied",
        ),
    )


def _without_profile_target(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
) -> ExecutionCaseResolution:
    return _exclusion(
        intent,
        classification,
        "needs_target_binding",
        tuple(item.requirement_id for item in contract.resource_requirements),
        _diagnostic(
            "target_profile_not_supplied",
            "the execution contract requests a profile but none was supplied",
        ),
    )


def _validate_target_agnostic_classification(
    classification: ExecutionClassification,
) -> tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]] | None:
    if _classification_matches(
        classification,
        BindingCompleteness.concrete,
        EnvironmentBasis.target_agnostic,
        ExecutionProfileFit.not_required,
        ExecutionClaimScope.model_behavior_only,
    ):
        return None
    return _binding_mismatch(
        "producer classification is inconsistent with a target-agnostic contract"
    )


def _validate_analytical_classification(
    classification: ExecutionClassification,
) -> tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]] | None:
    if _classification_matches(
        classification,
        BindingCompleteness.analytical_only,
        EnvironmentBasis.none,
        ExecutionProfileFit.invalid,
        ExecutionClaimScope.no_execution_claim,
    ):
        return None
    return _binding_mismatch(
        "producer analytical classification is inconsistent with its contract"
    )


def _classification_matches(
    classification: ExecutionClassification,
    completeness: BindingCompleteness,
    environment: EnvironmentBasis,
    profile_fit: ExecutionProfileFit,
    claim_scope: ExecutionClaimScope,
) -> bool:
    return _classification_tuple(classification) == (
        completeness,
        environment,
        profile_fit,
        claim_scope,
    ) and _classification_has_no_bindings(classification)


def _classification_tuple(
    classification: ExecutionClassification,
) -> tuple[BindingCompleteness, EnvironmentBasis, ExecutionProfileFit, ExecutionClaimScope]:
    return (
        classification.binding_completeness,
        classification.environment_basis,
        classification.profile_fit,
        classification.claim_scope,
    )


def _classification_has_no_bindings(classification: ExecutionClassification) -> bool:
    return not any(
        (
            classification.resolved_bindings,
            classification.unresolved_requirement_ids,
            classification.ambiguous_matches,
            classification.unsupported_requirement_ids,
        )
    )


def _with_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    requested = contract.requested_environment_basis
    if requested is RequestedEnvironmentBasis.target_agnostic:
        # A caller may pass one global profile for a mixed bundle.  A
        # target-agnostic contract does not consume it and must retain its
        # model-only claim rather than becoming an invalid target case.
        return _without_profile(intent, contract, classification)
    if requested is None:
        return _resolve_inferred_profile(intent, contract, classification, profile)
    if requested is RequestedEnvironmentBasis.target_profile:
        return _resolve_requested_target_profile(intent, contract, classification, profile)
    return _resolve_requested_simulation_profile(intent, contract, classification, profile)


def _resolve_inferred_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    if profile.basis is ProfileBasis.target:
        return _resolve_target_profile(intent, contract, classification, profile)
    return _resolve_simulation_profile(intent, contract, classification, profile)


def _resolve_requested_target_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    if profile.basis is ProfileBasis.target:
        return _resolve_target_profile(intent, contract, classification, profile)
    return _exclusion(
        intent,
        classification,
        "invalid_profile",
        (),
        _diagnostic("profile_inferred_only", "a target profile is required"),
    )


def _resolve_requested_simulation_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    if profile.basis is ProfileBasis.simulation:
        return _resolve_simulation_profile(intent, contract, classification, profile)
    return _exclusion(
        intent,
        classification,
        "invalid_profile",
        (),
        _diagnostic("simulation_contract_missing", "a simulation profile is required"),
    )


def _resolve_target_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    return _resolve_profile_bindings(
        intent,
        contract,
        classification,
        profile=profile,
        expected_environment=EnvironmentBasis.target_profile,
        claim_scope=ExecutionClaimScope.target_specific_intent,
    )


def _resolve_simulation_profile(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> ExecutionCaseResolution:
    missing_behavior = _missing_simulation_behavior(profile)
    if missing_behavior:
        return _exclusion(
            intent,
            classification,
            "invalid_profile",
            (),
            _diagnostic(
                "simulation_contract_missing",
                "every simulation resource needs an explicit simulation_behavior",
                candidates=missing_behavior,
            ),
        )
    return _resolve_profile_bindings(
        intent,
        contract,
        classification,
        profile=profile,
        expected_environment=EnvironmentBasis.simulation_profile,
        claim_scope=ExecutionClaimScope.agent_behavior_with_simulated_tools,
    )


def _missing_simulation_behavior(
    profile: ExecutionTargetProfile,
) -> tuple[str, ...]:
    return tuple(
        resource.resource_id
        for resource in profile.resources
        if resource.simulation_behavior is None
    )


def _resolve_profile_bindings(
    intent: ExecutionIntent,
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    *,
    profile: ExecutionTargetProfile,
    expected_environment: EnvironmentBasis,
    claim_scope: ExecutionClaimScope,
) -> ExecutionCaseResolution:
    bindings, exclusion = _resolve_requirements(contract.resource_requirements, profile)
    source_error = _verify_producer_binding(
        classification,
        bindings,
        exclusion,
        profile=profile,
        expected_environment=expected_environment,
        expected_claim=claim_scope,
    )
    if source_error is not None:
        code, requirement_ids, diagnostics = source_error
        return _exclusion(intent, classification, code, requirement_ids, *diagnostics)
    if exclusion is not None:
        code, requirement_ids, diagnostics = exclusion
        return _exclusion(intent, classification, code, requirement_ids, *diagnostics)
    return _bound(
        intent,
        classification,
        profile=profile,
        resolved_bindings=bindings,
        claim_scope=claim_scope,
    )


def _resolve_requirements(
    requirements: tuple[ExecutionResourceRequirement, ...],
    profile: ExecutionTargetProfile,
) -> tuple[
    tuple[ResolvedExecutionBinding, ...],
    tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]] | None,
]:
    resolved: list[ResolvedExecutionBinding] = []
    for requirement in requirements:
        candidates = (
            _matching_exact_resource(requirement, profile.resources)
            if requirement.exact_resource_id is not None
            else _matching_resources(requirement, profile.resources)
        )
        binding, exclusion = _resolve_one_requirement(requirement, candidates, profile)
        if exclusion is not None:
            return (), exclusion
        if binding is not None:
            resolved.append(binding)
    return tuple(resolved), None


_RequirementExclusion = tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]]


def _resolve_one_requirement(
    requirement: ExecutionResourceRequirement,
    candidates: tuple[tuple[TargetProfileResource, object], ...],
    profile: ExecutionTargetProfile,
) -> tuple[ResolvedExecutionBinding | None, _RequirementExclusion | None]:
    if requirement.exact_resource_id is not None:
        return _resolve_exact_requirement(requirement, candidates)
    return _resolve_role_requirement(requirement, candidates, profile)


def _resolve_exact_requirement(
    requirement: ExecutionResourceRequirement,
    candidates: tuple[tuple[TargetProfileResource, object], ...],
) -> tuple[ResolvedExecutionBinding | None, _RequirementExclusion | None]:
    exact = tuple(
        item for item in candidates if item[0].resource_id == requirement.exact_resource_id
    )
    if len(exact) != 1:
        return (
            None,
            (
                "invalid_source_binding",
                (requirement.requirement_id,),
                (
                    _diagnostic(
                        "explicit_target_ref_dangling",
                        "exact resource ID is absent or does not satisfy the requirement",
                        requirement.requirement_id,
                        (requirement.exact_resource_id,),
                    ),
                ),
            ),
        )
    resource, operation = exact[0]
    return (
        ResolvedExecutionBinding(
            requirement_id=requirement.requirement_id,
            resource_id=resource.resource_id,
            operation_id=operation.operation_id,
        ),
        None,
    )


def _matching_exact_resource(
    requirement: ExecutionResourceRequirement,
    resources: Iterable[TargetProfileResource],
) -> tuple[tuple[TargetProfileResource, object], ...]:
    """Match a producer-selected resource and operation without role inference."""

    matches: list[tuple[TargetProfileResource, object]] = []
    for resource in resources:
        if resource.resource_id != requirement.exact_resource_id:
            continue
        if resource.resource_kind not in requirement.acceptable_resource_kinds:
            continue
        if not set(requirement.required_surfaces).issubset(resource.surfaces):
            continue
        for operation in resource.operations:
            if operation.operation_id != requirement.operation:
                continue
            if not set(requirement.required_properties).issubset(operation.observable_properties):
                continue
            matches.append((resource, operation))
    return tuple(matches)


def _profile_supports_role_resolution(profile: ExecutionTargetProfile) -> bool:
    """Return whether profile evidence closes role-based resource matching."""

    # A target inventory proves the observed interface, but it does not select
    # the operation that a producer target-realization row chose for this
    # control action.  Only an explicit exact_resource_id/operation pair may
    # close a target binding.  Simulation remains role-bindable because its
    # reviewed mock contract is the execution authority.
    return (
        profile.basis is ProfileBasis.simulation and profile.semantic_authority.value == "reviewed"
    )


def _resolve_role_requirement(
    requirement: ExecutionResourceRequirement,
    candidates: tuple[tuple[TargetProfileResource, object], ...],
    profile: ExecutionTargetProfile,
) -> tuple[ResolvedExecutionBinding | None, _RequirementExclusion | None]:
    candidate_ids = tuple(resource.resource_id for resource, _ in candidates)
    if len(candidates) > 1:
        return (
            None,
            (
                "ambiguous",
                (requirement.requirement_id,),
                (
                    _diagnostic(
                        "target_resource_ambiguous",
                        "more than one exact profile resource matches the role",
                        requirement.requirement_id,
                        candidate_ids,
                    ),
                ),
            ),
        )
    if not candidates:
        return None, _missing_role_exclusion(requirement, profile)
    resource, operation = candidates[0]
    if not _profile_supports_role_resolution(profile):
        if profile.basis is ProfileBasis.target:
            diagnostic_code = "profile_inferred_only"
            detail = (
                "target profiles require an exact resource and operation selected "
                "by target realization"
            )
        else:
            diagnostic_code = "profile_inventory_unknown"
            detail = "a role match is not authoritative in an unreviewed simulation profile"
        return (
            None,
            (
                "needs_target_binding",
                (requirement.requirement_id,),
                (
                    _diagnostic(
                        diagnostic_code,
                        detail,
                        requirement.requirement_id,
                        candidate_ids,
                    ),
                ),
            ),
        )
    return (
        ResolvedExecutionBinding(
            requirement_id=requirement.requirement_id,
            resource_id=resource.resource_id,
            operation_id=operation.operation_id,
        ),
        None,
    )


def _missing_role_exclusion(
    requirement: ExecutionResourceRequirement,
    profile: ExecutionTargetProfile,
) -> _RequirementExclusion:
    complete = _profile_supports_role_resolution(profile)
    if complete:
        code = "unsupported"
        diagnostic_code = "operation_unsupported"
    elif profile.basis is ProfileBasis.target:
        code = "needs_target_binding"
        diagnostic_code = "profile_inferred_only"
    else:
        code = "needs_target_binding"
        diagnostic_code = "target_resource_unresolved"
    return (
        code,
        (requirement.requirement_id,),
        (
            _diagnostic(
                diagnostic_code,
                "no exact profile resource satisfies the role",
                requirement.requirement_id,
            ),
        ),
    )


def _verify_producer_binding(
    classification: ExecutionClassification,
    bindings: tuple[ResolvedExecutionBinding, ...],
    exclusion: tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]] | None,
    *,
    profile: ExecutionTargetProfile,
    expected_environment: EnvironmentBasis,
    expected_claim: ExecutionClaimScope,
) -> tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]] | None:
    """Reject a concrete producer claim that disagrees with the profile.

    Parameterized classifications are intentionally resolved by this consumer.
    A producer that already claimed ``concrete`` must instead match the exact
    resource/operation tuples selected from the pinned profile; otherwise the
    consumer would silently reinterpret the source classification.
    """

    if classification.binding_completeness is not BindingCompleteness.concrete:
        return None
    claim_error = _producer_claim_error(classification, expected_environment, expected_claim)
    if claim_error is not None:
        return _binding_mismatch(claim_error)
    authority_error = _producer_authority_error(classification, profile)
    if authority_error is not None:
        return _binding_mismatch(authority_error)
    if exclusion is not None:
        _, requirement_ids, _ = exclusion
        return _binding_mismatch(
            "pinned profile cannot satisfy the producer's concrete requirements",
            requirement_ids,
        )
    expected = tuple(
        sorted(
            bindings,
            key=lambda item: (
                item.requirement_id,
                item.resource_id,
                item.operation_id,
            ),
        )
    )
    if not _bindings_match(classification, expected):
        return _binding_mismatch(
            "producer concrete bindings do not match the pinned profile",
            tuple(item.requirement_id for item in classification.resolved_bindings),
        )
    return None


def _producer_claim_error(
    classification: ExecutionClassification,
    expected_environment: EnvironmentBasis,
    expected_claim: ExecutionClaimScope,
) -> str | None:
    if classification.environment_basis is not expected_environment:
        return "producer classification environment basis does not match the selected profile"
    if classification.profile_fit is not ExecutionProfileFit.matched:
        return "producer concrete classification does not report a matched profile"
    if classification.claim_scope is not expected_claim:
        return "producer concrete classification has an incompatible claim scope"
    return None


def _producer_authority_error(
    classification: ExecutionClassification,
    profile: ExecutionTargetProfile,
) -> str | None:
    if classification.inventory_authority is not profile.inventory_authority:
        return "producer inventory authority does not match the selected profile"
    if classification.semantic_authority is not profile.semantic_authority:
        return "producer semantic authority does not match the selected profile"
    return None


def _bindings_match(
    classification: ExecutionClassification,
    expected: tuple[ResolvedExecutionBinding, ...],
) -> bool:
    supplied = tuple(
        sorted(
            classification.resolved_bindings,
            key=lambda item: (
                item.requirement_id,
                item.resource_id,
                item.operation_id,
            ),
        )
    )
    return supplied == expected


def _binding_mismatch(
    detail: str, requirement_ids: tuple[str, ...] = ()
) -> tuple[str, tuple[str, ...], tuple[ExecutionCaseDiagnostic, ...]]:
    return (
        "invalid_source_binding",
        tuple(sorted(set(requirement_ids))),
        (_diagnostic("producer_binding_mismatch", detail),),
    )


def _matching_resources(
    requirement: ExecutionResourceRequirement,
    resources: Iterable[TargetProfileResource],
) -> tuple[tuple[TargetProfileResource, object], ...]:
    matches: list[tuple[TargetProfileResource, object]] = []
    for resource in resources:
        if not _resource_matches_requirement(requirement, resource):
            continue
        for operation in resource.operations:
            if _operation_matches_requirement(requirement, operation):
                matches.append((resource, operation))
    return tuple(matches)


def _resource_matches_requirement(
    requirement: ExecutionResourceRequirement,
    resource: TargetProfileResource,
) -> bool:
    if resource.resource_kind not in requirement.acceptable_resource_kinds:
        return False
    if requirement.role_id not in resource.role_ids:
        return False
    if requirement.owner_ref not in resource.structural_refs:
        return False
    if not set(requirement.required_surfaces).issubset(resource.surfaces):
        return False
    return not (
        requirement.required_attacker_influence is not None
        and resource.attacker_influence is not requirement.required_attacker_influence
    )


def _operation_matches_requirement(
    requirement: ExecutionResourceRequirement,
    operation: object,
) -> bool:
    if (
        operation.operation_id != requirement.operation
        and operation.semantic_operation != requirement.operation
    ):
        return False
    return set(requirement.required_properties) <= set(operation.observable_properties)


def _bound(
    intent: ExecutionIntent,
    classification: ExecutionClassification,
    *,
    claim_scope: ExecutionClaimScope,
    profile: ExecutionTargetProfile | None = None,
    resolved_bindings: tuple[ResolvedExecutionBinding, ...] = (),
) -> BoundExecutionCase:
    profile_fields = _bound_profile_fields(profile, intent)
    return BoundExecutionCase(
        case_id=f"{intent.scenario_id}:{profile_fields['profile_suffix']}",
        intent=intent,
        execution_classification=classification,
        selected_profile_id=profile_fields["selected_profile_id"],
        selected_profile_basis=profile_fields["selected_profile_basis"],
        target_environment_id=profile_fields["target_environment_id"],
        target_profile_digest=profile_fields["target_profile_digest"],
        target_realization_digest=profile_fields["target_realization_digest"],
        inventory_authority=profile_fields["inventory_authority"],
        semantic_authority=profile_fields["semantic_authority"],
        resolved_bindings=resolved_bindings,
        selected_simulation_resources=_selected_simulation_resources(profile, resolved_bindings),
        binding_completeness=BindingCompleteness.concrete,
        environment_basis=_bound_environment_basis(profile),
        profile_fit=_bound_profile_fit(profile),
        claim_scope=claim_scope,
    )


def _bound_profile_fields(
    profile: ExecutionTargetProfile | None,
    intent: ExecutionIntent,
) -> dict[str, object]:
    target_realization_digest = (
        intent.trace_refs.source_pins.get("target_realization") if profile is not None else None
    )
    if profile is None:
        return {
            "profile_suffix": "target-agnostic",
            "selected_profile_id": None,
            "selected_profile_basis": None,
            "target_environment_id": None,
            "target_profile_digest": None,
            "target_realization_digest": None,
            "inventory_authority": None,
            "semantic_authority": None,
        }
    return {
        "profile_suffix": profile.profile_id,
        "selected_profile_id": profile.profile_id,
        "selected_profile_basis": profile.basis.value,
        "target_environment_id": profile.environment_id,
        "target_profile_digest": profile.semantic_digest,
        "target_realization_digest": target_realization_digest,
        "inventory_authority": profile.inventory_authority,
        "semantic_authority": profile.semantic_authority,
    }


def _selected_simulation_resources(
    profile: ExecutionTargetProfile | None,
    bindings: tuple[ResolvedExecutionBinding, ...],
) -> tuple[SelectedSimulationResource, ...]:
    """Copy the exact selected simulation entries into the bound case."""

    if profile is None or profile.basis is not ProfileBasis.simulation:
        return ()
    resources = {item.resource_id: item for item in profile.resources}
    selected: list[SelectedSimulationResource] = []
    for resource_id in sorted({item.resource_id for item in bindings}):
        resource = resources[resource_id]
        if resource.simulation_behavior is None:
            raise ValueError(f"simulation resource {resource_id!r} has no simulation_behavior")
        selected.append(
            SelectedSimulationResource(
                resource_id=resource_id,
                simulation_behavior=resource.simulation_behavior,
            )
        )
    return tuple(selected)


def _bound_environment_basis(profile: ExecutionTargetProfile | None) -> EnvironmentBasis:
    if profile is None:
        return EnvironmentBasis.target_agnostic
    if profile.basis is ProfileBasis.target:
        return EnvironmentBasis.target_profile
    return EnvironmentBasis.simulation_profile


def _bound_profile_fit(profile: ExecutionTargetProfile | None) -> ExecutionProfileFit:
    return ExecutionProfileFit.matched if profile is not None else ExecutionProfileFit.not_required


def _exclusion(
    intent: ExecutionIntent,
    classification: ExecutionClassification,
    code: str,
    requirement_ids: tuple[str, ...],
    *diagnostics: ExecutionCaseDiagnostic,
) -> ExecutionCaseExclusion:
    return ExecutionCaseExclusion(
        intent=intent,
        execution_classification=classification,
        code=code,
        requirement_ids=requirement_ids,
        diagnostics=diagnostics,
    )


def _diagnostic(
    code: str,
    detail: str,
    requirement_id: str | None = None,
    candidates: tuple[str, ...] = (),
) -> ExecutionCaseDiagnostic:
    return ExecutionCaseDiagnostic(
        code=code,
        detail=detail,
        requirement_id=requirement_id,
        candidate_resource_ids=candidates,
    )


def _contract_gap_diagnostics(
    contract: SemanticExecutionContract,
) -> tuple[ExecutionCaseDiagnostic, ...]:
    return tuple(_diagnostic(gap.code.value, gap.detail) for gap in contract.gaps)


__all__ = ["resolve_execution_case"]
