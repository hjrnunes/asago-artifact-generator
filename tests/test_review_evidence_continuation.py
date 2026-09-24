from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from asago_artifact_generator.authoring import (
    PLAN_FIELD_MEANINGS,
    PLAN_REVIEW_PROMPT_VERSION_V2,
    PLAN_REVIEW_PROMPT_VERSION_V3,
    PLAN_REVIEW_PROMPT_VERSION_V4,
    AuthoringOrchestrator,
    AuthoringPolicy,
    ScriptedAuthoringTransport,
    build_plan_review_packet,
    load_failure_evidence,
    prepare_saved_plan_continuation_v2,
)
from asago_artifact_generator.package_io import load_package

from .test_stage_local_orchestration import (
    _framed,
    _inventory,
    _plan,
    _review,
    _runtime_contract,
    _view,
)


def _provenance(plan: dict, view, inventory: dict, runtime: dict) -> dict[str, str]:
    def canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    meaning = {
        key: plan.get(key)
        for key in (
            "interpretation",
            "selected_evidence",
            "assumptions",
            "stimulus_approach",
            "observation_claim",
            "required_observations",
            "semantic_judge",
            "unresolved_requirements",
        )
    }
    return {
        "input_sha256": view.source_sha256,
        "inventory_sha256": hashlib.sha256(canonical(inventory)).hexdigest(),
        "runtime_contract_sha256": hashlib.sha256(canonical(runtime)).hexdigest(),
        "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
        "meaning_sha256": hashlib.sha256(canonical(meaning)).hexdigest(),
        "wire_version": "v2",
    }


