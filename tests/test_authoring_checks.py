from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.authoring.checks import (
    _call2_semantic_judge_spec_findings,
    _call2_stimulus_findings,
    _normalize_prerequisite_binding_consumers,
    _prerequisite_evidence_ref_findings,
    _stimulus_slot_findings,
    collect_plan_findings,
)


def _coded(findings: list[Any]) -> list[tuple[str, str, str]]:
    return [(f.code, f.detail, f.path) for f in findings]


def test_call2_stimulus_findings_report_shape_and_type_errors_in_order() -> None:
    assert _coded(_call2_stimulus_findings("x")) == [
        ("type_error", "stimulus must be an object", "stimulus")
    ]
    assert (
        _call2_stimulus_findings(
            {"user_text": "t", "history": [], "slots": ["a"], "delivery": "d"}
        )
        == []
    )

    findings = _call2_stimulus_findings(
        {"user_text": 1, "history": {}, "slots": [1], "extra": 0, "b": 0}
    )

    assert _coded(findings) == [
        ("missing_field", "stimulus missing field: delivery", "stimulus.delivery"),
        ("unexpected_field", "unexpected stimulus field: b", "stimulus.b"),
        ("unexpected_field", "unexpected stimulus field: extra", "stimulus.extra"),
        ("type_error", "stimulus.user_text must be a string", "stimulus.user_text"),
        ("type_error", "stimulus.delivery must be a string", "stimulus.delivery"),
        ("type_error", "stimulus.history must be a list", "stimulus.history"),
        ("type_error", "stimulus.slots must be a list of strings", "stimulus.slots"),
    ]


def test_call2_judge_spec_findings_report_shape_and_type_errors_in_order() -> None:
    assert _call2_semantic_judge_spec_findings(None) == []
    assert _coded(_call2_semantic_judge_spec_findings(3)) == [
        (
            "type_error",
            "semantic_judge_spec must be an object or null",
            "semantic_judge_spec",
        )
    ]
    assert (
        _call2_semantic_judge_spec_findings({"question": "q", "criteria": "c", "fact_refs": ["f"]})
        == []
    )

    findings = _call2_semantic_judge_spec_findings(
        {"question": 1, "fact_refs": "f", "z": 0, "a": 0}
    )

    assert _coded(findings) == [
        ("unexpected_field", "unexpected semantic_judge_spec field: a", "semantic_judge_spec.a"),
        ("unexpected_field", "unexpected semantic_judge_spec field: z", "semantic_judge_spec.z"),
        (
            "missing_field",
            "semantic_judge_spec missing field: criteria",
            "semantic_judge_spec.criteria",
        ),
        (
            "type_error",
            "semantic_judge_spec.question must be a string",
            "semantic_judge_spec.question",
        ),
        (
            "type_error",
            "semantic_judge_spec.fact_refs must be a list of strings",
            "semantic_judge_spec.fact_refs",
        ),
    ]


def test_stimulus_slot_findings_ignore_a_stimulus_without_text_and_slot_list() -> None:
    assert _stimulus_slot_findings({"slots": "x", "user_text": 1}, []) == []
    assert _stimulus_slot_findings({"slots": [], "user_text": None}, []) == []


def test_stimulus_slot_findings_report_without_deriving_the_slots() -> None:
    stimulus = {"slots": [], "user_text": "Show {{item_id}}"}

    findings = _stimulus_slot_findings(stimulus, [{"name": "item_id"}])

    assert [finding.code for finding in findings] == ["slot_mismatch"]
    assert stimulus == {"slots": [], "user_text": "Show {{item_id}}"}


def test_collect_plan_findings_rejects_a_non_object_plan() -> None:
    findings = collect_plan_findings("plan", {}, {})

    assert _coded(findings) == [("response_type_error", "plan must be an object", "response")]


def test_collect_plan_findings_checks_each_unresolved_requirement() -> None:
    unresolved = [
        "text",
        {"name": "n", "essential": "yes", "reason": "r"},
        {"name": "n", "essential": True, "reason": "r"},
    ]

    findings = collect_plan_findings({"unresolved_requirements": unresolved}, {}, {})

    assert [
        (f.detail, f.path) for f in findings if f.path.startswith("unresolved_requirements[")
    ] == [
        ("unresolved requirement must be an object", "unresolved_requirements[0]"),
        (
            "unresolved requirement requires name, essential, and reason",
            "unresolved_requirements[1]",
        ),
    ]


