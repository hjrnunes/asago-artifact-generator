from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import ReviewResponseError
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import (
    AuthoringBudget,
    AuthoringPolicy,
    policy_max_dispatches,
    policy_role_limits,
)
from asago_artifact_generator.authoring.review import parse_review_response

from .support import ScriptedAuthoringTransport, load_failure_evidence, scripted_orchestrator
from .test_versioned_authoring_wire import (
    _framed,
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


def _review(decision: str = "accept", findings: list[dict] | None = None) -> bytes:
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted {decision} routing example",
            "findings": findings or [],
        }
    ).encode()


def _finding(question: str = "scenario_fidelity") -> dict[str, str]:
    return {
        "question": question,
        "location": "plan.prerequisites[0]",
        "problem": "The prerequisite removes the scenario's starting condition.",
        "basis": "The supplied scenario requires the condition to remain present.",
        "required_change": "Preserve the supplied condition without inventing facts.",
    }


_orchestrator = partial(scripted_orchestrator, task_id="stage-local")


def test_policy_defaults_and_strict_correction_validation() -> None:
    policy = AuthoringPolicy()

    assert policy.plan_max_corrections == 1
    assert policy.artifact_max_corrections == 1
    assert policy.review_plan is True
    assert policy.review_artifact is True

    for field_name in ("plan_max_corrections", "artifact_max_corrections"):
        for value in (True, False, -1, 1.5, "1"):
            with pytest.raises(ValueError, match=field_name):
                AuthoringPolicy(**{field_name: value})


def test_orchestrator_records_the_supplied_stage_policy(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _framed()])
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="direct-options",
        policy=AuthoringPolicy(
            plan_max_corrections=0,
            artifact_max_corrections=2,
            review_plan=False,
            review_artifact=False,
        ),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert result.package is not None
    assert result.package.manifest.authoring["policy"] == {
        "plan_max_corrections": 0,
        "artifact_max_corrections": 2,
        "plan_max_review_revisions": 0,
        "artifact_max_review_revisions": 0,
        "review_plan": False,
        "review_artifact": False,
        "review_model_profile": None,
        "review_temperature": 0,
        "max_retries": 0,
    }


def test_reviewer_controls_record_profile_model_and_zero_temperature(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _review(), _framed(), _review()])
    transport.model = "reviewer-model"
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="review-controls",
        policy=AuthoringPolicy(review_model_profile="reviewer-profile"),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert result.ledger[1]["controls"] == {
        "review_model_profile": "reviewer-profile",
        "temperature": 0,
        "max_retries": 0,
        "model": "reviewer-model",
    }
    assert result.package is not None
    assert result.package.manifest.authoring["policy"]["review_model_profile"] == (
        "reviewer-profile"
    )


def test_unparseable_candidate_records_checks_that_did_not_run(tmp_path: Path) -> None:
    orchestrator, _transport = _orchestrator(
        tmp_path,
        [b"not json"],
        policy=AuthoringPolicy(plan_max_corrections=0),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert result.ledger[0]["checks_not_run"] == ["plan_validation"]
    assert result.ledger[0]["checks"]["not_run"] == ["plan_validation"]


def test_review_response_parser_accepts_bare_and_single_json_fenced_objects() -> None:
    payload = _review()
    assert parse_review_response(payload).decision == "accept"
    fenced = parse_review_response(b"```json\n" + payload + b"\n```")
    assert fenced.findings == ()
    assert fenced.transformation == "outer_fence_removed"

    for raw in (b"prefix\n" + payload, payload + b"\n{}", b"```json\n{}\n```\n```json\n{}\n```"):
        with pytest.raises(ReviewResponseError):
            parse_review_response(raw)


def test_default_review_order_is_plan_author_review_artifact_author_review(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review(), _framed(), _review()],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    assert result.review_status == {
        "plan": "accepted",
        "artifact": "accepted",
    }
    assert result.package is not None
    assert result.package.manifest.authoring["policy"]["plan_max_corrections"] == 1
    assert result.package.manifest.authoring["terminal_status"] == "accepted"
    assert result.failure_evidence_path is None
    assert result.ledger[1]["reviewed_input_sha256"]
    assert result.ledger[1]["reviewed_candidate_sha256"]


def test_disabling_reviews_omits_only_review_dispatches_and_records_not_requested(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _framed()],
        policy=AuthoringPolicy(review_plan=False, review_artifact=False),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    assert result.review_status == {"plan": "not_requested", "artifact": "not_requested"}


def test_plan_review_revision_uses_only_plan_review_allowance_before_artifact(
    tmp_path: Path,
) -> None:
    revised = _plan()
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review("revise", [_finding()]),
            json.dumps(revised),
            _review(),
            _framed(),
            _review(),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "correction",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    assert result.allowances == {"plan": 1, "artifact": 1}
    assert result.review_revision_allowances == {"plan": 0, "artifact": 1}
    correction = next(record for record in result.ledger if record["stage"] == "correction")
    assert correction["allowance"] == "review_revision"


def test_artifact_review_revision_preserves_plan_and_uses_artifact_review_allowance(
    tmp_path: Path,
) -> None:
    plan = _plan()
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(plan),
            _review(),
            _framed(),
            _review("revise", [_finding("judge_spec_implements_plan")]),
            _framed(),
            _review(),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "artifact_review",
        "correction",
        "artifact_review",
    ]
    assert result.allowances == {"plan": 1, "artifact": 1}
    assert result.review_revision_allowances == {"plan": 1, "artifact": 0}
    assert result.package is not None
    assert json.loads(result.package.members["plan.json"]) == plan
    assert (
        result.package.members["authoring/05-correction.prompt"]
        == transport.requests[4]["user"].encode()
    )


