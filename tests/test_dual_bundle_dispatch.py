"""Dual bundle-v1/bundle-v2 dispatch acceptance tests for the strict loader.

The committed producer kit fixtures are the only inputs: no new fixture files.
Legacy bundle-v1/projection-v2 loading stays byte-for-byte unchanged; the
bundle-v2 generation requires projection-v3 entries and carries the closed
``stpa-omission-evidence-v1`` carrier through the intent and readiness.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from asago_artifact_generator.bundle.loader import (
    BundleValidationError,
    _validate_projection,
    load_execution_bundle,
)
from asago_artifact_generator.models._base import canonical_json_bytes, compute_framed_digest
from asago_artifact_generator.models.execution_classification import (
    ExecutionClassification,
    ExecutionResourceKind,
    ExecutionSurface,
    ExecutionTargetProfile,
    ProfileBasis,
    SemanticAuthority,
    SimulationBehavior,
    SourceProtocol,
    TargetProfileOperation,
    TargetProfileResource,
)
from asago_artifact_generator.models.execution_intent import ExecutionIntent, UnsafeOutcome
from asago_artifact_generator.models.omission_evidence import OMISSION_EVIDENCE_SCHEMA_VERSION
from asago_artifact_generator.models.readiness import PlatformCapabilities
from asago_artifact_generator.models.runtime_binding import (
    AdversarialStimulusBinding,
    ControlActionBinding,
    ObservationBinding,
    ReviewEvidence,
    RuntimeBindingSet,
    SurfaceBinding,
)
from asago_artifact_generator.planning.bind import bind_and_plan
from asago_artifact_generator.planning.resolve_case import resolve_execution_case

CONTRACT_ROOT = Path(__file__).parents[1] / "contracts" / "stpa-execution"
BUNDLE_V2_VALID = CONTRACT_ROOT / "bundle-v2" / "valid" / "minimal-run"
BUNDLE_V1_VALID = CONTRACT_ROOT / "bundle-v1" / "valid" / "minimal-run"
CARRIER_FRAME = "stpa-omission-evidence-v1"
PROJECTION_V3_FRAME = "stpa-execution-projection-v3"
BUNDLE_V2_FRAME = "stpa-execution-bundle-v2"


def _committed_v3_projection() -> dict[str, Any]:
    return json.loads(
        (BUNDLE_V2_VALID / "scenarios" / "canonical" / "SCN-001.projection.json").read_text()
    )


def _committed_v3_index() -> dict[str, Any]:
    return json.loads((BUNDLE_V2_VALID / "execution-bundle.json").read_text())


def _write_bundle_copy(
    tmp_path: Path,
    projection: dict[str, Any],
    *,
    scenario_bytes: bytes | None = None,
) -> Path:
    """Publish one projection as a self-consistent canonical bundle-v2 run."""

    run_dir = tmp_path / "run"
    scenario_dir = run_dir / "scenarios"
    canonical_dir = scenario_dir / "canonical"
    canonical_dir.mkdir(parents=True)
    if scenario_bytes is None:
        scenario_bytes = (BUNDLE_V2_VALID / "scenarios" / "SCN-001.scenario.json").read_bytes()
    (scenario_dir / "SCN-001.scenario.json").write_bytes(scenario_bytes)
    projection_bytes = canonical_json_bytes(projection)
    (canonical_dir / "SCN-001.projection.json").write_bytes(projection_bytes)

    index = _committed_v3_index()
    entry = index["entries"][0]
    entry["scenario"]["content_sha256"] = hashlib.sha256(scenario_bytes).hexdigest()
    entry["projection"]["content_sha256"] = hashlib.sha256(projection_bytes).hexdigest()
    entry["projection"]["semantic_digest"] = projection["semantic_digest"]
    index["bundle_digest"] = compute_framed_digest(
        BUNDLE_V2_FRAME,
        {key: value for key, value in index.items() if key != "bundle_digest"},
    )
    path = run_dir / "execution-bundle.json"
    path.write_bytes(canonical_json_bytes(index))
    return path


def _tampered_bundle(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    *,
    projection_name: str | None = None,
    fix_carrier_digest: bool = True,
) -> Path:
    """Copy a committed valid fixture, mutate it, and republish self-consistently."""

    if projection_name is None:
        document = _committed_v3_projection()
    else:
        document = json.loads(
            (CONTRACT_ROOT / "projection-v3" / "valid" / projection_name).read_text()
        )
    mutate(document)
    outcome = document["unsafe_outcome"]
    if fix_carrier_digest and outcome.get("omission_evidence") is not None:
        outcome["omission_evidence_digest"] = compute_framed_digest(
            CARRIER_FRAME, outcome["omission_evidence"]
        )
    document["semantic_digest"] = compute_framed_digest(
        PROJECTION_V3_FRAME,
        {key: value for key, value in document.items() if key != "semantic_digest"},
    )
    return _write_bundle_copy(tmp_path, document)


def _load_v2_intent(fixture_name: str) -> ExecutionIntent:
    projection_bytes = (CONTRACT_ROOT / "projection-v3" / "valid" / fixture_name).read_bytes()
    return ExecutionIntent.from_projection(
        json.loads(projection_bytes),
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256=hashlib.sha256(projection_bytes).hexdigest(),
        bundle_schema_version="stpa-execution-bundle-v2",
    )


# ---------------------------------------------------------------------------
# Committed fixtures load through both generations


def test_committed_bundle_v1_fixture_loads_unchanged() -> None:
    verified = load_execution_bundle(BUNDLE_V1_VALID / "execution-bundle.json")
    intent = verified.intent

    assert verified.schema_version == "stpa-execution-bundle-v1"
    assert intent.bundle_schema_version == "stpa-execution-bundle-v1"
    assert intent.projection_schema_version == "stpa-execution-projection-v2"
    assert intent.unsafe_outcome.omission_evidence is None
    assert intent.unsafe_outcome.omission_evidence_digest is None


def test_committed_bundle_v2_fixture_loads_with_carrier_through_intent() -> None:
    verified = load_execution_bundle(BUNDLE_V2_VALID / "execution-bundle.json")
    intent = verified.intent
    outcome = intent.unsafe_outcome
    document = _committed_v3_projection()
    document_outcome = document["unsafe_outcome"]

    assert verified.schema_version == "stpa-execution-bundle-v2"
    assert intent.bundle_schema_version == "stpa-execution-bundle-v2"
    assert intent.projection_schema_version == "stpa-execution-projection-v3"
    assert outcome.omission_evidence is not None
    assert outcome.omission_evidence.schema_version == OMISSION_EVIDENCE_SCHEMA_VERSION
    assert outcome.omission_evidence_digest == (outcome.omission_evidence.compute_carrier_digest())
    assert (
        outcome.omission_evidence.model_dump(mode="json")
        == (document_outcome["omission_evidence"])
    )
    assert outcome.omission_evidence.source_pins == dict(intent.trace_refs.source_pins)
    assert intent.stimulus_requirements[0].prepared_user_text is not None


@pytest.mark.parametrize(
    "fixture_name",
    ("structured-omission-direct.json", "structured-omission-conversation.json"),
)
def test_committed_v3_projections_normalize_to_intents(fixture_name: str) -> None:
    intent = _load_v2_intent(fixture_name)

    assert intent.projection_schema_version == "stpa-execution-projection-v3"
    assert intent.bundle_schema_version == "stpa-execution-bundle-v2"
    assert intent.unsafe_outcome.omission_evidence is not None


@pytest.mark.parametrize("fixture_name", ("plain-non-omission.json",))
def test_committed_v3_plain_projection_stays_carrier_free(fixture_name: str) -> None:
    intent = _load_v2_intent(fixture_name)

    assert intent.unsafe_outcome.omission_evidence is None
    assert intent.unsafe_outcome.omission_evidence_digest is None


# ---------------------------------------------------------------------------
# Committed invalid fixtures fail with their recorded codes


@pytest.mark.parametrize("fixture_name", ("hash-mismatch", "pair-mismatch"))
def test_bundle_v2_invalid_fixtures_fail_with_recorded_codes(fixture_name: str) -> None:
    path = CONTRACT_ROOT / "bundle-v2" / "invalid" / fixture_name / "execution-bundle.json"
    expected_codes = json.loads(
        (CONTRACT_ROOT / "bundle-v2" / "expected-violations.json").read_text()
    )[f"invalid/{fixture_name}"]

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(path)

    actual_codes = {violation.code for violation in error.value.violations}
    assert set(expected_codes) <= actual_codes


def test_bundle_v2_hash_mismatch_fails_with_the_recorded_sequence() -> None:
    path = CONTRACT_ROOT / "bundle-v2" / "invalid" / "hash-mismatch" / "execution-bundle.json"
    expected_codes = json.loads(
        (CONTRACT_ROOT / "bundle-v2" / "expected-violations.json").read_text()
    )["invalid/hash-mismatch"]

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(path)

    assert [violation.code for violation in error.value.violations] == expected_codes


@pytest.mark.parametrize(
    "fixture_name",
    (
        "carrier-digest-mismatch.json",
        "carrier-field-invalid.json",
        "carrier-on-non-omission.json",
        "omission-evidence-missing.json",
        "prepared-text-missing.json",
        "unknown-carrier-field.json",
    ),
)
def test_projection_v3_invalid_fixtures_fail_with_recorded_codes(fixture_name: str) -> None:
    document = json.loads((CONTRACT_ROOT / "projection-v3" / "invalid" / fixture_name).read_text())
    expected_codes = json.loads(
        (CONTRACT_ROOT / "projection-v3" / "expected-violations.json").read_text()
    )[fixture_name]

    actual_codes = {violation.code for violation in _validate_projection(document)}

    assert set(expected_codes) <= actual_codes


# ---------------------------------------------------------------------------
# Carrier tampering fails closed through the full bundle transaction


def test_unknown_carrier_field_is_rejected(tmp_path: Path) -> None:
    def add_field(document: dict[str, Any]) -> None:
        document["unsafe_outcome"]["omission_evidence"]["note"] = "x"

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, add_field))

    codes = {violation.code for violation in error.value.violations}
    assert "unexpected_field" in codes


def test_missing_carrier_on_v3_action_presence_outcome_is_rejected(tmp_path: Path) -> None:
    def drop_carrier(document: dict[str, Any]) -> None:
        outcome = document["unsafe_outcome"]
        outcome.pop("omission_evidence")
        outcome.pop("omission_evidence_digest")

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, drop_carrier))

    codes = {violation.code for violation in error.value.violations}
    assert "omission_evidence_missing" in codes


def test_carrier_trigger_tampering_fails_closed(tmp_path: Path) -> None:
    def reword_trigger(document: dict[str, Any]) -> None:
        carrier = document["unsafe_outcome"]["omission_evidence"]
        carrier["trigger"] = f"{carrier['trigger']} Tampered."

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, reword_trigger))

    codes = {violation.code for violation in error.value.violations}
    assert "omission_evidence_invalid" in codes


def test_carrier_digest_tampering_fails_closed(tmp_path: Path) -> None:
    def forge_digest(document: dict[str, Any]) -> None:
        document["unsafe_outcome"]["omission_evidence_digest"] = "0" * 64

    bundle = _tampered_bundle(tmp_path, forge_digest, fix_carrier_digest=False)

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(bundle)

    codes = {violation.code for violation in error.value.violations}
    assert "omission_evidence_digest_mismatch" in codes


def test_carrier_source_pin_tampering_fails_closed(tmp_path: Path) -> None:
    def swap_pin(document: dict[str, Any]) -> None:
        carrier = document["unsafe_outcome"]["omission_evidence"]
        carrier["source_pins"]["control_structure"] = "f" * 64

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, swap_pin))

    codes = {violation.code for violation in error.value.violations}
    assert "source_pin_mismatch" in codes


def test_prepared_text_tampering_fails_closed(tmp_path: Path) -> None:
    def rewrite_prepared_text(document: dict[str, Any]) -> None:
        document["stimulus_requirements"][0]["prepared_user_text"] = (
            "A completely different user turn that never happened."
        )

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, rewrite_prepared_text))

    codes = {violation.code for violation in error.value.violations}
    assert "prepared_text_mismatch" in codes


def test_prepared_text_on_a_conversation_stimulus_fails_closed(tmp_path: Path) -> None:
    """The loader reports the producer's settled code for the exclusivity rule."""

    def inject_prepared_text(document: dict[str, Any]) -> None:
        document["stimulus_requirements"][0]["prepared_user_text"] = "Injected."

    bundle = _tampered_bundle(
        tmp_path,
        inject_prepared_text,
        projection_name="structured-omission-conversation.json",
    )

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(bundle)

    codes = {violation.code for violation in error.value.violations}
    assert "prepared_text_mismatch" in codes


