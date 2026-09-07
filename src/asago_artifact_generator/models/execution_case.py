"""Immutable semantic execution cases and typed environment exclusions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, StrictStr, field_validator, model_validator

from ._base import ImmutableModel, SHA256Digest, compute_framed_digest
from .execution_classification import (
    BindingCompleteness,
    EnvironmentBasis,
    ExecutionClaimScope,
    ExecutionClassification,
    ExecutionProfileFit,
    InventoryAuthority,
    ResolvedExecutionBinding,
    SemanticAuthority,
    SimulationBehavior,
)
from .execution_intent import ExecutionIntent

EXECUTION_CASE_SCHEMA_VERSION = "bound-execution-case-v1"
EXECUTION_CASE_EXCLUSION_SCHEMA_VERSION = "execution-case-exclusion-v1"
EXECUTION_CASE_DIGEST_FRAME = EXECUTION_CASE_SCHEMA_VERSION
ExecutionCaseExclusionCode = Literal[
    "analytical_only",
    "needs_environment_binding",
    "needs_target_binding",
    "needs_simulation_binding",
    "ambiguous",
    "unsupported",
    "invalid_profile",
    "invalid_source_binding",
]


class ExecutionCaseDiagnostic(ImmutableModel):
    """One deterministic consumer-side case resolution diagnostic."""

    code: StrictStr = Field(min_length=1)
    detail: StrictStr = Field(min_length=1)
    requirement_id: StrictStr | None = Field(default=None, min_length=1)
    candidate_resource_ids: tuple[StrictStr, ...] = ()

    @field_validator("candidate_resource_ids", mode="before")
    @classmethod
    def _candidate_ids_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("candidate_resource_ids must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _canonicalize_candidates(self) -> ExecutionCaseDiagnostic:
        values = tuple(sorted(self.candidate_resource_ids))
        if len(values) != len(set(values)):
            raise ValueError("candidate_resource_ids must be unique")
        object.__setattr__(self, "candidate_resource_ids", values)
        return self


class SelectedSimulationResource(ImmutableModel):
    """One selected profile resource's deterministic simulation behavior."""

    resource_id: StrictStr = Field(min_length=1)
    simulation_behavior: SimulationBehavior


class BoundExecutionCase(ImmutableModel):
    """One exact semantic scenario-to-profile resolution."""

    schema_version: Literal["bound-execution-case-v1"] = EXECUTION_CASE_SCHEMA_VERSION
    case_id: StrictStr = Field(min_length=1)
    intent: ExecutionIntent
    execution_classification: ExecutionClassification
    selected_profile_id: StrictStr | None = Field(default=None, min_length=1)
    selected_profile_basis: Literal["target", "simulation"] | None = None
    target_environment_id: StrictStr | None = Field(default=None, min_length=1)
    target_profile_digest: SHA256Digest | None = None
    target_realization_digest: SHA256Digest | None = None
    inventory_authority: InventoryAuthority | None = None
    semantic_authority: SemanticAuthority | None = None
    resolved_bindings: tuple[ResolvedExecutionBinding, ...] = ()
    selected_simulation_resources: tuple[SelectedSimulationResource, ...] = ()
    binding_completeness: BindingCompleteness
    environment_basis: EnvironmentBasis
    profile_fit: ExecutionProfileFit
    claim_scope: ExecutionClaimScope
    case_digest: SHA256Digest | None = None

    @field_validator("resolved_bindings", "selected_simulation_resources", mode="before")
    @classmethod
    def _bindings_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("resolved_bindings must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_and_attest(self) -> BoundExecutionCase:
        _validate_profile_identity(self)
        _validate_binding_identity(self)
        _validate_bound_result(self)
        expected = self.compute_case_digest()
        if self.case_digest is not None and self.case_digest != expected:
            raise ValueError("case_digest does not match case content")
        object.__setattr__(self, "case_digest", expected)
        return self

    def digest_payload(self) -> dict[str, Any]:
        """Return case content excluding its derived digest."""

        return self.model_dump(mode="json", exclude={"case_digest"})

    def compute_case_digest(self) -> str:
        """Compute the content-addressed bound-case digest."""

        return compute_framed_digest(EXECUTION_CASE_DIGEST_FRAME, self.digest_payload())

    def assert_integrity(self) -> None:
        """Raise when the recorded case digest no longer matches its content."""

        if self.case_digest != self.compute_case_digest():
            raise ValueError("case_digest does not match case content")


class ExecutionCaseExclusion(ImmutableModel):
    """A typed reason why one intent cannot become a bound case."""

    schema_version: Literal["execution-case-exclusion-v1"] = (
        EXECUTION_CASE_EXCLUSION_SCHEMA_VERSION
    )
    intent: ExecutionIntent
    execution_classification: ExecutionClassification
    code: ExecutionCaseExclusionCode
    requirement_ids: tuple[StrictStr, ...] = ()
    diagnostics: tuple[ExecutionCaseDiagnostic, ...] = ()
    exclusion_digest: SHA256Digest | None = None

    @field_validator("requirement_ids", "diagnostics", mode="before")
    @classmethod
    def _collections_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("exclusion collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _attest(self) -> ExecutionCaseExclusion:
        values = tuple(sorted(self.requirement_ids))
        if len(values) != len(set(values)):
            raise ValueError("exclusion requirement_ids must be unique")
        object.__setattr__(self, "requirement_ids", values)
        expected = self.compute_exclusion_digest()
        if self.exclusion_digest is not None and self.exclusion_digest != expected:
            raise ValueError("exclusion_digest does not match exclusion content")
        object.__setattr__(self, "exclusion_digest", expected)
        return self

    def digest_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"exclusion_digest"})

    def compute_exclusion_digest(self) -> str:
        return compute_framed_digest(
            EXECUTION_CASE_EXCLUSION_SCHEMA_VERSION, self.digest_payload()
        )

    def assert_integrity(self) -> None:
        """Raise when the recorded exclusion digest no longer matches its content."""

        if self.exclusion_digest != self.compute_exclusion_digest():
            raise ValueError("exclusion_digest does not match exclusion content")


