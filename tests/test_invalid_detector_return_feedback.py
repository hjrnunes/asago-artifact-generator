import json

import pytest

from asago_artifact_generator.authoring.correction import _correction_detector_feedback_view
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


def _failed(**fields):
    item = {
        "name": "case",
        "evidence": {"tool_calls": []},
        "expected_outcome": "detected",
        "expected_claim_level": "command_attempt",
        "actual_result": None,
        "actual_outcome": None,
        "actual_claim_level": None,
        "error": None,
        "outcome_class": "pre_result_failure",
        "runtime_contract_explanation": "",
    }
    item.update(fields)
    return item


def test_feedback_view_passes_through_values_without_a_failed_control_list():
    assert _correction_detector_feedback_view(None) is None
    assert _correction_detector_feedback_view("text") == "text"
    unchanged = {"failed_controls": "none", "correction_guidance": "g"}
    assert _correction_detector_feedback_view(unchanged) is unchanged


def test_feedback_view_derives_the_actual_result_from_each_error_class():
    view = _correction_detector_feedback_view(
        {
            "failed_controls": [
                "not a control",
                _failed(name="slow", error="Detector Timeout after 5s"),
                _failed(name="raised", error="boom", outcome_class="detector_exception"),
                _failed(name="bad", error="no refs", outcome_class="invalid_returned_result"),
                _failed(name="early", error="no container"),
                _failed(name="no_error", error=None),
                _failed(name="no_dict_input", evidence=None, error="x"),
            ],
            "passing_controls": [
                "not a control",
                {"name": "ok", "observed_outcome": "not_detected", "status": "passed"},
            ],
            "correction_guidance": "guidance",
        }
    )

    actual = {entry["name"]: entry["actual"] for entry in view["failed_controls"]}
    assert actual == {
        "slow": {"timeout": "Detector Timeout after 5s"},
        "raised": {"exception": "boom"},
        "bad": {"invalid_result": "no refs"},
        "early": {"pre_result_failure": "no container"},
        "no_error": None,
        "no_dict_input": {"pre_result_failure": "x"},
    }
    first = view["failed_controls"][0]
    assert list(first) == ["name", "input", "input_shapes", "expected", "actual", "explanation"]
    assert first["expected"] == {"outcome": "detected", "claim_level": "command_attempt"}
    assert view["failed_controls"][-1]["input_shapes"] == {}
    assert view["passing_controls"] == [{"name": "ok", "outcome": "not_detected"}]
    assert view["correction_guidance"] == "guidance"


def test_feedback_view_without_passing_controls_renders_an_empty_list():
    view = _correction_detector_feedback_view({"failed_controls": []})

    assert view == {"failed_controls": [], "passing_controls": [], "correction_guidance": None}


@pytest.mark.parametrize(
    ("fields", "explanation"),
    [
        (
            {
                "outcome_class": "structurally_valid_wrong_outcome",
                "actual_result": {"outcome": "not_detected", "claim_level": "command_attempt"},
            },
            "returned outcome 'not_detected'; expected outcome 'detected'",
        ),
        (
            {
                "outcome_class": "structurally_valid_wrong_outcome",
                "actual_outcome": "detected",
                "actual_claim_level": "message_content",
            },
            "returned claim level 'message_content'; expected claim level 'command_attempt'",
        ),
        (
            {
                "outcome_class": "structurally_valid_wrong_outcome",
                "actual_result": {"outcome": "detected", "claim_level": "command_attempt"},
            },
            "returned result differs from the expected control result",
        ),
        ({"error": "TIMEOUT"}, "detector timed out before returning a result"),
        (
            {"outcome_class": "detector_exception", "error": "boom"},
            "detector raised an exception before returning a result",
        ),
        (
            {"outcome_class": "invalid_returned_result", "error": "no refs"},
            "Runtime rejected the unvalidated return: no refs.",
        ),
        (
            {
                "outcome_class": "invalid_returned_result",
                "error": "no refs",
                "actual_outcome": "detected",
            },
            "Runtime rejected the unvalidated return: no refs.",
        ),
        (
            {
                "outcome_class": "invalid_returned_result",
                "error": "no refs",
                "actual_outcome": "not_detected",
            },
            "Runtime rejected the unvalidated return: no refs. Its outcome 'not_detected' "
            "also differs from expected 'detected'.",
        ),
        (
            {"outcome_class": "container/evaluator_failure_before_result"},
            "container or evaluator failed before exposing a result",
        ),
        ({"runtime_contract_explanation": "runtime says so"}, "runtime says so"),
        ({}, "returned outcome or claim level differs from the expected control result"),
    ],
)
def test_feedback_explanation_covers_each_outcome_class(fields, explanation):
    view = _correction_detector_feedback_view({"failed_controls": [_failed(**fields)]})

    assert view["failed_controls"][0]["explanation"] == explanation


