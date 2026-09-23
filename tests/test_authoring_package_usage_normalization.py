"""Package usage summaries preserve the closed metadata envelope."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import AuthoringError, _package_from_responses
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
            "detector_source": "def evaluate(evidence): return {}\n",
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