ExecutionCaseResolution = BoundExecutionCase | ExecutionCaseExclusion


def _validate_profile_identity(value: BoundExecutionCase) -> None:
    if not _has_profile_identity(value):
        return
    _require_profile_identity(value)
    _require_profile_pin(value)
    _require_profile_authority(value)


def _has_profile_identity(value: BoundExecutionCase) -> bool:
    return any(
        item is not None
        for item in (
            value.selected_profile_id,
            value.selected_profile_basis,
            value.target_environment_id,
            value.target_profile_digest,
            value.target_realization_digest,
            value.inventory_authority,
            value.semantic_authority,
        )
    )


def _require_profile_identity(value: BoundExecutionCase) -> None:
    if value.selected_profile_id is None or value.selected_profile_basis is None:
        raise ValueError("bound case profile identity is incomplete")


def _require_profile_pin(value: BoundExecutionCase) -> None:
    if value.target_environment_id is None or value.target_profile_digest is None:
        raise ValueError("bound case profile pin is incomplete")


def _require_profile_authority(value: BoundExecutionCase) -> None:
    if value.selected_profile_basis == "target":
        if value.inventory_authority is not InventoryAuthority.observed:
            raise ValueError("target bound case requires observed inventory_authority")
        if value.semantic_authority is None:
            raise ValueError("target bound case requires semantic_authority")
        return
    if value.selected_profile_basis == "simulation":
        if value.inventory_authority is not None:
            raise ValueError("simulation bound case cannot carry inventory_authority")
        if value.semantic_authority is not SemanticAuthority.reviewed:
            raise ValueError("simulation bound case requires reviewed semantic_authority")
        return
    raise ValueError("bound case profile basis is required for profile authority")


def _validate_binding_identity(value: BoundExecutionCase) -> None:
    bindings = tuple(item.requirement_id for item in value.resolved_bindings)
    if len(bindings) != len(set(bindings)):
        raise ValueError("bound case requirement bindings must be unique")
    resources = tuple(item.resource_id for item in value.selected_simulation_resources)
    if len(resources) != len(set(resources)):
        raise ValueError("bound case simulation resources must be unique")


def _validate_bound_result(value: BoundExecutionCase) -> None:
    _validate_concrete_binding_result(value)
    _validate_requirement_bindings(value)
    _validate_simulation_resources(value)
    if value.environment_basis is EnvironmentBasis.target_agnostic:
        _validate_target_agnostic_result(value)
        return
    _validate_profile_backed_result(value)


def _validate_concrete_binding_result(value: BoundExecutionCase) -> None:
    if value.binding_completeness is not BindingCompleteness.concrete:
        raise ValueError("bound case result must have concrete binding_completeness")


def _validate_requirement_bindings(value: BoundExecutionCase) -> None:
    requirements = value.intent.execution_contract.resource_requirements
    requirement_ids = {item.requirement_id for item in requirements}
    binding_ids = {item.requirement_id for item in value.resolved_bindings}
    if requirement_ids != binding_ids:
        raise ValueError("bound case bindings must resolve every semantic requirement")


