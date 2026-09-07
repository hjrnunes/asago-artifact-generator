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
MCP_INVENTORY_SCHEMA_VERSION = "mcp-inventory-v1"
MCP_INVENTORY_DIGEST_FRAME = MCP_INVENTORY_SCHEMA_VERSION


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
    """Retired overloaded authority spelling kept for import compatibility."""

    reviewed = "reviewed"
    inferred = "inferred"


class InventoryCompleteness(StrEnum):
    """Completeness of an observed inventory."""

    unknown = "unknown"
    observed_partial = "observed_partial"
    observed_complete = "observed_complete"
    # Python-level compatibility aliases.  They serialize to the revised
    # authority-neutral values and are never accepted as wire values by the
    # canonical contract fixtures.
    inferred_partial = "observed_partial"
    reviewed_complete = "observed_complete"


class InventoryAuthority(StrEnum):
    """Authority of protocol inventory facts."""

    observed = "observed"


class SemanticAuthority(StrEnum):
    """Authority of semantic interpretations attached to a profile."""

    inferred = "inferred"
    reviewed = "reviewed"


class SourceProtocol(StrEnum):
    """Source family for a target profile."""

    mcp = "mcp"
    simulation = "simulation"


class TargetInterpretationDisposition(StrEnum):
    """Closed interpretation outcome for one observed MCP tool."""

    supported = "supported"
    ambiguous = "ambiguous"
    contradictory = "contradictory"
    unresolved = "unresolved"


class TargetOperationEffect(StrEnum):
    """Bounded likely operation effect vocabulary."""

    read = "read"
    create = "create"
    update = "update"
    delete = "delete"
    execute = "execute"
    notify = "notify"
    escalate = "escalate"
    observe = "observe"
    unknown = "unknown"


class TargetStateEffect(StrEnum):
    """Bounded likely state-effect vocabulary."""

    none = "none"
    may_change = "may_change"
    changes = "changes"
    unknown = "unknown"


class InterpreterVerifierAgreement(StrEnum):
    """Agreement state between interpretation and independent verification."""

    agree = "agree"
    disagree = "disagree"
    unverified = "unverified"


class DiscoveryMode(StrEnum):
    """Whether discovery only observes MCP schemas or may call tools."""

    schema_only = "schema_only"
    disposable_test_environment = "disposable_test_environment"


class TargetDiscoveryDiagnosticCode(StrEnum):
    """Closed scanner diagnostic vocabulary."""

    inventory_protocol_failure = "inventory_protocol_failure"
    unsupported_protocol = "unsupported_protocol"
    duplicate_tool_name = "duplicate_tool_name"
    malformed_tool = "malformed_tool"
    malformed_schema = "malformed_schema"
    incomplete_pagination = "incomplete_pagination"
    interpretation_missing = "interpretation_missing"
    interpretation_invalid = "interpretation_invalid"
    interpretation_unknown_reference = "interpretation_unknown_reference"
    interpretation_contradictory = "interpretation_contradictory"
    verifier_disagreement = "verifier_disagreement"
    interpreter_failure = "interpreter_failure"
    active_inspection_disabled = "active_inspection_disabled"
    active_inspection_failure = "active_inspection_failure"


class TargetDiscoverySeverity(StrEnum):
    """Severity of a retained scanner diagnostic."""

    warning = "warning"
    error = "error"


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
    environment_profile_not_supplied = "environment_profile_not_supplied"
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
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
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
        # An omitted basis is an intentional unresolved environment choice.
        # The caller may select a target or simulation profile later at the
        # consumer boundary; it must not be rewritten as a real-target request.
        return
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


class DiscoveryProvenance(ImmutableModel):
    """Non-secret identities and prompt pins for one discovery run."""

    scanner_id: StrictStr = Field(min_length=1)
    interpreter_id: StrictStr = Field(min_length=1)
    verifier_id: StrictStr = Field(min_length=1)
    scanner_contract_version: StrictStr = Field(
        default=EXECUTION_TARGET_PROFILE_SCHEMA_VERSION, min_length=1
    )
    interpreter_prompt_hash: SHA256Digest | None = None
    verifier_prompt_hash: SHA256Digest | None = None
    model_profile: StrictStr | None = Field(default=None, min_length=1)
    model_name: StrictStr | None = Field(default=None, min_length=1)


