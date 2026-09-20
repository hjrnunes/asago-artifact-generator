from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.authoring import (
    MAX_AUTHORING_REQUESTS,
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    ScriptedAuthoringTransport,
)

from .test_authoring_orchestration import HANDOFF
from .test_versioned_authoring_wire import (
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


def test_a03_resume_seed_stops_before_fourth_in_run_author_request(tmp_path: Path) -> None:
    """One preserved A03 author request leaves only three author slots."""

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
        package_dir=tmp_path / "a03-resume",
        task_id="A03-live-20260920-resume",
        wire_version="v2",
        policy=AuthoringPolicy(),
        prior_author_correction_spend=1,
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
        task_id="O03",
        wire_version="v2",
        policy=AuthoringPolicy(),
        prior_author_correction_spend=4,
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
        task_id="O03",
        wire_version="v2",
        policy=AuthoringPolicy(),
        prior_author_correction_spend=0,
        prior_review_spend=4,
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
        task_id="O03",
        wire_version="v2",
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
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(_inventory()), encoding="utf-8")
    runtime_contract = tmp_path / "runtime-contract.json"
    runtime_contract.write_text(json.dumps(_runtime_contract()), encoding="utf-8")
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
    monkeypatch.setattr(cli, "BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setattr(cli, "MODEL", "offline-model")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-key")

    result = CliRunner().invoke(
        cli.app,
        [
            "author",
            str(HANDOFF),
            "--inventory",
            str(inventory),
            "--runtime-contract",
            str(runtime_contract),
            "--output-dir",
            str(tmp_path / "output"),
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
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(_inventory()), encoding="utf-8")
    runtime_contract = tmp_path / "runtime-contract.json"
    runtime_contract.write_text(json.dumps(_runtime_contract()), encoding="utf-8")
    constructed = False

    def fail_if_constructed(**_: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", fail_if_constructed)

    result = CliRunner().invoke(
        cli.app,
        [
            "author",
            str(HANDOFF),
            "--inventory",
            str(inventory),
            "--runtime-contract",
            str(runtime_contract),
            "--output-dir",
            str(tmp_path / "output"),
            "--prior-author-correction-spend",
            "-1",
            "--prior-review-spend",
            "0",
        ],
    )

    assert result.exit_code == 2
    assert "nonnegative integer" in result.output
    assert constructed is False
