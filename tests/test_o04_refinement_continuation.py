from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import asago_artifact_generator.authoring as authoring
from asago_artifact_generator.authoring import (
    O04ContinuationValidationError,
    PrivateModelAuthoringTransport,
    ScriptedAuthoringTransport,
    build_control_cases,
    parse_call2_response,
    prepare_o04_refinement_continuation,
)

MISSION_ROOT = Path(
    "/Users/hjrnunes/.factory/missions/"
    "fb1ebe78-2eda-4bd9-a9c7-f51f0028f03a"
)
CONSUMER_ROOT = Path(
    "/Users/hjrnunes/workspace/redhat/hjrnunes/asago-scenario-generator/"
    ".worktrees/llm-designed-artifacts/asago-artifact-generator"
)
FAILURE_SIDECAR = (
    CONSUMER_ROOT
    / "runs/authoring/O04-live-20260920/O04-live-20260920.failure-evidence.json"
)
FAILURE_SIDECAR_SHA256 = (
    "7e6d3c8814e6138400751f61a89558ec377c89622c87617d1113a91232763abc"
)
MISMATCH_PROOF = (
    MISSION_ROOT / "evidence/o04-offline-control-proof-20260920/mismatch-evidence.json"
)
MISMATCH_PROOF_SHA256 = (
    "a1199338d91acacbd2416136d85a26166e0686d0695669ef12a8dabfe90c2c98"
)
CONFORMANT_DETECTOR = (
    b"def evaluate(evidence: dict) -> dict:\n"
    b"    return {'outcome': 'inconclusive', 'claim_level': 'reply'}\n"
)
PRIOR_CONTINUATION = (
    MISSION_ROOT
    / "evidence/o04-correction-continuation-20260921/continuation-evidence.json"
)
PRIOR_DELIVERY_REPORT = (
    MISSION_ROOT / "evidence/o04-continuation-delivery-20260921/o04-continuation-report.md"
)
PRIOR_PRESERVATION = (
    MISSION_ROOT / "evidence/o04-continuation-delivery-20260921/preservation-digests.json"
)


def _saved_metadata() -> dict:
    evidence = json.loads(FAILURE_SIDECAR.read_text(encoding="utf-8"))
    attempt = next(item for item in evidence["attempts"] if item["stage"] == "call2")
    raw = base64.b64decode(attempt["raw_response"]["base64"])
    return parse_call2_response(raw).metadata


def _correction_response(
    detector: bytes = CONFORMANT_DETECTOR,
    metadata: dict | None = None,
) -> bytes:
    return (
        b"```json\n"
        + json.dumps(metadata or _saved_metadata(), sort_keys=True).encode()
        + b"\n```\n```python\n"
        + detector
        + b"```\n"
    )


def _review_response(decision: str = "accept") -> bytes:
    findings = (
        []
        if decision == "accept"
        else [
            {
                "location": "detector.py",
                "problem": "The review outcome is terminal.",
                "basis": "The scripted review exercises terminal routing.",
                "required_change": "Preserve the terminal outcome without retry.",
            }
        ]
    )
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted O04 {decision} outcome",
            "findings": findings,
        }
    ).encode()


def _prepared(tmp_path: Path):
    return prepare_o04_refinement_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        prior_continuation_evidence=PRIOR_CONTINUATION,
        prior_delivery_report=PRIOR_DELIVERY_REPORT,
        prior_preservation=PRIOR_PRESERVATION,
        expected_failure_sidecar_sha256=FAILURE_SIDECAR_SHA256,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        package_dir=tmp_path / "package",
        task_id="O04-refinement-continuation-test",
    )


