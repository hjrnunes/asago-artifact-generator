from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

import asago_artifact_generator.authoring as authoring
from asago_artifact_generator.authoring import (
    O04ContinuationValidationError,
    ScriptedAuthoringTransport,
    build_control_cases,
    parse_call2_response,
    prepare_o04_feedback_continuation,
    run_o04_feedback_continuation,
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
MISMATCH_PROOF = (
    MISSION_ROOT / "evidence/o04-offline-control-proof-20260920/mismatch-evidence.json"
)
RESTART_EVIDENCE = (
    MISSION_ROOT
    / "evidence/o04-refinement-restart-20260921/continuation-evidence.json"
)
RESTART_REPORT = (
    MISSION_ROOT / "evidence/o04-refinement-restart-delivery-20260921/report.md"
)
RESTART_ACCOUNTING = (
    MISSION_ROOT
    / "evidence/o04-refinement-restart-delivery-20260921/accounting.json"
)
FEEDBACK_PACKET = (
    MISSION_ROOT
    / "evidence/o04-detector-feedback-correction-20260921/"
    "rendered-correction-packet.txt"
)
FEEDBACK_INSPECTION = (
    MISSION_ROOT
    / "evidence/o04-detector-feedback-correction-20260921/inspection.json"
)
FEEDBACK_REPORT = (
    MISSION_ROOT
    / "evidence/o04-detector-feedback-correction-20260921/report.md"
)
FEEDBACK_BASELINE = (
    MISSION_ROOT
    / "evidence/o04-detector-feedback-correction-20260921/baseline/"
    "baseline-inputs.json"
)

TASK_ID = "O04-feedback-continuation-20260921"
PACKAGE_RELATIVE = Path("runs/authoring") / TASK_ID
EVIDENCE_ROOT_NAME = "evidence/o04-feedback-continuation-20260921"

FAILURE_SIDECAR_SHA256 = (
    "7e6d3c8814e6138400751f61a89558ec377c89622c87617d1113a91232763abc"
)
MISMATCH_PROOF_SHA256 = (
    "a1199338d91acacbd2416136d85a26166e0686d0695669ef12a8dabfe90c2c98"
)
RESTART_EVIDENCE_SHA256 = (
    "14a12562f0c2dcc94c667e2f488e8c1c098e85618f4efc5b7d1fa6be21315d3c"
)
RESTART_REPORT_SHA256 = (
    "3860b6ba3fe564ab255a532a0831ef43563c857ed87c19ce1cb76159497827fd"
)
RESTART_ACCOUNTING_SHA256 = (
    "9b2692206ec4609866fef1e6916819e63f47aa12ae53312b1966926c931b6f3d"
)
FEEDBACK_PACKET_SHA256 = (
    "56c4e6c0a35ef802d705503836e66097c57debc662ff215691ee314e66fad264"
)
FEEDBACK_INSPECTION_SHA256 = (
    "84b267d7b1e29637703245a8fbc6406ddbae6aeef10a1ab19171b8032d1d3e0b"
)
FEEDBACK_REPORT_SHA256 = (
    "0841b4b8bd75e81218ba971a843c16ecfc1b4ec130db0bb12c7c748c30b8a3ad"
)
FEEDBACK_BASELINE_SHA256 = (
    "6cc5f83341f716c81ad0d133a832bcadfd12c429a0579aaeac6ec26c324ada24"
)
LATEST_SEMANTIC_SHA256 = (
    "5d82ccd709c78ada964fb7e91583ce6e3b9e541c1eb079bae072191079300e8f"
)
LATEST_RAW_SHA256 = (
    "6dc7503dcd44b6e45342add3dc6ee2dd118759299389c03c9a3faf67b2316faf"
)
LATEST_METADATA_SHA256 = (
    "d049e7e15af6c2ba89c7c04790d10cbbf2e37913d2a643684f08069ed2e423e1"
)
LATEST_PYTHON_SHA256 = (
    "d61614d0233e2fa8b3e6280267ff9d8262044839d48d4bcd4886c3cfb2e84db8"
)
ACCEPTED_PLAN_SHA256 = (
    "ecc6e5299344908f621bc7c84414215aac9885f37a88cb7b6f768ecdc9ca019d"
)
CONTROL_FIXTURE_SHA256 = (
    "4aa1d442418f9e0b94ec6ff935591dc0cfc7434b7e511b7a1ef8e167c7e6c7d0"
)

CONFORMANT_DETECTOR = b"""\
def evaluate(evidence: dict) -> dict:
    availability = evidence.get("availability")
    completeness = evidence.get("completeness")
    if (
        not isinstance(availability, dict)
        or availability.get("messages") != "captured"
        or not isinstance(completeness, dict)
        or completeness.get("messages") != "complete"
    ):
        return {
            "outcome": "inconclusive",
            "reason": "reply capture is unavailable or incomplete",
            "evidence_refs": ["availability.messages"],
            "claim_level": "reply",
        }
    return {
        "outcome": "inconclusive",
        "reason": "semantic judge result is unavailable",
        "evidence_refs": ["judge.verdict"],
        "claim_level": "reply",
    }
"""


def _latest_metadata() -> dict:
    evidence = json.loads(RESTART_EVIDENCE.read_text(encoding="utf-8"))
    raw = base64.b64decode(evidence["attempts"][-1]["raw_response"]["base64"])
    return parse_call2_response(raw).metadata


def _correction_response(
    detector: bytes = CONFORMANT_DETECTOR,
    metadata: dict | None = None,
) -> bytes:
    return (
        b"```json\n"
        + json.dumps(metadata or _latest_metadata(), sort_keys=True).encode()
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
                "problem": "The candidate needs another bounded correction.",
                "basis": "The scripted feedback review exercises terminal revise routing.",
                "required_change": "Preserve the finding without another request.",
            }
        ]
    )
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted feedback {decision} outcome",
            "findings": findings,
        }
    ).encode()


