"""The shared inconclusive vectors, as this package's ``parse`` route reads them.

MiDojo owns the vectors: its ``tests/fixtures/inconclusive_vectors`` holds them, and its
``scripts/sync_inconclusive_vectors.py`` writes this package's byte copy under
``tests/fixtures/inconclusive_vectors``. The copy's ``MANIFEST.json`` is its reference: it
pins every case file's sha256, so a stale or edited copy fails the digest test, and no test
reads MiDojo's tree at run time.

Each case is one sealed run: ``document.json`` is MiDojo's episode document, ``snapshot.json``
and ``verdict.json`` are what the run sealed and what ``midojo.grade`` made of it, and
``expected.json`` says what every route owes. This package's envelope is its own format, so
the test builds it here from the document; the copy carries no Asago fields.

The cases are the four ways a run ends undecided, and the control: a security grade the
judge service cannot decide, a seal interrupted before the grade, a capture with an
unfinished call, and a payload the agent never received. Undecided is ``inconclusive`` in
every one, and never ``not_detected``: the control is the only case this route scores
resisted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_bundle_midojo.document import build_envelope
from asago_bundle_midojo.sealed import INCONCLUSIVE, parse_sealed
from conftest import RECEIPT_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures" / "inconclusive_vectors"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST = json.loads((FIXTURES / MANIFEST_NAME).read_text(encoding="utf-8"))
CASES = MANIFEST["cases"]
CASE_FILES = ("document.json", "snapshot.json", "verdict.json", "expected.json")
SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
# The MiDojo revision the vectors were sealed at, as the envelope names the range it needs.
TOOL_REVISION_RANGE = ">=89b25df"
DETECTORS = {"command_attempt": "ToolCallCondition", "reply": "RubricJudge"}
UNDECIDED = [case for case in CASES if case != "control-resisted"]


def read(case: str, name: str) -> dict[str, Any]:
    """The copy's ``case``/``name`` document, decoded."""

    document: dict[str, Any] = json.loads((FIXTURES / case / name).read_text(encoding="utf-8"))
    return document


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected(case: str) -> dict[str, Any]:
    return read(case, "expected.json")


def test_the_copy_holds_exactly_the_files_the_manifest_pins():
    """A copy is the pinned cases and the manifest: nothing added, nothing left out."""

    on_disk = sorted(
        path.relative_to(FIXTURES).as_posix()
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    )
    assert on_disk == sorted(MANIFEST["copies"])


@pytest.mark.parametrize("case", CASES)
def test_every_case_file_matches_the_owner_digest(case: str):
    """The copy is byte for byte MiDojo's: a sealed file is never edited here."""

    for name in CASE_FILES:
        assert digest(FIXTURES / case / name) == MANIFEST["files"][f"{case}/{name}"], (
            f"{case}/{name} differs from MiDojo's copy"
        )


def compiled(tmp_path: Path, case: str) -> tuple[Path, Path, Path]:
    """Write the case's episode as this package's envelope, and return the sealed files."""

    episode = read(case, "document.json")
    root = tmp_path / "execute" / case
    root.mkdir(parents=True)
    document = build_envelope(
        episode,
        package_id=f"SCN-051-{case}",
        scenario_id=f"SCN-051-{case}",
        package_digest=digest(FIXTURES / case / "document.json"),
        claim_level=expected(case)["claim_level"],
        delivery=expected(case)["delivery"],
        placements=[],
        tool_revision_range=TOOL_REVISION_RANGE,
    )
    (root / "episode.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    sealed = []
    for name in ("snapshot.json", "verdict.json"):
        target = root / name
        shutil.copyfile(FIXTURES / case / name, target)
        sealed.append(target)
    return root, sealed[0], sealed[1]


def receipt_of(tmp_path: Path, case: str) -> dict[str, Any]:
    """``parse`` the case and return its receipt, checked against the receipt schema."""

    root, snapshot, verdict = compiled(tmp_path, case)
    receipt = parse_sealed(root, snapshot, verdict, root / "execution-receipt.json")
    jsonschema.validate(receipt, SCHEMA)
    return receipt


@pytest.mark.parametrize("case", CASES)
def test_every_case_reads_as_the_vector_expects(tmp_path: Path, case: str) -> None:
    """The receipt's outcome is the case's, and it names the snapshot the run sealed."""

    receipt = receipt_of(tmp_path, case)
    want = expected(case)
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == want["routes"]["midojo_parse"]["outcome"]
    assert attempt["detections"][0]["detector"] == DETECTORS[want["claim_level"]]
    assert receipt["episode"]["snapshot_id"] == read(case, "snapshot.json")["snapshot_id"]


@pytest.mark.parametrize("case", UNDECIDED)
def test_no_undecided_case_reads_as_resisted(tmp_path: Path, case: str) -> None:
    """An undecided run is inconclusive here, never ``not_detected``."""

    receipt = receipt_of(tmp_path, case)
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == INCONCLUSIVE
    assert attempt["detections"][0]["outcome"] == INCONCLUSIVE


def test_the_control_reads_as_resisted(tmp_path: Path) -> None:
    """A valid false grade of a delivered payload is the one case that reads resisted."""

    receipt = receipt_of(tmp_path, "control-resisted")
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == "not_detected"
    assert receipt["execution_status"] == "completed"
    assert receipt["episode"]["verdict"]["security"] is False
    assert receipt["episode"]["verdict"]["injection_delivered"] is True


def test_an_undelivered_payload_names_the_delivery_and_reads_inconclusive(
    tmp_path: Path,
) -> None:
    """A false grade of a payload the agent never got resisted nothing: the receipt says so.

    MiDojo's grade is false and valid, so its words say only that the condition did not hold;
    the receipt adds the delivery it graded and reads inconclusive.
    """

    receipt = receipt_of(tmp_path, "not-delivered")
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == INCONCLUSIVE
    assert "the payload was not delivered" in attempt["result"]["reason"]
    assert receipt["execution_status"] == "completed"


def test_an_interrupted_seal_fails_the_run_and_still_reads_inconclusive(
    tmp_path: Path,
) -> None:
    """A chain cut short before its last turn fails the run, and its attempt is not resisted."""

    receipt = receipt_of(tmp_path, "interrupted-seal")
    (attempt,) = receipt["attempts"]
    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"].startswith("turn_failed:")
    assert attempt["result"]["outcome"] == INCONCLUSIVE


def test_a_capture_with_an_unfinished_call_reads_inconclusive(tmp_path: Path) -> None:
    """The unfinished call is the attempt's own evidence, and it decides nothing."""

    receipt = receipt_of(tmp_path, "capture-incomplete")
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == INCONCLUSIVE
    assert [call["status"] for call in attempt["observation"]["tool_calls"]] == ["unfinished"]


def test_the_reply_case_carries_the_judge_it_could_not_reach(tmp_path: Path) -> None:
    """A reply episode grades through the judge, which names no verdict and no request count."""

    receipt = receipt_of(tmp_path, "security-unknown")
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == INCONCLUSIVE
    assert attempt["judge"]["verdict"] == "unresolved"
    assert attempt["judge"]["request_count"] is None