@pytest.mark.parametrize(
    ("prerequisite", "expected"),
    [
        (
            {"evidence_refs": ["known", "gone", " ", 3]},
            [
                ("unknown_reference", "unknown_reference: gone", "p.evidence_refs[1]"),
                ("unknown_reference", "unknown_reference:  ", "p.evidence_refs[2]"),
                ("unknown_reference", "unknown_reference: 3", "p.evidence_refs[3]"),
            ],
        ),
        (
            {"evidence_refs": "known"},
            [("type_error", "prerequisite evidence_refs must be a list", "p.evidence_refs")],
        ),
        ({}, []),
    ],
)
def test_prerequisite_evidence_ref_findings(
    prerequisite: dict[str, Any], expected: list[tuple[str, str, str]]
) -> None:
    findings = _prerequisite_evidence_ref_findings(prerequisite, "p", {"known"})

    assert _coded(findings) == expected


def test_prerequisite_consumers_are_added_once_and_recorded() -> None:
    bindings = [
        {"name": "order", "consumers": ["stimulus.user_text"]},
        {"name": "done", "consumers": ["prerequisites.done"]},
        {"name": "bad", "consumers": "x"},
        "not a dict",
    ]
    prerequisites = [
        "skip",
        {"binding": "order", "name": "p"},
        {"binding": "done"},
        {"binding": "bad"},
    ]
    transformations: list[dict[str, Any]] = []

    _normalize_prerequisite_binding_consumers(
        prerequisites, bindings, transformations=transformations
    )

    assert bindings[0]["consumers"] == ["stimulus.user_text", "prerequisites.order"]
    assert bindings[1]["consumers"] == ["prerequisites.done"]
    assert bindings[2]["consumers"] == "x"
    assert transformations == [
        {
            "transformation": "binding_consumer_added",
            "binding": "order",
            "original_consumers": ["stimulus.user_text"],
            "canonical_consumers": ["stimulus.user_text", "prerequisites.order"],
            "prerequisite_index": 1,
            "prerequisite_name": "p",
            "consumer": "prerequisites.order",
        }
    ]


def test_prerequisite_consumers_ignore_non_list_inputs() -> None:
    bindings = [{"name": "order", "consumers": []}]

    _normalize_prerequisite_binding_consumers("x", bindings)
    _normalize_prerequisite_binding_consumers([{"binding": "order"}], "x")

    assert bindings == [{"name": "order", "consumers": []}]


def test_every_registered_mechanical_check_runs_on_a_passing_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from asago_artifact_generator.authoring import checks
    from tests import test_semantic_judge_spec_wording as reply_case
    from tests.support import validate_neutral_example

    called: set[str] = set()
    registered = {
        function.__name__
        for check in (*checks.PLAN_MECHANICAL_CHECKS, *checks.ARTIFACT_MECHANICAL_CHECKS)
        for function in check.functions
    }
    for name in registered:
        original = getattr(checks, name)

        def spy(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            called.add(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(checks, name, spy)

    plan = reply_case._plan()
    inventory, runtime = reply_case._inventory(), reply_case._runtime_contract()
    reply_findings = [
        *checks.collect_plan_findings_v2(plan, inventory, runtime),
        *checks.collect_artifact_findings_v2(
            reply_case._metadata("Does the reply assert the claim?"), plan, inventory, runtime
        ),
    ]

    assert validate_neutral_example() == []
    assert reply_findings == []
    assert called == registered


def test_mechanical_check_ids_are_unique_per_stage() -> None:
    from asago_artifact_generator.authoring import checks

    for registry in (checks.PLAN_MECHANICAL_CHECKS, checks.ARTIFACT_MECHANICAL_CHECKS):
        ids = [check.check_id for check in registry]
        assert len(ids) == len(set(ids))
        assert all(check.functions for check in registry)
