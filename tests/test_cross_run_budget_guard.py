from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.authoring.core import MAX_AUTHORING_REQUESTS
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import (
    AuthoringBudget,
    AuthoringPolicy,
    policy_role_limits,
)

from .support import ScriptedAuthoringTransport
from .test_authoring_orchestration import HANDOFF
from .test_profile_bridge import _inputs as _cli_inputs
from .test_profile_bridge import _profile_file
from .test_versioned_authoring_wire import (
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


def test_prior_author_seed_stops_before_fourth_in_run_author_request(tmp_path: Path) -> None:
    """Prior author spend leaves only three in-run author slots."""

    transport = ScriptedAuthoringTransport(
        [
            b"{}",  # in-run author 1: mechanical plan failure
            json.dumps(_plan()),  # in-run author 2: plan correction
            json.dumps({"decision": "accept", "summary": "routing", "findings": []}),
            b"not a two-block artifact",  # in-run author 3: artifact failure
        ]
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "prior-author-resume",
        task_id="prior-author-resume",
        policy=AuthoringPolicy(),
        prior_author_correction_spend=policy_role_limits(AuthoringPolicy())["author"] - 3,
        prior_review_spend=0,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "plan_review",
        "call2",
    ]
    assert [record["role"] for record in result.ledger] == [
        "author",
        "author",
        "reviewer",
        "author",
    ]
    assert result.findings[-1].code == "budget_exhausted"
    assert "author/correction" in result.findings[-1].detail


def test_prior_author_spend_at_case_cap_stops_before_first_dispatch(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan())])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "exhausted",
        task_id="author-exhausted",
        policy=AuthoringPolicy(),
        prior_author_correction_spend=policy_role_limits(AuthoringPolicy())["author"],
        prior_review_spend=0,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert transport.requests == []
    assert result.ledger == []
    assert len(result.findings) == 1
    assert result.findings[0].code == "budget_exhausted"
    assert "author/correction" in result.findings[0].detail


def test_prior_review_spend_at_case_cap_stops_before_review_dispatch(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(_plan()),
            json.dumps({"decision": "accept", "summary": "unused", "findings": []}),
        ]
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "review-exhausted",
        task_id="review-exhausted",
        policy=AuthoringPolicy(),
        prior_author_correction_spend=0,
        prior_review_spend=policy_role_limits(AuthoringPolicy())["reviewer"],
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert [record["stage"] for record in result.ledger] == ["call1"]
    assert result.findings[-1].code == "budget_exhausted"
    assert "review" in result.findings[-1].detail


def test_aggregate_budget_exhaustion_is_typed_and_pre_dispatch(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan())])
    budget = AuthoringBudget(
        aggregate_limit=MAX_AUTHORING_REQUESTS,
        total_dispatched=MAX_AUTHORING_REQUESTS,
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "aggregate-exhausted",
        task_id="aggregate-exhausted",
        policy=AuthoringPolicy(),
        budget=budget,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert transport.requests == []
    assert result.ledger == []
    assert result.findings[0].code == "budget_exhausted"
    assert "aggregate" in result.findings[0].detail


def test_author_cli_threads_prior_spend_to_orchestrator_without_provider_contact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_profile, runtime_contract = _cli_inputs(tmp_path)
    captured: dict[str, object] = {}

    class FakeTransport:
        max_retries = 0

        def __init__(self, **_: object) -> None:
            pass

    class FakeOrchestrator:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(
                status="failed",
                package_path=None,
                review_status={},
                findings=[],
            )

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", FakeTransport)
    monkeypatch.setattr(cli, "AuthoringOrchestrator", FakeOrchestrator)
    profiles_file, _ = _profile_file(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        [
            "author",
            str(HANDOFF),
            "--target-profile",
            str(target_profile),
            "--runtime-contract",
            str(runtime_contract),
            "--output-dir",
            str(tmp_path / "output"),
            "--profile",
            "gemma4-oc",
            "--profiles-file",
            str(profiles_file),
            "--prior-author-correction-spend",
            "1",
            "--prior-review-spend",
            "2",
        ],
    )

    assert result.exit_code == 1, result.output
    assert captured["prior_author_correction_spend"] == 1
    assert captured["prior_review_spend"] == 2


def test_author_cli_rejects_negative_prior_spend_before_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_profile, runtime_contract = _cli_inputs(tmp_path)
    constructed = False

    def fail_if_constructed(**_: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", fail_if_constructed)
    profiles_file, _ = _profile_file(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        [
            "author",
            str(HANDOFF),
            "--target-profile",
            str(target_profile),
            "--runtime-contract",
            str(runtime_contract),
            "--output-dir",
            str(tmp_path / "output"),
            "--profile",
            "gemma4-oc",
            "--profiles-file",
            str(profiles_file),
            "--prior-author-correction-spend",
            "-1",
            "--prior-review-spend",
            "0",
        ],
    )

    assert result.exit_code == 2
    assert "nonnegative integer" in result.output
    assert constructed is False
