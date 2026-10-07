from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from asago_artifact_generator.authoring.binding_repair import correction_repair_inputs
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import (
    build_artifact_author_context,
    build_plan_author_context,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_artifact_reviewer_context,
    build_plan_review_packet,
    build_plan_reviewer_context,
)
from tests.test_versioned_prompt_roles import (
    _framed,
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _view,
)

_OWNER_SCOPE_LABEL = "OWNER-SUPPLIED SCOPE (NOT OBSERVED TARGET FACTS)"
# SHA-256 of system + NUL + user bytes captured for these prompt contracts.
_BASE_REVISION = (
    "authoring-call1-v20 / authoring-call2-v23 / authoring-correction-v31 (plan) / "
    "authoring-correction-v32 (artifact) / authoring-plan-review-v19 / "
    "authoring-artifact-review-v19"
)
_BASE_STAGE_DIGESTS = {
    "call1": "70d8ba46d43f4b728670935e00dcba5034eb3b90b5b039da70f8d512ea41f3f5",
    "plan_review": "18f0a56a90f316a6f359d0e9b6aad2b35b1d6d92b97cc69becba0a143a539c6d",
    "call2": "1e86361300981cf1da2e1b93ff6874b0f6d44b23d3513b6d70937ca55c40fa5b",
    # The fixture artifact has no judge spec, so its summary omits the judge fact-ref check.
    "artifact_review": "f1238ae913c1c4b09df65f7dfed32db5b03e69d4a14bda521d368ff92ae28c41",
    "plan_correction": "9157e0ee767fe5c0bb8e5df2c0add2314fc35db35705efe1abab2c15451bba72",
    "artifact_correction": "2d063ba1c2b15084fea4e19da4c51ac6a6b61d02ec516abf07d88d6f7e458537",
}
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


def _render_all_stage_packets(view):
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    return {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "call2": build_call2_packet_v2(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, _metadata(), inventory, runtime
        ),
        "plan_correction": _render_correction_packet(
            build_correction_context(
                failed_stage="call1",
                original_context=build_plan_author_context(view, inventory, runtime),
                current_output="{}",
                findings=[],
            ),
            correction_repair_inputs(view, inventory, runtime),
        ),
        "artifact_correction": _render_correction_packet(
            build_correction_context(
                failed_stage="call2",
                original_context=build_artifact_author_context(view, plan, inventory, runtime),
                current_output=_framed(),
                findings=[],
            ),
            correction_repair_inputs(view, inventory, runtime),
        ),
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
        packets = _render_all_stage_packets(view)
        digests = {
            name: hashlib.sha256((packet.system + "\0" + packet.user).encode("utf-8")).hexdigest()
            for name, packet in packets.items()
        }
        assert digests == _BASE_STAGE_DIGESTS, f"base revision: {_BASE_REVISION}"
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
    packets = [
        build_call1_packet_v2(view, inventory, runtime),
        build_plan_review_packet(view, plan, inventory, runtime),
        build_call2_packet_v2(view, plan, inventory, runtime),
        build_artifact_review_packet(
            view,
            plan,
            _metadata(),
            inventory,
            runtime,
        ),
        _render_correction_packet(
            build_correction_context(
                failed_stage="call1",
                original_context=build_plan_author_context(view, inventory, runtime),
                current_output="{}",
                findings=[],
            ),
            correction_repair_inputs(view, inventory, runtime),
        ),
        _render_correction_packet(
            build_correction_context(
                failed_stage="call2",
                original_context=build_artifact_author_context(view, plan, inventory, runtime),
                current_output=_framed(),
                findings=[],
            ),
            correction_repair_inputs(view, inventory, runtime),
        ),
    ]
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