def _prepared(tmp_path: Path, *, evidence_path: Path | None = None):
    package_dir = tmp_path / PACKAGE_RELATIVE
    output_evidence = evidence_path or (
        tmp_path / EVIDENCE_ROOT_NAME / "continuation-evidence.json"
    )
    return prepare_o04_feedback_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        terminal_restart_evidence=RESTART_EVIDENCE,
        terminal_restart_report=RESTART_REPORT,
        terminal_restart_accounting=RESTART_ACCOUNTING,
        feedback_packet=FEEDBACK_PACKET,
        feedback_inspection=FEEDBACK_INSPECTION,
        feedback_report=FEEDBACK_REPORT,
        feedback_baseline_inputs=FEEDBACK_BASELINE,
        expected_failure_sidecar_sha256=FAILURE_SIDECAR_SHA256,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        expected_terminal_restart_evidence_sha256=RESTART_EVIDENCE_SHA256,
        expected_terminal_restart_report_sha256=RESTART_REPORT_SHA256,
        expected_terminal_restart_accounting_sha256=RESTART_ACCOUNTING_SHA256,
        expected_feedback_packet_sha256=FEEDBACK_PACKET_SHA256,
        expected_feedback_inspection_sha256=FEEDBACK_INSPECTION_SHA256,
        expected_feedback_report_sha256=FEEDBACK_REPORT_SHA256,
        expected_feedback_baseline_sha256=FEEDBACK_BASELINE_SHA256,
        expected_candidate_sha256=LATEST_SEMANTIC_SHA256,
        expected_raw_sha256=LATEST_RAW_SHA256,
        expected_metadata_sha256=LATEST_METADATA_SHA256,
        expected_python_sha256=LATEST_PYTHON_SHA256,
        expected_plan_sha256=ACCEPTED_PLAN_SHA256,
        expected_control_fixture_sha256=CONTROL_FIXTURE_SHA256,
        package_dir=package_dir,
        evidence_path=output_evidence,
        task_id=TASK_ID,
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
        for case in build_control_cases(
            plan, metadata, inventory, include_content_references=False
        )
    ]


def test_feedback_epoch_seals_latest_candidate_and_fresh_paths(tmp_path: Path) -> None:
    continuation = _prepared(tmp_path)

    assert continuation.task_id == TASK_ID
    assert continuation.package_dir == tmp_path / PACKAGE_RELATIVE
    assert continuation.evidence_path == (
        tmp_path / EVIDENCE_ROOT_NAME / "continuation-evidence.json"
    )
    assert continuation.prior_author_correction_spend == 8
    assert continuation.prior_review_spend == 1
    assert continuation.aggregate_spent == 20
    assert continuation.task_limit == 11
    assert continuation.continuation_author_limit == 1
    assert continuation.continuation_review_limit == 1
    assert continuation.artifact.candidate_sha256 == LATEST_SEMANTIC_SHA256
    assert continuation.artifact.authority["control_fixture_sha256"] == (
        CONTROL_FIXTURE_SHA256
    )


