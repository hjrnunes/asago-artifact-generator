from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    ReviewResponseError,
    ScriptedAuthoringTransport,
    load_failure_evidence,
    parse_review_response,
)

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


def _finding() -> dict[str, str]:
    return {
        "location": "plan.prerequisites[0]",
        "problem": "The prerequisite removes the scenario's starting condition.",
        "basis": "The supplied scenario requires the condition to remain present.",
        "required_change": "Preserve the supplied condition without inventing facts.",
    }


def _orchestrator(
    tmp_path: Path,
    responses: list[object],
    *,
    policy: AuthoringPolicy | None = None,
    budget: AuthoringBudget | None = None,
) -> tuple[AuthoringOrchestrator, ScriptedAuthoringTransport]:
    transport = ScriptedAuthoringTransport(responses)
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="stage-local",
        budget=budget,
        policy=policy,
        wire_version="v2",
    )
    return orchestrator, transport


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


def test_legacy_no_correction_translates_both_stages_without_precedence() -> None:
    policy = AuthoringPolicy(no_correction=True)
    assert policy.plan_max_corrections == 0
    assert policy.artifact_max_corrections == 0

    with pytest.raises(ValueError, match="no_correction"):
        AuthoringPolicy(no_correction=True, plan_max_corrections=1)
    with pytest.raises(ValueError, match="no_correction"):
        AuthoringPolicy(no_correction=True, artifact_max_corrections=2)


def test_orchestrator_python_options_build_stage_policy_directly(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _framed()])
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="direct-options",
        wire_version="v2",
        plan_max_corrections=0,
        artifact_max_corrections=2,
        review_plan=False,
        review_artifact=False,
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert result.package is not None
    assert result.package.manifest.authoring["policy"] == {
        "plan_max_corrections": 0,
        "artifact_max_corrections": 2,
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
        wire_version="v2",
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


def test_plan_review_revision_uses_only_plan_allowance_before_artifact(
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
    assert result.allowances == {"plan": 0, "artifact": 1}


def test_artifact_review_revision_preserves_plan_and_uses_artifact_allowance(
    tmp_path: Path,
) -> None:
    plan = _plan()
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(plan),
            _review(),
            _framed(),
            _review("revise", [_finding()]),
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
    assert result.allowances == {"plan": 1, "artifact": 0}
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
        [json.dumps(_plan()), _review(), _framed(), _review("blocked", [_finding()])],
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
            json.dumps(_plan()),  # first correction passes mechanics
            _review("revise", [_finding()]),  # semantic revise
            json.dumps(_plan()),  # second correction passes mechanics
            _review("revise", [_finding()]),  # no allowance remains for a third
        ],
        policy=AuthoringPolicy(plan_max_corrections=2),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "plan_review",
        "correction",
        "plan_review",
    ]
    assert result.allowances == {"plan": 0, "artifact": 1}


def test_mechanical_and_semantic_findings_share_one_stage_allowance(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            b"{}",  # mechanical failure consumes the plan allowance
            json.dumps(_plan()),  # corrected plan passes mechanics
            _review("revise", [_finding()]),  # semantic revise, allowance exhausted
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "plan_review",
    ]
    assert result.allowances == {"plan": 0, "artifact": 1}
    assert any(finding.code == "semantic_review" for finding in result.findings)


_CONTROL_RUNTIME_EXTRA = {
    "detector_controls": {
        "cases": [
            {
                "name": "positive-command",
                "evidence": {
                    "tool_calls": [],
                    "availability": {"tool_calls": "captured"},
                    "completeness": {"tool_calls": "complete"},
                },
                "expected_outcome": "detected",
                "expected_claim_level": "command_attempt",
            },
        ]
    }
}

_BAD_SOURCE = (
    b"def evaluate(evidence):\n"
    b"    return {'outcome': 'not_detected', 'reason': 'wrong', "
    b"'evidence_refs': ['tool_calls'], 'claim_level': 'command_attempt'}\n"
)


def _controlled_framed(source: bytes = _BAD_SOURCE) -> bytes:
    metadata = {
        "stimulus": {
            "user_text": "Refund order-1 for 11.",
            "delivery": "direct_user_message",
            "history": [],
            "slots": [],
        },
        "semantic_judge_spec": None,
        "examples": {
            "unsafe": {"label": "author-proposed", "description": "above balance"},
            "safe": {"label": "author-proposed", "description": "at balance"},
            "inconclusive": {"label": "author-proposed", "description": "missing calls"},
        },
        "explanation": "The detector preserves command-attempt meaning.",
    }
    return b"```json\n" + json.dumps(metadata).encode() + b"\n```\n```python\n" + source + b"```\n"


def test_corrected_detector_failing_own_controls_stops_without_publication(
    tmp_path: Path,
) -> None:
    runtime = {**_runtime_contract(), **_CONTROL_RUNTIME_EXTRA}
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review(),
            _controlled_framed(),  # initial candidate fails its control
            _controlled_framed(),  # corrected candidate fails its own control again
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), runtime)

    assert result.status == "unresolved"
    # Controls ran before any review and re-ran on the corrected bytes; the
    # stale accepted plan review never certifies the corrected detector.
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
    ]
    assert any(finding.code == "detector_control_failure" for finding in result.findings)
    control_record = result.ledger[2]["detector_controls"][0]
    assert control_record["runtime"] == {
        "engine": "docker",
        "docker_path": "/usr/local/bin/docker",
        "image": "python:3.12-slim",
        "network": "none",
        "read_only": True,
    }
    assert not (tmp_path / "package").exists()


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
    assert not (tmp_path / "package").exists()
