"""Each request's exact prompt and raw reply live only in the failure-evidence sidecar.

Neither a package nor a directory beside it keeps a copy: the sidecar is
written for accepted and failed scenarios alike, and the report links into it.
"""

from __future__ import annotations

import base64
import json
from functools import partial
from pathlib import Path
from typing import Any

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


def _assert_evidence_holds_each_request(
    evidence: dict[str, Any], requests: list[dict[str, Any]], responses: list[bytes]
) -> None:
    attempts = evidence["attempts"]
    assert [attempt["prompt"]["system"] for attempt in attempts] == [
        request["system"] for request in requests
    ]
    assert [attempt["prompt"]["user"] for attempt in attempts] == [
        request["user"] for request in requests
    ]
    returned = [attempt["raw_response"] for attempt in attempts]
    assert [
        base64.b64decode(raw["base64"]) for raw in returned if raw["availability"] == "available"
    ] == responses


def test_an_unresolved_scenario_keeps_its_requests_only_in_its_evidence(
    tmp_path: Path,
) -> None:
    revise = review_response("revise", [review_finding("scenario_fidelity")])
    responses = [json.dumps(_plan()).encode(), revise, json.dumps(_plan()).encode(), revise]
    orchestrator, transport = _orchestrator(tmp_path, responses, policy=AuthoringPolicy())

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status != "accepted"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["package.failure-evidence.json"]
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    _assert_evidence_holds_each_request(evidence, transport.requests, responses)


def test_an_accepted_scenario_keeps_its_requests_only_in_its_evidence(tmp_path: Path) -> None:
    responses = [json.dumps(_plan()).encode(), review_response(), _framed(), review_response()]
    orchestrator, transport = _orchestrator(tmp_path, responses, policy=AuthoringPolicy())

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "package",
        "package.failure-evidence.json",
    ]
    assert not (tmp_path / "package" / "authoring").exists()
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["status"] == "accepted"
    _assert_evidence_holds_each_request(evidence, transport.requests, responses)