def test_carrier_stimulus_id_mismatch_fails_closed(tmp_path: Path) -> None:
    def rename_delivery(document: dict[str, Any]) -> None:
        carrier = document["unsafe_outcome"]["omission_evidence"]
        carrier["delivery"]["stimulus_id"] = "STIM-9"

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, rename_delivery))

    codes = {violation.code for violation in error.value.violations}
    assert "stimulus_delivery_mismatch" in codes


def test_carrier_delivery_class_mismatch_fails_closed(tmp_path: Path) -> None:
    def swap_published_class(document: dict[str, Any]) -> None:
        document["stimulus_requirements"][0]["delivery_class"] = "conversation_context"

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(_tampered_bundle(tmp_path, swap_published_class))

    codes = {violation.code for violation in error.value.violations}
    assert "stimulus_delivery_mismatch" in codes


def test_carrier_turn_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    def point_at_the_wrong_turn(document: dict[str, Any]) -> None:
        evidence = document["unsafe_outcome"]["omission_evidence"]["evidence"]
        evidence[0]["turn_id"] = "T-1"

    bundle = _tampered_bundle(
        tmp_path,
        point_at_the_wrong_turn,
        projection_name="structured-omission-conversation.json",
    )

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(bundle)

    codes = {violation.code for violation in error.value.violations}
    assert "stimulus_delivery_mismatch" in codes


