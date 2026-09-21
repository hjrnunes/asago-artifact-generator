from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import asago_artifact_generator.authoring as authoring
from asago_artifact_generator.authoring import (
    O04ContinuationValidationError,
    ScriptedAuthoringTransport,
    build_control_cases,
    parse_call2_response,
    prepare_o04_refinement_restart_continuation,
    run_o04_refinement_restart_continuation,
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
TERMINAL_EVIDENCE = (
    MISSION_ROOT / "evidence/o04-refinement-20260921/continuation-evidence.json"
)
TERMINAL_REPORT = (
    MISSION_ROOT / "evidence/o04-refinement-delivery-20260921/report.md"
)
TERMINAL_ACCOUNTING = (
    MISSION_ROOT / "evidence/o04-refinement-delivery-20260921/accounting.json"
)

FAILURE_SIDECAR_SHA256 = (
    "7e6d3c8814e6138400751f61a89558ec377c89622c87617d1113a91232763abc"
)
MISMATCH_PROOF_SHA256 = (
    "a1199338d91acacbd2416136d85a26166e0686d0695669ef12a8dabfe90c2c98"
)
TERMINAL_EVIDENCE_SHA256 = (
    "61f8aa1e7e23f7f5921dc8b04f0bf69d4eaecd316a48e4d88fdff6adfa4b801e"
)
TERMINAL_REPORT_SHA256 = (
    "c8f50d35612059b5465d71d020c48cc3a0c35e19bb903db34fe1ca4bbaf17b37"
)
TERMINAL_ACCOUNTING_SHA256 = (
    "58ef7361edf9fcec591090850bb533291bd9183bf9dd80033e19a453ebc7cf0c"
)
SAVED_CANDIDATE_SHA256 = (
    "f374565bef6fe20e4f8863a0f75c6f869b074aadb8d38cef9ff29124e81c9e4e"
)
ACCEPTED_PLAN_SHA256 = (
    "ecc6e5299344908f621bc7c84414215aac9885f37a88cb7b6f768ecdc9ca019d"
)
CONTROL_FIXTURE_SHA256 = (
    "4aa1d442418f9e0b94ec6ff935591dc0cfc7434b7e511b7a1ef8e167c7e6c7d0"
)
OUTAGE_HTML_SHA256 = (
    "0ccdd3b4f240a05716e9dd3e8a7c28afa2b37c5d77a85fb6dc5a644f3b01a2e4"
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
                "problem": "The candidate needs another bounded correction.",
                "basis": "The scripted restart review exercises revise routing.",
                "required_change": "Preserve the finding in the next correction.",
            }
        ]
    )
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted restart {decision} outcome",
            "findings": findings,
        }
    ).encode()


def _prepared(tmp_path: Path):
    return prepare_o04_refinement_restart_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        terminal_refinement_evidence=TERMINAL_EVIDENCE,
        terminal_delivery_report=TERMINAL_REPORT,
        terminal_accounting=TERMINAL_ACCOUNTING,
        expected_failure_sidecar_sha256=FAILURE_SIDECAR_SHA256,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        expected_terminal_refinement_evidence_sha256=TERMINAL_EVIDENCE_SHA256,
        expected_terminal_delivery_report_sha256=TERMINAL_REPORT_SHA256,
        expected_terminal_accounting_sha256=TERMINAL_ACCOUNTING_SHA256,
        package_dir=tmp_path / "package",
        task_id="O04-provider-recovery-restart-test",
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


