"""A scenario without a package keeps its prompts and raw responses beside its evidence.

An accepted package holds ``authoring/NN-<stage>.prompt`` and ``.raw`` files;
a failed scenario writes the same files under ``<package>.authoring/`` so the
report can link each request of a failure.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

from asago_artifact_generator.authoring.policy import AuthoringPolicy

from .support import (
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