class McpToolObservation(ImmutableModel):
    """Exact, semantic-neutral fields copied from one MCP ``tools/list`` row."""

    name: StrictStr = Field(min_length=1)
    source_observation_sha256: SHA256Digest
    title: StrictStr | None = Field(default=None, min_length=1)
    description: StrictStr | None = None
    input_schema: Mapping[str, Any]
    output_schema: Any = None
    annotations: Mapping[str, Any] | None = None
    argument_names: tuple[StrictStr, ...] = ()

    @field_validator("input_schema", "output_schema", "annotations", mode="before")
    @classmethod
    def _freeze_json_fields(cls, value: Any) -> Any:
        return _freeze_json(value)

    @model_validator(mode="after")
    def _validate_observation(self) -> McpToolObservation:
        _validate_json_schema(self.input_schema, "input_schema")
        if self.output_schema is not None and isinstance(self.output_schema, Mapping):
            _validate_json_schema(self.output_schema, "output_schema")
        properties = self.input_schema.get("properties", {})
        if properties is None:
            properties = {}
        if not isinstance(properties, Mapping):
            raise ValueError("input_schema.properties must be a mapping")
        names = tuple(sorted(str(name) for name in properties))
        _ensure_unique_nonempty(names, "argument_names")
        object.__setattr__(self, "argument_names", names)
        return self

    @property
    def tool_name(self) -> str:
        """Return the exact MCP tool identity."""

        return self.name


