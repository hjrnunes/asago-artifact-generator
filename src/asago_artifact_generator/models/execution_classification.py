"""Consumer mirror of the producer's semantic execution classification.

This module is platform-neutral.  It contains semantic resource roles and
classification evidence, not endpoints, credentials, runtime locators, or
platform-specific tool choices.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from ._base import (
    FrozenDict,
    FrozenList,
    ImmutableModel,
    SHA256Digest,
    compute_framed_digest,
)

EXECUTION_CONTRACT_SCHEMA_VERSION = "stpa-execution-contract-v1"
EXECUTION_CLASSIFICATION_SCHEMA_VERSION = "stpa-execution-classification-v1"
EXECUTION_TARGET_PROFILE_SCHEMA_VERSION = "execution-target-profile-v1"
EXECUTION_CONTRACT_DIGEST_FRAME = EXECUTION_CONTRACT_SCHEMA_VERSION
EXECUTION_CLASSIFICATION_DIGEST_FRAME = EXECUTION_CLASSIFICATION_SCHEMA_VERSION
EXECUTION_TARGET_PROFILE_DIGEST_FRAME = EXECUTION_TARGET_PROFILE_SCHEMA_VERSION


class ExecutionDeliveryClass(StrEnum):
    """The producer-selected way an adversarial stimulus enters."""

    direct_prompt = "direct_prompt"
    indirect_content = "indirect_content"
    conversation_context = "conversation_context"


class ExecutionActionKind(StrEnum):
    """The semantic action whose unsafe outcome is observed."""

    model_output = "model_output"
    tool_call = "tool_call"
    state_change = "state_change"
    agent_message = "agent_message"
    environment_action = "environment_action"


class ExecutionContractDisposition(StrEnum):
    """Whether the producer supplied a route or an analytical gap."""

    executable_route = "executable_route"
    analytical_only = "analytical_only"


class ExecutionResourcePurpose(StrEnum):
    """Why a semantic execution resource is needed."""

    stimulus_carrier = "stimulus_carrier"
    target_action = "target_action"
    state_resource = "state_resource"
    agent_channel = "agent_channel"


class ExecutionResourceKind(StrEnum):
    """Platform-neutral kind of semantic resource."""

    surface = "surface"
    tool = "tool"
    integration = "integration"
    state_store = "state_store"
    agent_channel = "agent_channel"


class RequestedEnvironmentBasis(StrEnum):
    """Environment basis explicitly selected by the producer caller."""

    target_profile = "target_profile"
    simulation_profile = "simulation_profile"
    target_agnostic = "target_agnostic"


class BindingCompleteness(StrEnum):
    """Whether semantic resource roles are completely resolved."""

    concrete = "concrete"
    parameterized = "parameterized"
    analytical_only = "analytical_only"


class EnvironmentBasis(StrEnum):
    """The evidence basis available for executing a semantic contract."""

    target_agnostic = "target_agnostic"
    target_profile = "target_profile"
    simulation_profile = "simulation_profile"
    none = "none"


class ExecutionProfileFit(StrEnum):
    """Deterministic fit of a contract against a selected profile."""

    not_required = "not_required"
    matched = "matched"
    needs_binding = "needs_binding"
    ambiguous = "ambiguous"
    unsupported = "unsupported"
    invalid = "invalid"


class ExecutionClaimScope(StrEnum):
    """The strongest claim a classification permits."""

    model_behavior_only = "model_behavior_only"
    target_specific_intent = "target_specific_intent"
    agent_behavior_with_simulated_tools = "agent_behavior_with_simulated_tools"
    no_execution_claim = "no_execution_claim"


class ExecutionSurface(StrEnum):
    """Closed semantic surface exposed by a profile resource."""

    user_input = "user_input"
    external_content = "external_content"
    conversation_context = "conversation_context"
    tool_call = "tool_call"
    tool_result = "tool_result"
    tool_definition = "tool_definition"
    state_observation = "state_observation"
    agent_message = "agent_message"
    environment_event = "environment_event"


class ProfileBasis(StrEnum):
    """Whether a profile describes a real target or an explicit simulation."""

    target = "target"
    simulation = "simulation"


class ProfileAuthority(StrEnum):
    """How profile resource facts were established."""

    reviewed = "reviewed"
    inferred = "inferred"


class InventoryCompleteness(StrEnum):
    """Whether a role search can be treated as unique."""

    unknown = "unknown"
    inferred_partial = "inferred_partial"
    reviewed_complete = "reviewed_complete"


class AttackerInfluence(StrEnum):
    """Profile fact describing whether an attacker can influence a resource."""

    none = "none"
    direct = "direct"
    indirect = "indirect"
    unknown = "unknown"


class ExecutionDiagnosticCode(StrEnum):
    """Closed deterministic diagnostic vocabulary."""

    target_profile_not_supplied = "target_profile_not_supplied"
    target_resource_unresolved = "target_resource_unresolved"
    target_resource_ambiguous = "target_resource_ambiguous"
    operation_unsupported = "operation_unsupported"
    profile_inventory_unknown = "profile_inventory_unknown"
    profile_inferred_only = "profile_inferred_only"
    simulation_contract_missing = "simulation_contract_missing"
    oracle_missing = "oracle_missing"
    execution_route_missing = "execution_route_missing"
    explicit_target_ref_dangling = "explicit_target_ref_dangling"
    target_profile_digest_mismatch = "target_profile_digest_mismatch"


class ExecutionSemanticGapCode(StrEnum):
    """Typed explanation for a valid analytical-only contract."""

    delivery_path_missing = "delivery_path_missing"
    operation_missing = "operation_missing"
    resource_role_missing = "resource_role_missing"
    observable_oracle_missing = "observable_oracle_missing"


class _DigestModel(ImmutableModel):
    """Immutable closed model whose semantic digest is derived on construction."""

    semantic_digest: SHA256Digest | None = None

    @property
    def _digest_frame(self) -> str:
        raise NotImplementedError

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"semantic_digest"})

    def compute_semantic_digest(self) -> str:
        return compute_framed_digest(self._digest_frame, self.semantic_payload())

    def assert_integrity(self) -> None:
        if self.semantic_digest != self.compute_semantic_digest():
            raise ValueError("semantic_digest does not match model content")


class _ClassificationDigestModel(ImmutableModel):
    """Closed classification model with its distinct digest field."""

    classification_digest: SHA256Digest | None = None

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"classification_digest"})

    def compute_classification_digest(self) -> str:
        return compute_framed_digest(
            EXECUTION_CLASSIFICATION_DIGEST_FRAME, self.semantic_payload()
        )

    def assert_integrity(self) -> None:
        if self.classification_digest != self.compute_classification_digest():
            raise ValueError("classification_digest does not match classification content")


class SemanticExecutionDelivery(ImmutableModel):
    """One selected delivery route and its request-local factor handle."""

    delivery_class: ExecutionDeliveryClass
    factor_id: StrictStr = Field(min_length=1, pattern=r"^CF-\d+$")
    source_role: StrictStr = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    )
    carrier_requirement_id: StrictStr | None = Field(
        default=None, pattern=r"^REQ-[A-Za-z0-9._-]+$"
    )


class SemanticExecutionGap(ImmutableModel):
    """Evidence-backed semantic gap that prevents executable routing."""

    code: ExecutionSemanticGapCode
    detail: StrictStr = Field(min_length=1)
    evidence_refs: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _evidence_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("execution contract gap evidence_refs must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _unique_evidence(self) -> SemanticExecutionGap:
        values = tuple(sorted(self.evidence_refs))
        _ensure_unique_nonempty(values, "evidence_refs")
        object.__setattr__(self, "evidence_refs", values)
        return self


# The first consumer tests used these temporary names.  Keep aliases only;
# there is one wire/model shape and one digest recipe.
ExecutionContractGap = SemanticExecutionGap
ExecutionContractGapCode = ExecutionSemanticGapCode


class ExecutionResourceRequirement(ImmutableModel):
    """One semantic resource role required by the selected execution path."""

    requirement_id: StrictStr = Field(pattern=r"^REQ-[A-Za-z0-9._-]+$")
    purpose: ExecutionResourcePurpose
    factor_id: StrictStr | None = Field(default=None, pattern=r"^CF-\d+$")
    owner_ref: StrictStr = Field(min_length=1)
    acceptable_resource_kinds: tuple[ExecutionResourceKind, ...] = Field(min_length=1)
    role_id: StrictStr = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    )
    operation: StrictStr = Field(
        min_length=1,
        pattern=r"^(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|(?:CA|CM)-[A-Za-z0-9._-]+)$",
    )
    required_surfaces: tuple[ExecutionSurface, ...] = Field(min_length=1)
    required_properties: tuple[StrictStr, ...] = ()
    required_attacker_influence: AttackerInfluence | None = None
    exact_resource_id: StrictStr | None = Field(default=None, min_length=1)
    late_bindable: StrictBool
    evidence_refs: tuple[StrictStr, ...] = ()

    @field_validator(
        "acceptable_resource_kinds",
        "required_surfaces",
        "required_properties",
        "evidence_refs",
        mode="before",
    )
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("resource requirement collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_requirement(self) -> ExecutionResourceRequirement:
        acceptable_kinds = tuple(
            sorted(self.acceptable_resource_kinds, key=lambda item: item.value)
        )
        required_surfaces = tuple(sorted(self.required_surfaces, key=lambda item: item.value))
        required_properties = tuple(sorted(self.required_properties))
        evidence_refs = tuple(sorted(self.evidence_refs))
        _ensure_unique_nonempty(acceptable_kinds, "acceptable_resource_kinds")
        _ensure_unique_nonempty(required_surfaces, "required_surfaces")
        _ensure_unique_nonempty(required_properties, "required_properties")
        _ensure_lower_snake(required_properties, "required_properties")
        _ensure_unique_nonempty(evidence_refs, "evidence_refs")
        object.__setattr__(self, "acceptable_resource_kinds", acceptable_kinds)
        object.__setattr__(self, "required_surfaces", required_surfaces)
        object.__setattr__(self, "required_properties", required_properties)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        if self.exact_resource_id is not None and self.late_bindable:
            raise ValueError("exact resource requirements cannot be late_bindable")
        if self.exact_resource_id is None and not self.late_bindable:
            raise ValueError("unresolved resource requirements must be late_bindable")
        return self


class SemanticExecutionContract(_DigestModel):
    """Closed scenario-owned semantic execution intent."""

    schema_version: Literal["stpa-execution-contract-v1"] = EXECUTION_CONTRACT_SCHEMA_VERSION
    disposition: ExecutionContractDisposition = ExecutionContractDisposition.executable_route
    requested_environment_basis: RequestedEnvironmentBasis | None = None
    delivery: SemanticExecutionDelivery | None = None
    action_kind: ExecutionActionKind | None = None
    resource_requirements: tuple[ExecutionResourceRequirement, ...] = ()
    gaps: tuple[SemanticExecutionGap, ...] = ()

    @property
    def _digest_frame(self) -> str:
        return EXECUTION_CONTRACT_DIGEST_FRAME

    @field_validator("resource_requirements", "gaps", mode="before")
    @classmethod
    def _requirements_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("resource_requirements must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_and_validate(self) -> SemanticExecutionContract:
        requirements, gaps = _canonical_contract_collections(self)
        object.__setattr__(self, "resource_requirements", requirements)
        object.__setattr__(self, "gaps", gaps)
        if self.disposition == "analytical_only":
            _validate_analytical_contract(self)
        else:
            _validate_executable_contract(self, requirements)
        _attest_contract(self)
        return self


def _canonical_contract_collections(
    value: SemanticExecutionContract,
) -> tuple[tuple[ExecutionResourceRequirement, ...], tuple[SemanticExecutionGap, ...]]:
    requirements = tuple(sorted(value.resource_requirements, key=lambda item: item.requirement_id))
    gaps = tuple(sorted(value.gaps, key=lambda item: (item.code.value, item.detail)))
    _ensure_unique_ids(requirements, "requirement_id", "resource requirements")
    return requirements, gaps


def _validate_analytical_contract(value: SemanticExecutionContract) -> None:
    if not value.gaps:
        raise ValueError("analytical_only contracts require at least one gap")
    if value.delivery is not None or value.action_kind is not None:
        raise ValueError("analytical_only contracts cannot carry executable route fields")
    if value.resource_requirements:
        raise ValueError("analytical_only contracts cannot carry resource requirements")
    if value.requested_environment_basis is not None:
        raise ValueError("analytical_only contracts cannot claim an environment basis")


def _validate_executable_contract(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    _validate_executable_header(value, requirements)
    _validate_delivery_requirements(value, requirements)
    _validate_action_requirements(value, requirements)
    _validate_agent_requirements(value, requirements)


def _validate_executable_header(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    _require_executable_fields(value)
    if value.requested_environment_basis is None:
        _set_default_environment_basis(value, requirements)
    _reject_target_agnostic_requirements(value, requirements)


def _require_executable_fields(value: SemanticExecutionContract) -> None:
    if value.delivery is None:
        raise ValueError("executable_route contracts require delivery")
    if value.action_kind is None:
        raise ValueError("executable_route contracts require action_kind")
    if value.gaps:
        raise ValueError("executable_route contracts cannot carry analytical gaps")


def _set_default_environment_basis(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    if requirements:
        raise ValueError(
            "resource-bearing executable routes require an explicit environment basis"
        )
    object.__setattr__(
        value,
        "requested_environment_basis",
        RequestedEnvironmentBasis.target_agnostic,
    )


def _reject_target_agnostic_requirements(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    if value.requested_environment_basis is RequestedEnvironmentBasis.target_agnostic:
        if requirements:
            raise ValueError("target_agnostic contracts cannot require domain resources")


def _validate_delivery_requirements(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    delivery = value.delivery
    if delivery is None:
        return
    carrier = delivery.carrier_requirement_id
    by_id = {item.requirement_id: item for item in requirements}
    if delivery.delivery_class is ExecutionDeliveryClass.indirect_content:
        _validate_indirect_carrier(carrier, by_id)
    elif carrier is not None:
        raise ValueError("only indirect_content delivery may name a carrier requirement")


def _validate_indirect_carrier(
    carrier: str | None,
    requirements: dict[str, ExecutionResourceRequirement],
) -> None:
    if carrier is None:
        raise ValueError("indirect_content delivery requires a carrier requirement")
    if carrier not in requirements:
        raise ValueError("delivery carrier requirement does not resolve")
    requirement = requirements[carrier]
    if requirement.purpose is not ExecutionResourcePurpose.stimulus_carrier:
        raise ValueError("delivery carrier must have stimulus_carrier purpose")
    if requirement.required_attacker_influence not in {
        AttackerInfluence.direct,
        AttackerInfluence.indirect,
    }:
        raise ValueError("indirect_content carrier requires direct or indirect attacker influence")


def _validate_action_requirements(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    action_requirements = tuple(
        item for item in requirements if item.purpose is ExecutionResourcePurpose.target_action
    )
    if _requires_target_action(value.action_kind):
        _require_one_target_action(action_requirements)
        return
    _reject_unused_target_actions(action_requirements)


def _require_one_target_action(
    action_requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    if len(action_requirements) != 1:
        raise ValueError("external action routes require one target_action requirement")


def _reject_unused_target_actions(
    action_requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    if action_requirements:
        raise ValueError("target_action requirements are unused by this action kind")


def _requires_target_action(action_kind: ExecutionActionKind | None) -> bool:
    return action_kind in {
        ExecutionActionKind.tool_call,
        ExecutionActionKind.state_change,
        ExecutionActionKind.environment_action,
    }


def _validate_agent_requirements(
    value: SemanticExecutionContract,
    requirements: tuple[ExecutionResourceRequirement, ...],
) -> None:
    agent_requirements = tuple(
        item for item in requirements if item.purpose is ExecutionResourcePurpose.agent_channel
    )
    if value.action_kind is ExecutionActionKind.agent_message:
        if len(agent_requirements) != 1:
            raise ValueError("agent_message routes require one agent_channel requirement")
    elif agent_requirements:
        raise ValueError("agent_channel requirements are unused by this action kind")


def _attest_contract(value: SemanticExecutionContract) -> None:
    expected = value.compute_semantic_digest()
    if value.semantic_digest is not None and value.semantic_digest != expected:
        raise ValueError("semantic_digest does not match contract content")
    object.__setattr__(value, "semantic_digest", expected)


class TargetProfileOperation(ImmutableModel):
    """One reviewed operation exposed by an execution target resource."""

    operation_id: StrictStr = Field(min_length=1)
    semantic_operation: StrictStr = Field(
        min_length=1,
        pattern=r"^(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|(?:CA|CM)-[A-Za-z0-9._-]+)$",
    )
    argument_names: tuple[StrictStr, ...] = ()
    observable_properties: tuple[StrictStr, ...] = ()

    @field_validator("argument_names", "observable_properties", mode="before")
    @classmethod
    def _names_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("operation name collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_operation(self) -> TargetProfileOperation:
        _ensure_unique_nonempty(self.argument_names, "argument_names")
        _ensure_unique_nonempty(self.observable_properties, "observable_properties")
        _ensure_lower_snake(self.argument_names, "argument_names")
        _ensure_lower_snake(self.observable_properties, "observable_properties")
        object.__setattr__(self, "argument_names", tuple(sorted(self.argument_names)))
        object.__setattr__(
            self,
            "observable_properties",
            tuple(sorted(self.observable_properties)),
        )
        return self


class SimulationBehavior(ImmutableModel):
    """Closed deterministic behavior for one explicitly simulated resource."""

    inputs: Mapping[str, Any] = Field(default_factory=dict)
    outputs: Mapping[str, Any] = Field(default_factory=dict)
    emitted_events: tuple[StrictStr, ...] = ()
    state_changes: Mapping[str, Any] = Field(default_factory=dict)
    observation_points: tuple[StrictStr, ...] = ()

    @field_validator("inputs", "outputs", "state_changes", mode="before")
    @classmethod
    def _simulation_mapping(cls, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("simulation behavior maps must be objects")
        return _freeze_json(value)

    @field_validator("emitted_events", "observation_points", mode="before")
    @classmethod
    def _simulation_collections(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("simulation behavior collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_behavior(self) -> SimulationBehavior:
        if not self.inputs or not self.outputs:
            raise ValueError("simulation behavior requires inputs and outputs")
        if not self.observation_points:
            raise ValueError("simulation behavior requires observation_points")
        _ensure_unique_nonempty(self.emitted_events, "emitted_events")
        _ensure_unique_nonempty(self.observation_points, "observation_points")
        object.__setattr__(self, "emitted_events", tuple(sorted(self.emitted_events)))
        object.__setattr__(
            self,
            "observation_points",
            tuple(sorted(self.observation_points)),
        )
        return self


class TargetProfileResource(ImmutableModel):
    """One semantic resource record in a target or simulation profile."""

    resource_id: StrictStr = Field(min_length=1)
    resource_kind: ExecutionResourceKind
    role_ids: tuple[StrictStr, ...] = ()
    structural_refs: tuple[StrictStr, ...] = ()
    attacker_influence: AttackerInfluence
    surfaces: tuple[ExecutionSurface, ...] = ()
    operations: tuple[TargetProfileOperation, ...] = ()
    interface_schema: Mapping[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[StrictStr, ...] = ()
    authority: ProfileAuthority = ProfileAuthority.reviewed
    simulation_behavior: SimulationBehavior | None = None

    @field_validator("role_ids", "structural_refs", "surfaces", "evidence_refs", mode="before")
    @classmethod
    def _collections_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("resource collections must be arrays")
        return tuple(value)

    @field_validator("operations", mode="before")
    @classmethod
    def _operations_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("resource operations must be an array")
        return tuple(value)

    @field_validator("interface_schema", mode="before")
    @classmethod
    def _mapping_value(cls, value: Any) -> Mapping[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("resource schema and simulation behavior must be objects")
        return _freeze_json(value)

    @model_validator(mode="after")
    def _canonicalize_and_validate(self) -> TargetProfileResource:
        _validate_resource_collections(self)
        _canonicalize_resource_operations(self)
        _validate_resource_authority(self)
        _sort_resource_collections(self)
        return self


def _validate_resource_collections(value: TargetProfileResource) -> None:
    for field_name in ("role_ids", "structural_refs", "surfaces", "evidence_refs"):
        _ensure_unique_nonempty(getattr(value, field_name), field_name)
    _ensure_lower_snake(value.role_ids, "role_ids")


def _canonicalize_resource_operations(value: TargetProfileResource) -> None:
    operations = tuple(sorted(value.operations, key=lambda item: item.operation_id))
    _ensure_unique_ids(operations, "operation_id", "resource operations")
    semantic_operations = tuple(item.semantic_operation for item in operations)
    _ensure_unique_nonempty(semantic_operations, "semantic_operation")
    object.__setattr__(value, "operations", operations)


def _validate_resource_authority(value: TargetProfileResource) -> None:
    if value.authority is ProfileAuthority.reviewed and not value.evidence_refs:
        raise ValueError("reviewed resources require evidence_refs")


def _sort_resource_collections(value: TargetProfileResource) -> None:
    for field_name in ("role_ids", "structural_refs", "evidence_refs"):
        object.__setattr__(value, field_name, tuple(sorted(getattr(value, field_name))))
    object.__setattr__(
        value,
        "surfaces",
        tuple(sorted(value.surfaces, key=lambda item: item.value)),
    )


class ExecutionTargetProfile(_DigestModel):
    """Reviewed, content-addressed semantic target or simulation inventory."""

    schema_version: Literal["execution-target-profile-v1"] = (
        EXECUTION_TARGET_PROFILE_SCHEMA_VERSION
    )
    profile_id: StrictStr = Field(min_length=1)
    environment_id: StrictStr = Field(min_length=1)
    basis: ProfileBasis
    authority: ProfileAuthority
    inventory_completeness: InventoryCompleteness
    evidence_refs: tuple[StrictStr, ...] = ()
    resources: tuple[TargetProfileResource, ...] = ()

    @property
    def _digest_frame(self) -> str:
        return EXECUTION_TARGET_PROFILE_DIGEST_FRAME

    @field_validator("evidence_refs", "resources", mode="before")
    @classmethod
    def _collections_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("profile collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_and_validate(self) -> ExecutionTargetProfile:
        _canonicalize_profile_collections(self)
        _validate_profile_basis(self)
        _validate_profile_authority(self)
        _attest_profile(self)
        return self


def _canonicalize_profile_collections(value: ExecutionTargetProfile) -> None:
    evidence_refs = tuple(sorted(value.evidence_refs))
    _ensure_unique_nonempty(evidence_refs, "evidence_refs")
    object.__setattr__(value, "evidence_refs", evidence_refs)
    resources = tuple(sorted(value.resources, key=lambda item: item.resource_id))
    _ensure_unique_ids(resources, "resource_id", "profile resources")
    object.__setattr__(value, "resources", resources)


def _validate_profile_basis(value: ExecutionTargetProfile) -> None:
    if value.basis is ProfileBasis.simulation:
        _require_simulation_behaviors(value)
        return
    _reject_target_simulation_behaviors(value)


def _require_simulation_behaviors(value: ExecutionTargetProfile) -> None:
    missing = [item.resource_id for item in value.resources if item.simulation_behavior is None]
    if missing:
        raise ValueError("simulation profiles require simulation_behavior for every resource")


def _reject_target_simulation_behaviors(value: ExecutionTargetProfile) -> None:
    if any(item.simulation_behavior is not None for item in value.resources):
        raise ValueError("target profiles cannot contain simulation_behavior")


def _validate_profile_authority(value: ExecutionTargetProfile) -> None:
    if value.authority is ProfileAuthority.reviewed and not value.evidence_refs:
        raise ValueError("reviewed profiles require evidence_refs")


def _attest_profile(value: ExecutionTargetProfile) -> None:
    expected = value.compute_semantic_digest()
    if value.semantic_digest is not None and value.semantic_digest != expected:
        raise ValueError("semantic_digest does not match profile content")
    object.__setattr__(value, "semantic_digest", expected)


class ResolvedExecutionBinding(ImmutableModel):
    """One exact requirement/resource/operation match."""

    requirement_id: StrictStr = Field(pattern=r"^REQ-[A-Za-z0-9._-]+$")
    resource_id: StrictStr = Field(min_length=1)
    operation_id: StrictStr = Field(min_length=1)


class AmbiguousExecutionMatch(ImmutableModel):
    """All exact candidates retained when a role is not uniquely resolved."""

    requirement_id: StrictStr = Field(pattern=r"^REQ-[A-Za-z0-9._-]+$")
    candidate_resource_ids: tuple[StrictStr, ...] = Field(min_length=2)

    @field_validator("candidate_resource_ids", mode="before")
    @classmethod
    def _candidate_ids_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("candidate_resource_ids must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_candidates(self) -> AmbiguousExecutionMatch:
        values = tuple(sorted(self.candidate_resource_ids))
        _ensure_unique_nonempty(values, "candidate_resource_ids")
        object.__setattr__(self, "candidate_resource_ids", values)
        return self


class ExecutionClassificationDiagnostic(ImmutableModel):
    """One deterministic reason a semantic contract is not fully bound."""

    code: ExecutionDiagnosticCode
    detail: StrictStr = Field(min_length=1)
    requirement_id: StrictStr | None = Field(default=None, pattern=r"^REQ-[A-Za-z0-9._-]+$")
    candidate_resource_ids: tuple[StrictStr, ...] = ()

    @field_validator("candidate_resource_ids", mode="before")
    @classmethod
    def _candidate_ids_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("diagnostic candidate_resource_ids must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_candidates(self) -> ExecutionClassificationDiagnostic:
        values = tuple(sorted(self.candidate_resource_ids))
        _ensure_unique_nonempty(values, "candidate_resource_ids")
        object.__setattr__(self, "candidate_resource_ids", values)
        return self


class ExecutionClassification(_ClassificationDigestModel):
    """Deterministic classification and evidence for one execution contract."""

    schema_version: Literal["stpa-execution-classification-v1"] = (
        EXECUTION_CLASSIFICATION_SCHEMA_VERSION
    )
    binding_completeness: BindingCompleteness
    environment_basis: EnvironmentBasis
    profile_fit: ExecutionProfileFit
    claim_scope: ExecutionClaimScope
    resolved_bindings: tuple[ResolvedExecutionBinding, ...] = ()
    unresolved_requirement_ids: tuple[StrictStr, ...] = ()
    ambiguous_matches: tuple[AmbiguousExecutionMatch, ...] = ()
    unsupported_requirement_ids: tuple[StrictStr, ...] = ()
    diagnostics: tuple[ExecutionClassificationDiagnostic, ...] = ()
    target_profile_digest: SHA256Digest | None = None

    @field_validator(
        "resolved_bindings",
        "unresolved_requirement_ids",
        "ambiguous_matches",
        "unsupported_requirement_ids",
        "diagnostics",
        mode="before",
    )
    @classmethod
    def _collections_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("classification collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_and_digest(self) -> ExecutionClassification:
        _ensure_unique_ids(self.resolved_bindings, "requirement_id", "resolved bindings")
        _ensure_unique_nonempty(self.unresolved_requirement_ids, "unresolved_requirement_ids")
        _ensure_unique_nonempty(self.unsupported_requirement_ids, "unsupported_requirement_ids")
        object.__setattr__(
            self,
            "unresolved_requirement_ids",
            tuple(sorted(self.unresolved_requirement_ids)),
        )
        object.__setattr__(
            self,
            "unsupported_requirement_ids",
            tuple(sorted(self.unsupported_requirement_ids)),
        )
        expected = self.compute_classification_digest()
        if self.classification_digest is not None and self.classification_digest != expected:
            raise ValueError("classification_digest does not match classification content")
        object.__setattr__(self, "classification_digest", expected)
        return self


def _ensure_unique_nonempty(values: tuple[str, ...], label: str) -> None:
    if any(not value for value in values):
        raise ValueError(f"{label} must contain non-empty values")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must contain unique values")


def _ensure_lower_snake(values: tuple[str, ...], label: str) -> None:
    import re

    pattern = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    if any(not pattern.fullmatch(value) for value in values):
        raise ValueError(f"{label} must use lower_snake_case values")


def _ensure_unique_ids(values: tuple[Any, ...], attribute: str, label: str) -> None:
    identities = [getattr(value, attribute) for value in values]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} must have unique {attribute} values")


def _freeze_json(value: Any) -> Any:
    """Close profile interface data to the producer's JSON value domain."""

    if _is_frozen_json_scalar(value):
        return _validate_frozen_json_scalar(value)
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return _freeze_json_sequence(value)
    raise TypeError("interface JSON must contain only JSON values")


