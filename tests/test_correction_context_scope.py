"""Correction context ordering, stage scope, and authoritative-context pruning."""

from __future__ import annotations

import pytest

from asago_artifact_generator.authoring.core import Finding
from asago_artifact_generator.authoring.correction import (
    _correction_findings_view,
    _scope_correction_authoritative_context,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import build_artifact_author_context

from .support import world_builders

_inventory, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "plan", "runtime_contract", "view"
)


def test_artifact_correction_context_has_no_detector_sections() -> None:
    context = build_correction_context(
        failed_stage="call2",
        original_context=build_artifact_author_context(
            _view(), _plan(), _inventory(), _runtime_contract()
        ),
        current_output=b"candidate",
        findings=[Finding("semantic_review", "review", "artifact")],
    )

    assert context["findings"] == [
        {"code": "semantic_review", "detail": "review", "path": "artifact"}
    ]
    assert list(context) == [
        "stage",
        "failed_stage",
        "original_context",
        "current_output",
        "current_output_encoding",
        "findings",
        "instruction",
        "accepted_plan_fixed",
        "format",
        "response_contract",
    ]
    assert "runtime_contract" in context["original_context"]
    assert "closed scope for this stage" in context["instruction"]
    assert context["accepted_plan_fixed"] is True


@pytest.mark.parametrize("failed_stage", ["deploy", "plan", "artifact", "plan_review"])
def test_correction_context_rejects_a_stage_other_than_call1_or_call2(failed_stage: str) -> None:
    with pytest.raises(ValueError, match=f"unsupported correction stage: {failed_stage}"):
        build_correction_context(
            failed_stage=failed_stage,
            original_context={},
            current_output=b"text",
            findings=[],
        )


def test_findings_view_keeps_findings_and_drops_review_details() -> None:
    findings = [
        "plain text",
        {"code": "semantic_review", "detail": "d", "details": {"k": 1}},
        {"code": "other", "details": {"k": 1}},
    ]

    assert _correction_findings_view(findings) == [
        "plain text",
        {"code": "semantic_review", "detail": "d"},
        {"code": "other", "details": {"k": 1}},
    ]
    assert _correction_findings_view("not a list") == "not a list"
    assert findings[1]["details"] == {"k": 1}


def test_correction_scope_keeps_only_operations_the_plan_names() -> None:
    authoritative = {
        "operations": [
            {"name": "refund", "description": "d", "extra": 1},
            {"name": "unused", "description": "u"},
            "not a dict",
        ],
        "facts": [1],
        "source_handles": [2],
        "runtime_capabilities": [3],
        "kept": True,
    }

    _scope_correction_authoritative_context(authoritative, {"step": "call refund"})

    assert authoritative == {
        "operations": [{"name": "refund", "description": "d"}],
        "kept": True,
    }


def test_correction_scope_leaves_the_context_unchanged_without_a_plan_mapping() -> None:
    authoritative = {"operations": [{"name": "refund"}], "facts": [1]}

    _scope_correction_authoritative_context(authoritative, None)

    assert authoritative == {"operations": [{"name": "refund"}], "facts": [1]}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("semantic_judge_spec.fact_refs[0]", True),
        ("semantic_judge_spec.fact_refs[4].ref", True),
        ("semantic_judge_spec.fact_refs[0]:slot", True),
        ("semantic_judge_spec.fact_refs", False),
        ("semantic_judge_spec.fact_ref[0]", False),
        ("judge.json.fact_refs[0]", False),
    ],
)
def test_fact_ref_guidance_reads_the_finding_target(path: str, expected: bool) -> None:
    from asago_artifact_generator.authoring.correction import _targets_a_fact_ref

    assert _targets_a_fact_ref(path) is expected
