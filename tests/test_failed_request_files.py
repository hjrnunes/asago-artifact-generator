"""A scenario without a package keeps its prompts and raw responses beside its evidence.

An accepted package holds ``authoring/NN-<stage>.prompt`` and ``.raw`` files;
a failed scenario writes the same files under ``<package>.authoring/`` so the
report can link each request of a failure.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import pytest

from asago_artifact_generator.authoring import orchestrator as orchestrator_module
from asago_artifact_generator.authoring.policy import AuthoringPolicy

from .policy_support import ONE_PLAN_CORRECTION, run_refund
from .support import (
    ScriptedAuthoringTransport,
    load_failure_evidence,
    review_finding,
    review_response,
    scripted_orchestrator,
    world_builders,
)

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)
_orchestrator = partial(scripted_orchestrator, task_id="requests")


def test_an_unresolved_scenario_writes_each_request_beside_its_evidence(tmp_path: Path) -> None:
    revise = review_response("revise", [review_finding("scenario_fidelity")])
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), revise, json.dumps(_plan()), revise],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status != "accepted"
    requests = tmp_path / "package.authoring"
    names = sorted(path.name for path in requests.iterdir())
    stages = [record["stage"] for record in result.ledger]
    assert names == sorted(
        f"{index:02d}-{stage}.{suffix}"
        for index, stage in enumerate(stages, start=1)
        for suffix in ("prompt", "raw")
    )
    assert (requests / "01-call1.prompt").read_text() == transport.requests[0]["user"]
    assert (requests / "02-plan_review.raw").read_bytes() == revise
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert len(evidence["attempts"]) == len(stages)
    assert not (tmp_path / "package").exists()


def _unwritable(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise OSError("disk full")


_UNWRITTEN = {"code": "request_files_unwritten", "detail": "OSError: disk full", "path": "run"}


def _unresolved_run(tmp_path: Path) -> Any:
    revise = review_response("revise", [review_finding("scenario_fidelity")])
    orchestrator, _ = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), revise, json.dumps(_plan()), revise],
        policy=AuthoringPolicy(),
    )
    return orchestrator.run(_view(), _inventory(), _runtime_contract())


def test_a_failed_request_file_write_keeps_the_terminal_record_and_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unresolved_run(tmp_path / "written")
    written = load_failure_evidence(tmp_path / "written" / "package.failure-evidence.json")
    monkeypatch.setattr(orchestrator_module, "write_request_files", _unwritable)

    result = _unresolved_run(tmp_path)

    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert result.status == written["status"] != "accepted"
    assert evidence["terminal"] == written["terminal"]
    assert evidence["findings"] == [*written["findings"], _UNWRITTEN]
    assert not (tmp_path / "package.authoring").exists()


def test_a_crash_whose_request_file_write_fails_keeps_the_crash_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_context(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise RuntimeError("no context")

    monkeypatch.setattr(orchestrator_module, "build_correction_context", no_context)
    monkeypatch.setattr(orchestrator_module, "write_request_files", _unwritable)

    with pytest.raises(RuntimeError, match="no context"):
        run_refund(tmp_path, ScriptedAuthoringTransport([b"{}"]), ONE_PLAN_CORRECTION)

    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["status"] == "failed"
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": 0,
        "reason": "authoring_crashed",
    }
    assert [finding["code"] for finding in evidence["findings"]] == [
        "authoring_crashed",
        "request_files_unwritten",
    ]


def test_an_accepted_scenario_writes_no_request_directory(tmp_path: Path) -> None:
    orchestrator, _ = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), review_response(), _framed(), review_response()],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert (tmp_path / "package" / "authoring" / "01-call1.prompt").is_file()
    assert not (tmp_path / "package.authoring").exists()