# ---------------------------------------------------------------------------
# Bundle generation homogeneity


BUNDLE_V1_FRAME = "stpa-execution-bundle-v1"


def _mixed_generation_bundle(tmp_path: Path, *, v3_document: bool) -> Path:
    """Publish one bundle whose projection is the other generation.

    Every recorded digest is retargeted to the swapped document so the run
    is self-consistent in every dimension except the projection generation.
    """
    source_root = BUNDLE_V2_VALID if v3_document else BUNDLE_V1_VALID
    target_root = BUNDLE_V1_VALID if v3_document else BUNDLE_V2_VALID
    document = json.loads(
        (source_root / "scenarios" / "canonical" / "SCN-001.projection.json").read_text()
    )

    run_dir = tmp_path / "run"
    canonical_dir = run_dir / "scenarios" / "canonical"
    canonical_dir.mkdir(parents=True)
    (run_dir / "scenarios" / "SCN-001.scenario.json").write_bytes(
        (target_root / "scenarios" / "SCN-001.scenario.json").read_bytes()
    )
    document_bytes = canonical_json_bytes(document)
    (canonical_dir / "SCN-001.projection.json").write_bytes(document_bytes)

    index = json.loads((target_root / "execution-bundle.json").read_text())
    entry = index["entries"][0]
    entry["scenario"]["content_sha256"] = hashlib.sha256(
        (target_root / "scenarios" / "SCN-001.scenario.json").read_bytes()
    ).hexdigest()
    entry["projection"]["content_sha256"] = hashlib.sha256(document_bytes).hexdigest()
    entry["projection"]["semantic_digest"] = document["semantic_digest"]
    frame = BUNDLE_V1_FRAME if v3_document else BUNDLE_V2_FRAME
    index["bundle_digest"] = compute_framed_digest(
        frame,
        {key: value for key, value in index.items() if key != "bundle_digest"},
    )
    path = run_dir / "execution-bundle.json"
    path.write_bytes(canonical_json_bytes(index))
    return path


