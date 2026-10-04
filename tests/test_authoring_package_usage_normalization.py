"""Package usage summaries preserve the closed metadata envelope."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import AuthoringError
from asago_artifact_generator.authoring.package_assembly import (
    _package_from_responses,
    _package_review_records,
)
from asago_artifact_generator.input_adapter import InputKind, load_input

HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "scenario-handoff"
    / "handoff-v1"
    / "valid"
    / "adversarial-refund.json"
)


def _package_with_usage(usage: object) -> object:
    return _package_from_responses(
        view=load_input(HANDOFF, kind=InputKind.SCENARIO_HANDOFF_V1),
        plan={},
        artifact={
            "stimulus": {},
            "setup_recipe": [],
            "runtime_bindings": [],
            "prerequisites": [],
            "required_observations": {},
            "semantic_judge_spec": None,
            "explanation": "",
            "examples": {},
        },
        task_id="usage-normalization",
        ledger=[{"stage": "call2", "usage": deepcopy(usage)}],
        raw_responses={},
        decoded_responses={},
        prompt_packets={},
        transformations=[],
        inventory={},
        runtime_contract={},
        detector_bytes=b"def evaluate(evidence): return {}\n",
    )


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"availability": "available", "value": {"prompt_tokens": 12}},
            {"availability": "available", "value": {"prompt_tokens": 12}},
        ),
        (
            {"availability": "unavailable", "reason": "provider_did_not_report_usage"},
            {"availability": "unavailable", "reason": "provider_did_not_report_usage"},
        ),
        (
            {"prompt_tokens": 12, "completion_tokens": 3},
            {
                "availability": "available",
                "value": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        ),
        (
            None,
            {"availability": "unavailable", "reason": "provider_did_not_report_usage"},
        ),
    ],
)
def test_package_usage_summary_normalizes_raw_and_wrapped_records(
    usage: object, expected: dict[str, object]
) -> None:
    package = _package_with_usage(usage)

    assert package.manifest.authoring["usage"] == [expected]


def test_package_usage_summary_keeps_invalid_wrapped_metadata_rejected() -> None:
    usage = {
        "availability": "available",
        "value": {"prompt_tokens": 12, "api_key": "must-not-pass"},
    }

    with pytest.raises(AuthoringError):
        _package_with_usage(usage)


_PRESERVED = {"plan": {"status": "accepted"}, "artifact": "not a record"}
_LEDGER = [
    {"stage": "plan_review", "review": {"status": "revise"}},
    {"stage": "plan_review", "review": "ignored"},
    {"stage": "plan_review", "review": {"status": "accepted"}},
    {"stage": "call1"},
]


@pytest.mark.parametrize(
    ("ledger", "review_status", "preserved", "expected"),
    [
        (_LEDGER, None, None, {"status": "accepted"}),
        (_LEDGER, {"plan": "not_requested"}, None, {"status": "not_requested"}),
        ([], {"plan": "unavailable"}, _PRESERVED, {"status": "accepted"}),
        ([], {"plan": "unavailable"}, "not a mapping", {"status": "unavailable"}),
        ([], None, _PRESERVED, {"status": "accepted"}),
        ([], {}, None, {"status": "not_requested"}),
    ],
)
def test_package_review_records_plan_truth(
    ledger: list[dict[str, object]],
    review_status: dict[str, str] | None,
    preserved: object,
    expected: dict[str, object],
) -> None:
    records = _package_review_records(
        ledger, review_status=review_status, preserved_reviews=preserved
    )

    assert records["plan"] == expected
    assert records["artifact"] == {
        "status": (review_status or {}).get("artifact", "not_requested")
    }


def test_package_review_records_copies_the_selected_review() -> None:
    records = _package_review_records(_LEDGER, review_status=None)

    records["plan"]["status"] = "changed"

    assert _LEDGER[2]["review"] == {"status": "accepted"}
