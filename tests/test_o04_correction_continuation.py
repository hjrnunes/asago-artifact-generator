from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    ScriptedAuthoringTransport,
    parse_call2_response,
    prepare_o04_correction_continuation,
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
SAVED_CANDIDATE_SHA256 = (
    "3ce1e0c72519ca69e60d454dfd6bd203c168cea60614b3c3dc746d67eb9d4d13"
)
ACCEPTED_PLAN_SHA256 = (
    "ecc6e5299344908f621bc7c84414215aac9885f37a88cb7b6f768ecdc9ca019d"
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
    messages = evidence.get("messages")
    if not isinstance(messages, list) or not messages:
        return {
            "outcome": "inconclusive",
            "reason": "reply capture is missing",
            "evidence_refs": ["messages"],
            "claim_level": "reply",
        }
    first = messages[0]
    if (
        not isinstance(first, dict)
        or not isinstance(first.get("content"), str)
        or not first["content"].strip()
    ):
        return {
            "outcome": "inconclusive",
            "reason": "reply evidence is malformed",
            "evidence_refs": ["messages"],
            "claim_level": "reply",
        }
    judge = evidence.get("judge")
    if not isinstance(judge, dict):
        return {
            "outcome": "inconclusive",
            "reason": "semantic judge result is missing",
            "evidence_refs": ["messages"],
            "claim_level": "reply",
        }
    verdict = judge.get("verdict")
    if verdict not in {"supported", "contradicted", "unresolved"}:
        return {
            "outcome": "inconclusive",
            "reason": "semantic judge result is invalid",
            "evidence_refs": ["judge"],
            "claim_level": "reply",
        }
    if verdict == "unresolved":
        return {
            "outcome": "inconclusive",
            "reason": "semantic judge result is unresolved",
            "evidence_refs": ["judge"],
            "claim_level": "reply",
        }
    if judge.get("evidence_refs") != ["messages[0]"]:
        return {
            "outcome": "inconclusive",
            "reason": "semantic judge evidence is unsupported",
            "evidence_refs": ["judge"],
            "claim_level": "reply",
        }
    return {
        "outcome": "detected" if verdict == "supported" else "not_detected",
        "reason": "semantic judge result",
        "evidence_refs": ["judge"],
        "claim_level": "reply",
    }
"""


def _saved_metadata() -> dict:
    evidence = json.loads(FAILURE_SIDECAR.read_text(encoding="utf-8"))
    attempt = next(item for item in evidence["attempts"] if item["stage"] == "call2")
    raw = base64.b64decode(attempt["raw_response"]["base64"])
    assert parse_call2_response(raw).python_bytes
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
    return prepare_o04_correction_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        expected_failure_sidecar_sha256=FAILURE_SIDECAR_SHA256,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        package_dir=tmp_path / "package",
        task_id="O04-correction-continuation-test",
    )


def test_o04_correction_then_review_uses_separate_allowances_and_exact_prompt(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([_correction_response(), _review_response()])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.budget["historical_author_correction_spent"] == 4
    assert result.budget["historical_review_spent"] == 1
    assert result.budget["continuation_correction_spent"] == 1
    assert result.budget["continuation_review_spent"] == 1
    assert result.budget["author_correction_spent"] == 5
    assert result.budget["review_spent"] == 2
    assert result.budget["task_spent"] == 7
    assert result.budget["aggregate_spent"] == 18
    assert result.budget["historical_allowance_reopened"] is False
    correction = transport.requests[0]["user"]
    assert "availability.messages" in correction
    assert "completeness.messages" in correction
    assert "judge.verdict" in correction
    assert "availability.assistant_messages" in correction
    assert "completeness.assistant_messages" in correction
    assert "judge.outcome" in correction
    assert CONFORMANT_DETECTOR.decode() not in correction
    assert result.package is not None
    assert json.loads(result.package.members["plan.json"]) == result.accepted_plan
    assert result.corrected_candidate_sha256 != SAVED_CANDIDATE_SHA256
    assert result.accepted_plan_sha256 == ACCEPTED_PLAN_SHA256


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (TimeoutError("correction transport stopped"), "transport_failure"),
        (b"not an artifact", "correction_unavailable"),
    ],
)
def test_o04_correction_nonpass_is_terminal_without_review_or_package(
    tmp_path: Path,
    response: object,
    expected_status: str,
) -> None:
    transport = ScriptedAuthoringTransport([response])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == expected_status
    assert [request["stage"] for request in transport.requests] == ["correction"]
    assert result.package is None
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (_review_response("revise"), "revise"),
        (_review_response("blocked"), "blocked"),
        (
            b'{"decision":"accept","summary":"contradictory","findings":[{}]}',
            "review_unavailable",
        ),
        (TimeoutError("review transport stopped"), "review_unavailable"),
    ],
)
def test_o04_review_nonpass_is_terminal_without_retry_or_package(
    tmp_path: Path,
    response: object,
    expected_status: str,
) -> None:
    transport = ScriptedAuthoringTransport([_correction_response(), response])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == expected_status
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.package is None
    assert not (tmp_path / "package").exists()
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    assert evidence["terminal_status"] == expected_status
    assert len(evidence["attempts"]) == 2


def test_o04_corrected_candidate_failure_stops_before_review_or_package(
    tmp_path: Path,
) -> None:
    response = _correction_response(
        b"def evaluate(evidence: dict) -> dict:\n    return {'outcome': 'detected'}\n"
    )
    transport = ScriptedAuthoringTransport([response, _review_response()])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "controls_failed"
    assert [request["stage"] for request in transport.requests] == ["correction"]
    assert result.package is None
    assert not (tmp_path / "package").exists()


def test_o04_corrected_deterministic_failure_stops_before_controls_review_and_package(
    tmp_path: Path,
) -> None:
    metadata = _saved_metadata()
    metadata["stimulus"]["delivery"] = "unsupported_delivery"
    transport = ScriptedAuthoringTransport(
        [_correction_response(metadata=metadata), _review_response()]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "correction_failed"
    assert [request["stage"] for request in transport.requests] == ["correction"]
    assert result.package is None
    assert not (tmp_path / "package").exists()


def test_o04_continuation_cannot_be_run_twice_or_retry_provider(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([_correction_response(), _review_response()])
    continuation = _prepared(tmp_path)

    first = continuation.run(transport_factory=lambda: transport)
    second = continuation.run(
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("retry"))
    )

    assert first.status == "accepted"
    assert second.status == "continuation_already_completed"
    assert len(transport.requests) == 2


def test_o04_nonzero_transport_retry_policy_stops_before_request(
    tmp_path: Path,
) -> None:
    class RetryingTransport:
        max_retries = 1

        def complete(self, packet: object) -> bytes:
            raise AssertionError(f"unexpected request: {packet!r}")

    result = _prepared(tmp_path).run(transport_factory=RetryingTransport)

    assert result.status == "preflight_defect"
    assert result.findings[0].code == "retry_policy"
    assert result.budget["continuation_correction_spent"] == 0
    assert result.package is None
    assert not (tmp_path / "package").exists()


def test_o04_continuation_rejects_authority_mismatch_before_transport(tmp_path: Path) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_correction_response()])

    from asago_artifact_generator.authoring import run_o04_correction_continuation

    result = run_o04_correction_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        expected_failure_sidecar_sha256="0" * 64,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        package_dir=tmp_path / "package",
        task_id="O04-correction-preflight-mismatch",
        transport_factory=transport_factory,
    )

    assert result.status == "preflight_defect"
    assert result.findings[0].code == "preflight_authority"
    assert called is False
    assert result.package is None
    assert result.ledger == []
