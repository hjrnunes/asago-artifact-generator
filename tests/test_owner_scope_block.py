from __future__ import annotations

import json
from dataclasses import replace

import pytest

from asago_artifact_generator.authoring.prompt_context import (
    build_artifact_author_context,
    build_plan_author_context,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_reviewer_context,
    build_plan_reviewer_context,
)

from .golden_prompts import golden_text, read_golden
from .prompt_support import STAGES, render_stage_packets
from .support import world_builders

_framed, _inventory, _metadata, _plan, _runtime_contract, _view = world_builders(
    "ehr", "framed", "inventory", "metadata", "plan", "runtime_contract", "view"
)

_OWNER_SCOPE_LABEL = "OWNER-SUPPLIED SCOPE (NOT OBSERVED TARGET FACTS)"
_OWNER_SCOPE = {
    "scenario_premises": [
        {
            "text": "A caller supplied one premise for this scenario.",
            "source": "owner-scope-spec.md",
        }
    ],
    "evaluation_instructions": [
        {
            "text": "Evaluate only the captured result.",
            "source": "owner-scope-spec.md",
        }
    ],
}


def test_absent_or_empty_owner_scope_preserves_base_prompt_bytes() -> None:
    empty_views = (
        _view(),
        replace(_view(), owner_scope={}),
        replace(
            _view(),
            owner_scope={
                "scenario_premises": [],
                "evaluation_instructions": [],
            },
        ),
    )

    for view in empty_views:
        packets = render_stage_packets(view=view)
        assert {name: golden_text(packet) for name, packet in packets.items()} == {
            stage: read_golden("ehr", stage) for stage in STAGES
        }
        assert (
            "owner_supplied_scope"
            not in build_plan_author_context(view, _inventory(), _runtime_contract())[
                "field_guide"
            ]
        )


@pytest.mark.parametrize(
    "owner_scope",
    [
        {"scenario_premises": [{"text": "premise without source"}]},
        {"evaluation_instructions": [{"text": "   ", "source": "owner.md"}]},
        {"unrecognized": [{"text": "not allowed", "source": "owner.md"}]},
        {"scenario_premises": "not a sequence"},
    ],
)
def test_owner_scope_rejects_unlabeled_or_unsupported_input(owner_scope) -> None:
    view = replace(_view(), owner_scope=owner_scope)

    with pytest.raises(ValueError, match="owner_scope"):
        build_plan_author_context(view, _inventory(), _runtime_contract())


def test_owner_scope_is_separate_and_labeled_in_every_source_context_stage() -> None:
    view = replace(_view(), owner_scope=_OWNER_SCOPE)
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()
    expected_section = {
        "classification": (
            "This is owner-supplied context, separate from verified inventory facts "
            "and policy data. It is not an observed target fact or runtime evidence."
        ),
        **_OWNER_SCOPE,
    }

    author_context = build_plan_author_context(view, inventory, runtime)
    plan_review_context = build_plan_reviewer_context(view, plan, inventory, runtime)
    artifact_context = build_artifact_author_context(view, plan, inventory, runtime)
    artifact_review_context = build_artifact_reviewer_context(
        view,
        plan,
        _metadata(),
        inventory,
        runtime,
    )
    source_contexts = [
        (author_context["source_context"], author_context["owner_scope"]),
        (
            plan_review_context["authoritative_context"],
            plan_review_context["owner_scope"],
        ),
        (artifact_context["authoritative_context"], artifact_context["owner_scope"]),
        (
            artifact_review_context["authoritative_context"],
            artifact_review_context["owner_scope"],
        ),
    ]
    for context, owner_scope in source_contexts:
        assert owner_scope == expected_section
        assert "owner_scope" not in context
        verified_data = json.dumps(
            {
                "facts": context["facts"],
                "runtime_capabilities": context["runtime_capabilities"],
            },
            ensure_ascii=False,
        )
        assert "A caller supplied one premise for this scenario." not in verified_data
        assert "Evaluate only the captured result." not in verified_data

    # Verify the stage builders render the separate owner block into the request.
    packets = render_stage_packets(view=view).values()
    for packet in packets:
        assert packet.user.count(_OWNER_SCOPE_LABEL) == 1
        assert "scenario_premises" in packet.user
        assert "evaluation_instructions" in packet.user
        assert packet.user.count("A caller supplied one premise for this scenario.") == 1
        assert packet.user.count("Evaluate only the captured result.") == 1
        assert packet.user.count("owner-scope-spec.md") == 2
        assert "not an observed target fact or runtime evidence" in packet.user

    field_guide = build_plan_author_context(view, inventory, runtime)["field_guide"]
    assert "owner_supplied_scope" in field_guide
    assert "not an observed target fact" in field_guide["owner_supplied_scope"]


@pytest.mark.parametrize(
    ("owner_scope", "message"),
    [
        (["not a mapping"], "owner_scope must be a mapping"),
        ({"other": [], "extra": []}, "owner_scope has unsupported categories: ['extra', 'other']"),
        (
            {"scenario_premises": {"text": "t", "source": "s"}},
            "owner_scope.scenario_premises must be a sequence",
        ),
        (
            {"scenario_premises": ["plain text"]},
            "owner_scope.scenario_premises[0] must contain exactly text and source",
        ),
        (
            {"evaluation_instructions": [{"text": 3, "source": "owner.md"}]},
            "owner_scope.evaluation_instructions[0].text must be nonblank",
        ),
        (
            {
                "scenario_premises": [
                    {"text": "first", "source": "owner.md"},
                    {"text": "second", "source": " "},
                ]
            },
            "owner_scope.scenario_premises[1].source must be nonblank",
        ),
    ],
)
def test_owner_scope_errors_name_the_rejected_entry(owner_scope, message) -> None:
    view = replace(_view(), owner_scope=owner_scope)

    with pytest.raises(ValueError) as error:
        build_plan_author_context(view, _inventory(), _runtime_contract())
    assert str(error.value) == message


def test_owner_scope_with_only_empty_categories_adds_no_section() -> None:
    view = replace(_view(), owner_scope={"scenario_premises": [], "evaluation_instructions": ()})

    assert "owner_scope" not in build_plan_author_context(view, _inventory(), _runtime_contract())