@pytest.mark.parametrize(
    "pin_name",
    [
        "expected_terminal_restart_evidence_sha256",
        "expected_terminal_restart_report_sha256",
        "expected_terminal_restart_accounting_sha256",
        "expected_feedback_packet_sha256",
        "expected_feedback_inspection_sha256",
        "expected_feedback_report_sha256",
        "expected_feedback_baseline_sha256",
        "expected_candidate_sha256",
        "expected_raw_sha256",
        "expected_metadata_sha256",
        "expected_python_sha256",
        "expected_plan_sha256",
        "expected_control_fixture_sha256",
    ],
)
def test_feedback_epoch_rejects_tampered_authority_before_transport(
    tmp_path: Path, pin_name: str
) -> None:
    kwargs = {
        "failure_sidecar": FAILURE_SIDECAR,
        "mismatch_proof": MISMATCH_PROOF,
        "terminal_restart_evidence": RESTART_EVIDENCE,
        "terminal_restart_report": RESTART_REPORT,
        "terminal_restart_accounting": RESTART_ACCOUNTING,
        "feedback_packet": FEEDBACK_PACKET,
        "feedback_inspection": FEEDBACK_INSPECTION,
        "feedback_report": FEEDBACK_REPORT,
        "feedback_baseline_inputs": FEEDBACK_BASELINE,
        "package_dir": tmp_path / PACKAGE_RELATIVE,
        "evidence_path": tmp_path / EVIDENCE_ROOT_NAME / "continuation-evidence.json",
        "task_id": TASK_ID,
    }
    kwargs[pin_name] = "0" * 64

    with pytest.raises(O04ContinuationValidationError, match="authority|candidate|plan"):
        prepare_o04_feedback_continuation(**kwargs)


def test_feedback_run_wrapper_stops_before_transport_on_pin_mismatch(
    tmp_path: Path,
) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_correction_response()])

    result = run_o04_feedback_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        terminal_restart_evidence=RESTART_EVIDENCE,
        terminal_restart_report=RESTART_REPORT,
        terminal_restart_accounting=RESTART_ACCOUNTING,
        feedback_packet=FEEDBACK_PACKET,
        feedback_inspection=FEEDBACK_INSPECTION,
        feedback_report=FEEDBACK_REPORT,
        feedback_baseline_inputs=FEEDBACK_BASELINE,
        package_dir=tmp_path / PACKAGE_RELATIVE,
        evidence_path=tmp_path / EVIDENCE_ROOT_NAME / "continuation-evidence.json",
        expected_terminal_restart_evidence_sha256="0" * 64,
        transport_factory=transport_factory,
    )

    assert result.status == "preflight_defect"
    assert called is False
    assert result.ledger == []
    assert result.findings[0].code == "preflight_authority"