def _artifact_context():
    from asago_artifact_generator.authoring.prompt_context import build_artifact_author_context

    from .test_versioned_prompt_roles import _inventory, _plan, _runtime_contract, _view

    return build_artifact_author_context(_view(), _plan(), _inventory(), _runtime_contract())


def _feedback(name: str, status: str):
    from asago_artifact_generator.detector_controls import DetectorControlFeedback

    return DetectorControlFeedback(
        name=name,
        evidence={"tool_calls": []},
        expected_outcome="detected",
        expected_claim_level="command_attempt",
        status=status,
        actual_result=None,
        actual_outcome="not_detected" if status == "passed" else None,
        actual_claim_level=None,
        error="boom" if status != "passed" else None,
        outcome_class="detector_exception",
        runtime_contract_explanation="",
    )


def test_correction_context_moves_control_findings_after_other_findings():
    from asago_artifact_generator.authoring.core import Finding
    from asago_artifact_generator.authoring.correction import build_correction_context

    context = build_correction_context(
        failed_stage="call2",
        original_context=_artifact_context(),
        current_output=b"candidate",
        findings=[
            {"code": "detector_control_failure", "detail": "x", "path": "detector_controls.a"},
            Finding("semantic_review", "review", "artifact"),
            {"detail": "y", "path": "detector_controls.b"},
        ],
        prior_unresolved_findings=[{"code": "old", "detail": "z", "path": "p"}],
        detector_feedback=[_feedback("a", "runtime_failure"), _feedback("ok", "passed")],
    )

    assert context["findings"] == [
        {"code": "semantic_review", "detail": "review", "path": "artifact"},
        {
            "code": "detector_control_failure",
            "detail": "See the shared feedback section for the exact executed case.",
            "path": "detector_controls.a",
        },
        {
            "code": "detector_control_failure",
            "detail": "See the shared feedback section for the exact executed case.",
            "path": "detector_controls.b",
        },
    ]
    assert list(context) == [
        "stage",
        "failed_stage",
        "original_context",
        "current_output",
        "current_output_encoding",
        "findings",
        "instruction",
        "observation_guide",
        "evidence_packet_interface",
        "detector_feedback",
        "prior_unresolved_findings",
        "accepted_plan_fixed",
        "format",
        "response_contract",
    ]
    assert [item["name"] for item in context["detector_feedback"]["failed_controls"]] == ["a"]
    assert context["prior_unresolved_findings"] == [{"code": "old", "detail": "z", "path": "p"}]
    assert "closed scope for this stage" in context["instruction"]
    assert context["accepted_plan_fixed"] is True


def test_correction_context_rejects_an_unknown_stage():
    from asago_artifact_generator.authoring.correction import build_correction_context

    with pytest.raises(ValueError, match="unsupported correction stage: deploy"):
        build_correction_context(
            failed_stage="deploy",
            original_context={},
            current_output="text",
            findings=[],
        )
