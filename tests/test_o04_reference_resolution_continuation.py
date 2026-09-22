from __future__ import annotations

import base64
import hashlib
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
    prepare_o04_reference_resolution_continuation,
)
from asago_artifact_generator.profiles import load_authoring_profile

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
PRIOR_EVIDENCE = (
    MISSION_ROOT
    / "evidence/o04-feedback-continuation-20260921/continuation-evidence.json"
)
PRIOR_REPORT = (
    MISSION_ROOT
    / "evidence/o04-feedback-continuation-delivery-20260921/report.md"
)
PRIOR_ACCOUNTING = (
    MISSION_ROOT
    / "evidence/o04-feedback-continuation-delivery-20260921/accounting.json"
)
PRIOR_RECONCILIATION = (
    MISSION_ROOT
    / "evidence/o04-feedback-continuation-delivery-20260921/attempt-reconciliation.json"
)

TASK_ID = "O04-reference-resolution-20260922"
PACKAGE_RELATIVE = Path("runs/authoring") / TASK_ID
EVIDENCE_RELATIVE = Path("evidence") / "o04-reference-resolution-20260922" / (
    "continuation-evidence.json"
)
LIVE_EVIDENCE_RELATIVE = (
    Path("evidence")
    / "o04-reference-resolution-live-20260922"
    / "continuation-evidence.json"
)
READINESS_RELATIVE = Path("evidence") / "o04-reference-resolution-readiness-20260922"
DELIVERY_RELATIVE = Path("evidence") / "o04-reference-resolution-delivery-20260922"
PRESERVED_ZERO_ATTEMPT_EVIDENCE = (
    MISSION_ROOT
    / "evidence/o04-reference-resolution-20260922/continuation-evidence.json"
)
PRESERVED_ZERO_ATTEMPT_SHA256 = (
    "908420645411856cb339af667108878430463e62568dccd373f1942d82318fbb"
)

PRIOR_EVIDENCE_SHA256 = (
    "0560f60a9d00f23aad1345556b3956dc03fd742d9a87b77263bd5d3959d6aee8"
)
PRIOR_REPORT_SHA256 = (
    "bd4852adec5adaf7499c3354c0bb5e8c91a0508de66f2ef84c5ab7abfb2c6ad0"
)
PRIOR_ACCOUNTING_SHA256 = (
    "cadfe1079d2b325a8c01adab25b656ed7426260b8ea0cd97192316bb2744198e"
)
PRIOR_RECONCILIATION_SHA256 = (
    "c3505b3d6dc2c2ebc030c0bbb07427c9f2b7b1d5ecb462f23da94abb59fa82ec"
)
FAILURE_SIDECAR_SHA256 = (
    "7e6d3c8814e6138400751f61a89558ec377c89622c87617d1113a91232763abc"
)
MISMATCH_PROOF_SHA256 = (
    "a1199338d91acacbd2416136d85a26166e0686d0695669ef12a8dabfe90c2c98"
)

CANDIDATE_SHA256 = (
    "04b4b57a6933196189c2e7b0ac07b5b5e06829b248fcced5f9348a50da804863"
)
RAW_SHA256 = (
    "ba4ad380b287b47d3fc26c3c65f4bfcb4f5ee54dc5b00c115d7df60addb37a7c"
)
METADATA_SHA256 = (
    "2f6e6bfbed120b5acf83dab8799ff5e5a8c946941bca1b4b47d5154f9459ecef"
)
PYTHON_SHA256 = (
    "e69563fbd30829eb55a01ea7af399f9c620e6f9d1279e7e9629b30c46cc48331"
)
PLAN_SHA256 = (
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


def _prior_candidate() -> tuple[bytes, dict]:
    evidence = json.loads(PRIOR_EVIDENCE.read_text(encoding="utf-8"))
    raw = base64.b64decode(evidence["attempts"][-1]["raw_response"]["base64"])
    return raw, parse_call2_response(raw).metadata


def _correction_response(metadata: dict | None = None) -> bytes:
    _, prior_metadata = _prior_candidate()
    return (
        b"```json\n"
        + json.dumps(metadata or prior_metadata, sort_keys=True).encode()
        + b"\n```\n```python\n"
        + CONFORMANT_DETECTOR
        + b"```\n"
    )


def _review_response(decision: str = "accept") -> bytes:
    findings = (
        []
        if decision == "accept"
        else [
            {
                "location": "detector.py",
                "problem": "The corrected detector still needs a bounded change.",
                "basis": "The scripted reference-resolution review exercises terminal routing.",
                "required_change": "Preserve the finding without another request.",
            }
        ]
    )
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted reference-resolution {decision} outcome",
            "findings": findings,
        }
    ).encode()


