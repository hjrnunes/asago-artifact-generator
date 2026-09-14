"""Cross-repository smoke coverage over committed producer bundle fixtures.

Each fixture directory under ``fixtures/crossrepo/`` is a complete,
self-consistent producer bundle.  The tests run the real public chain exactly
as the producer's cross-repository smoke driver does: bundle loading, case
resolution, default Garak runtime bindings, readiness planning, artifact
compilation, and public case/trace validation.  No provider client is
constructed and an autouse guard fails any attempt to open a network
connection.

Fixture provenance (sealed run bytes versus contract-kit derivatives) is
documented in ``tests/fixtures/crossrepo/PROVENANCE.md``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from asago_artifact_generator.bundle.loader import (
    BundleValidationError,
    load_execution_bundle,
)
from asago_artifact_generator.garak.capabilities import garak_capabilities
from asago_artifact_generator.garak.compile import compile_execution_artifact
from asago_artifact_generator.garak.conversation import (
    validate_conversation_case,
    validate_conversation_trace,
)
from asago_artifact_generator.garak.default_bindings import (
    complete_garak_runtime_bindings,
)
from asago_artifact_generator.models._base import (
    canonical_json_bytes,
    compute_framed_digest,
)
from asago_artifact_generator.models.execution_classification import (
    ExecutionTargetProfile,
)
from asago_artifact_generator.models.execution_intent import ExecutionIntent
from asago_artifact_generator.models.omission_evidence import (
    OMISSION_EVIDENCE_SCHEMA_VERSION,
)
from asago_artifact_generator.models.readiness import ReadyExecutionPlan
from asago_artifact_generator.planning.bind import bind_and_plan
from asago_artifact_generator.planning.resolve_case import (
    resolve_execution_case,
)

CROSSREPO_ROOT = Path(__file__).parent / "fixtures" / "crossrepo"
BUNDLE_V2_FRAME = "stpa-execution-bundle-v2"
PROJECTION_V3_FRAME = "stpa-execution-projection-v3"
CARRIER_FRAME = "stpa-omission-evidence-v1"
OMISSION_JUDGE_LABEL = "Structured omission evidence:"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    """Fail any network contact: the smoke chain must be fully offline."""

    def _deny(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cross-repo smoke must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)


@dataclass
class _ChainOutcome:
    """The compiled result of one fixture intent through the public chain."""

    intent: ExecutionIntent
    plan: ReadyExecutionPlan
    artifact: dict
    trace: dict
    case_errors: list[str]
    trace_errors: list[str]


def _load_fixture_profile(name: str) -> ExecutionTargetProfile:
    return ExecutionTargetProfile.model_validate_json(
        (CROSSREPO_ROOT / name / "execution-target-profile.json").read_text()
    )


def _run_chain(name: str) -> list[_ChainOutcome]:
    """Run load → resolve → default bindings → plan → compile → validate."""

    profile = _load_fixture_profile(name)
    verified = load_execution_bundle(CROSSREPO_ROOT / name / "execution-bundle.json")
    outcomes: list[_ChainOutcome] = []
    for intent in verified.intents:
        resolved = resolve_execution_case(intent, profile)
        case = resolved if type(resolved).__name__ == "BoundExecutionCase" else None
        assert case is not None, f"{name}/{intent.scenario_id} did not resolve to a bound case"
        bindings = complete_garak_runtime_bindings(resolved, target_profile=profile)
        readiness = bind_and_plan(resolved, bindings, garak_capabilities())
        assert readiness.plan is not None, (
            f"{name}/{intent.scenario_id} was not ready: "
            f"{[item.code for item in readiness.diagnostics]}"
        )
        compiled = compile_execution_artifact(readiness.plan)
        outcomes.append(
            _ChainOutcome(
                intent=intent,
                plan=readiness.plan,
                artifact=compiled.artifact,
                trace=compiled.trace,
                case_errors=list(
                    validate_conversation_case(compiled.artifact, readiness.plan, compiled.trace)
                ),
                trace_errors=list(
                    validate_conversation_trace(compiled.trace, readiness.plan, compiled.artifact)
                ),
            )
        )
    return outcomes


def _restamped_copy(
    tmp_path: Path,
    name: str,
    mutate: Callable[[dict], None],
    *,
    rehash_carrier: bool = False,
) -> Path:
    """Copy a fixture, mutate the projection, and refresh every relevant digest.

    A consumer that accepts any of these fully rehashed copies would be a
    failed semantic boundary: the bytes are internally self-consistent.
    """

    source = CROSSREPO_ROOT / name
    destination = tmp_path / name
    shutil.copytree(source, destination)
    # Every committed fixture holds exactly one entry, so mutating
    # ``entries[0]`` mutates the fixture's only projection.
    index_path = destination / "execution-bundle.json"
    index = json.loads(index_path.read_text())
    entry = index["entries"][0]
    projection_path = destination / entry["projection"]["path"]
    document = json.loads(projection_path.read_text())
    mutate(document)
    if rehash_carrier:
        outcome = document["unsafe_outcome"]
        outcome["omission_evidence_digest"] = compute_framed_digest(
            CARRIER_FRAME, outcome["omission_evidence"]
        )
    document["semantic_digest"] = compute_framed_digest(
        PROJECTION_V3_FRAME,
        {key: value for key, value in document.items() if key != "semantic_digest"},
    )
    projection_bytes = canonical_json_bytes(document)
    projection_path.write_bytes(projection_bytes)
    entry["projection"]["content_sha256"] = hashlib.sha256(projection_bytes).hexdigest()
    entry["projection"]["semantic_digest"] = document["semantic_digest"]
    index["bundle_digest"] = compute_framed_digest(
        BUNDLE_V2_FRAME, {key: value for key, value in index.items() if key != "bundle_digest"}
    )
    index_path.write_bytes(canonical_json_bytes(index))
    return destination


def _violation_codes(path: Path) -> set[str]:
    with pytest.raises(BundleValidationError) as error:
        load_execution_bundle(path / "execution-bundle.json")
    return {violation.code for violation in error.value.violations}


# ---------------------------------------------------------------------------
# Required coverage 1: structured event ordering on real Klarna bytes


def test_klarna_ordering_fixture_compiles_event_order_oracle() -> None:
    """SCN-007 compiles with a null proposition and exact ordering fidelity."""

    (outcome,) = _run_chain("klarna-ordering")

    assert outcome.intent.projection_schema_version == "stpa-execution-projection-v3"
    assert outcome.case_errors == []
    assert outcome.trace_errors == []
    condition = outcome.intent.unsafe_outcome.condition
    assert condition.type == "ordering"
    oracle = outcome.artifact["structured_oracle"]
    assert oracle["kind"] == "event_order"
    assert oracle["tool_name"] == "process_refund"
    assert oracle["reference_tool"] == condition.reference_tool == "lookup_order"
    assert oracle["field_path"] == f"arguments.{condition.reference_argument.property}"
    assert oracle["field_path"] == "arguments.order_id"
    assert oracle["comparison"] == condition.reference_argument.operator == "equals"
    assert oracle["expected"] == condition.reference_argument.expected == "ORD-104"
    assert oracle["relation"] == condition.relation == "before"
    assert oracle["semantic_proposition"] is None
    judge = outcome.artifact["judge_description"]
    assert repr("process_refund") in judge
    assert repr("lookup_order") in judge
    assert "arguments.order_id equals 'ORD-104'" in judge
    # The ordering direction survives: the reference call must precede.
    assert "no matching reference call precedes" in judge
    assert outcome.artifact["profile"]["delivery_class"] == "direct_prompt"


# ---------------------------------------------------------------------------
# Required coverage 2: omission evidence and source pins survive unchanged


def test_occiai_omission_fixture_carrier_survives_the_whole_chain() -> None:
    """The carrier reaches the compiled artifact byte-for-byte with its pins."""

    (outcome,) = _run_chain("occiai-omission")

    carrier = outcome.intent.unsafe_outcome.omission_evidence
    assert carrier is not None
    assert carrier.schema_version == OMISSION_EVIDENCE_SCHEMA_VERSION
    assert outcome.plan.omission_evidence == carrier
    assert outcome.artifact["structured_oracle"]["omission_evidence"] == carrier.model_dump(
        mode="json"
    )
    assert outcome.intent.unsafe_outcome.omission_evidence_digest == (
        carrier.compute_carrier_digest()
    )
    assert carrier.source_pins == dict(outcome.intent.trace_refs.source_pins)
    oracle = outcome.artifact["structured_oracle"]
    assert oracle["kind"] == "action_absence"
    assert oracle["tool_name"] == "escalate_to_clinician"
    assert outcome.case_errors == []
    assert outcome.trace_errors == []
    # The delivered user text is the prepared omission stimulus, verbatim.
    prepared = outcome.intent.stimulus_requirements[0].prepared_user_text
    assert [m["content"] for m in outcome.artifact["messages"] if m["role"] == "user"] == [
        prepared
    ]
    assert OMISSION_JUDGE_LABEL in outcome.artifact["judge_description"]
    assert carrier.trigger in outcome.artifact["judge_description"]


def test_occiai_omission_fixture_exclusion_retains_the_carrier() -> None:
    """Resolving without a profile keeps the carrier and its pins intact."""

    profile = _load_fixture_profile("occiai-omission")
    verified = load_execution_bundle(CROSSREPO_ROOT / "occiai-omission" / "execution-bundle.json")
    (intent,) = verified.intents

    resolution = resolve_execution_case(intent, None)

    assert resolution.code == "needs_environment_binding"
    assert resolution.intent.unsafe_outcome.omission_evidence == (
        intent.unsafe_outcome.omission_evidence
    )
    assert resolution.intent.trace_refs.source_pins == intent.trace_refs.source_pins
    assert profile.semantic_digest  # the committed profile stays digest-verifiable


# ---------------------------------------------------------------------------
# Required coverage 3: prepared user-history metadata survives compilation


def test_occiai_conversation_fixture_compiles_prepared_history_verbatim() -> None:
    """Prepared turns keep their exact roles, order, and text; no assistant reply."""

    (outcome,) = _run_chain("occiai-conversation")

    stimulus = outcome.intent.stimulus_requirements[0]
    assert stimulus.delivery_class == "conversation_context"
    turns = stimulus.turns
    assert [turn.turn_id for turn in turns] == ["T-1", "T-2"]
    expected_texts = [turn.text for turn in turns]
    assert outcome.case_errors == []
    assert outcome.trace_errors == []
    messages = outcome.artifact["messages"]
    assert [message["role"] for message in messages] == ["user", "user"]
    assert [message["content"] for message in messages] == expected_texts
    history = outcome.artifact["supplied_history"]
    assert history["kind"] == "user_only"
    assert [item["turn_id"] for item in history["user_turns"]] == ["T-1", "T-2"]
    assert [item["text"] for item in history["user_turns"]] == expected_texts
    # The omission carrier rides beside the prepared history unchanged.
    carrier = outcome.intent.unsafe_outcome.omission_evidence
    assert carrier is not None
    assert outcome.artifact["structured_oracle"]["omission_evidence"] == carrier.model_dump(
        mode="json"
    )


# ---------------------------------------------------------------------------
# Required coverage 4: propositions required by their observer kind


def test_occiai_output_text_fixture_compiles_the_required_proposition() -> None:
    """The output-text oracle carries the producer proposition to the judge."""

    (outcome,) = _run_chain("occiai-output-text")

    proposition = outcome.intent.unsafe_outcome.semantic_proposition
    assert proposition
    oracle = outcome.artifact["structured_oracle"]
    assert oracle["kind"] == "output_text"
    assert oracle["semantic_proposition"] == proposition
    assert proposition in outcome.artifact["judge_description"]
    assert outcome.case_errors == []
    assert outcome.trace_errors == []


def _strip_proposition(document: dict) -> None:
    document["unsafe_outcome"]["semantic_proposition"] = None


def _invert_omission_direction(document: dict) -> None:
    proposition = document["unsafe_outcome"]["semantic_proposition"]
    document["unsafe_outcome"]["semantic_proposition"] = proposition.replace(
        "is not called", "is called"
    )


def _swap_carrier_source_pin(document: dict) -> None:
    document["unsafe_outcome"]["omission_evidence"]["source_pins"]["control_structure"] = "0" * 64


# ---------------------------------------------------------------------------
# Required coverage 5: malformed or mismatched evidence fails typed


def test_missing_required_semantic_proposition_fails_typed(tmp_path: Path) -> None:
    """A rehashed output-text projection without its proposition is rejected."""

    copied = _restamped_copy(tmp_path, "occiai-output-text", _strip_proposition)

    assert _violation_codes(copied) == {"condition_value_invalid"}


def test_inverted_omission_direction_fails_typed(tmp_path: Path) -> None:
    """Reversing the omission direction against the carrier is rejected."""

    copied = _restamped_copy(tmp_path, "occiai-omission", _invert_omission_direction)

    assert "omission_proposition_mismatch" in _violation_codes(copied)


def test_mismatched_carrier_source_pin_fails_typed(tmp_path: Path) -> None:
    """A carrier pin that diverges from the projection pins is rejected."""

    copied = _restamped_copy(
        tmp_path, "occiai-omission", _swap_carrier_source_pin, rehash_carrier=True
    )

    assert _violation_codes(copied) == {"source_pin_mismatch"}
