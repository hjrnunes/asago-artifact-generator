"""Each review attempt in the failure evidence stores the candidate it reviewed.

The report compares a field before and after a revision from these stored
candidates, so each must hash to the review's ``reviewed_candidate_sha256``.
The package's ledger, reviews, and manifest stay as they were.
"""

from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path

from asago_artifact_generator.authoring.core import _canonical_json
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
_orchestrator = partial(scripted_orchestrator, task_id="candidate")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reviews(evidence: dict) -> list[dict]:
    return [attempt["review"] for attempt in evidence["attempts"] if attempt["role"] == "reviewer"]


def test_every_review_stores_the_candidate_that_hashes_to_its_digest(tmp_path: Path) -> None:
    revise = review_response("revise", [review_finding("scenario_fidelity")])
    orchestrator, _ = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), revise, json.dumps(_plan()), review_response()]
        + [_framed(), review_response()],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    reviews = _reviews(evidence)
    assert [review["decision"] for review in reviews] == ["revise", "accept", "accept"]
    for review in reviews:
        assert _digest(review["reviewed_candidate"]) == review["reviewed_candidate_sha256"]
    assert "observation_claim" in reviews[0]["reviewed_candidate"]
    assert "stimulus" in reviews[2]["reviewed_candidate"]


def test_the_package_records_keep_no_candidate_copy(tmp_path: Path) -> None:
    orchestrator, _ = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), review_response(), _framed(), review_response()],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.package is not None
    assert all("reviewed_candidate" not in record for record in result.ledger)
    assert all("reviewed_candidate" not in (r.get("review") or {}) for r in result.ledger)
    assert '"reviewed_candidate"' not in json.dumps(result.package.manifest.authoring)