def test_loader_rejects_a_v3_projection_under_a_bundle_v1_index(tmp_path: Path) -> None:
    bundle = _mixed_generation_bundle(tmp_path, v3_document=True)

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(bundle)

    codes = {violation.code for violation in error.value.violations}
    assert "schema_version_mismatch" in codes


def test_loader_rejects_a_v2_projection_under_a_bundle_v2_index(tmp_path: Path) -> None:
    bundle = _mixed_generation_bundle(tmp_path, v3_document=False)

    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(bundle)

    codes = {violation.code for violation in error.value.violations}
    assert "schema_version_mismatch" in codes


# ---------------------------------------------------------------------------
# Strict-model pairing and carrier coupling


def test_intent_rejects_mismatched_bundle_pairing() -> None:
    document = _committed_v3_projection()
    with pytest.raises(ValueError, match="cannot pair"):
        ExecutionIntent.from_projection(
            document,
            bundle_digest="0" * 64,
            scenario_content_sha256="1" * 64,
            projection_content_sha256="2" * 64,
            bundle_schema_version="stpa-execution-bundle-v1",
        )
    intent = _load_v2_intent("structured-omission-direct.json")
    payload = intent.model_dump(mode="json")
    payload["bundle_schema_version"] = "stpa-execution-bundle-v1"
    with pytest.raises(ValidationError, match="cannot pair"):
        ExecutionIntent.model_validate(payload)