def test_reviewer_blocked_and_malformed_are_distinct_terminal_states(tmp_path: Path) -> None:
    blocked, blocked_transport = _orchestrator(
        tmp_path / "blocked",
        [json.dumps(_plan()), _review("blocked", [_finding()])],
        policy=AuthoringPolicy(),
    )
    blocked_result = blocked.run(_view(), _inventory(), _runtime_contract())

    unavailable, unavailable_transport = _orchestrator(
        tmp_path / "unavailable",
        [json.dumps(_plan()), b'{"decision":"accept","summary":"contradictory","findings":[{}]}'],
        policy=AuthoringPolicy(),
    )
    unavailable_result = unavailable.run(_view(), _inventory(), _runtime_contract())

    assert blocked_result.status == "blocked"
    assert unavailable_result.status == "review_unavailable"
    assert len(blocked_transport.requests) == len(unavailable_transport.requests) == 2


def test_artifact_review_blocked_requires_plan_revision_without_recursing(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review(),
            _framed(),
            _review("blocked", [_finding("judge_spec_implements_plan")]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "needs_plan_revision"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    assert result.plan is not None
    assert not (tmp_path / "package").exists()


def test_author_and_reviewer_transport_failures_count_once_without_retry(
    tmp_path: Path,
) -> None:
    author, author_transport = _orchestrator(
        tmp_path / "author",
        [TimeoutError("author timeout")],
        policy=AuthoringPolicy(),
    )
    author_result = author.run(_view(), _inventory(), _runtime_contract())

    reviewer, reviewer_transport = _orchestrator(
        tmp_path / "reviewer",
        [json.dumps(_plan()), TimeoutError("review timeout")],
        policy=AuthoringPolicy(),
    )
    reviewer_result = reviewer.run(_view(), _inventory(), _runtime_contract())

    assert author_result.status == "transport_failure"
    assert reviewer_result.status == "review_unavailable"
    # Each timeout counts as exactly one dispatched request with no retry:
    # the author run stops after call1, the reviewer run after its first
    # review dispatch.
    assert len(author_transport.requests) == 1
    assert [request["stage"] for request in author_transport.requests] == ["call1"]
    assert len(reviewer_transport.requests) == 2
    assert [request["stage"] for request in reviewer_transport.requests] == [
        "call1",
        "plan_review",
    ]
    assert [record["role"] for record in reviewer_result.ledger] == ["author", "reviewer"]


def test_budget_exhaustion_stops_before_provider_dispatch(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=1, task_limit=8)
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan())],
        policy=AuthoringPolicy(),
        budget=budget,
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert [record["stage"] for record in result.ledger] == ["call1"]
    assert result.findings[-1].code == "budget_exhausted"


def test_zero_limit_disables_only_the_selected_stage(tmp_path: Path) -> None:
    plan_disabled, plan_transport = _orchestrator(
        tmp_path / "plan-zero",
        [b"{}"],
        policy=AuthoringPolicy(plan_max_corrections=0),
    )
    plan_result = plan_disabled.run(_view(), _inventory(), _runtime_contract())

    artifact_disabled, artifact_transport = _orchestrator(
        tmp_path / "artifact-zero",
        [json.dumps(_plan()), _review(), b"not a plan or artifact"],
        policy=AuthoringPolicy(artifact_max_corrections=0),
    )
    artifact_result = artifact_disabled.run(_view(), _inventory(), _runtime_contract())

    assert plan_result.status == "unresolved"
    assert plan_result.allowances == {"plan": 0, "artifact": 1}
    assert [request["stage"] for request in plan_transport.requests] == ["call1"]

    assert artifact_result.status == "unresolved"
    assert artifact_result.allowances == {"plan": 1, "artifact": 0}
    assert [request["stage"] for request in artifact_transport.requests] == [
        "call1",
        "plan_review",
        "call2",
    ]


