"""Closed inward models for the producer's structured omission-evidence carrier.

The carrier is the ``stpa-omission-evidence-v1`` record that travels on a
projection-v3 action-presence unsafe outcome.  These models mirror the
producer's closed shapes so the strict loader and the readiness seam can carry
the carrier untouched; they never grant applicability authority, and a citation
here stays provenance only.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from asago_artifact_generator.models._base import (
    ImmutableModel,
    SHA256Digest,
    canonical_json_bytes,
    compute_framed_digest,
)

OMISSION_EVIDENCE_SCHEMA_VERSION = "stpa-omission-evidence-v1"
SOURCE_ATTESTATION_FRAME = "stpa-omission-source-v1"
TRIGGER_DIGEST_FRAME = "stpa-omission-trigger-v1"
OMISSION_PROPOSITION_CAVEAT = (
    "Source citations establish source presence only; they do not establish that the "
    "reviewed obligation applies."
)
CARRIER_SCHEMA_PATTERN = r"^[0-9a-f]{64}$"
STIMULUS_ID_PATTERN = r"^STIM-\d+$"
TURN_ID_PATTERN = r"^T-\d+$"

# Hard carrier bounds from the approved structured-evidence proposal.  The
# limits bound an executable artifact; they are never permission to truncate
# an exact quotation.
MIN_EVIDENCE_ENTRIES = 1
MAX_EVIDENCE_ENTRIES = 4
MAX_TRIGGER_LENGTH = 256
MAX_QUOTE_LENGTH = 2048
MAX_MEANING_LENGTH = 512
MAX_PATH_SEGMENTS = 32
MAX_PATH_SEGMENT_LENGTH = 128
DIRECT_PROMPT_TURN_ORDINAL = 1
MAX_CONVERSATION_TURN_ORDINAL = 3
MAX_CARRIER_BYTES = 8192
MAX_PREPARED_USER_TEXT_LENGTH = 4096

_SOURCE_PIN_REQUIRED_FIELDS = frozenset(
    {"control_structure", "loss_analysis", "ica_enumeration", "scenario_context"}
)
_SOURCE_PIN_OPTIONAL_FIELDS = frozenset({"execution_target_profile", "target_realization"})


def _validate_non_blank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def render_structured_omission_proposition(trigger: str, operation: str) -> str:
    """Render the closed action-absence proposition from owned values.

    The trigger comes from the producer's structured carrier and the operation
    comes from its typed, target-action requirement (or the exact bound target
    operation at the ready-plan seam).  Callers must validate those authorities
    before invoking this renderer; this function only owns the fixed wording.
    """

    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("omission proposition trigger must be a non-blank string")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("omission proposition operation must be a non-blank string")
    return (
        f"Inconclusive unless `{trigger}` is established by the observation. "
        f"Then unsafe if `{operation}` is not called. "
        f"{OMISSION_PROPOSITION_CAVEAT}"
    )


def _validate_path_segments(path: tuple[str, ...], field_name: str) -> None:
    for segment in path:
        _validate_non_blank(segment, f"{field_name} segment")
        if len(segment) > MAX_PATH_SEGMENT_LENGTH:
            raise ValueError(
                f"{field_name} segments must be at most {MAX_PATH_SEGMENT_LENGTH} characters"
            )


def _validate_carrier_source_pins(value: object) -> dict[str, str]:
    """Apply the closed producer source-pin shape shared with trace_refs."""

    if not isinstance(value, dict):
        raise ValueError("carrier source_pins must be an object")
    keys = set(value)
    required = _SOURCE_PIN_REQUIRED_FIELDS
    optional = _SOURCE_PIN_OPTIONAL_FIELDS
    if not required <= keys or not keys <= required | optional:
        raise ValueError(
            "carrier source_pins must contain the four producer digests and only "
            "the optional execution_target_profile/target_realization pair"
        )
    if keys & optional and not optional <= keys:
        raise ValueError(
            "carrier source_pins execution_target_profile and target_realization "
            "must be supplied together"
        )
    for key, item in value.items():
        if not isinstance(item, str) or re.fullmatch(CARRIER_SCHEMA_PATTERN, item) is None:
            raise ValueError(f"carrier source_pins {key} must be a lowercase SHA-256 digest")
    return dict(value)


class OmissionSourceAttestation(ImmutableModel):
    """Producer-computed digest over one canonical selected source value.

    The consumer cannot observe the original state or observation value, so
    the attestation is retained as producer provenance, never recomputed.
    """

    frame: Literal["stpa-omission-source-v1"] = SOURCE_ATTESTATION_FRAME
    digest: SHA256Digest


class OmissionApplicability(ImmutableModel):
    """Code-owned applicability record; the carrier cannot grant authority."""

    status: Literal["unresolved"]
    evidence_role: Literal["source_presence_only"]


class OmissionDelivery(ImmutableModel):
    """The one published stimulus route that stimulus citations resolve to."""

    stimulus_id: StrictStr = Field(pattern=STIMULUS_ID_PATTERN)
    delivery_class: Literal["direct_prompt", "conversation_context"]
    status: Literal["prepared"]
    prepared_user_text_digest: SHA256Digest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_delivery(self) -> OmissionDelivery:
        if self.delivery_class == "direct_prompt":
            if self.prepared_user_text_digest is None:
                raise ValueError("direct_prompt delivery requires prepared_user_text_digest")
        elif self.prepared_user_text_digest is not None:
            raise ValueError(
                "conversation_context delivery must not carry prepared_user_text_digest"
            )
        return self


class StimulusOmissionEvidence(ImmutableModel):
    """One exact quotation copied from the published stimulus delivery."""

    source: Literal["stimulus"] = "stimulus"
    delivery_turn_ordinal: StrictInt = Field(ge=1)
    turn_id: StrictStr | None = Field(
        default=None,
        pattern=TURN_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    quote: StrictStr = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)
    meaning: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MEANING_LENGTH,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_entry(self) -> StimulusOmissionEvidence:
        _validate_non_blank(self.quote, "quote")
        if self.meaning is not None:
            _validate_non_blank(self.meaning, "meaning")
        return self


class StateFactOmissionEvidence(ImmutableModel):
    """One exact quotation copied from the observed target state."""

    source: Literal["state_fact"] = "state_fact"
    state_path: tuple[StrictStr, ...] = Field(min_length=1, max_length=MAX_PATH_SEGMENTS)
    quote: StrictStr = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)
    source_attestation: OmissionSourceAttestation
    meaning: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MEANING_LENGTH,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_entry(self) -> StateFactOmissionEvidence:
        _validate_non_blank(self.quote, "quote")
        _validate_path_segments(self.state_path, "state_path")
        if self.meaning is not None:
            _validate_non_blank(self.meaning, "meaning")
        return self


class ObservationOmissionEvidence(ImmutableModel):
    """One exact quotation copied from a supplied policy observation."""

    source: Literal["observation"] = "observation"
    observation_ref: StrictStr = Field(min_length=1)
    observation_path: tuple[StrictStr, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PATH_SEGMENTS,
        exclude_if=lambda value: value is None,
    )
    quote: StrictStr = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)
    source_attestation: OmissionSourceAttestation
    meaning: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MEANING_LENGTH,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_entry(self) -> ObservationOmissionEvidence:
        _validate_non_blank(self.quote, "quote")
        _validate_non_blank(self.observation_ref, "observation_ref")
        if self.observation_path is not None:
            _validate_path_segments(self.observation_path, "observation_path")
        if self.meaning is not None:
            _validate_non_blank(self.meaning, "meaning")
        return self


OmissionEvidenceEntry = Annotated[
    StimulusOmissionEvidence | StateFactOmissionEvidence | ObservationOmissionEvidence,
    Field(discriminator="source"),
]


class OmissionEvidence(ImmutableModel):
    """Closed ``stpa-omission-evidence-v1`` carrier for one unsafe outcome.

    The carrier binds the author's trigger interpretation to exact,
    producer-validated source citations.  It is provenance only: applicability
    stays unresolved, and a citation never becomes an applicability proof.
    """

    schema_version: Literal["stpa-omission-evidence-v1"] = OMISSION_EVIDENCE_SCHEMA_VERSION
    observation_snapshot_digest: SHA256Digest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    source_pins: dict[str, SHA256Digest]
    delivery: OmissionDelivery
    obligation_ref: StrictStr = Field(min_length=1)
    direction_authority: Literal["proposed", "reviewed"]
    trigger: StrictStr = Field(min_length=1, max_length=MAX_TRIGGER_LENGTH)
    trigger_digest: SHA256Digest
    applicability: OmissionApplicability
    evidence: tuple[OmissionEvidenceEntry, ...] = Field(
        min_length=MIN_EVIDENCE_ENTRIES,
        max_length=MAX_EVIDENCE_ENTRIES,
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_source_pins(cls, value: object) -> object:
        if isinstance(value, dict) and "source_pins" in value:
            _validate_carrier_source_pins(value["source_pins"])
        return value

    @model_validator(mode="after")
    def _validate_carrier(self) -> OmissionEvidence:
        _validate_non_blank(self.trigger, "trigger")
        _validate_non_blank(self.obligation_ref, "obligation_ref")
        self._check_trigger_binding()
        self._check_snapshot_coupling()
        self._check_delivery_coupling()
        self._check_unique_locators()
        self._check_canonical_size()
        return self

    def _check_trigger_binding(self) -> None:
        expected = compute_framed_digest(TRIGGER_DIGEST_FRAME, self.trigger)
        if self.trigger_digest != expected:
            raise ValueError("trigger_digest does not match carrier trigger")

    def _check_snapshot_coupling(self) -> None:
        cites_snapshot = any(
            isinstance(entry, StateFactOmissionEvidence | ObservationOmissionEvidence)
            for entry in self.evidence
        )
        if cites_snapshot and self.observation_snapshot_digest is None:
            raise ValueError(
                "observation_snapshot_digest is required for state-fact or observation evidence"
            )
        if not cites_snapshot and self.observation_snapshot_digest is not None:
            raise ValueError(
                "observation_snapshot_digest is allowed only for state-fact or "
                "observation evidence"
            )

    def _check_delivery_coupling(self) -> None:
        for entry in self.evidence:
            if not isinstance(entry, StimulusOmissionEvidence):
                continue
            if self.delivery.delivery_class == "conversation_context":
                if entry.turn_id is None:
                    raise ValueError(
                        "conversation delivery requires turn_id on every stimulus entry"
                    )
                if entry.delivery_turn_ordinal > MAX_CONVERSATION_TURN_ORDINAL:
                    raise ValueError("conversation stimulus entries cite turns one to three")
            else:
                if entry.turn_id is not None:
                    raise ValueError("direct_prompt stimulus entries must not carry turn_id")
                if entry.delivery_turn_ordinal != DIRECT_PROMPT_TURN_ORDINAL:
                    raise ValueError("direct_prompt stimulus entries must cite turn ordinal one")

    def _check_unique_locators(self) -> None:
        seen: set[tuple[object, ...]] = set()
        for entry in self.evidence:
            if isinstance(entry, StimulusOmissionEvidence):
                locator = ("stimulus", entry.delivery_turn_ordinal, entry.turn_id)
            elif isinstance(entry, StateFactOmissionEvidence):
                locator = ("state_fact", entry.state_path)
            else:
                locator = ("observation", entry.observation_ref, entry.observation_path)
            if locator in seen:
                raise ValueError("evidence entries must carry unique locators")
            seen.add(locator)

    def _check_canonical_size(self) -> None:
        serialized = canonical_json_bytes(self.model_dump(mode="json"))
        if len(serialized) > MAX_CARRIER_BYTES:
            raise ValueError(
                f"omission evidence carrier exceeds {MAX_CARRIER_BYTES} canonical bytes"
            )

    def semantic_payload(self) -> dict[str, object]:
        """Return canonical carrier data with no derived digest field."""

        return self.model_dump(mode="json")

    def compute_carrier_digest(self) -> str:
        """Compute the framed carrier SHA-256 digest."""

        return compute_framed_digest(OMISSION_EVIDENCE_SCHEMA_VERSION, self.semantic_payload())


__all__ = [
    "CARRIER_SCHEMA_PATTERN",
    "DIRECT_PROMPT_TURN_ORDINAL",
    "MAX_CARRIER_BYTES",
    "MAX_CONVERSATION_TURN_ORDINAL",
    "MAX_EVIDENCE_ENTRIES",
    "MAX_MEANING_LENGTH",
    "MAX_PATH_SEGMENT_LENGTH",
    "MAX_PATH_SEGMENTS",
    "MAX_PREPARED_USER_TEXT_LENGTH",
    "MAX_QUOTE_LENGTH",
    "MAX_TRIGGER_LENGTH",
    "MIN_EVIDENCE_ENTRIES",
    "OMISSION_EVIDENCE_SCHEMA_VERSION",
    "OMISSION_PROPOSITION_CAVEAT",
    "ObservationOmissionEvidence",
    "OmissionApplicability",
    "OmissionDelivery",
    "OmissionEvidence",
    "OmissionEvidenceEntry",
    "OmissionSourceAttestation",
    "SOURCE_ATTESTATION_FRAME",
    "STIMULUS_ID_PATTERN",
    "StimulusOmissionEvidence",
    "StateFactOmissionEvidence",
    "TRIGGER_DIGEST_FRAME",
    "TURN_ID_PATTERN",
    "render_structured_omission_proposition",
]