class McpInventoryObservation(_DigestModel):
    """Content-addressed normalized result of one MCP ``tools/list`` call."""

    schema_version: Literal[MCP_INVENTORY_SCHEMA_VERSION] = MCP_INVENTORY_SCHEMA_VERSION
    target_id: StrictStr = Field(min_length=1)
    authorization_scope_id: StrictStr = Field(min_length=1)
    source_protocol: Literal["mcp"] = "mcp"
    tools: tuple[McpToolObservation, ...] = ()
    pagination_complete: StrictBool = True
    page_count: int = Field(default=1, ge=1)

    @property
    def _digest_frame(self) -> str:
        return MCP_INVENTORY_DIGEST_FRAME

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("MCP inventory tools must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_and_attest(self) -> McpInventoryObservation:
        tools = tuple(sorted(self.tools, key=lambda item: item.name))
        names = tuple(item.name for item in tools)
        _ensure_unique_nonempty(names, "MCP tool names")
        object.__setattr__(self, "tools", tools)
        expected = self.compute_semantic_digest()
        if self.semantic_digest is not None and self.semantic_digest != expected:
            raise ValueError("semantic_digest does not match MCP inventory content")
        object.__setattr__(self, "semantic_digest", expected)
        return self

    @classmethod
    def from_json(cls, text: str | bytes) -> McpInventoryObservation:
        """Load and verify one normalized inventory document."""

        import json

        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid MCP inventory JSON: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ValueError("MCP inventory JSON must be an object")
        inventory = cls.model_validate(value)
        inventory.assert_integrity()
        return inventory


# Short public spellings used by scanner-facing callers.
McpInventory = McpInventoryObservation
McpInventoryTool = McpToolObservation


class TargetProfileOperation(ImmutableModel):
    """One exact operation exposed by a profile resource."""

    operation_id: StrictStr = Field(min_length=1)
    semantic_operation: StrictStr | None = Field(
        default=None,
        min_length=1,
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
        semantic_operation = self.semantic_operation or self.operation_id
        _ensure_unique_nonempty(self.argument_names, "argument_names")
        _ensure_unique_nonempty(self.observable_properties, "observable_properties")
        _ensure_lower_snake(self.observable_properties, "observable_properties")
        object.__setattr__(self, "argument_names", tuple(sorted(self.argument_names)))
        object.__setattr__(
            self,
            "observable_properties",
            tuple(sorted(self.observable_properties)),
        )
        object.__setattr__(self, "semantic_operation", semantic_operation)
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
    """One semantic resource in either an observed MCP or simulation profile.

    MCP resources populate the exact tool identity and schema fields.  The
    generic resource vocabulary remains available for explicit simulation
    profiles, which must provide deterministic ``simulation_behavior``.
    """

    resource_id: StrictStr = Field(min_length=1)
    resource_kind: ExecutionResourceKind = ExecutionResourceKind.tool
    target_id: StrictStr | None = Field(default=None, min_length=1)
    tool_name: StrictStr | None = Field(default=None, min_length=1)
    title: StrictStr | None = Field(default=None, min_length=1)
    description: StrictStr | None = None
    role_ids: tuple[StrictStr, ...] = ()
    structural_refs: tuple[StrictStr, ...] = ()
    attacker_influence: AttackerInfluence = AttackerInfluence.unknown
    surfaces: tuple[ExecutionSurface, ...] = ()
    operations: tuple[TargetProfileOperation, ...] = ()
    input_schema: Mapping[str, Any] = Field(default_factory=dict)
    output_schema: Any = None
    annotations: Mapping[str, Any] | None = None
    argument_names: tuple[StrictStr, ...] = ()
    evidence_refs: tuple[StrictStr, ...] = Field(min_length=1)
    simulation_behavior: SimulationBehavior | None = None

    @field_validator(
        "role_ids",
        "structural_refs",
        "surfaces",
        "evidence_refs",
        "argument_names",
        mode="before",
    )
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

    @field_validator("input_schema", "annotations", mode="before")
    @classmethod
    def _mapping_value(cls, value: Any) -> Any:
        if value is None and cls is TargetProfileResource:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("resource interface mappings must be objects")
        return _freeze_json(value)

    @field_validator("output_schema", mode="before")
    @classmethod
    def _output_schema_value(cls, value: Any) -> Any:
        if value is None:
            return None
        return _freeze_json(value)

    @model_validator(mode="after")
    def _canonicalize_and_validate(self) -> TargetProfileResource:
        _validate_resource_collections(self)
        _validate_resource_interfaces(self)
        _canonicalize_resource_operations(self)
        _sort_resource_collections(self)
        return self

    @property
    def interface_schema(self) -> Mapping[str, Any]:
        """Compatibility view for adapters that consume the tool input schema."""

        return self.input_schema


def _validate_resource_collections(value: TargetProfileResource) -> None:
    for field_name in ("role_ids", "structural_refs", "surfaces", "evidence_refs"):
        _ensure_unique_nonempty(getattr(value, field_name), field_name)


def _resource_argument_names(value: TargetProfileResource) -> tuple[str, ...]:
    properties = value.input_schema.get("properties", {})
    if properties is None:
        properties = {}
    if not isinstance(properties, Mapping):
        raise ValueError("input_schema.properties must be a mapping")
    derived_args = tuple(sorted(str(name) for name in properties))
    provided_args = tuple(sorted(value.argument_names))
    if provided_args and derived_args and provided_args != derived_args:
        raise ValueError("argument_names must match input_schema properties")
    effective_args = derived_args or provided_args
    _ensure_unique_nonempty(effective_args, "argument_names")
    return effective_args


def _validate_resource_schemas(value: TargetProfileResource) -> None:
    _validate_json_schema(value.input_schema, "input_schema")
    if value.output_schema is not None and isinstance(value.output_schema, Mapping):
        _validate_json_schema(value.output_schema, "output_schema")


def _validate_mcp_resource_interface(value: TargetProfileResource) -> None:
    if value.tool_name is None:
        return
    if value.resource_kind is not ExecutionResourceKind.tool:
        raise ValueError("tool_name is only valid for tool resources")
    if value.target_id is None:
        raise ValueError("MCP tool resources require target_id")
    if not {
        ExecutionSurface.tool_call,
        ExecutionSurface.tool_result,
    }.issubset(value.surfaces):
        raise ValueError("MCP resources require tool_call and tool_result surfaces")
    if len(value.operations) != 1:
        raise ValueError("MCP resources require exactly one operation")
    operation = value.operations[0]
    if operation.operation_id != value.tool_name:
        raise ValueError("MCP operation_id must equal tool_name")
    if operation.semantic_operation != value.tool_name:
        raise ValueError("MCP semantic_operation must equal the exact tool name")
    if operation.argument_names != value.argument_names:
        raise ValueError("operation argument_names must match input schema")


def _validate_resource_interfaces(value: TargetProfileResource) -> None:
    """Validate exact MCP interface facts when this is a tool resource."""

    _validate_resource_schemas(value)
    object.__setattr__(value, "argument_names", _resource_argument_names(value))
    _validate_mcp_resource_interface(value)


def _canonicalize_resource_operations(value: TargetProfileResource) -> None:
    operations = tuple(sorted(value.operations, key=lambda item: item.operation_id))
    _ensure_unique_ids(operations, "operation_id", "resource operations")
    semantic_operations = tuple(item.semantic_operation for item in operations)
    _ensure_unique_nonempty(semantic_operations, "semantic_operation")
    object.__setattr__(value, "operations", operations)


def _sort_resource_collections(value: TargetProfileResource) -> None:
    for field_name in ("role_ids", "structural_refs", "evidence_refs"):
        object.__setattr__(value, field_name, tuple(sorted(getattr(value, field_name))))
    object.__setattr__(
        value,
        "surfaces",
        tuple(sorted(value.surfaces, key=lambda item: item.value)),
    )


class TargetSemanticInterpretation(ImmutableModel):
    """Typed semantic interpretation of one observed MCP tool."""

    resource_id: StrictStr = Field(min_length=1)
    tool_name: StrictStr = Field(min_length=1)
    disposition: TargetInterpretationDisposition
    likely_effect: TargetOperationEffect = TargetOperationEffect.unknown
    likely_state_effect: TargetStateEffect = TargetStateEffect.unknown
    semantic_roles: tuple[StrictStr, ...] = ()
    observer_resource_ids: tuple[StrictStr, ...] = ()
    evidence_refs: tuple[StrictStr, ...] = Field(min_length=1)
    rationale: StrictStr = Field(min_length=1)
    interpreter_verifier_agreement: InterpreterVerifierAgreement = (
        InterpreterVerifierAgreement.unverified
    )

    @field_validator("semantic_roles", "observer_resource_ids", "evidence_refs", mode="before")
    @classmethod
    def _references_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("interpretation references must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize(self) -> TargetSemanticInterpretation:
        for field_name in (
            "semantic_roles",
            "observer_resource_ids",
            "evidence_refs",
        ):
            values = tuple(sorted(getattr(self, field_name)))
            _ensure_unique_nonempty(values, field_name)
            object.__setattr__(self, field_name, values)
        _ensure_lower_snake(self.semantic_roles, "semantic_roles")
        return self

    @property
    def agreement(self) -> InterpreterVerifierAgreement:
        """Compatibility spelling for the typed agreement field."""

        return self.interpreter_verifier_agreement


class TargetDiscoveryDiagnostic(ImmutableModel):
    """One retained scanner or interpretation diagnostic."""

    code: TargetDiscoveryDiagnosticCode
    severity: TargetDiscoverySeverity = TargetDiscoverySeverity.error
    detail: StrictStr = Field(min_length=1)
    tool_name: StrictStr | None = Field(default=None, min_length=1)
    evidence_refs: tuple[StrictStr, ...] = ()

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _evidence_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("discovery diagnostic evidence_refs must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize(self) -> TargetDiscoveryDiagnostic:
        values = tuple(sorted(self.evidence_refs))
        _ensure_unique_nonempty(values, "evidence_refs")
        object.__setattr__(self, "evidence_refs", values)
        return self


class ExecutionTargetProfile(_DigestModel):
    """Closed, content-addressed observed MCP or explicit simulation profile."""

    schema_version: Literal["execution-target-profile-v1"] = (
        EXECUTION_TARGET_PROFILE_SCHEMA_VERSION
    )
    target_id: StrictStr = Field(min_length=1)
    authorization_scope_id: StrictStr = Field(min_length=1)
    basis: ProfileBasis = ProfileBasis.target
    inventory_authority: InventoryAuthority | None = None
    semantic_authority: SemanticAuthority
    inventory_completeness: InventoryCompleteness = InventoryCompleteness.unknown
    source_protocol: SourceProtocol
    source_inventory_digest: SHA256Digest | None = None
    discovery_provenance: DiscoveryProvenance | None = None
    inventory: McpInventoryObservation | None = None
    resources: tuple[TargetProfileResource, ...] = ()
    interpretations: tuple[TargetSemanticInterpretation, ...] = ()
    diagnostics: tuple[TargetDiscoveryDiagnostic, ...] = ()

    @property
    def _digest_frame(self) -> str:
        return EXECUTION_TARGET_PROFILE_DIGEST_FRAME

    @field_validator("resources", "interpretations", "diagnostics", mode="before")
    @classmethod
    def _collections_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("profile collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_and_validate(self) -> ExecutionTargetProfile:
        resources = tuple(sorted(self.resources, key=lambda item: item.resource_id))
        _ensure_unique_ids(resources, "resource_id", "profile resources")
        object.__setattr__(self, "resources", resources)
        if self.source_protocol is SourceProtocol.mcp:
            self._validate_mcp_branch(resources)
        else:
            self._validate_simulation_branch(resources)
        interpretations = tuple(sorted(self.interpretations, key=lambda item: item.resource_id))
        _ensure_unique_ids(interpretations, "resource_id", "profile interpretations")
        object.__setattr__(self, "interpretations", interpretations)
        diagnostics = tuple(
            sorted(
                self.diagnostics,
                key=lambda item: (item.tool_name or "", item.code, item.detail),
            )
        )
        object.__setattr__(self, "diagnostics", diagnostics)
        _attest_profile(self)
        return self

    def _validate_mcp_branch(self, resources: tuple[TargetProfileResource, ...]) -> None:
        """Require identity closure for an observed MCP inventory."""

        _validate_mcp_profile_identity(self)
        _validate_mcp_inventory_completeness(self)
        expected_resources = _expected_mcp_resources(self)
        _validate_mcp_resources(self, resources, expected_resources)
        _validate_mcp_interpretations(self, expected_resources)

    def _validate_simulation_branch(self, resources: tuple[TargetProfileResource, ...]) -> None:
        """Require explicit simulation behavior without fake MCP evidence."""

        if self.basis is not ProfileBasis.simulation:
            raise ValueError("simulation profiles require basis=simulation")
        if self.inventory_authority is not None:
            raise ValueError("simulation profiles cannot claim observed inventory")
        if self.source_inventory_digest is not None:
            raise ValueError("simulation profiles cannot carry source_inventory_digest")
        if self.discovery_provenance is not None:
            raise ValueError("simulation profiles cannot carry discovery_provenance")
        if self.inventory is not None:
            raise ValueError("simulation profiles cannot carry an MCP inventory")
        if self.inventory_completeness is not InventoryCompleteness.unknown:
            raise ValueError("simulation profiles require unknown inventory_completeness")
        if self.interpretations:
            raise ValueError("simulation profiles cannot carry MCP interpretations")
        if self.semantic_authority is not SemanticAuthority.reviewed:
            raise ValueError("simulation profiles require reviewed semantic_authority")
        for resource in resources:
            if resource.simulation_behavior is None:
                raise ValueError(
                    "simulation profiles require simulation_behavior for every resource"
                )

    @property
    def profile_id(self) -> str:
        """Compatibility identity used by the execution consumer."""

        return self.target_id

    @property
    def environment_id(self) -> str:
        """Compatibility environment identity used by bound cases."""

        return self.target_id

    @property
    def authority(self) -> ProfileAuthority:
        """Legacy runtime view of semantic authority; never serialized."""

        return ProfileAuthority(self.semantic_authority.value)


def _validate_mcp_profile_identity(profile: ExecutionTargetProfile) -> None:
    if profile.basis is not ProfileBasis.target:
        raise ValueError("MCP profiles require basis=target")
    if profile.inventory_authority is not InventoryAuthority.observed:
        raise ValueError("MCP inventory authority must be observed")
    if profile.inventory is None:
        raise ValueError("MCP profiles require an embedded inventory")
    if profile.source_inventory_digest is None:
        raise ValueError("MCP profiles require source_inventory_digest")
    if profile.discovery_provenance is None:
        raise ValueError("MCP profiles require discovery_provenance")
    _validate_mcp_inventory_identity(profile)


def _validate_mcp_inventory_identity(profile: ExecutionTargetProfile) -> None:
    inventory = profile.inventory
    assert inventory is not None
    if inventory.target_id != profile.target_id:
        raise ValueError("inventory target_id does not match profile target_id")
    if inventory.authorization_scope_id != profile.authorization_scope_id:
        raise ValueError("inventory authorization_scope_id does not match profile scope")
    if profile.source_inventory_digest != inventory.semantic_digest:
        raise ValueError("source_inventory_digest does not match embedded inventory")


def _validate_mcp_inventory_completeness(profile: ExecutionTargetProfile) -> None:
    if profile.inventory_completeness is not InventoryCompleteness.observed_complete:
        return
    inventory = profile.inventory
    assert inventory is not None
    if not inventory.pagination_complete:
        raise ValueError("observed_complete profiles require complete inventory")
    error_codes = {
        TargetDiscoveryDiagnosticCode.inventory_protocol_failure,
        TargetDiscoveryDiagnosticCode.unsupported_protocol,
        TargetDiscoveryDiagnosticCode.duplicate_tool_name,
        TargetDiscoveryDiagnosticCode.malformed_tool,
        TargetDiscoveryDiagnosticCode.malformed_schema,
        TargetDiscoveryDiagnosticCode.incomplete_pagination,
    }
    if any(
        item.severity is TargetDiscoverySeverity.error and item.code in error_codes
        for item in profile.diagnostics
    ):
        raise ValueError("observed_complete profiles cannot contain discovery errors")


def _expected_mcp_resources(
    profile: ExecutionTargetProfile,
) -> dict[str, McpToolObservation]:
    inventory = profile.inventory
    assert inventory is not None
    return {mcp_resource_id(profile.target_id, tool.name): tool for tool in inventory.tools}


def _validate_mcp_resource(
    profile: ExecutionTargetProfile,
    resource: TargetProfileResource,
    expected_resources: dict[str, McpToolObservation],
) -> None:
    tool = expected_resources[resource.resource_id]
    if resource.target_id != profile.target_id or resource.tool_name != tool.name:
        raise ValueError("profile resource does not match source inventory tool")
    if resource.resource_id != mcp_resource_id(profile.target_id, tool.name):
        raise ValueError("MCP resource_id does not match target and tool")
    for field_name in (
        "title",
        "description",
        "input_schema",
        "output_schema",
        "annotations",
        "argument_names",
    ):
        if getattr(resource, field_name) != getattr(tool, field_name):
            raise ValueError(f"profile resource {field_name} drifted from inventory")
    if not set(resource.evidence_refs).issubset(set(mcp_inventory_evidence_refs(tool.name))):
        raise ValueError("MCP resource evidence_refs must resolve to inventory fields")
    if resource.simulation_behavior is not None:
        raise ValueError("MCP resources cannot contain simulation_behavior")


def _validate_mcp_resources(
    profile: ExecutionTargetProfile,
    resources: tuple[TargetProfileResource, ...],
    expected_resources: dict[str, McpToolObservation],
) -> None:
    if {item.resource_id for item in resources} != set(expected_resources):
        raise ValueError("profile resources must close exactly over inventory tools")
    for resource in resources:
        _validate_mcp_resource(profile, resource, expected_resources)


def _validate_mcp_interpretation(
    interpretation: TargetSemanticInterpretation,
    expected_resources: dict[str, McpToolObservation],
) -> None:
    if interpretation.tool_name != expected_resources[interpretation.resource_id].name:
        raise ValueError("interpretation tool_name does not match resource")
    if not set(interpretation.evidence_refs).issubset(
        set(mcp_inventory_evidence_refs(interpretation.tool_name))
    ):
        raise ValueError("interpretation evidence_refs must resolve to inventory fields")
    if any(ref not in expected_resources for ref in interpretation.observer_resource_ids):
        raise ValueError("interpretation observer references unknown resource")


def _validate_mcp_interpretations(
    profile: ExecutionTargetProfile,
    expected_resources: dict[str, McpToolObservation],
) -> None:
    resource_ids = set(expected_resources)
    if {item.resource_id for item in profile.interpretations} != resource_ids:
        raise ValueError("profile interpretations must contain one record per inventory tool")
    for interpretation in profile.interpretations:
        _validate_mcp_interpretation(interpretation, expected_resources)


def _attest_profile(value: ExecutionTargetProfile) -> None:
    expected = value.compute_semantic_digest()
    if value.semantic_digest is not None and value.semantic_digest != expected:
        raise ValueError("semantic_digest does not match profile content")
    object.__setattr__(value, "semantic_digest", expected)


def mcp_resource_id(target_id: str, tool_name: str) -> str:
    """Derive the stable resource identity from target and exact tool name."""

    if not target_id or not tool_name:
        raise ValueError("target_id and tool_name must be non-empty")
    return f"mcp:{target_id}:{tool_name}"


def mcp_inventory_evidence_refs(tool_name: str) -> tuple[str, ...]:
    """Return the closed inventory-field evidence namespace for one tool."""

    if not tool_name:
        raise ValueError("tool_name must be non-empty")
    prefix = f"inventory:tool:{tool_name}"
    return (
        prefix,
        f"{prefix}:name",
        f"{prefix}:title",
        f"{prefix}:description",
        f"{prefix}:input_schema",
        f"{prefix}:output_schema",
        f"{prefix}:annotations",
        f"{prefix}:argument_names",
    )


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
    inventory_authority: InventoryAuthority | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    semantic_authority: SemanticAuthority | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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
        _validate_classification_authority(self)
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


def _validate_classification_authority(value: ExecutionClassification) -> None:
    """Keep authority axes aligned with the classification environment."""

    profile_backed = value.environment_basis in {
        EnvironmentBasis.target_profile,
        EnvironmentBasis.simulation_profile,
    }
    if not profile_backed:
        if value.inventory_authority is not None or value.semantic_authority is not None:
            raise ValueError(
                "target-agnostic or unbound classifications cannot carry profile authority"
            )
        return
    if value.semantic_authority is None:
        raise ValueError("profile-backed classifications require semantic_authority")
    if value.environment_basis is EnvironmentBasis.target_profile:
        if value.inventory_authority is not InventoryAuthority.observed:
            raise ValueError("target classifications require observed inventory_authority")
    else:
        if value.inventory_authority is not None:
            raise ValueError("simulation classifications cannot carry inventory_authority")
        if value.semantic_authority is not SemanticAuthority.reviewed:
            raise ValueError("simulation classifications require reviewed semantic_authority")


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


_JSON_SCHEMA_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def _validate_schema_type(schema_type: Any, field_name: str) -> None:
    if schema_type is None:
        return
    valid = isinstance(schema_type, str) and schema_type in _JSON_SCHEMA_TYPES
    valid = valid or (
        isinstance(schema_type, list)
        and bool(schema_type)
        and all(isinstance(item, str) and item in _JSON_SCHEMA_TYPES for item in schema_type)
    )
    if not valid:
        raise ValueError(f"{field_name}.type must be a supported JSON Schema type")


def _validate_schema_required(required: Any, field_name: str) -> None:
    if required is None:
        return
    if (
        not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or len(required) != len(set(required))
    ):
        raise ValueError(f"{field_name}.required must be an array of unique strings")


def _validate_schema_properties(properties: Any, field_name: str) -> None:
    if properties is None:
        return
    if not isinstance(properties, Mapping):
        raise ValueError(f"{field_name}.properties must be an object")
    for name, child in properties.items():
        if not isinstance(name, str) or not isinstance(child, Mapping):
            raise ValueError(f"{field_name}.properties must contain schema objects")
        _validate_json_schema(child, f"{field_name}.properties.{name}")


def _validate_schema_additional(additional: Any, field_name: str) -> None:
    if additional is not None and not isinstance(additional, (bool, Mapping)):
        raise ValueError(f"{field_name}.additionalProperties must be boolean or a schema")
    if isinstance(additional, Mapping):
        _validate_json_schema(additional, f"{field_name}.additionalProperties")


def _validate_schema_nested(value: Mapping[str, Any], field_name: str) -> None:
    for key in ("items", "contains", "propertyNames"):
        child = value.get(key)
        if isinstance(child, Mapping):
            _validate_json_schema(child, f"{field_name}.{key}")


def _validate_json_schema(value: Mapping[str, Any], field_name: str) -> None:
    """Validate the structural JSON-Schema subset accepted by the compiler."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    _validate_schema_type(value.get("type"), field_name)
    _validate_schema_required(value.get("required"), field_name)
    _validate_schema_properties(value.get("properties"), field_name)
    _validate_schema_additional(value.get("additionalProperties"), field_name)
    _validate_schema_nested(value, field_name)


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
    "DiscoveryMode",
    "DiscoveryProvenance",
    "InventoryAuthority",
    "InventoryCompleteness",
    "InterpreterVerifierAgreement",
    "McpInventory",
    "McpInventoryObservation",
    "McpInventoryTool",
    "McpToolObservation",
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
    "TargetDiscoveryDiagnostic",
    "TargetDiscoveryDiagnosticCode",
    "TargetDiscoverySeverity",
    "TargetInterpretationDisposition",
    "TargetOperationEffect",
    "TargetSemanticInterpretation",
    "TargetStateEffect",
    "SourceProtocol",
    "mcp_resource_id",
    "mcp_inventory_evidence_refs",
    "EXECUTION_CLASSIFICATION_SCHEMA_VERSION",
    "EXECUTION_CONTRACT_SCHEMA_VERSION",
    "EXECUTION_TARGET_PROFILE_SCHEMA_VERSION",
]
