from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    A03ContinuationValidationError,
    ScriptedAuthoringTransport,
    prepare_a03_recovered_continuation,
    run_a03_recovered_continuation,
)

RECOVERY_SIDECAR = Path(
    "/Users/hjrnunes/.factory/missions/"
    "fb1ebe78-2eda-4bd9-a9c7-f51f0028f03a/evidence/"
    "offline-saved-artifact-recovery-20260920/recovery-candidates.json"
)
RECOVERY_SIDECAR_SHA256 = (
    "7d668116bfb7e8070da034c18f321a5d257ca2f5bc5e36a91554524fcc8e773c"
)
CANDIDATE_SHA256 = "9f634b9bf73d805cd9b13a917ceebc4a0640c9e2da3ce2f27a897ef5b0272a94"


def _review(decision: str = "accept") -> bytes:
    findings = (
        []
        if decision == "accept"
        else [
            {
                "location": "detector.py",
                "problem": "The review needs a terminal decision.",
                "basis": "The scripted fixture exercises terminal review routing.",
                "required_change": "Preserve the terminal outcome without a correction.",
            }
        ]
    )
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted {decision} continuation outcome",
            "findings": findings,
        }
    ).encode()


def _prepared(tmp_path: Path):
    return prepare_a03_recovered_continuation(
        recovery_sidecar=RECOVERY_SIDECAR,
        expected_recovery_sidecar_sha256=RECOVERY_SIDECAR_SHA256,
        package_dir=tmp_path / "package",
        task_id="A03-recovered-continuation-test",
    )


def test_preflight_rejects_sidecar_mismatch_before_transport_or_package(
    tmp_path: Path,
) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_review()])

    result = run_a03_recovered_continuation(
        recovery_sidecar=RECOVERY_SIDECAR,
        expected_recovery_sidecar_sha256="0" * 64,
        package_dir=tmp_path / "package",
        task_id="A03-recovered-preflight-mismatch",
        transport_factory=transport_factory,
    )

    assert result.status == "preflight_defect"
    assert result.findings[0].code == "preflight_authority"
    assert called is False
    assert result.package is None
    assert result.ledger == []
    assert not (tmp_path / "package").exists()


def test_sealed_continuation_seeds_spend_and_dispatches_only_one_review(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([_review()])

    result = _prepared(tmp_path).run(transport_factory=lambda: transport)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["artifact_review"]
    assert result.budget["author_correction_spent"] == 5
    assert result.budget["review_spent"] == 2
    assert result.budget["task_spent"] == 7
    assert result.budget["aggregate_spent"] == 16
    assert result.package is not None
    assert result.review["candidate_bytes_sha256"] == CANDIDATE_SHA256
    assert result.review["decision"] == "accept"


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (_review("revise"), "revise"),
        (_review("blocked"), "blocked"),
        (b'{"decision":"accept","summary":"contradictory","findings":[{}]}', "review_unavailable"),
        (TimeoutError("review transport stopped"), "transport_failure"),
    ],
)
def test_non_accept_review_outcomes_are_terminal_without_package_or_retry(
    tmp_path: Path,
    response: object,
    expected_status: str,
) -> None:
    transport = ScriptedAuthoringTransport([response])
    continuation = _prepared(tmp_path)

    result = continuation.run(transport_factory=lambda: transport)

    assert result.status == expected_status
    assert result.package is None
    assert len(transport.requests) == 1
    assert [record["stage"] for record in result.ledger] == ["artifact_review"]
    assert not (tmp_path / "package").exists()
    assert result.failure_evidence_path is not None
    evidence = json.loads(result.failure_evidence_path.read_text(encoding="utf-8"))
    assert evidence["terminal_status"] == expected_status
    assert len(evidence["attempts"]) == 1


def test_continuation_cannot_be_run_twice_or_enter_authoring_paths(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([_review()])
    continuation = _prepared(tmp_path)

    first = continuation.run(transport_factory=lambda: transport)
    second = continuation.run(
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("retry"))
    )

    assert first.status == "accepted"
    assert second.status == "continuation_already_completed"
    assert len(transport.requests) == 1


def test_accept_package_carries_exact_candidate_and_review_evidence(tmp_path: Path) -> None:
    result = _prepared(tmp_path).run(
        transport_factory=lambda: ScriptedAuthoringTransport(
            [_review()],
        )
    )

    assert result.package is not None
    loaded = result.package
    reviews = json.loads(loaded.members["authoring/reviews.json"])
    assert reviews["plan"]["status"] == "accepted"
    assert reviews["artifact"]["status"] == "accepted"
    assert reviews["artifact"]["accepted_plan_sha256"] == (
        "fc8f4245dfd8ddcdb0609d2af681759b3ebd70d60649bb9a1139c9a88af807ed"
    )
    assert len(reviews["artifact"]["original_input_pins"]) == 10
    assert reviews["artifact"]["prompt_version"] == "authoring-artifact-review-v2"
    assert reviews["artifact"]["effective_controls"]["max_retries"] == 0
    assert reviews["artifact"]["candidate_bytes_sha256"] == CANDIDATE_SHA256
    assert reviews["artifact"]["raw_response_sha256"] == hashlib.sha256(
        json.dumps(
            {
                "decision": "accept",
                "summary": "scripted accept continuation outcome",
                "findings": [],
            }
        ).encode()
    ).hexdigest()
    authoring = loaded.manifest.authoring
    assert authoring["continuation"]["mode"] == "sealed-a03-artifact-review"
    assert authoring["continuation"]["candidate_sha256"] == CANDIDATE_SHA256
    assert authoring["budget"]["author_correction_spent"] == 5
    assert hashlib.sha256(loaded.members["authoring/recovered-candidate.raw"]).hexdigest() == (
        CANDIDATE_SHA256
    )


def test_prepare_surfaces_typed_preflight_failure(tmp_path: Path) -> None:
    with pytest.raises(A03ContinuationValidationError, match="recovery sidecar hash"):
        prepare_a03_recovered_continuation(
            recovery_sidecar=RECOVERY_SIDECAR,
            expected_recovery_sidecar_sha256="0" * 64,
            package_dir=tmp_path / "package",
            task_id="A03-recovered-preflight-error",
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"expected_candidate_sha256": "0" * 64}, "candidate hash"),
        ({"expected_plan_sha256": "0" * 64}, "plan hash"),
    ],
)
def test_preflight_rejects_candidate_and_plan_pin_mismatches(
    tmp_path: Path,
    override: dict[str, str],
    message: str,
) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_review()])

    result = run_a03_recovered_continuation(
        recovery_sidecar=RECOVERY_SIDECAR,
        package_dir=tmp_path / "package",
        task_id="A03-recovered-pin-mismatch",
        transport_factory=transport_factory,
        **override,
    )

    assert result.status == "preflight_defect"
    assert message in result.findings[0].detail
    assert called is False
    assert result.ledger == []
    assert result.package is None


def test_continuation_budget_guard_stops_before_review_dispatch(tmp_path: Path) -> None:
    called = False

    def transport_factory() -> ScriptedAuthoringTransport:
        nonlocal called
        called = True
        return ScriptedAuthoringTransport([_review()])

    continuation = prepare_a03_recovered_continuation(
        recovery_sidecar=RECOVERY_SIDECAR,
        package_dir=tmp_path / "package",
        task_id="A03-recovered-budget-stop",
        aggregate_limit=15,
    )
    result = continuation.run(transport_factory=transport_factory)

    assert result.status == "budget_exhausted"
    assert called is False
    assert result.budget["aggregate_spent"] == 15
    assert result.budget["review_spent"] == 1
    assert result.failure_evidence_path is not None