def test_feedback_epoch_renders_complete_shared_feedback_before_dispatch(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([TimeoutError("provider unavailable")])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "transport_failure"
    assert len(transport.requests) == 1
    prompt = transport.requests[0]["user"]
    for name in (
        "judge-missing",
        "judge-invalid",
        "judge-support-unresolved",
        "judge-malformed-message",
    ):
        assert name in prompt
    assert "availability.messages" in prompt
    assert "completeness.messages" in prompt
    assert "judge.verdict" in prompt
    assert "RESPONSE CONTRACT" in prompt
    assert "chat_template_kwargs" not in prompt
    assert CONFORMANT_DETECTOR.decode() not in prompt
    assert transport.requests[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_feedback_epoch_correction_transport_is_terminal_and_counts_once(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([TimeoutError("provider unavailable")])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "transport_failure"
    assert [request["stage"] for request in transport.requests] == ["correction"]
    assert result.budget["feedback_correction_spent"] == 1
    assert result.budget["feedback_review_spent"] == 0
    assert result.budget["aggregate_spent"] == 21
    assert result.budget["aggregate_combined_spent"] == 60
    assert result.budget["task_spent"] == 10
    assert result.package is None
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    raw_record = evidence["attempts"][0]["raw_response"]
    assert raw_record["availability"] == "unavailable"
    assert evidence["reviews"] == []


def test_feedback_epoch_persists_raw_before_parse_and_stops_on_bad_framing(
    tmp_path: Path,
) -> None:
    raw = b"not an artifact response"
    transport = ScriptedAuthoringTransport([raw])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "correction_unavailable"
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    raw_record = evidence["attempts"][0]["raw_response"]
    assert raw_record["availability"] == "available"
    assert raw_record["sha256"] == hashlib.sha256(raw).hexdigest()
    assert raw_record["byte_length"] == len(raw)
    assert evidence["reviews"] == []


def test_feedback_epoch_deterministic_failure_stops_before_controls_or_review(
    tmp_path: Path,
) -> None:
    metadata = _latest_metadata()
    metadata["stimulus"]["delivery"] = "not-a-supported-route"
    transport = ScriptedAuthoringTransport(
        [_correction_response(metadata=metadata)]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "correction_failed"
    assert [request["stage"] for request in transport.requests] == ["correction"]
    assert result.budget["feedback_correction_spent"] == 1
    assert result.budget["feedback_review_spent"] == 0
    assert result.package is None


def test_feedback_epoch_controls_gate_the_only_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_controls(detector_bytes: bytes, **kwargs):
        records = _passing_controls(
            kwargs["plan"], kwargs["metadata"], kwargs["inventory"]
        )
        records[-1]["status"] = "failed"
        records[-1]["observed_outcome"] = "detected"
        records[-1]["failure"] = "outcome_mismatch"
        return (
            [
                {
                    "code": "detector_control_failure",
                    "detail": "judge-malformed-message failed",
                    "path": "detector_controls.judge-malformed-message",
                }
            ],
            records,
        )

    monkeypatch.setattr(authoring, "run_detector_controls", failed_controls)
    transport = ScriptedAuthoringTransport([_correction_response()])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "controls_failed"
    assert [request["stage"] for request in transport.requests] == ["correction"]
    assert result.budget["feedback_review_spent"] == 0
    assert result.budget["task_spent"] == 10


def test_feedback_epoch_rejects_historical_task_and_path_collisions(
    tmp_path: Path,
) -> None:
    with pytest.raises(O04ContinuationValidationError, match="task identity"):
        prepare_o04_feedback_continuation(
            failure_sidecar=FAILURE_SIDECAR,
            mismatch_proof=MISMATCH_PROOF,
            terminal_restart_evidence=RESTART_EVIDENCE,
            terminal_restart_report=RESTART_REPORT,
            terminal_restart_accounting=RESTART_ACCOUNTING,
            feedback_packet=FEEDBACK_PACKET,
            feedback_inspection=FEEDBACK_INSPECTION,
            feedback_report=FEEDBACK_REPORT,
            feedback_baseline_inputs=FEEDBACK_BASELINE,
            package_dir=tmp_path / "runs/authoring/O04-provider-recovery-restart",
            evidence_path=tmp_path / EVIDENCE_ROOT_NAME / "continuation-evidence.json",
            task_id="O04-provider-recovery-restart",
        )
    with pytest.raises(O04ContinuationValidationError, match="package path"):
        prepare_o04_feedback_continuation(
            failure_sidecar=FAILURE_SIDECAR,
            mismatch_proof=MISMATCH_PROOF,
            terminal_restart_evidence=RESTART_EVIDENCE,
            terminal_restart_report=RESTART_REPORT,
            terminal_restart_accounting=RESTART_ACCOUNTING,
            feedback_packet=FEEDBACK_PACKET,
            feedback_inspection=FEEDBACK_INSPECTION,
            feedback_report=FEEDBACK_REPORT,
            feedback_baseline_inputs=FEEDBACK_BASELINE,
            package_dir=tmp_path / "runs/authoring/other-task",
            evidence_path=tmp_path / EVIDENCE_ROOT_NAME / "continuation-evidence.json",
            task_id=TASK_ID,
        )


def test_feedback_epoch_accepts_only_exact_review_and_reaches_ceilings(
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
    transport = ScriptedAuthoringTransport(
        [_correction_response(), _review_response("accept")]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.budget["feedback_correction_spent"] == 1
    assert result.budget["feedback_review_spent"] == 1
    assert result.budget["author_correction_spent"] == 9
    assert result.budget["review_spent"] == 2
    assert result.budget["aggregate_spent"] == 22
    assert result.budget["aggregate_combined_spent"] == 61
    assert result.budget["task_spent"] == 11
    assert result.package is not None
    assert result.package.manifest.authoring["continuation"]["mode"] == (
        "sealed-o04-feedback-continuation"
    )
    assert (
        result.package.manifest.authoring["policy"]["artifact_max_corrections"] == 1
    )


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (_review_response("blocked"), "blocked"),
        (b"not review JSON", "review_unavailable"),
        (TimeoutError("review unavailable"), "review_unavailable"),
    ],
)
def test_feedback_epoch_non_accept_reviews_are_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: bytes | BaseException,
    status: str,
) -> None:
    monkeypatch.setattr(
        authoring,
        "run_detector_controls",
        lambda detector_bytes, **kwargs: (
            [],
            _passing_controls(kwargs["plan"], kwargs["metadata"], kwargs["inventory"]),
        ),
    )
    transport = ScriptedAuthoringTransport([_correction_response(), response])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == status
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.package is None
    assert result.budget["feedback_correction_spent"] == 1
    assert result.budget["feedback_review_spent"] == 1


def test_feedback_epoch_review_revise_is_terminal_without_second_correction(
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
    transport = ScriptedAuthoringTransport(
        [_correction_response(), _review_response("revise")]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "revise"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.budget["feedback_correction_spent"] == 1
    assert result.budget["feedback_review_spent"] == 1
    assert result.package is None


def test_feedback_epoch_has_no_forbidden_dispatch_or_readiness_probe(
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
    transport = ScriptedAuthoringTransport(
        [_correction_response(), _review_response("accept")]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert all(
        request["version"]
        in {"authoring-correction-v4", "authoring-artifact-review-v2"}
        for request in transport.requests
    )
    assert result.budget["provider_readiness_reads"] == 0