def test_intent_derives_the_bundle_version_from_the_projection() -> None:
    document = _committed_v3_projection()
    intent = ExecutionIntent.from_projection(
        document,
        bundle_digest="0" * 64,
        scenario_content_sha256="1" * 64,
        projection_content_sha256="2" * 64,
    )

    assert intent.bundle_schema_version == "stpa-execution-bundle-v2"


def test_v2_intent_rejects_v3_only_carrier_fields() -> None:
    intent = _load_v2_intent("structured-omission-direct.json")
    payload = intent.model_dump(mode="json")
    payload["bundle_schema_version"] = "stpa-execution-bundle-v1"
    payload["projection_schema_version"] = "stpa-execution-projection-v2"

    with pytest.raises(ValidationError, match="omission_evidence requires the projection-v3"):
        ExecutionIntent.model_validate(payload)

    stimulus_payload = intent.model_dump(mode="json")
    outcome = stimulus_payload["unsafe_outcome"]
    outcome.pop("omission_evidence")
    outcome.pop("omission_evidence_digest")
    stimulus_payload["bundle_schema_version"] = "stpa-execution-bundle-v1"
    stimulus_payload["projection_schema_version"] = "stpa-execution-projection-v2"
    stimulus_payload["stimulus_requirements"][0]["prepared_user_text"] = "prepared text"
    with pytest.raises(ValidationError, match="prepared_user_text requires the projection-v3"):
        ExecutionIntent.model_validate(stimulus_payload)


def test_unsafe_outcome_rejects_a_carrier_digest_mismatch() -> None:
    intent = _load_v2_intent("structured-omission-direct.json")
    payload = intent.model_dump(mode="json")
    payload["unsafe_outcome"]["omission_evidence_digest"] = "0" * 64

    with pytest.raises(ValidationError, match="does not match the carrier content"):
        UnsafeOutcome.model_validate(payload["unsafe_outcome"])


def test_unsafe_outcome_rejects_a_carrier_outside_action_presence() -> None:
    intent = _load_v2_intent("structured-omission-direct.json")
    payload = intent.model_dump(mode="json")
    outcome = payload["unsafe_outcome"]
    carrier = outcome["omission_evidence"]
    tampered = {
        **outcome,
        "uca_type": "INCORRECT",
        "control_action_id": "CM-1",
        "condition": {
            "type": "action_value",
            "control_action_id": "CM-1",
            "property": "semantic_proposition",
            "operator": "equals",
            "expected": True,
        },
    }
    del tampered["omission_evidence_digest"]
    tampered["omission_evidence"] = carrier

    with pytest.raises(ValidationError, match="only on action-presence outcomes"):
        UnsafeOutcome.model_validate(tampered)


# ---------------------------------------------------------------------------
# The carrier survives case resolution and readiness untouched


def _simulation_profile() -> ExecutionTargetProfile:
    schema = {
        "type": "object",
        "properties": {"patient_id": {"type": "string"}, "note": {"type": "string"}},
        "required": ["patient_id", "note"],
    }
    resource = TargetProfileResource(
        resource_id="sim:fixture:escalate",
        resource_kind=ExecutionResourceKind.tool,
        target_id="sim-fixture",
        tool_name="escalate_to_clinician",
        role_ids=("target_control_action",),
        structural_refs=("CA-1-1",),
        surfaces=(ExecutionSurface.tool_call, ExecutionSurface.tool_result),
        operations=(
            TargetProfileOperation(
                operation_id="escalate_to_clinician",
                semantic_operation="escalate_to_clinician",
                argument_names=("patient_id", "note"),
            ),
        ),
        evidence_refs=("sim:fixture:escalate",),
        input_schema=schema,
        argument_names=("patient_id", "note"),
        simulation_behavior=SimulationBehavior(
            inputs={"patient_id": "string"},
            outputs={"status": "string"},
            observation_points=("trace.tool_calls",),
        ),
    )
    return ExecutionTargetProfile(
        target_id="sim-fixture",
        authorization_scope_id="sim-scope",
        basis=ProfileBasis.simulation,
        semantic_authority=SemanticAuthority.reviewed,
        source_protocol=SourceProtocol.simulation,
        resources=(resource,),
    )


