from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

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
    _source,
    _view,
)

_OWNER_SCOPE_LABEL = "OWNER-SUPPLIED SCOPE (NOT OBSERVED TARGET FACTS)"
# SHA-256 of system + NUL + user bytes captured for these prompt contracts.
_BASE_REVISION = (
    "authoring-call1-v18 / authoring-call2-v21 / authoring-correction-v27 (plan) / "
    "authoring-correction-v25 (artifact) / authoring-plan-review-v17 / "
    "authoring-artifact-review-v16"
)
_BASE_STAGE_DIGESTS = {
    "call1": "360badaeb0bf63f97616a7486056e1bb13260c149808a9cca8b0072337638058",
    "plan_review": "4320f809299d88271625131f6f2b02a6ae129eaf104557968e4074e95304cf10",
    "call2": "86635e851ab6f27ff733788aeb7362fe574af47c786fbe1991c72f768b20e8b6",
    "artifact_review": "38e3e3a88fa5c2d7832782c18f68b20a12e56e19a285b6b36091a210c91e398c",
    "plan_correction": "c2ff111ae0e5db1f43e0f208fbcafdae511032b84c1daa70da09e59f33835b52",
    "artifact_correction": "89d2bc48c59d23c83cf169f39bb901ebdb440241942c233883129ccb67da2204",
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
            view, plan, _metadata(), _source(), [], inventory, runtime
        ),
        "plan_correction": _render_correction_packet(
            build_correction_context(
                failed_stage="call1",
                original_context=build_plan_author_context(view, inventory, runtime),
                current_output="{}",
                findings=[],
            )
        ),
        "artifact_correction": _render_correction_packet(
            build_correction_context(
                failed_stage="call2",
                original_context=build_artifact_author_context(view, plan, inventory, runtime),
                current_output=_framed(),
                findings=[],
            )
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
        _source(),
        [],
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
            _source(),
            [],
            inventory,
            runtime,
        ),
        _render_correction_packet(
            build_correction_context(
                failed_stage="call1",
                original_context=build_plan_author_context(view, inventory, runtime),
                current_output="{}",
                findings=[],
            )
        ),
        _render_correction_packet(
            build_correction_context(
                failed_stage="call2",
                original_context=build_artifact_author_context(view, plan, inventory, runtime),
                current_output=_framed(),
                findings=[],
            )
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
