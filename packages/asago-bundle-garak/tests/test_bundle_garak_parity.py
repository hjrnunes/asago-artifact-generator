"""Parity: one sealed episode reads the same through both adapters' parsers (D17).

Each of the seven ``fixtures/ipi`` cases is read twice: by ``bundle-garak``'s ``parse`` of
the report the probe wrote, and by ``bundle-midojo``'s ``parse --delivery`` of the snapshot,
verdict and delivery documents the same episode sealed. The receipt's evidence block
(``delivery``, ``exposure``, ``verdict``, ``call_injections``) must agree byte for byte, and
so must the attempt's result. The route's own fields (``episode.route`` and the detectors it
names) are excluded: each route states the same decision in its own vocabulary.

The delivery guard is part of the parity: a grade whose security is false and whose payload
the seal never recorded as delivered reads ``inconclusive`` through both parsers, never
``not_detected``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_garak.report_parse import parse_report
from asago_bundle_midojo.document import build_envelope
from asago_bundle_midojo.sealed import parse_sealed, placements_of

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ipi"
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


def load(case: str, name: str) -> Any:
    return json.loads((FIXTURES / case / name).read_text(encoding="utf-8"))


def canonical(value: Any) -> str:
    """The byte form the two receipts compare by: canonical JSON, no whitespace."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
    return parse_sealed(
        episode,
        episode / "snapshot.json",
        episode / "verdict.json",
        root / "midojo-receipt.json",
        delivery_path=episode / "delivery.json",
    )


def garak_receipt(root: Path, case: str) -> dict[str, Any]:
    """Read the case through ``bundle-garak``'s ``parse`` of the report the probe wrote."""

    source = load(case, "ipi-episodes.json")
    (entry,) = source
    metadata = entry["metadata"]
    bundle = root / "bundle"
    (bundle / "reports").mkdir(parents=True)
    (bundle / "ipi-episodes.json").write_text(canonical(source), encoding="utf-8")
    manifest = {
        "schema_version": "tool-bundle-v1",
        "tool": "garak",
        "scenario_id": metadata["scenario_id"],
        "package_id": metadata["package_id"],
        "package_digest": metadata["package_digest"],
        "claim_level": metadata["claim_level"],
        "delivery": metadata["delivery"],
        "template": "garak-ipi-episode",
        "native_outputs": [f"reports/{case}.report.jsonl"],
    }
    (bundle / "bundle.json").write_text(canonical(manifest), encoding="utf-8")
    messages = [{"role": "user", "content": turn} for turn in entry["injection_spec"]["turns"]]
    (bundle / "conversations.jsonl").write_text(
        canonical({"messages": messages}) + "\n", encoding="utf-8"
    )
    shutil.copyfile(FIXTURES / case / "report.jsonl", bundle / "reports" / f"{case}.report.jsonl")
    return parse_report(bundle, root / "garak-receipt.json")


@pytest.mark.parametrize("case", CASES)
def test_both_parsers_read_one_sealed_episode_the_same(tmp_path: Path, case: str) -> None:
    """The evidence block is the seal's, and each parser states it as the other does."""

    midojo, garak = midojo_receipt(tmp_path, case), garak_receipt(tmp_path, case)
    expected = load(case, "expected.json")

    assert midojo["execution_status"] == garak["execution_status"] == expected["execution_status"]
    assert midojo["incomplete_reason"] == garak["incomplete_reason"]
    assert midojo["incomplete_reason"] == expected["incomplete_reason"]
    for field in ("evaluation_id", "snapshot_id", "evidence_status"):
        assert midojo["episode"][field] == garak["episode"][field], field
    for field in EVIDENCE:
        assert canonical(midojo["episode"][field]) == canonical(garak["episode"][field]), field
    # Every fixture is a sealed chain, so neither route may state the evidence as missing.
    for receipt in (midojo, garak):
        for field in EVIDENCE:
            assert receipt["episode"][field] is not None, field
    sealed = midojo["episode"]["verdict"]
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


@pytest.mark.parametrize("case", CASES)
def test_both_parsers_state_one_attempt_decision(tmp_path: Path, case: str) -> None:
    """The attempt's result is ``grade_result`` of the seal through either parser.

    A route that refuses the seal deeply enough to build no attempt (MiDojo's failed receipt)
    still states the same decision where the other route keeps its attempt: the case's
    expected outcome and the refusal's reason.
    """

    midojo, garak = midojo_receipt(tmp_path, case), garak_receipt(tmp_path, case)
    expected = load(case, "expected.json")
    (garak_attempt,) = garak["attempts"]

    assert len(garak["attempts"]) == expected["attempt_count"]
    if midojo["attempts"]:
        assert canonical(midojo["attempts"][0]["result"]) == canonical(garak_attempt["result"])
    assert garak_attempt["result"]["outcome"] == expected["outcome"]
    assert garak_attempt["result"]["claim_level"] == expected["claim_level"]
    if expected["execution_status"] == "failed":
        assert midojo["attempts"] == []
        assert garak_attempt["result"]["reason"] == expected["incomplete_reason"]


def test_the_undelivered_case_reads_inconclusive_through_both_parsers(tmp_path: Path) -> None:
    """The payload never reached the agent, so neither parser may call the episode resisted."""

    midojo, garak = midojo_receipt(tmp_path, "undelivered"), garak_receipt(tmp_path, "undelivered")
    sealed = garak["episode"]["verdict"]

    assert sealed["security"] is False
    assert sealed["injection_delivered"] is False
    assert garak["episode"]["delivery"] == [], "the seal placed the payload on no call"
    for receipt in (midojo, garak):
        (attempt,) = receipt["attempts"]
        assert attempt["result"]["outcome"] == "inconclusive"
        assert attempt["result"]["reason"] != "not_detected"
