from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.authoring.checks import (
    _call2_semantic_judge_spec_findings,
    _call2_stimulus_findings,
    _normalize_prerequisite_binding_consumers,
    _prerequisite_evidence_ref_findings,
    _stimulus_slot_findings,
    collect_plan_findings_v2,
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


def test_collect_plan_findings_v2_rejects_a_non_object_plan() -> None:
    findings = collect_plan_findings_v2("plan", {}, {})

    assert _coded(findings) == [("response_type_error", "plan must be an object", "response")]


_V2_ROOT_FIELDS_BESIDES_INTERPRETATION = (
    "selected_evidence",
    "assumptions",
    "setup_recipe",
    "runtime_bindings",
    "prerequisites",
    "stimulus_approach",
    "observation_claim",
    "required_observations",
    "semantic_judge",
    "unresolved_requirements",
)


def _root_presence(findings: list[Any]) -> list[tuple[str, str]]:
    return [
        (f.code, f.path) for f in findings if f.code in {"unexpected_field", "plan_validation"}
    ]


def test_collect_plan_findings_v2_reports_each_root_field_problem_once() -> None:
    findings = collect_plan_findings_v2({"x": 1, "interpretation": "bad"}, {}, {})

    assert _root_presence(findings) == [
        ("unexpected_field", "x"),
        *(("plan_validation", name) for name in _V2_ROOT_FIELDS_BESIDES_INTERPRETATION),
    ]
    assert len(_coded(findings)) == len(set(_coded(findings)))


def test_collect_plan_findings_v2_still_reports_a_root_list_field_of_the_wrong_type() -> None:
    plan = {"selected_evidence": "x", "setup_recipe": {}, "prerequisites": 1}

    findings = collect_plan_findings_v2(plan, {}, {})

    assert [(f.detail, f.path) for f in findings if f.code == "type_error"] == [
        ("selected_evidence must be a list", "selected_evidence"),
        ("setup_recipe must be a list", "setup_recipe"),
        ("prerequisites must be a list", "prerequisites"),
    ]


def test_collect_plan_findings_v2_checks_each_unresolved_requirement() -> None:
    unresolved = [
        "text",
        {"name": "n", "essential": "yes", "reason": "r"},
        {"name": "n", "essential": True, "reason": "r"},
    ]

    findings = collect_plan_findings_v2({"unresolved_requirements": unresolved}, {}, {})

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
    from tests.reply_support import (
        reply_inventory,
        reply_metadata,
        reply_plan,
        reply_runtime_contract,
    )
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

    plan = reply_plan()
    inventory, runtime = reply_inventory(), reply_runtime_contract()
    reply_findings = [
        *checks.collect_plan_findings_v2(plan, inventory, runtime),
        *checks.collect_artifact_findings_v2(
            reply_metadata("Does the reply assert the claim?"), plan, inventory, runtime
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


def test_plan_that_is_not_an_object_yields_one_response_type_finding() -> None:
    from asago_artifact_generator.authoring.checks import collect_plan_findings_v2

    findings = collect_plan_findings_v2(["plan"], {}, {})

    assert _coded(findings) == [("response_type_error", "plan must be an object", "response")]
    assert findings[0].stage == "plan"


def test_judge_decision_must_match_the_accepted_plan() -> None:
    from asago_artifact_generator.authoring.checks import _semantic_judge_decision_findings

    needed = {"semantic_judge": {"needed": True}}
    not_needed = {"semantic_judge": {"needed": False}}
    reply = {"observation_claim": {"claim_level": "reply"}}
    conflict = [
        (
            "plan_conflict",
            "semantic judge specification differs from accepted plan decision",
            "semantic_judge_spec",
        )
    ]

    assert _coded(_semantic_judge_decision_findings(needed, None)) == conflict
    assert _coded(_semantic_judge_decision_findings(not_needed, {"question": "q"})) == conflict
    assert _semantic_judge_decision_findings(needed, {"question": "q"}) == []
    assert _semantic_judge_decision_findings(not_needed, None) == []
    assert _semantic_judge_decision_findings({"semantic_judge": {"needed": "yes"}}, None) == []
    assert _semantic_judge_decision_findings({"semantic_judge": "x"}, None) == []
    assert _semantic_judge_decision_findings({}, None) == []
    assert [f.code for f in _semantic_judge_decision_findings(reply, None)] == [
        "semantic_judge_spec_required"
    ]


def test_observation_claim_level_must_be_closed_and_supported() -> None:
    from asago_artifact_generator.authoring.checks import _observation_claim_level_findings

    assert _coded(_observation_claim_level_findings("belief", {})) == [
        (
            "closed_value_error",
            "observation_claim must declare a closed claim_level",
            "observation_claim.claim_level",
        )
    ]
    assert [f.code for f in _observation_claim_level_findings(None, {})] == ["closed_value_error"]
    narrowed = {"observation": {"claim_levels": ["reply"]}}
    assert [f.code for f in _observation_claim_level_findings("command_attempt", narrowed)] == [
        "unsupported_claim_level"
    ]
    assert _observation_claim_level_findings("reply", narrowed) == []


def test_semantic_judge_plan_findings_report_each_shape_fault_in_order() -> None:
    from asago_artifact_generator.authoring.checks import _semantic_judge_plan_findings

    assert _semantic_judge_plan_findings({}) == []
    assert _coded(_semantic_judge_plan_findings({"semantic_judge": "x"})) == [
        ("type_error", "semantic_judge must be an object", "semantic_judge")
    ]
    assert _coded(
        _semantic_judge_plan_findings({"semantic_judge": {"needed": "yes", "z": 0, "a": 0}})
    ) == [
        ("unexpected_field", "unexpected semantic_judge field: a", "semantic_judge.a"),
        ("unexpected_field", "unexpected semantic_judge field: z", "semantic_judge.z"),
        ("type_error", "semantic_judge.needed must be a boolean", "semantic_judge.needed"),
        ("missing_field", "semantic_judge missing field: scope", "semantic_judge.scope"),
    ]
    assert _coded(_semantic_judge_plan_findings({"semantic_judge": {"scope": None}})) == [
        ("missing_field", "semantic_judge missing field: needed", "semantic_judge.needed")
    ]
    assert (
        _semantic_judge_plan_findings({"semantic_judge": {"needed": False, "scope": None}}) == []
    )