def _passing_controls(plan: dict, metadata: dict, inventory: dict) -> list[dict]:
    return [
        {
            "name": case.name,
            "expected_outcome": case.expected_outcome,
            "expected_claim_level": case.expected_claim_level,
            "status": "passed",
            "observed_outcome": case.expected_outcome,
            "observed_claim_level": case.expected_claim_level,
            "failure": None,
            "runtime": {
                "engine": "docker",
                "image": "python:3.12-slim",
                "network": "none",
                "read_only": True,
            },
        }
        for case in build_control_cases(plan, metadata, inventory)
    ]


def _failed_controls(plan: dict, metadata: dict, inventory: dict) -> list[dict]:
    records = _passing_controls(plan, metadata, inventory)
    for record in records[-4:]:
        record["status"] = "failed"
        record["observed_outcome"] = "detected"
        record["observed_claim_level"] = "reply"
        record["failure"] = "outcome_mismatch"
    return records


def test_refinement_shared_allowance_covers_control_and_review_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def controls(
        detector_bytes: bytes,
        *,
        plan: dict,
        metadata: dict,
        inventory: dict,
        runtime_contract: dict,
    ) -> tuple[list[dict], list[dict]]:
        nonlocal calls
        calls += 1
        records = (
            _failed_controls(plan, metadata, inventory)
            if calls == 1
            else _passing_controls(plan, metadata, inventory)
        )
        findings = [
            {
                "code": "detector_control_failure",
                "detail": f"{record['name']} failed",
                "path": f"detector_controls.{record['name']}",
            }
            for record in records
            if record["status"] != "passed"
        ]
        return findings, records

    monkeypatch.setattr(authoring, "run_detector_controls", controls)
    first = _correction_response(CONFORMANT_DETECTOR)
    second_metadata = _saved_metadata()
    second_metadata["explanation"] = "second exact candidate"
    second = _correction_response(CONFORMANT_DETECTOR, metadata=second_metadata)
    transport = ScriptedAuthoringTransport([first, second, _review_response()])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "correction",
        "artifact_review",
    ]
    assert result.budget["prior_author_correction_spent"] == 5
    assert result.budget["prior_review_spent"] == 1
    assert result.budget["refinement_correction_spent"] == 2
    assert result.budget["refinement_review_spent"] == 1
    assert result.budget["author_correction_spent"] == 7
    assert result.budget["review_spent"] == 2
    assert result.budget["aggregate_spent"] == 20
    assert result.budget["historical_allowance_reopened"] is False
    assert result.package is not None
    assert len(result.ledger) == 3
    assert result.reviews[0]["raw_response"]["availability"] == "available"
    assert result.reviews[0]["usage"]["availability"] == "unavailable"
    assert len(result.candidate_attempts) == 2
    assert all(
        {
            "prompt",
            "raw_response",
            "usage",
            "controls",
            "metadata_sha256",
            "python_sha256",
            "candidate_sha256",
            "findings",
            "detector_controls",
            "budget_before_dispatch",
            "budget_after_dispatch",
        }
        <= attempt.keys()
        for attempt in result.candidate_attempts
    )
    assert transport.requests[1]["user"] != transport.requests[0]["user"]
    assert "detector_controls.judge-malformed-message" in transport.requests[1]["user"]
    assert all(
        request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        for request in transport.requests
    )


def test_refinement_review_revise_consumes_remaining_shared_author_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authoring,
        "run_detector_controls",
        lambda detector_bytes, **kwargs: (
            [],
            _passing_controls(kwargs["plan"], kwargs["metadata"], kwargs["inventory"]),
        ),
    )
    first = _correction_response(CONFORMANT_DETECTOR)
    second_metadata = _saved_metadata()
    second_metadata["explanation"] = "review revision candidate"
    second = _correction_response(CONFORMANT_DETECTOR, metadata=second_metadata)
    transport = ScriptedAuthoringTransport(
        [first, _review_response("revise"), second, _review_response("accept")]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
        "correction",
        "artifact_review",
    ]
    assert result.budget["refinement_correction_spent"] == 2
    assert result.budget["refinement_review_spent"] == 2
    assert result.budget["author_correction_spent"] == 7
    assert result.budget["review_spent"] == 3
    reviews = [record for record in result.ledger if record["stage"] == "artifact_review"]
    assert [record["reviewed_candidate_sha256"] for record in reviews][0] != [
        record["reviewed_candidate_sha256"] for record in reviews
    ][1]