def _validate_simulation_resources(value: BoundExecutionCase) -> None:
    """Require selected simulation behavior to close every bound resource."""

    selected_ids = {item.resource_id for item in value.selected_simulation_resources}
    bound_ids = {item.resource_id for item in value.resolved_bindings}
    if value.environment_basis is EnvironmentBasis.simulation_profile:
        if not value.selected_simulation_resources:
            raise ValueError("simulation bound case requires selected simulation resources")
        if selected_ids != bound_ids:
            raise ValueError("selected simulation resources must match bound resource identities")
    elif value.selected_simulation_resources:
        raise ValueError("only simulation bound cases may carry simulation resources")


def _validate_target_agnostic_result(value: BoundExecutionCase) -> None:
    if value.intent.execution_contract.resource_requirements:
        raise ValueError("target-agnostic bound case cannot require domain resources")
    if value.profile_fit is not ExecutionProfileFit.not_required:
        raise ValueError("target-agnostic bound case must have profile_fit not_required")
    if value.claim_scope is not ExecutionClaimScope.model_behavior_only:
        raise ValueError("target-agnostic bound case must have model_behavior_only claim")
    if any(
        item is not None
        for item in (
            value.selected_profile_id,
            value.selected_profile_basis,
            value.target_environment_id,
            value.target_profile_digest,
            value.target_realization_digest,
            value.inventory_authority,
            value.semantic_authority,
        )
    ):
        raise ValueError("target-agnostic bound case cannot retain a profile")


def _validate_profile_backed_result(value: BoundExecutionCase) -> None:
    expected_basis = _profile_basis_name(value)
    if value.profile_fit is not ExecutionProfileFit.matched:
        raise ValueError("profile-backed bound case must have profile_fit matched")
    if value.selected_profile_basis != expected_basis:
        raise ValueError("bound case profile basis does not match environment basis")
    if value.claim_scope is not _profile_claim_scope(value):
        raise ValueError("bound case claim scope does not match environment basis")
    _require_profile_authority(value)
    _validate_source_profile_lineage(value)
    classification = value.execution_classification
    if classification.target_profile_digest != value.target_profile_digest:
        raise ValueError("bound case target_profile_digest disagrees with classification")
    if classification.inventory_authority is not None and (
        classification.inventory_authority is not value.inventory_authority
    ):
        raise ValueError("bound case inventory authority disagrees with classification")
    if classification.semantic_authority is not None and (
        classification.semantic_authority is not value.semantic_authority
    ):
        raise ValueError("bound case semantic authority disagrees with classification")


def _validate_source_profile_lineage(value: BoundExecutionCase) -> None:
    """Keep copied target source pins closed over the bound case."""

    pins = value.intent.trace_refs.source_pins
    profile_pin = pins.get("execution_target_profile")
    realization_pin = pins.get("target_realization")
    exact_resource_required = any(
        requirement.exact_resource_id is not None
        for requirement in value.intent.execution_contract.resource_requirements
    )
    if exact_resource_required and (profile_pin is None or realization_pin is None):
        raise ValueError(
            "exact resource bound cases require execution_target_profile and "
            "target_realization source pins"
        )
    if profile_pin is not None and profile_pin != value.target_profile_digest:
        raise ValueError("bound case target_profile_digest disagrees with source pin")
    if realization_pin != value.target_realization_digest:
        raise ValueError("bound case target_realization_digest disagrees with source pin")
    if value.selected_profile_basis != "target" and (
        profile_pin is not None or realization_pin is not None
    ):
        raise ValueError("only target bound cases may carry target source pins")


def _profile_basis_name(value: BoundExecutionCase) -> str:
    expected_basis = {
        EnvironmentBasis.target_profile: "target",
        EnvironmentBasis.simulation_profile: "simulation",
    }.get(value.environment_basis)
    if expected_basis is None:
        raise ValueError("bound case environment basis must be executable")
    return expected_basis


def _profile_claim_scope(value: BoundExecutionCase) -> ExecutionClaimScope:
    if value.environment_basis is EnvironmentBasis.target_profile:
        return ExecutionClaimScope.target_specific_intent
    return ExecutionClaimScope.agent_behavior_with_simulated_tools


__all__ = [
    "BoundExecutionCase",
    "EXECUTION_CASE_DIGEST_FRAME",
    "EXECUTION_CASE_EXCLUSION_SCHEMA_VERSION",
    "EXECUTION_CASE_SCHEMA_VERSION",
    "ExecutionCaseDiagnostic",
    "ExecutionCaseExclusion",
    "ExecutionCaseExclusionCode",
    "ExecutionCaseResolution",
    "SelectedSimulationResource",
]