def _v2_bindings(intent: ExecutionIntent) -> RuntimeBindingSet:
    review = ReviewEvidence(
        reviewed_by="operator@example",
        reviewed_at="2026-09-02T12:00:00Z",
        rationale="test binding",
        evidence_refs=("change-1",),
    )
    return RuntimeBindingSet.create(
        binding_set_id="BIND-1",
        projection_semantic_digest=intent.projection_semantic_digest,
        target_environment_id="sim-fixture",
        review=review,
        semantic_bindings=(),
        surface_bindings=[
            SurfaceBinding(
                source_ref="PM-1-1",
                surface="user_turn",
                locator="conversation.input",
                writable=True,
            ),
            SurfaceBinding(
                source_ref="CA-1-1",
                surface="tool_call",
                locator="trace.tool_calls",
                writable=True,
            ),
        ],
        control_action_bindings=[
            ControlActionBinding(
                control_action_id="CA-1-1",
                adapter_operation="tool_call",
                tool_name="escalate_to_clinician",
                tool_schema={"type": "object"},
                safe_defaults={},
            )
        ],
        observation_bindings=[
            ObservationBinding(
                condition_ref="OUTCOME-1",
                observer_kind="tool_call",
                event_source="trace.tool_calls",
                semantic_property="escalate_to_clinician",
                comparison="equals",
                expected_from="projection",
            )
        ],
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
        clock_binding=None,
    )


def _garak_capabilities() -> PlatformCapabilities:
    return PlatformCapabilities(
        platform="garak",
        adapter_version="garak-stpa-v1",
        writable_surfaces=("user_turn", "tool_call"),
        invocable_operations=("tool_call", "chat_completion"),
        observer_kinds=("tool_call",),
    )


def test_committed_v3_case_resolves_and_readiness_carries_the_carrier() -> None:
    bundle = load_execution_bundle(BUNDLE_V2_VALID / "execution-bundle.json")
    intent = bundle.intent
    profile = _simulation_profile()
    source_classification = intent.execution_classification
    # A fresh constructor derives the classification digest over the new pin.
    classification = ExecutionClassification(
        binding_completeness=source_classification.binding_completeness,
        environment_basis=source_classification.environment_basis,
        profile_fit=source_classification.profile_fit,
        claim_scope=source_classification.claim_scope,
        unresolved_requirement_ids=source_classification.unresolved_requirement_ids,
        diagnostics=source_classification.diagnostics,
        target_profile_digest=profile.semantic_digest,
    )
    intent = intent.model_copy(update={"execution_classification": classification})

    resolution = resolve_execution_case(intent, profile)
    assert type(resolution).__name__ == "BoundExecutionCase"

    result = bind_and_plan(resolution, _v2_bindings(intent), _garak_capabilities())
    assert result.overall == "ready"
    plan = result.plan
    assert plan is not None
    assert plan.bundle_schema_version == "stpa-execution-bundle-v2"
    assert plan.projection_schema_version == "stpa-execution-projection-v3"
    assert plan.omission_evidence == intent.unsafe_outcome.omission_evidence
    assert plan.omission_evidence_digest == intent.unsafe_outcome.omission_evidence_digest
    assert plan.omission_evidence is not None
    assert plan.omission_evidence.compute_carrier_digest() == plan.omission_evidence_digest


def test_committed_v3_case_exclusion_retains_the_carrier() -> None:
    bundle = load_execution_bundle(BUNDLE_V2_VALID / "execution-bundle.json")
    intent = bundle.intent

    resolution = resolve_execution_case(intent, None)

    assert resolution.code == "needs_environment_binding"
    assert resolution.intent.unsafe_outcome.omission_evidence == (
        intent.unsafe_outcome.omission_evidence
    )
