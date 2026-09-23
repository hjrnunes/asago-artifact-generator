import json

import pytest

from asago_artifact_generator.authoring import _correction_detector_feedback_view
from asago_artifact_generator.detector_controls import (
    ControlCase,
    _control_result,
    build_detector_feedback,
)
from asago_artifact_generator.detector_runtime import DetectorExecution


def execution(stdout: bytes) -> DetectorExecution:
    return DetectorExecution(
        status="failed",
        result=None,
        failure="detected and not_detected results require evidence_refs",
        package_digest=None,
        package_digest_after=None,
        detector_sha256=None,
        detector_sha256_after=None,
        docker_argv=(),
        stdout=stdout,
    )


def test_invalid_return_is_available_for_feedback_but_never_a_validated_verdict():
    raw = {
        "outcome": "not_detected",
        "claim_level": "command_attempt",
        "reason": "incorrect absence",
        "evidence_refs": [],
    }
    case = ControlCase("unreadable-call", {"tool_calls": []}, "inconclusive")
    record = _control_result(case, execution(json.dumps({"status": "ok", "result": raw}).encode()))
    assert record.status == "runtime_failure"
    assert record.observed_outcome is None
    assert record.actual_result == raw
    feedback = build_detector_feedback([case], [record])[0]
    assert feedback.outcome_class == "invalid_returned_result"
    assert feedback.actual_result == raw
    assert "unvalidated" in feedback.runtime_contract_explanation
    rendered = _correction_detector_feedback_view({"failed_controls": [feedback.as_dict()]})
    explanation = rendered["failed_controls"][0]["explanation"]
    assert "detected and not_detected results require evidence_refs" in explanation
    assert "also differs from expected 'inconclusive'" in explanation


@pytest.mark.parametrize("stdout", [b"", b"broken", b'{"status":"error","error":"boom"}'])
def test_unavailable_raw_return_is_not_reconstructed(stdout):
    case = ControlCase("unavailable", {}, "inconclusive")
    record = _control_result(case, execution(stdout))
    assert record.actual_result is None
    assert record.status == "runtime_failure"