def _populate_readiness_root(tmp_path: Path) -> Path:
    readiness_root = tmp_path / READINESS_RELATIVE
    readiness_root.mkdir(parents=True, exist_ok=True)
    (readiness_root / "readiness.json").write_text(
        '{"decision":"GO_FOR_ORCHESTRATOR_REVIEW_ONLY"}\n',
        encoding="utf-8",
    )
    (readiness_root / "readiness-report.md").write_text(
        "offline readiness authority\n",
        encoding="utf-8",
    )
    return readiness_root


def _prepared(
    tmp_path: Path,
    *,
    readiness_root: Path | None = None,
    evidence_path: Path | None = None,
):
    if readiness_root is None:
        readiness_root = _populate_readiness_root(tmp_path)
    return prepare_o04_reference_resolution_continuation(
        failure_sidecar=FAILURE_SIDECAR,
        mismatch_proof=MISMATCH_PROOF,
        prior_continuation_evidence=PRIOR_EVIDENCE,
        prior_delivery_report=PRIOR_REPORT,
        prior_accounting=PRIOR_ACCOUNTING,
        prior_attempt_reconciliation=PRIOR_RECONCILIATION,
        package_dir=tmp_path / PACKAGE_RELATIVE,
        evidence_path=evidence_path or tmp_path / LIVE_EVIDENCE_RELATIVE,
        readiness_root=readiness_root,
        delivery_root=tmp_path / DELIVERY_RELATIVE,
        expected_failure_sidecar_sha256=FAILURE_SIDECAR_SHA256,
        expected_mismatch_proof_sha256=MISMATCH_PROOF_SHA256,
        expected_prior_continuation_evidence_sha256=PRIOR_EVIDENCE_SHA256,
        expected_prior_delivery_report_sha256=PRIOR_REPORT_SHA256,
        expected_prior_accounting_sha256=PRIOR_ACCOUNTING_SHA256,
        expected_prior_attempt_reconciliation_sha256=PRIOR_RECONCILIATION_SHA256,
        expected_candidate_sha256=CANDIDATE_SHA256,
        expected_raw_sha256=RAW_SHA256,
        expected_metadata_sha256=METADATA_SHA256,
        expected_python_sha256=PYTHON_SHA256,
        expected_plan_sha256=PLAN_SHA256,
        expected_control_fixture_sha256=CONTROL_FIXTURE_SHA256,
        task_id=TASK_ID,
    )


def _profile_scripted_transport(responses: list[object]) -> ScriptedAuthoringTransport:
    transport = ScriptedAuthoringTransport(responses)
    transport.profile_name = "gemma4-oc"
    return transport


def _opaque_wire_model() -> str:
    return "opaque-wire-model"


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


def test_reference_resolution_seals_candidate_authority_and_budget(tmp_path: Path) -> None:
    continuation = _prepared(tmp_path)

    assert continuation.task_id == TASK_ID
    assert continuation.artifact.candidate_sha256 == CANDIDATE_SHA256
    assert continuation.artifact.authority["prior_feedback_evidence"]["sha256"] == (
        PRIOR_EVIDENCE_SHA256
    )
    assert continuation.prior_author_correction_spend == 9
    assert continuation.prior_review_spend == 1
    assert continuation.aggregate_spent == 21
    assert continuation.task_limit == 12
    assert continuation.continuation_author_limit == 1
    assert continuation.continuation_review_limit == 1


def test_reference_resolution_requires_populated_readiness_authority(
    tmp_path: Path,
) -> None:
    continuation = _prepared(tmp_path)

    assert continuation.readiness_root == tmp_path / READINESS_RELATIVE
    assert continuation.readiness_root.is_dir()
    assert any(continuation.readiness_root.iterdir())


@pytest.mark.parametrize("state", ["missing", "empty", "file"])
def test_reference_resolution_rejects_missing_or_invalid_readiness(
    tmp_path: Path,
    state: str,
) -> None:
    readiness_root = tmp_path / READINESS_RELATIVE
    if state == "empty":
        readiness_root.mkdir(parents=True)
    elif state == "file":
        readiness_root.parent.mkdir(parents=True)
        readiness_root.write_text("not a readiness root\n", encoding="utf-8")

    with pytest.raises(
        O04ContinuationValidationError,
        match=r"readiness.*(required|valid|populated)",
    ):
        _prepared(tmp_path, readiness_root=readiness_root)


def test_reference_resolution_rejects_occupied_fresh_live_evidence(
    tmp_path: Path,
) -> None:
    _populate_readiness_root(tmp_path)
    live_root = tmp_path / LIVE_EVIDENCE_RELATIVE
    live_root.mkdir(parents=True)

    with pytest.raises(O04ContinuationValidationError, match="evidence"):
        _prepared(tmp_path)