def _accepted_review_evidence(packet, plan: dict) -> dict:
    input_digest = hashlib.sha256(
        json.dumps(
            {
                "version": packet.version,
                "system": packet.system,
                "payload": {
                    key: value for key, value in packet.payload.items() if key != "candidate_plan"
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    candidate_digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    review_evidence = {
        "stage": "plan",
        "status": "accepted",
        "decision": "accept",
        "summary": "scripted accepted authority",
        "prompt_version": packet.version,
        "prompt_sha256": packet.sha256,
        "reviewed_input_sha256": input_digest,
        "reviewed_candidate_sha256": candidate_digest,
        "candidate_bytes_sha256": candidate_digest,
        "effective_controls": {
            "review_model_profile": None,
            "temperature": 0,
            "max_retries": 0,
        },
        "contract_sha256": hashlib.sha256(
            json.dumps(
                packet.payload["response_contract"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    review_evidence["configuration_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "controls": review_evidence["effective_controls"],
                "policy": {
                    "plan_max_corrections": 1,
                    "artifact_max_corrections": 1,
                    "review_plan": True,
                    "review_artifact": False,
                    "review_model_profile": None,
                    "review_temperature": 0,
                    "max_retries": 0,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return review_evidence


def test_package_round_trip_preserves_review_evidence_and_terminal_failure_sidecar(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _review(), _framed(), _review()])
    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="evidence-package",
        wire_version="v2",
        policy=AuthoringPolicy(),
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    loaded = load_package(tmp_path / "package")
    review_member = json.loads(loaded.members["authoring/reviews.json"])
    assert review_member["plan"]["status"] == "accepted"
    assert review_member["artifact"]["status"] == "accepted"
    assert review_member["plan"]["reviewed_candidate_sha256"]
    assert review_member["plan"]["reviewed_input_sha256"]
    assert review_member["plan"]["prompt_sha256"] == result.ledger[1]["prompt_sha256"]
    assert review_member["plan"]["effective_controls"]["temperature"] == 0
    assert review_member["artifact"]["candidate_bytes_sha256"]
    assert loaded.manifest.authoring["review_status"] == {
        "plan": "accepted",
        "artifact": "accepted",
    }
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["status"] == "accepted"
    assert evidence["review_status"] == {
        "plan": "accepted",
        "artifact": "accepted",
    }
    assert all("terminal_status" in attempt for attempt in evidence["attempts"])


def test_malformed_review_keeps_raw_bytes_and_review_state_in_terminal_evidence(
    tmp_path: Path,
) -> None:
    raw_review = b'{"decision":"accept","summary":"contradiction","findings":[{}]}'
    result = AuthoringOrchestrator(
        transport=ScriptedAuthoringTransport([json.dumps(_plan()), raw_review]),
        package_dir=tmp_path / "package",
        task_id="review-failure-evidence",
        wire_version="v2",
        policy=AuthoringPolicy(),
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "review_unavailable"
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    attempt = evidence["attempts"][-1]
    assert attempt["stage"] == "plan_review"
    assert attempt["raw_response"]["sha256"] == hashlib.sha256(raw_review).hexdigest()
    assert attempt["review"]["status"] == "unavailable"
    assert evidence["review_status"]["plan"] == "unavailable"
    assert all("terminal_status" in item for item in evidence["attempts"])


def test_exact_saved_review_reuse_skips_plan_review_dispatch(tmp_path: Path) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    review_packet = build_plan_review_packet(view, plan, inventory, runtime)
    review_evidence = _accepted_review_evidence(review_packet, plan)
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=_provenance(plan, view, inventory, runtime),
        review_evidence=review_evidence,
        policy=AuthoringPolicy(review_artifact=False),
    )

    assert prepared.decision.mode == "call2_only"
    assert prepared.decision.review_reused is True
    assert prepared.call2_packet is not None
    assert prepared.call2_packet.user.count(PLAN_FIELD_MEANINGS) == 1
    assert prepared.plan_review_packet is not None
    assert prepared.plan_review_packet.user.count(PLAN_FIELD_MEANINGS) == 1
    assert prepared.plan_review_packet.version == PLAN_REVIEW_PROMPT_VERSION_V4
    transport = ScriptedAuthoringTransport([_framed()])
    result = prepared.run(
        transport_factory=lambda: transport,
        package_dir=tmp_path / "continued",
        task_id="saved-review-reuse",
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["call2"]
    assert result.review_reuse == {"plan": "reused", "artifact": "not_requested"}
    assert result.package is not None
    continuation = result.package.manifest.authoring["continuation"]
    assert continuation["review_reuse"]["plan"] == "reused"


def test_accepted_v2_saved_review_reuse_keeps_sealed_packet(tmp_path: Path) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    v2_packet = build_plan_review_packet(
        view,
        plan,
        inventory,
        runtime,
        sealed_version=PLAN_REVIEW_PROMPT_VERSION_V2,
    )
    review_evidence = _accepted_review_evidence(v2_packet, plan)
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=_provenance(plan, view, inventory, runtime),
        review_evidence=review_evidence,
        policy=AuthoringPolicy(review_artifact=False),
    )

    assert prepared.decision.mode == "call2_only"
    assert prepared.decision.review_reused is True
    assert prepared.plan_review_packet is not None
    assert prepared.plan_review_packet.version == PLAN_REVIEW_PROMPT_VERSION_V2
    transport = ScriptedAuthoringTransport([_framed()])
    result = prepared.run(
        transport_factory=lambda: transport,
        package_dir=tmp_path / "continued-v2",
        task_id="saved-review-v2-reuse",
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["call2"]


def test_accepted_v3_saved_review_reuse_keeps_sealed_packet(tmp_path: Path) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    v3_packet = build_plan_review_packet(
        view,
        plan,
        inventory,
        runtime,
        sealed_version=PLAN_REVIEW_PROMPT_VERSION_V3,
    )
    review_evidence = _accepted_review_evidence(v3_packet, plan)
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=_provenance(plan, view, inventory, runtime),
        review_evidence=review_evidence,
        policy=AuthoringPolicy(review_artifact=False),
    )

    assert prepared.decision.mode == "call2_only"
    assert prepared.decision.review_reused is True
    assert prepared.plan_review_packet is not None
    assert prepared.plan_review_packet.version == PLAN_REVIEW_PROMPT_VERSION_V3
    transport = ScriptedAuthoringTransport([_framed()])
    result = prepared.run(
        transport_factory=lambda: transport,
        package_dir=tmp_path / "continued-v3",
        task_id="saved-review-v3-reuse",
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["call2"]


def test_saved_continuation_correction_keeps_shared_meanings_in_dispatched_packet(
    tmp_path: Path,
) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    provenance = _provenance(plan, view, inventory, runtime)
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=provenance,
        meaning_review={
            "status": "passed",
            "meaning_sha256": provenance["meaning_sha256"],
        },
        policy=AuthoringPolicy(
            artifact_max_corrections=1,
            review_plan=False,
            review_artifact=False,
        ),
    )

    transport = ScriptedAuthoringTransport([b"not a Call 2 response", _framed()])
    result = prepared.run(
        transport_factory=lambda: transport,
        package_dir=tmp_path / "continued-correction",
        task_id="saved-correction-guidance",
    )

    assert result.status == "accepted"
    correction_requests = [item for item in transport.requests if item["stage"] == "correction"]
    assert len(correction_requests) == 1
    assert correction_requests[0]["user"].count(PLAN_FIELD_MEANINGS) == 1


def test_saved_review_mismatch_dispatches_fresh_review_without_call1(tmp_path: Path) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    review_evidence = {
        "status": "accepted",
        "decision": "accept",
        "prompt_sha256": "0" * 64,
        "reviewed_input_sha256": "0" * 64,
        "reviewed_candidate_sha256": "0" * 64,
        "effective_controls": {
            "review_model_profile": None,
            "temperature": 0,
            "max_retries": 0,
        },
        "contract_sha256": "0" * 64,
    }
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=_provenance(plan, view, inventory, runtime),
        review_evidence=review_evidence,
        policy=AuthoringPolicy(review_artifact=False),
    )

    assert prepared.decision.mode == "call2_only_review"
    assert prepared.decision.review_reused is False
    transport = ScriptedAuthoringTransport([_review(), _framed()])
    result = prepared.run(
        transport_factory=lambda: transport,
        package_dir=tmp_path / "fresh-review",
        task_id="saved-review-refresh",
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["plan_review", "call2"]
    assert result.review_reuse == {"plan": "fresh_dispatch", "artifact": "not_requested"}


def test_mismatched_v2_saved_review_dispatches_fresh_v3_review(tmp_path: Path) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    v2_packet = build_plan_review_packet(
        view,
        plan,
        inventory,
        runtime,
        sealed_version=PLAN_REVIEW_PROMPT_VERSION_V2,
    )
    review_evidence = _accepted_review_evidence(v2_packet, plan)
    review_evidence["prompt_sha256"] = "0" * 64
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=_provenance(plan, view, inventory, runtime),
        review_evidence=review_evidence,
        policy=AuthoringPolicy(review_artifact=False),
    )

    assert prepared.decision.mode == "call2_only_review"
    assert prepared.decision.review_reused is False
    assert prepared.plan_review_packet is not None
    assert prepared.plan_review_packet.version == PLAN_REVIEW_PROMPT_VERSION_V4
    transport = ScriptedAuthoringTransport([_review(), _framed()])
    result = prepared.run(
        transport_factory=lambda: transport,
        package_dir=tmp_path / "fresh-v3-review",
        task_id="saved-review-v2-refresh",
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["plan_review", "call2"]
    plan_review_request = transport.requests[0]
    assert plan_review_request["version"] == PLAN_REVIEW_PROMPT_VERSION_V4
    assert "BINDING AND SETUP RULES" in plan_review_request["user"]


def test_invalid_recovered_plan_never_becomes_preaccepted_authority(tmp_path: Path) -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    invalid = deepcopy(_plan())
    invalid.update(
        runtime_bindings=[],
        prerequisites=[
            {
                "name": "unknown",
                "binding": "recovered_binding",
                "equals": "READY",
            }
        ],
    )
    prepared = prepare_saved_plan_continuation_v2(
        saved_plan=invalid,
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime,
        provenance=_provenance(invalid, view, inventory, runtime),
        review_evidence={"status": "accepted", "decision": "accept"},
    )

    assert prepared.decision.mode == "fresh_call1"
    assert prepared.call2_packet is None
    assert any(finding.code == "unknown_binding" for finding in prepared.decision.findings)