def test_restart_seeds_hash_pinned_authority_and_fresh_paths(tmp_path: Path) -> None:
    continuation = _prepared(tmp_path)

    assert continuation.task_id == "O04-provider-recovery-restart-test"
    assert continuation.evidence_path != TERMINAL_EVIDENCE
    assert continuation.package_dir == tmp_path / "package"
    assert continuation.prior_author_correction_spend == 6
    assert continuation.prior_review_spend == 1
    assert continuation.aggregate_spent == 18
    assert continuation.task_limit == 11
    assert continuation.continuation_author_limit == 2
    assert continuation.continuation_review_limit == 2
    assert continuation.artifact.candidate_sha256 == SAVED_CANDIDATE_SHA256
    assert continuation.terminal_refinement_evidence == TERMINAL_EVIDENCE
    assert continuation.provider_readiness["http_status"] == 200
    assert continuation.provider_readiness["model_discoverable"] is True
    assert continuation.provider_readiness["latency_ms"] == 534.6


def test_restart_rejects_terminal_pin_before_transport_and_persists_typed_evidence(
    tmp_path: Path,
) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_correction_response()])

    result = run_o04_refinement_restart_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        terminal_refinement_evidence=TERMINAL_EVIDENCE,
        terminal_delivery_report=TERMINAL_REPORT,
        terminal_accounting=TERMINAL_ACCOUNTING,
        expected_failure_sidecar_sha256=FAILURE_SIDECAR_SHA256,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        expected_terminal_refinement_evidence_sha256="0" * 64,
        expected_terminal_delivery_report_sha256=TERMINAL_REPORT_SHA256,
        expected_terminal_accounting_sha256=TERMINAL_ACCOUNTING_SHA256,
        package_dir=tmp_path / "package",
        task_id="O04-provider-recovery-restart-pin-mismatch",
        transport_factory=transport_factory,
    )

    assert result.status == "preflight_defect"
    assert called is False
    assert result.findings[0].code == "preflight_authority"
    assert result.ledger == []
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize(
    "pin_name",
    ["expected_terminal_delivery_report_sha256", "expected_terminal_accounting_sha256"],
)
def test_restart_rejects_each_delivery_authority_pin_before_transport(
    tmp_path: Path, pin_name: str
) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_correction_response()])

    kwargs = {
        "failure_sidecar": FAILURE_SIDECAR,
        "mismatch_proof": MISMATCH_PROOF,
        "terminal_refinement_evidence": TERMINAL_EVIDENCE,
        "terminal_delivery_report": TERMINAL_REPORT,
        "terminal_accounting": TERMINAL_ACCOUNTING,
        "expected_failure_sidecar_sha256": FAILURE_SIDECAR_SHA256,
        "expected_mismatch_proof_sha256": MISMATCH_PROOF_SHA256,
        "expected_terminal_refinement_evidence_sha256": TERMINAL_EVIDENCE_SHA256,
        "expected_terminal_delivery_report_sha256": TERMINAL_REPORT_SHA256,
        "expected_terminal_accounting_sha256": TERMINAL_ACCOUNTING_SHA256,
        "package_dir": tmp_path / "package",
        "task_id": f"O04-provider-recovery-restart-{pin_name}",
        "transport_factory": transport_factory,
    }
    kwargs[pin_name] = "0" * 64

    result = run_o04_refinement_restart_continuation(**kwargs)

    assert result.status == "preflight_defect"
    assert called is False
    assert result.findings[0].code == "preflight_authority"
    assert result.ledger == []
    assert not (tmp_path / "package").exists()


