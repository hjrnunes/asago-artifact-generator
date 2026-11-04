"""Parity: one sealed episode reads the same through both adapters' parsers (D17).

Each of the seven ``fixtures/ipi`` cases is read twice: by ``bundle-garak``'s ``parse`` of
the report the probe wrote, and by ``bundle-midojo``'s ``parse --delivery`` of the snapshot,
verdict and delivery documents the same episode sealed. The receipt's evidence block
(``delivery``, ``exposure``, ``verdict``, ``call_injections``) must agree byte for byte, and
so must every attempt: the count, each attempt's result (outcome, claim level and reason) and
its judge verdict. The route's own fields (``episode.route``, the ``detections[]`` it names
and the judge fields beyond the agreed verdict vocabulary) are excluded: each route states
the same decision in its own vocabulary.

A seal a parser cannot verify fails both receipts with MiDojo's reason and, as MiDojo's
reader does, states no attempt. The tampered case states no attempt on either route.

The delivery guard is part of the parity: a grade whose security is false and whose payload
the seal never recorded as delivered reads ``inconclusive`` through both parsers, never
``not_detected``.

This is the only Garak test that reads the MiDojo adapter, so it is marked ``parity``: the
adapter is a test-only dependency. The isolated CI run of ``bundle-garak`` installs no MiDojo
adapter, and this module skips when it cannot import one; the run also deselects the marker
with ``-m "not parity"`` where the adapter is installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from garak_receipts import canonical, garak_receipt, load

pytest.importorskip("asago_bundle_midojo", reason="the adapter is a test-only dependency")
from asago_bundle_midojo.cli import main as midojo_main  # noqa: E402  the test-only adapter
from asago_injection_spec.document import build_envelope
from asago_injection_spec.sealed import placements_of

pytestmark = pytest.mark.parity

CASES = (
    "blocked",
    "delivered",
    "incomplete",
    "reply-judged",
    "sequential",
    "tampered",
    "undelivered",
)
# The episode fields both parsers derive from the seal, compared as canonical JSON.
EVIDENCE = ("delivery", "exposure", "verdict", "call_injections")
# The MiDojo revision range the fixtures' injection specs were compiled for.
TOOL_REVISION_RANGE = ">=89b25df"
DOCUMENTS = ("snapshot.json", "verdict.json", "delivery.json")


def write_midojo_episode(root: Path, case: str) -> Path:
    """Write the case as ``bundle-midojo`` reads it: the envelope and the sealed documents.

    The fixtures hold the injection spec the Garak adapter compiled, not the MiDojo
    adapter's ``injection-spec.json``, so the envelope is rebuilt around that same spec. Its
    placements are the entry's own, the ones the package was compiled with, which name the
    seed slot; a case compiled before the entry carried them falls back to the delivery
    document, whose facts name the planted target by its digest.
    """

    (entry,) = load(case, "ipi-episodes.json")
    metadata = entry["metadata"]
    envelope = build_envelope(
        entry["injection_spec"],
        package_id=metadata["package_id"],
        scenario_id=metadata["scenario_id"],
        package_digest=metadata["package_digest"],
        claim_level=metadata["claim_level"],
        delivery=metadata["delivery"],
        placements=entry.get("placements") or placements_of(load(case, "delivery.json")),
        tool_revision_range=TOOL_REVISION_RANGE,
    )
    episode = root / "midojo"
    episode.mkdir()
    (episode / "injection-spec.json").write_text(canonical(envelope), encoding="utf-8")
    for name in DOCUMENTS:
        (episode / name).write_text(canonical(load(case, name)), encoding="utf-8")
    return episode


def midojo_receipt(root: Path, case: str) -> dict[str, Any]:
    """Read the case through ``bundle-midojo``'s ``parse --delivery``."""

    episode = write_midojo_episode(root, case)
    out = root / "midojo-receipt.json"
    assert (
        midojo_main(
            [
                "parse",
                str(episode),
                "--snapshot",
                str(episode / "snapshot.json"),
                "--verdict",
                str(episode / "verdict.json"),
                "--delivery",
                str(episode / "delivery.json"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    return load_json(out)


def load_json(path: Path) -> dict[str, Any]:
    """Return the receipt ``bundle-midojo`` wrote."""

    receipt: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return receipt


@pytest.mark.parametrize("case", CASES)
def test_both_parsers_read_one_sealed_episode_the_same(tmp_path: Path, case: str) -> None:
    """The evidence block is the seal's, and each parser states it as the other does."""

    midojo, garak = midojo_receipt(tmp_path, case), garak_receipt(tmp_path, case)
    expected = load(case, "expected.json")

    assert midojo["execution_status"] == garak["execution_status"] == expected["execution_status"]
    assert midojo["incomplete_reason"] == garak["incomplete_reason"]
    assert midojo["incomplete_reason"] == expected["incomplete_reason"]
    for field in ("evaluation_id", "snapshot_id", "evidence_status"):
        assert midojo["evaluation"][field] == garak["evaluation"][field], field
    for field in EVIDENCE:
        assert canonical(midojo["evaluation"][field]) == canonical(garak["evaluation"][field]), (
            field
        )
    # Every fixture is a sealed chain, so neither route may state the evidence as missing.
    for receipt in (midojo, garak):
        for field in EVIDENCE:
            assert receipt["evaluation"][field] is not None, field
    sealed = midojo["evaluation"]["verdict"]
    assert sealed["security"] == expected["security"]
    assert sealed["validity"] == expected["security_validity"]
    assert sealed["injection_delivered"] == expected["injection_delivered"]
    assert sealed["effect_observed"] == expected["effect_observed"]
    # The delivery guard: a not-detected decision needs a grade that decided the attack was
    # resisted, never a grade the seal could not decide.
    for receipt in (midojo, garak):
        for attempt in receipt["attempts"]:
            if attempt["result"]["outcome"] == "not_detected":
                assert sealed["security"] is True


def judge_verdict(attempt: dict[str, Any]) -> Any:
    """The attempt's judge verdict: the one judge field both routes state as the same."""

    judge = attempt.get("judge")
    return judge.get("verdict") if isinstance(judge, dict) else None


@pytest.mark.parametrize("case", CASES)
def test_both_parsers_state_the_same_attempts(tmp_path: Path, case: str) -> None:
    """Every attempt, and its decision, reads the same through either parser.

    The tampered case decides the failed-evidence representation: MiDojo's reader builds no
    attempt for a seal it cannot verify, so the Garak receipt has none either. Only the
    route's ``detections[]``, ``episode.route`` and the judge fields beyond the agreed
    verdict vocabulary are excluded (D17); the count and every result field are compared.
    """

    midojo, garak = midojo_receipt(tmp_path, case), garak_receipt(tmp_path, case)
    expected = load(case, "expected.json")

    assert len(garak["attempts"]) == len(midojo["attempts"]) == expected["attempt_count"]
    for garak_attempt, midojo_attempt in zip(garak["attempts"], midojo["attempts"], strict=True):
        assert canonical(garak_attempt["result"]) == canonical(midojo_attempt["result"])
        assert judge_verdict(garak_attempt) == judge_verdict(midojo_attempt)
        assert garak_attempt["result"]["outcome"] == expected["outcome"]
        assert garak_attempt["result"]["claim_level"] == expected["claim_level"]
    if expected["execution_status"] == "failed":
        assert garak["attempts"] == [], "a failed seal states no attempt on either route"


def test_the_undelivered_case_reads_inconclusive_through_both_parsers(tmp_path: Path) -> None:
    """The payload never reached the agent, so neither parser may call the episode resisted."""

    midojo, garak = midojo_receipt(tmp_path, "undelivered"), garak_receipt(tmp_path, "undelivered")
    sealed = garak["evaluation"]["verdict"]

    assert sealed["security"] is False
    assert sealed["injection_delivered"] is False
    assert garak["evaluation"]["delivery"] == [], "the seal placed the payload on no call"
    for receipt in (midojo, garak):
        (attempt,) = receipt["attempts"]
        assert attempt["result"]["outcome"] == "inconclusive"
        assert attempt["result"]["reason"] != "not_detected"