def test_reference_resolution_rejects_occupied_package_root(tmp_path: Path) -> None:
    _populate_readiness_root(tmp_path)
    package_root = tmp_path / PACKAGE_RELATIVE
    package_root.mkdir(parents=True)

    with pytest.raises(O04ContinuationValidationError, match="package"):
        _prepared(tmp_path)


def test_reference_resolution_rejects_occupied_delivery_root(tmp_path: Path) -> None:
    _populate_readiness_root(tmp_path)
    delivery_root = tmp_path / DELIVERY_RELATIVE
    delivery_root.mkdir(parents=True)

    with pytest.raises(O04ContinuationValidationError, match="delivery"):
        _prepared(tmp_path)


def test_reference_resolution_preserves_zero_attempt_historical_evidence(
    tmp_path: Path,
) -> None:
    before = hashlib.sha256(PRESERVED_ZERO_ATTEMPT_EVIDENCE.read_bytes()).hexdigest()
    assert before == PRESERVED_ZERO_ATTEMPT_SHA256

    with pytest.raises(O04ContinuationValidationError, match="fresh sealed names"):
        _prepared(tmp_path, evidence_path=PRESERVED_ZERO_ATTEMPT_EVIDENCE)

    after = hashlib.sha256(PRESERVED_ZERO_ATTEMPT_EVIDENCE.read_bytes()).hexdigest()
    assert after == before


def test_reference_resolution_accepts_named_profile_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        (
            "gemma4-oc:\n"
            "  base_url: https://profile.example.invalid/v1\n"
            "  api_key: synthetic-profile-key\n"
            "  model: opaque-wire-model\n"
        ),
        encoding="utf-8",
    )
    profile = load_authoring_profile(profiles_file, "gemma4-oc")

    captured_request: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            captured_request.update(kwargs)
            raise TimeoutError("offline transport fixture")

    class FakeOpenAI:
        def __init__(self, **_: object) -> None:
            self.chat = type("Chat", (), {})()
            self.chat.completions = FakeCompletions()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    transport = PrivateModelAuthoringTransport(
        base_url=profile.base_url,
        api_key=profile.api_key,
        model=profile.model,
        profile_name=profile.name,
    )
    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "transport_failure"
    assert transport.profile_name == "gemma4-oc"
    assert transport.model == profile.model
    assert transport.model != transport.profile_name
    assert captured_request["model"] == profile.model
    evidence = result.failure_evidence_path.read_text(encoding="utf-8")
    assert profile.model not in evidence


@pytest.mark.parametrize("profile_name", [None, "other-public-profile"])
def test_reference_resolution_rejects_missing_or_wrong_profile_attestation(
    tmp_path: Path,
    profile_name: str | None,
) -> None:
    transport = _profile_scripted_transport([b"unexpected response"])
    wire_model = _opaque_wire_model()
    transport.model = wire_model
    if profile_name is None:
        del transport.profile_name
    else:
        transport.profile_name = profile_name

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "preflight_defect"
    assert result.findings[0].code == "model_profile"
    assert result.findings[0].path == "transport.profile_name"
    assert transport.requests == []
    assert transport.model == wire_model
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    assert evidence["attempts"] == []
    assert evidence["ledger"] == []
    assert evidence["reviews"] == []
    assert evidence["budget"]["reference_resolution_correction_spent"] == 0
    assert wire_model not in json.dumps(evidence)


@pytest.mark.parametrize(
    "pin_name",
    [
        "expected_prior_continuation_evidence_sha256",
        "expected_prior_delivery_report_sha256",
        "expected_prior_accounting_sha256",
        "expected_prior_attempt_reconciliation_sha256",
        "expected_candidate_sha256",
        "expected_raw_sha256",
        "expected_metadata_sha256",
        "expected_python_sha256",
        "expected_plan_sha256",
        "expected_control_fixture_sha256",
    ],
)
def test_reference_resolution_rejects_tampered_authority_before_transport(
    tmp_path: Path, pin_name: str
) -> None:
    kwargs = {
        "failure_sidecar": FAILURE_SIDECAR,
        "mismatch_proof": MISMATCH_PROOF,
        "prior_continuation_evidence": PRIOR_EVIDENCE,
        "prior_delivery_report": PRIOR_REPORT,
        "prior_accounting": PRIOR_ACCOUNTING,
        "prior_attempt_reconciliation": PRIOR_RECONCILIATION,
        "package_dir": tmp_path / PACKAGE_RELATIVE,
        "evidence_path": tmp_path / EVIDENCE_RELATIVE,
        "readiness_root": tmp_path / READINESS_RELATIVE,
        "delivery_root": tmp_path / DELIVERY_RELATIVE,
        "task_id": TASK_ID,
    }
    kwargs[pin_name] = "0" * 64

    with pytest.raises(O04ContinuationValidationError, match="authority|candidate|plan"):
        prepare_o04_reference_resolution_continuation(**kwargs)