def test_restart_full_four_dispatch_path_reaches_new_and_combined_ceilings(
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
    second_metadata = _saved_metadata()
    second_metadata["explanation"] = "second restart candidate"
    transport = ScriptedAuthoringTransport(
        [
            _correction_response(),
            _review_response("revise"),
            _correction_response(metadata=second_metadata),
            _review_response("accept"),
        ]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
        "correction",
        "artifact_review",
    ]
    assert result.budget["restart_correction_spent"] == 2
    assert result.budget["restart_review_spent"] == 2
    assert result.budget["author_correction_spent"] == 8
    assert result.budget["review_spent"] == 3
    assert result.budget["aggregate_spent"] == 22
    assert result.budget["aggregate_combined_spent"] == 61
    assert result.budget["task_spent"] == 11
    assert result.budget["historical_allowance_reopened"] is False
    assert result.package is not None
    package_continuation = result.package.manifest.authoring["continuation"]
    assert package_continuation["mode"] == "sealed-o04-provider-recovery-restart"
    assert package_continuation["refinement_allowance"]["expired"] is True
    assert package_continuation["restart_allowance"] == {
        "correction": 2,
        "review": 2,
        "dispatch": 4,
        "correction_spent": 2,
        "review_spent": 2,
        "expired": False,
    }
    assert package_continuation["terminal_authority"]["evidence_sha256"] == (
        TERMINAL_EVIDENCE_SHA256
    )
    reviews = [record for record in result.ledger if record["stage"] == "artifact_review"]
    assert reviews[0]["reviewed_candidate_sha256"] != reviews[1]["reviewed_candidate_sha256"]
    assert SAVED_CANDIDATE_SHA256 in transport.requests[0]["user"]
    assert OUTAGE_HTML_SHA256 not in transport.requests[0]["user"]
    assert CONFORMANT_DETECTOR.decode() not in transport.requests[0]["user"]
    assert all(
        request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        for request in transport.requests
    )


def test_restart_control_failure_uses_only_remaining_correction_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def controls(detector_bytes: bytes, **kwargs):
        nonlocal calls
        calls += 1
        records = (
            _passing_controls(kwargs["plan"], kwargs["metadata"], kwargs["inventory"])
            if calls == 2
            else _passing_controls(kwargs["plan"], kwargs["metadata"], kwargs["inventory"])
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

    monkeypatch.setattr(authoring, "run_detector_controls", controls)
    transport = ScriptedAuthoringTransport(
        [_correction_response(), _correction_response()]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "controls_failed"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "correction",
    ]
    assert result.budget["restart_correction_spent"] == 2
    assert result.budget["restart_review_spent"] == 0


def test_restart_transport_failure_consumes_role_and_never_uses_other_slot(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([TimeoutError("provider unavailable")])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "transport_failure"
    assert len(transport.requests) == 1
    assert result.budget["restart_correction_spent"] == 1
    assert result.budget["restart_review_spent"] == 0
    assert result.budget["aggregate_spent"] == 19
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    assert evidence["provider_readiness"]["request_count"] == 1
    assert evidence["attempts"][0]["raw_response"]["availability"] == "unavailable"
    assert OUTAGE_HTML_SHA256 not in evidence["attempts"][0]["raw_response"].get(
        "sha256", ""
    )


def test_restart_rejects_used_task_and_package_paths(tmp_path: Path) -> None:
    (tmp_path / "package").mkdir()
    with pytest.raises(O04ContinuationValidationError, match="package path"):
        _prepared(tmp_path)

    fresh = tmp_path / "fresh"
    with pytest.raises(O04ContinuationValidationError, match="task identity"):
        prepare_o04_refinement_restart_continuation(
            failure_sidecar=FAILURE_SIDECAR,
            mismatch_proof=MISMATCH_PROOF,
            terminal_refinement_evidence=TERMINAL_EVIDENCE,
            terminal_delivery_report=TERMINAL_REPORT,
            terminal_accounting=TERMINAL_ACCOUNTING,
            package_dir=fresh,
            task_id="O04-artifact-refinement-continuation",
        )


def test_restart_rejects_mismatched_readiness_without_reprobing(tmp_path: Path) -> None:
    with pytest.raises(O04ContinuationValidationError, match="readiness"):
        prepare_o04_refinement_restart_continuation(
            failure_sidecar=FAILURE_SIDECAR,
            mismatch_proof=MISMATCH_PROOF,
            terminal_refinement_evidence=TERMINAL_EVIDENCE,
            terminal_delivery_report=TERMINAL_REPORT,
            terminal_accounting=TERMINAL_ACCOUNTING,
            package_dir=tmp_path / "package",
            task_id="O04-provider-recovery-restart-readiness-mismatch",
            provider_readiness={},
        )