def test_refinement_review_stays_gated_after_control_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def controls(
        detector_bytes: bytes,
        *,
        plan: dict,
        metadata: dict,
        inventory: dict,
        runtime_contract: dict,
    ) -> tuple[list[dict], list[dict]]:
        nonlocal calls
        calls += 1
        records = _failed_controls(plan, metadata, inventory) if calls == 1 else []
        if not records:
            records = _passing_controls(plan, metadata, inventory)
        findings = [
            {
                "code": "detector_control_failure",
                "detail": record["failure"] or "failed",
                "path": f"detector_controls.{record['name']}",
            }
            for record in records
            if record["status"] != "passed"
        ]
        return findings, records

    monkeypatch.setattr(authoring, "run_detector_controls", controls)
    transport = ScriptedAuthoringTransport(
        [
            _correction_response(CONFORMANT_DETECTOR),
            _correction_response(CONFORMANT_DETECTOR),
            _review_response(),
        ]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "correction",
        "artifact_review",
    ]
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    assert evidence["attempts"][0]["raw_response"]["availability"] == "available"
    assert evidence["attempts"][0]["detector_controls"][0]["name"] == (
        "missing-relevant-capture"
    )
    assert evidence["attempts"][0]["findings"]
    assert evidence["attempts"][1]["findings"] == []


def test_refinement_records_fixed_thinking_off_before_first_dispatch(tmp_path: Path) -> None:
    class FakeCompletions:
        def create(self, **kwargs):
            self.kwargs = kwargs
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"message": type("Message", (), {"content": "{}"})()},
                        )()
                    ],
                    "usage": None,
                },
            )()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {})()
            self.chat.completions = FakeCompletions()

    original = authoring.PrivateModelAuthoringTransport
    assert original is PrivateModelAuthoringTransport
    pytest.importorskip("openai")
    import openai

    old = openai.OpenAI
    openai.OpenAI = FakeOpenAI
    try:
        transport = PrivateModelAuthoringTransport(
            base_url="https://private.invalid/v1",
            api_key="secret-value",
            model="gemma4-oc",
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        packet = authoring.PromptPacket(
            stage="correction",
            version="test",
            system="system",
            user="user",
            payload={"thinking_choice": {"chat_template_kwargs.enable_thinking": False}},
        )
        transport.complete(packet)
    finally:
        openai.OpenAI = old

    assert transport._client.chat.completions.kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_refinement_transport_failure_consumes_one_role_without_retry(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([TimeoutError("correction unavailable")])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "transport_failure"
    assert len(transport.requests) == 1
    assert result.budget["refinement_correction_spent"] == 1
    assert result.budget["refinement_review_spent"] == 0
    assert result.package is None
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    assert evidence["attempts"][0]["raw_response"]["availability"] == "unavailable"
    assert evidence["attempts"][0]["raw_response"]["reason"] == "provider_failure"


def test_refinement_rejects_mismatched_prior_candidate_before_transport(
    tmp_path: Path,
) -> None:
    with pytest.raises(O04ContinuationValidationError, match="saved candidate hash"):
        prepare_o04_refinement_continuation(
            failure_sidecar=FAILURE_SIDECAR,
            mismatch_proof=MISMATCH_PROOF,
            prior_continuation_evidence=PRIOR_CONTINUATION,
            prior_delivery_report=PRIOR_DELIVERY_REPORT,
            prior_preservation=PRIOR_PRESERVATION,
            expected_candidate_sha256="0" * 64,
            package_dir=tmp_path / "package",
            task_id="O04-refinement-preflight-mismatch-test",
        )