def test_limit_of_two_permits_exactly_two_plan_corrections(tmp_path: Path) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            b"{}",  # initial candidate fails mechanics
            b"{}",  # first correction fails mechanics
            json.dumps(_plan()),  # second correction passes mechanics
            _review("revise", [_finding()]),  # semantic revise
            json.dumps(_plan()),  # review revision passes mechanics
            _review("revise", [_finding()]),  # no review revision remains
        ],
        policy=AuthoringPolicy(plan_max_corrections=2),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "correction",
        "plan_review",
        "correction",
        "plan_review",
    ]
    assert result.allowances == {"plan": 0, "artifact": 1}
    assert result.review_revision_allowances == {"plan": 0, "artifact": 1}
    assert [
        record["allowance"] for record in result.ledger if record["stage"] == "correction"
    ] == ["correction", "correction", "review_revision"]
    assert result.findings[-1].code == "correction_limit_exhausted"
    assert "review revision" in result.findings[-1].detail


def test_semantic_revise_after_mechanical_correction_uses_review_revision_allowance(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            b"{}",  # mechanical failure consumes the plan correction allowance
            json.dumps(_plan()),  # corrected plan passes mechanics
            _review("revise", [_finding()]),  # semantic revise spends the review revision
            json.dumps(_plan()),  # revised plan passes mechanics
            _review(),
            _framed(),
            _review(),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "plan_review",
        "correction",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    assert result.allowances == {"plan": 0, "artifact": 1}
    assert result.review_revision_allowances == {"plan": 0, "artifact": 1}
    assert result.budget["task_limit"] == policy_max_dispatches(AuthoringPolicy())


def test_second_semantic_revise_exhausts_review_revision_allowance(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review("revise", [_finding()]),
            json.dumps(_plan()),
            _review("revise", [_finding()]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "correction",
        "plan_review",
    ]
    assert result.allowances == {"plan": 1, "artifact": 1}
    assert result.review_revision_allowances == {"plan": 0, "artifact": 1}
    assert any(finding.code == "semantic_review" for finding in result.findings)
    assert result.findings[-1].code == "correction_limit_exhausted"
    assert result.failure_evidence_path is not None
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["allowances"] == {"plan": 1, "artifact": 1}
    assert evidence["review_revision_allowances"] == {"plan": 0, "artifact": 1}
    assert evidence["policy"]["plan_max_review_revisions"] == 1
    correction = next(item for item in evidence["attempts"] if item["stage"] == "correction")
    assert correction["allowance"] == "review_revision"
    assert evidence["findings"] == [finding.to_dict() for finding in result.findings]
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": 3,
        "reason": "correction_limit_exhausted",
    }


def test_policy_budget_covers_review_revisions() -> None:
    policy = AuthoringPolicy()
    assert policy_role_limits(policy) == {"author": 6, "reviewer": 6}
    assert policy_max_dispatches(policy) == 12
    unreviewed = AuthoringPolicy(review_plan=False, review_artifact=False)
    assert policy_role_limits(unreviewed) == {"author": 4, "reviewer": 0}
    assert policy_max_dispatches(unreviewed) == 4


def test_reviewer_transport_failure_records_elapsed_time_and_cause(tmp_path: Path) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), TimeoutError("review timeout")],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "review_unavailable"
    assert len(transport.requests) == 2
    assert result.failure_evidence_path is not None
    evidence = load_failure_evidence(result.failure_evidence_path)
    attempt = evidence["attempts"][-1]
    assert attempt["stage"] == "plan_review"
    assert attempt["raw_response"]["reason"] == "provider_failure"
    assert attempt["failure"]["elapsed_ms"] >= 0
    assert "review timeout" in attempt["failure"]["detail"]
    assert any(finding.code == "transport_failure" for finding in result.findings)


def test_blocked_review_preserves_its_reason_as_terminal_state(tmp_path: Path) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review("blocked", [_finding()])],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "blocked"
    assert [record["role"] for record in result.ledger] == ["author", "reviewer"]
    review_record = result.ledger[-1]["review"]
    assert review_record["decision"] == "blocked"
    assert review_record["summary"]
    assert review_record["findings"] == [_finding()]
    assert result.decoded_responses["plan_review"] == review_record
    assert not (tmp_path / "package").exists()
