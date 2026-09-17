"""R6 production prompt and schema regressions."""

from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.design.authoring import _author_system_prompt
from tests.design_fixtures import prebound_result
from tests.test_artifact_design import STIMULUS, _designed, _functional_verified


def test_functional_author_prompt_is_kind_aware() -> None:
    prompt = _author_system_prompt(
        amount_bearing=True,
        scenario_kind="functional",
    )

    assert "functional scenario" in prompt
    assert "legitimate request" in prompt
    assert "adversarial scenario" not in prompt
    assert "what they gain" not in prompt
    assert "malicious objective" in prompt


def test_adversarial_prompt_requires_supported_objective_not_benefit() -> None:
    prompt = _author_system_prompt(
        amount_bearing=False,
        scenario_kind="adversarial",
    )

    assert "adversarial scenario" in prompt
    assert "possible benefit alone does not establish" in prompt
    assert "unsupported rather than guessing" in prompt


def test_domain_guidance_is_scoped_to_supplied_role() -> None:
    prompt = _author_system_prompt(
        amount_bearing=False,
        scenario_kind="functional",
        target_context={
            "required_argument_context": [
                {
                    "argument": "patient_id",
                    "role": "non_attacked_context",
                    "delivery": "user_prompt",
                    "value": "PAT-104",
                }
            ],
            "selected_operation": {
                "name": "commit_to_ehr",
                "description": "Commit a reviewed patient draft.",
            },
        },
    )

    assert "patient identity" in prompt
    assert "customer identity" not in prompt
    assert "guest and host" not in prompt
    assert "permission" in prompt
    assert "clinical-review" in prompt


@pytest.mark.parametrize(
    ("approach", "required", "forbidden"),
    [
        (
            "direct_request",
            ("first and only user turn", "author sees designer-only"),
            ("materially different reason",),
        ),
        (
            "designed_history",
            ("materially different reason", "target-visible context"),
            ("first and only user turn",),
        ),
    ],
)
def test_history_visibility_guidance_matches_approach(
    approach: str,
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> None:
    prompt = _author_system_prompt(
        amount_bearing=False,
        scenario_kind="functional",
        design_approach=approach,
    )

    for text in required:
        assert text in prompt
    for text in forbidden:
        assert text not in prompt


def test_unknown_handling_is_request_metadata_not_an_output_field() -> None:
    """The author contract names unknown handling without demanding fake fields."""

    prompt = _author_system_prompt(False, scenario_kind="functional")
    assert "UNKNOWN" not in prompt
    assert "target-visible context" in prompt


def test_functional_production_request_carries_kind_and_unknown_contract() -> None:
    captured: dict[str, Any] = {}

    class CapturingAuthor:
        def author(self, request: dict[str, Any]) -> dict[str, Any]:
            captured.update(request)
            return {
                **prebound_result(
                    STIMULUS,
                    100.0,
                    argument_values={"reason": "The lamp arrived broken"},
                )
            }

    outcome = _designed(
        verified=_functional_verified(),
        author=CapturingAuthor(),
    )

    assert outcome.exclusion is None
    assert captured["scenario_kind"] == "functional"
    assert "legitimate request" in captured["scenario_kind_meaning"]
    assert captured["unknown_handling"]["required_identity"].startswith("unresolved blocks")
    assert captured["unknown_handling"]["actor_subtype"].startswith("not applicable")
