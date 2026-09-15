"""Typed records for the artifact-design path.

The design path emits its own closed plan (``artifact-design-plan-v1``). The
legacy ``ReadyExecutionPlan`` structurally requires bundle/projection digest
fields; this mission retires those producer artifacts, so the design plan
carries handoff-derived provenance instead and references no execution
projection or bundle anywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import StrictStr, field_validator, model_validator

from ..models._base import ImmutableModel, SHA256Digest, compute_framed_digest

DESIGN_PLAN_SCHEMA_VERSION = "artifact-design-plan-v1"
DESIGN_PLAN_DIGEST_FRAME = "artifact-design-plan-v1"
FREEZE_SCHEMA_VERSION = "artifact-freeze-v1"
FREEZE_DIGEST_FRAME = "artifact-freeze-v1"
DESIGN_TRACE_SCHEMA_VERSION = "artifact-design-trace-v1"
DESIGN_VALIDATION_SCHEMA_VERSION = "artifact-design-validation-v1"
DESIGN_RECORD_SCHEMA_VERSION = "artifact-design-record-v1"
DESIGN_EXCLUSION_SCHEMA_VERSION = "design-exclusion-v1"
DESIGN_MANIFEST_SCHEMA_VERSION = "design-manifest-v1"
AUTHOR_RESULT_DIGEST_FRAME = "artifact-author-result-v1"

#: Typed exclusion codes. Blocked designs are preserved with one of these
#: reasons and never silently compiled or dropped.
DESIGN_EXCLUSION_CODES = (
    "needs-environment-binding",
    "unsupported-observation",
    "unsupported-criterion-shape",
    "missing-setup",
    "unresolved-prerequisite",
    "invalid-design",
    "unsupported-scenario-kind",
    "environment-mismatch",
)

AUTHORITIES = ("declared", "observed", "interpreted", "reviewed", "unresolved")


class DesignedTurn(ImmutableModel):
    """One user-role designed turn; the consumer never authors assistant turns."""

    turn_id: StrictStr
    text: StrictStr


class StimulusDesign(ImmutableModel):
    """The consumer-designed stimulus and its provenance.

    ``amount_requested`` is the numeric amount the stimulus asks to refund for
    the amount-bearing criterion shapes; it is ``None`` for shapes whose
    criterion concerns a record's state rather than an amount.
    """

    delivery_class: Literal["direct_prompt", "conversation_context"]
    turns: tuple[DesignedTurn, ...]
    amount_requested: float | None = None
    rationale: StrictStr = ""
    provenance: Mapping[str, Any]

    @field_validator("turns", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("designed turns must be an array")
        return tuple(value)


class SetupDesign(ImmutableModel):
    """The selected test record and the prerequisites the design establishes."""

    selected_record_id: StrictStr
    record_state: Mapping[str, Any]
    established_prerequisites: tuple[Mapping[str, Any], ...] = ()
    establishment: tuple[StrictStr, ...] = ()

    @field_validator("established_prerequisites", "establishment", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("setup collections must be arrays")
        return tuple(value)


class DetectorDesign(ImmutableModel):
    """The executable detector derived from the semantic failure criterion.

    ``expected`` is the numeric limit for a threshold detector
    (``greater_than``) or the record identifier for an equality detector
    (``equals``) when the criterion concerns a specific record.
    """

    tool_name: StrictStr
    field_path: StrictStr
    comparison: StrictStr
    expected: float | str
    limit_source: Mapping[str, Any]
    rationale: StrictStr
    derived_from: Literal["handoff.semantic_failure_criterion"] = (
        "handoff.semantic_failure_criterion"
    )
    observation_level: Literal["command"] = "command"
    observation_limits: tuple[StrictStr, ...] = ()

    @field_validator("observation_limits", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("observation limits must be an array")
        return tuple(value)


class FidelityAnswer(ImmutableModel):
    """One fidelity answer with its evidence and authority label."""

    answer: bool
    evidence: StrictStr
    authority: Literal["declared", "observed", "interpreted", "reviewed", "unresolved"]


class FidelityAssessment(ImmutableModel):
    """The recorded fidelity assessment for one selected test."""

    stimulus_exercises_scenario: FidelityAnswer
    prerequisites_hold: FidelityAnswer
    detector_distinguishes: FidelityAnswer


class DesignEnvironmentRecord(ImmutableModel):
    """The explicit environment the design resolved against."""

    basis: Literal["target_profile", "simulation_profile", "none"]
    profile_id: StrictStr = ""
    profile_digest: StrictStr = ""
    runtime_context_digest: StrictStr = ""
    state_digest: StrictStr = ""


class FreezeRecord(ImmutableModel):
    """Frozen artifact-owned text and evidence, published before execution."""

    schema_version: Literal["artifact-freeze-v1"] = FREEZE_SCHEMA_VERSION
    design_id: StrictStr
    case_id: StrictStr
    source_scenario_id: StrictStr
    source_scenario_version: int
    handoff_schema_version: StrictStr
    handoff_digest: StrictStr
    frozen_content: Mapping[str, Any]
    frozen_content_digest: SHA256Digest
    frozen_at: StrictStr = ""


class ArtifactDesignPlan(ImmutableModel):
    """The closed design plan; the only accepted input to design compilation."""

    schema_version: Literal["artifact-design-plan-v1"] = DESIGN_PLAN_SCHEMA_VERSION
    platform: StrictStr
    adapter_version: StrictStr
    scenario_id: StrictStr
    scenario_version: int
    handoff_schema_version: StrictStr
    handoff_digest: StrictStr
    lineage: Mapping[str, Any]
    semantic_failure_criterion: StrictStr
    safe_alternative: StrictStr
    design_id: StrictStr
    case_id: StrictStr
    conversation_scope: Mapping[str, Any]
    environment: DesignEnvironmentRecord
    setup: SetupDesign
    stimulus: StimulusDesign
    detector: DetectorDesign
    fidelity: FidelityAssessment
    freeze: Mapping[str, Any]
    tool_declarations: tuple[Mapping[str, Any], ...] = ()
    judge_description: StrictStr | None = None
    case_digest: StrictStr = ""
    #: The freeze record's frozen-content digest, carried at the top level so
    #: downstream execution receipts can cite and verify it directly. Plans
    #: persisted before this field existed load with the empty default.
    frozen_content_digest: StrictStr = ""

    @field_validator("tool_declarations", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("tool declarations must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _attest_case_digest(self) -> ArtifactDesignPlan:
        payload = {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "case_digest"
        }
        # Legacy plans persisted before `frozen_content_digest` existed carry
        # the empty default; their recorded case digest covers the payload
        # without the field, so the field is excluded while absent.
        if not self.frozen_content_digest:
            payload.pop("frozen_content_digest", None)
        expected = compute_framed_digest(DESIGN_PLAN_DIGEST_FRAME, payload)
        if self.case_digest and self.case_digest != expected:
            raise ValueError("case_digest does not match plan content")
        object.__setattr__(self, "case_digest", expected)
        return self


class DesignExclusion(ImmutableModel):
    """One typed exclusion preserving a blocked scenario with its reason."""

    schema_version: Literal["design-exclusion-v1"] = DESIGN_EXCLUSION_SCHEMA_VERSION
    scenario_id: StrictStr
    design_id: StrictStr
    code: StrictStr
    detail: StrictStr
    handoff_schema_version: StrictStr
    handoff_digest: StrictStr
    fidelity: FidelityAssessment | None = None
    #: Live authoring attempt evidence for the blocked design: every attempt
    #: (including malformed or rejected responses) and the authoring call
    #: count. ``None`` when the design blocked before any authoring call.
    authoring: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DesignOutcome:
    """The result of designing one scenario: a plan or a typed exclusion."""

    scenario_id: str
    design_id: str
    plan: ArtifactDesignPlan | None
    exclusion: DesignExclusion | None
    freeze: FreezeRecord | None
    design_record: Mapping[str, Any]
    #: Live authoring attempt evidence: every attempt with its raw response
    #: and classification, plus the authoring call count.
    authoring: Mapping[str, Any] = field(default_factory=dict)

    @property
    def compiled(self) -> bool:
        return self.plan is not None


__all__ = [
    "AUTHOR_RESULT_DIGEST_FRAME",
    "AUTHORITIES",
    "ArtifactDesignPlan",
    "DesignEnvironmentRecord",
    "DesignExclusion",
    "DESIGN_EXCLUSION_CODES",
    "DesignOutcome",
    "DesignedTurn",
    "DetectorDesign",
    "FidelityAnswer",
    "FidelityAssessment",
    "FreezeRecord",
    "SetupDesign",
    "StimulusDesign",
]