def test_reference_resolution_packet_has_exact_feedback_and_owner_distinction(
    tmp_path: Path,
) -> None:
    transport = _profile_scripted_transport([TimeoutError("provider unavailable")])
    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "transport_failure"
    assert [request["stage"] for request in transport.requests] == ["correction"]
    prompt = transport.requests[0]["user"]
    raw, _ = _prior_candidate()
    assert prompt.count(raw.decode("utf-8")) == 1
    for marker in (
        "judge-missing",
        "judge-support-unresolved",
        "evidence reference 'judge' does not resolve",
        "evidence reference 'messages[99]' does not resolve",
        "validate_detector_result",
        "nine passing controls",
        "complete corrected artifact",
        "missing path in `reason`",
        "nonexistent path from `evidence_refs`",
        "existing container or field must exist",
        "valid supporting references for decisive outcomes",
    ):
        assert marker in prompt
    assert CONFORMANT_DETECTOR.decode() not in prompt
    assert "manual patch" not in prompt
    assert "new response format" not in prompt
    assert transport.requests[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_reference_resolution_persists_raw_before_parse_and_never_retries(
    tmp_path: Path,
) -> None:
    raw = b"not a two-block artifact"
    transport = _profile_scripted_transport([raw])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "correction_unavailable"
    assert len(transport.requests) == 1
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    response = evidence["attempts"][0]["raw_response"]
    assert response["availability"] == "available"
    assert response["sha256"] == hashlib.sha256(raw).hexdigest()
    assert response["byte_length"] == len(raw)
    assert evidence["reviews"] == []
    assert evidence["budget"]["reference_resolution_correction_spent"] == 1
    assert evidence["budget"]["reference_resolution_review_spent"] == 0


def test_reference_resolution_controls_gate_review(
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
    result = _prepared(tmp_path).run(
        transport_factory=lambda: _profile_scripted_transport(
            [_correction_response()]
        )
    )

    assert result.status == "controls_failed"
    assert [request["stage"] for request in result.ledger] == ["correction"]
    assert result.budget["reference_resolution_correction_spent"] == 1
    assert result.budget["reference_resolution_review_spent"] == 0
    assert result.package is None


def test_reference_resolution_accept_only_reaches_all_new_ceilings(
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
    transport = _profile_scripted_transport(
        [_correction_response(), _review_response("accept")]
    )

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.budget["reference_resolution_correction_spent"] == 1
    assert result.budget["reference_resolution_review_spent"] == 1
    assert result.budget["aggregate_spent"] == 23
    assert result.budget["aggregate_combined_spent"] == 62
    assert result.budget["task_spent"] == 12
    assert result.budget["author_correction_spent"] == 10
    assert result.budget["review_spent"] == 2
    assert result.package is not None
    assert result.package.manifest.authoring["continuation"]["mode"] == (
        "sealed-o04-reference-resolution-continuation"
    )


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (_review_response("blocked"), "blocked"),
        (_review_response("revise"), "revise"),
        (b"not review JSON", "review_unavailable"),
        (TimeoutError("review unavailable"), "review_unavailable"),
    ],
)
def test_reference_resolution_review_non_accept_is_terminal(
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
    transport = _profile_scripted_transport([_correction_response(), response])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == status
    assert [request["stage"] for request in transport.requests] == [
        "correction",
        "artifact_review",
    ]
    assert result.package is None
    assert result.budget["reference_resolution_correction_spent"] == 1
    assert result.budget["reference_resolution_review_spent"] == 1


def test_reference_resolution_rejects_historical_collisions(
    tmp_path: Path,
) -> None:
    with pytest.raises(O04ContinuationValidationError, match="task identity"):
        prepare_o04_reference_resolution_continuation(
            failure_sidecar=FAILURE_SIDECAR,
            mismatch_proof=MISMATCH_PROOF,
            prior_continuation_evidence=PRIOR_EVIDENCE,
            prior_delivery_report=PRIOR_REPORT,
            prior_accounting=PRIOR_ACCOUNTING,
            prior_attempt_reconciliation=PRIOR_RECONCILIATION,
            package_dir=tmp_path / PACKAGE_RELATIVE,
            evidence_path=tmp_path / EVIDENCE_RELATIVE,
            readiness_root=tmp_path / READINESS_RELATIVE,
            delivery_root=tmp_path / DELIVERY_RELATIVE,
            task_id="O04-feedback-continuation-20260921",
        )

    existing_package = tmp_path / PACKAGE_RELATIVE
    existing_package.mkdir(parents=True)
    with pytest.raises(O04ContinuationValidationError, match="package path"):
        _prepared(tmp_path)