def _is_frozen_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool, FrozenDict, FrozenList))


def _validate_frozen_json_scalar(value: Any) -> Any:
    if isinstance(value, float) and not value == value:
        raise ValueError("interface JSON cannot contain NaN")
    if isinstance(value, float) and value in {float("inf"), float("-inf")}:
        raise ValueError("interface JSON cannot contain infinity")
    return value


def _freeze_json_mapping(value: Mapping[str, Any]) -> FrozenDict:
    if any(not isinstance(key, str) for key in value):
        raise TypeError("interface JSON mapping keys must be strings")
    return FrozenDict({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json_sequence(value: list[Any] | tuple[Any, ...]) -> FrozenList:
    return FrozenList(_freeze_json(item) for item in value)


__all__ = [
    "AmbiguousExecutionMatch",
    "AttackerInfluence",
    "BindingCompleteness",
    "ExecutionContractDisposition",
    "ExecutionContractGap",
    "ExecutionContractGapCode",
    "ExecutionSemanticGapCode",
    "EnvironmentBasis",
    "ExecutionActionKind",
    "ExecutionClaimScope",
    "ExecutionClassification",
    "ExecutionClassificationDiagnostic",
    "ExecutionDeliveryClass",
    "ExecutionDiagnosticCode",
    "ExecutionProfileFit",
    "ExecutionResourceKind",
    "ExecutionResourcePurpose",
    "ExecutionResourceRequirement",
    "ExecutionSurface",
    "ExecutionTargetProfile",
    "InventoryCompleteness",
    "ProfileAuthority",
    "ProfileBasis",
    "RequestedEnvironmentBasis",
    "ResolvedExecutionBinding",
    "SemanticExecutionContract",
    "SemanticExecutionDelivery",
    "SemanticExecutionGap",
    "SimulationBehavior",
    "TargetProfileOperation",
    "TargetProfileResource",
    "EXECUTION_CLASSIFICATION_SCHEMA_VERSION",
    "EXECUTION_CONTRACT_SCHEMA_VERSION",
    "EXECUTION_TARGET_PROFILE_SCHEMA_VERSION",
]
