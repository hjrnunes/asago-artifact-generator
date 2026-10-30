"""parse: what MiDojo seals of a placement from a19562e, read before the plan.

Each placement (``applied``, ``partial``, ``fallback``) sealed from a19562e carries
``payload_digest``, ``placed_digest`` and ``slot``. parse takes the outcome's
``payload_sha256`` from its sealed ``payload_digest`` and its ``slot`` from its sealed
``slot``, named after the plan's ``placements``, for a static plan and a plan that may
generate payloads alike; the plan is the fallback when a key is absent, and a static plan
that disagrees with the sealed digest establishes neither. ``fixtures/placed-a19562e/``
holds what MiDojo sealed at a19562e (``placed_cases.py``).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from episode_cases import EPISODES, PLANTED, REGRADED
from midojo_sdk.snapshot import snapshot_digest
from placed_cases import PLACED
from result_record_cases import CLINIC_PLANTED
from test_sealed_placement import NOTE_SLOT, REFERRAL_SLOT, outcomes, parsed, placed, sha256

from asago_bundle_midojo.sealed import PLACEMENT_FIELDS, slot_id

RECORDING = json.loads((PLACED / "recording.json").read_text(encoding="utf-8"))
GENERATED = RECORDING["generated_payloads"]
NOTE = PLACED / "note" / "detected"
# Case -> what each of its run's outcomes places, in call order.
EXPECTED: dict[str, list[tuple[str | None, dict[str, str] | None]]] = {
    "note": [(sha256(PLANTED), NOTE_SLOT)],
    "note-generated": [(sha256(GENERATED["note-generated"]), NOTE_SLOT)],
    "referral": [(sha256(CLINIC_PLANTED), REFERRAL_SLOT), (None, None)],
    "referral-generated": [
        (sha256(GENERATED["referral-generated"]), REFERRAL_SLOT),
        (None, None),
    ],
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_of(case: str) -> Path:
    return PLACED / case / "detected"


def note_snapshot(**changes: Any) -> dict[str, Any]:
    """The static note run's snapshot, its one outcome changed as given."""

    snapshot = _read(NOTE / "snapshot.json")
    snapshot["function_calls"][0]["injection_outcomes"][0].update(changes)
    return snapshot


def other_slot(record: str = "NOTE-2") -> dict[str, Any]:
    """A well-formed sealed slot for a place the note plan does not name."""

    slot = copy.deepcopy(
        _read(NOTE / "snapshot.json")["function_calls"][0]["injection_outcomes"][0]["slot"]
    )
    slot["target"]["structured_content_path"][1]["equals"] = record
    slot["id"] = slot_id(slot["target"])
    return slot


# --- the fixtures ------------------------------------------------------------------------


def test_the_fixtures_were_sealed_at_a19562e_and_their_generated_payloads_are_not_planned() -> (
    None
):
    assert RECORDING["midojo_revision"].startswith("a19562e")
    for case, payload in GENERATED.items():
        (instruction,) = _read(PLACED / case / "episode.json")["episode"]["plan"]["instructions"]
        assert payload != instruction["payload"]


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_the_digest_port_reproduces_each_snapshot_id_midojo_sealed(case: str) -> None:
    snapshot = _read(run_of(case) / "snapshot.json")

    assert snapshot_digest(snapshot) == snapshot["snapshot_id"]


def test_a_placement_key_that_is_null_is_left_out_of_the_digest() -> None:
    sealed = _read(run_of("referral") / "snapshot.json")
    unmatched = sealed["function_calls"][1]["injection_outcomes"][0]
    assert {key: unmatched[key] for key in PLACEMENT_FIELDS} == dict.fromkeys(PLACEMENT_FIELDS)

    for key in PLACEMENT_FIELDS:
        del unmatched[key]
    assert snapshot_digest(sealed) == sealed["snapshot_id"]

    before = _read(REGRADED / "command-single" / "detected" / "snapshot.json")
    for outcome in before["function_calls"][0]["injection_outcomes"]:
        outcome.update(dict.fromkeys(PLACEMENT_FIELDS))
    assert snapshot_digest(before) == before["snapshot_id"]


def test_a_placement_key_that_is_set_is_in_the_digest() -> None:
    snapshot = note_snapshot(placed_digest="sha256:" + "0" * 64)

    assert snapshot_digest(snapshot) != snapshot["snapshot_id"]


# --- snapshot first ----------------------------------------------------------------------


@pytest.mark.parametrize("delivery", ["recorded", None], ids=["with-delivery", "without"])
@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_each_run_s_placements_are_what_midojo_sealed(
    tmp_path: Path, case: str, delivery: Any
) -> None:
    receipt = parsed(tmp_path, run_of(case), episode=PLACED / case, delivery=delivery)

    assert receipt["execution_status"] == "completed"
    assert placed(receipt) == EXPECTED[case]


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_the_receipt_carries_no_sealed_placement_key(tmp_path: Path, case: str) -> None:
    receipt = parsed(tmp_path, run_of(case), episode=PLACED / case)

    facts = receipt["episode"]["delivery"]
    assert facts and all(not PLACEMENT_FIELDS & fact.keys() for fact in facts)
    assert all({"payload_digest", "placed_digest"}.isdisjoint(item) for item in outcomes(receipt))


def test_a_generated_payload_s_digest_is_the_sealed_one_not_the_plan_s(tmp_path: Path) -> None:
    receipt = parsed(tmp_path, run_of("note-generated"), episode=PLACED / "note-generated")

    ((digest, _slot),) = placed(receipt)
    assert digest == sha256(GENERATED["note-generated"]) != sha256(PLANTED)


@pytest.mark.parametrize("case", ["note", "note-generated"])
def test_without_the_sealed_keys_parse_reads_the_plan_as_before(tmp_path: Path, case: str) -> None:
    snapshot = _read(run_of(case) / "snapshot.json")
    snapshot["function_calls"][0]["injection_outcomes"][0].update(dict.fromkeys(PLACEMENT_FIELDS))

    receipt = parsed(
        tmp_path, run_of(case), episode=PLACED / case, snapshot=snapshot, delivery=None
    )

    # A static plan's text is what was placed; a generated plan's is not, so neither is known.
    assert placed(receipt) == (
        [(sha256(PLANTED), NOTE_SLOT)] if case == "note" else [(None, None)]
    )


def test_a_sealed_digest_alone_falls_back_to_the_plan_s_slot(tmp_path: Path) -> None:
    snapshot = note_snapshot(slot=None)

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(sha256(PLANTED), NOTE_SLOT)]


def test_a_sealed_slot_is_named_after_a_plan_that_cannot_resolve_the_instruction(
    tmp_path: Path,
) -> None:
    def unresolved(document: dict[str, Any]) -> None:
        document["episode"]["plan"]["instructions"][0]["probe_key"] = "planted_item:seed_9"

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", delivery=None, plan=unresolved)

    assert placed(receipt) == [(sha256(PLANTED), NOTE_SLOT)]


def test_an_outcome_that_did_not_select_its_place_has_the_digest_and_no_slot(
    tmp_path: Path,
) -> None:
    snapshot = note_snapshot(status="fallback", selected=False)

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(sha256(PLANTED), None)]


def test_an_outcome_that_emitted_nothing_has_neither_whatever_it_sealed(tmp_path: Path) -> None:
    snapshot = note_snapshot(emitted=[])

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(None, None)]


def test_the_slot_carries_the_sealed_mode(tmp_path: Path) -> None:
    sealed = _read(NOTE / "snapshot.json")["function_calls"][0]["injection_outcomes"][0]["slot"]
    snapshot = note_snapshot(slot={**sealed, "mode": "replace"})

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(sha256(PLANTED), {**NOTE_SLOT, "mode": "replace"})]


# --- what does not hold together ---------------------------------------------------------


def test_a_static_plan_whose_payload_is_not_the_sealed_one_establishes_neither(
    tmp_path: Path,
) -> None:
    def another(document: dict[str, Any]) -> None:
        document["episode"]["plan"]["instructions"][0]["payload"] = "Another text."

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", plan=another)

    assert receipt["execution_status"] == "completed"
    assert placed(receipt) == [(None, None)]


def test_a_sealed_digest_a_static_plan_disagrees_with_establishes_neither(tmp_path: Path) -> None:
    other = "sha256:" + hashlib.sha256(b"Another text.").hexdigest()
    snapshot = note_snapshot(payload_digest=other, placed_digest=other)

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(None, None)]


def test_a_generated_plan_has_no_payload_to_disagree_with(tmp_path: Path) -> None:
    def another(document: dict[str, Any]) -> None:
        document["episode"]["plan"]["instructions"][0]["payload"] = "Another text."

    case = "note-generated"
    receipt = parsed(tmp_path, run_of(case), episode=PLACED / case, plan=another)

    assert placed(receipt) == EXPECTED[case]


@pytest.mark.parametrize("case", ["note", "note-generated"])
def test_a_sealed_slot_no_plan_instruction_targets_gives_no_slot(
    tmp_path: Path, case: str
) -> None:
    snapshot = _read(run_of(case) / "snapshot.json")
    snapshot["function_calls"][0]["injection_outcomes"][0]["slot"] = other_slot()

    receipt = parsed(
        tmp_path, run_of(case), episode=PLACED / case, snapshot=snapshot, delivery=None
    )

    assert placed(receipt) == [(EXPECTED[case][0][0], None)]


@pytest.mark.parametrize("case", ["note", "note-generated"])
def test_placements_naming_different_slots_for_the_sealed_place_give_no_slot(
    tmp_path: Path, case: str
) -> None:
    def conflicting(document: dict[str, Any]) -> None:
        entry = document["placements"][0]
        document["placements"] = [entry, {**entry, "slot": "piggybank.note.read"}]

    receipt = parsed(tmp_path, run_of(case), episode=PLACED / case, plan=conflicting)

    assert placed(receipt) == [(EXPECTED[case][0][0], None)]


def test_a_placements_record_that_is_not_the_sealed_target_s_gives_no_slot(
    tmp_path: Path,
) -> None:
    def elsewhere(document: dict[str, Any]) -> None:
        document["placements"][0]["record"] = "NOTE-2"

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", plan=elsewhere)

    assert placed(receipt) == [(sha256(PLANTED), None)]


def test_a_failed_receipt_establishes_neither(tmp_path: Path) -> None:
    verdict = _read(NOTE / "verdict.json")
    verdict["evaluation_id"] = "another"

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", verdict=verdict)

    assert receipt["incomplete_reason"] == "evaluation_mismatch"
    assert placed(receipt) == [(None, None)]


# --- shape -------------------------------------------------------------------------------


def _slot_changes() -> dict[str, Any]:
    sealed = _read(NOTE / "snapshot.json")["function_calls"][0]["injection_outcomes"][0]["slot"]
    return {
        "slot-extra-key": {**sealed, "record": "NOTE-1"},
        "slot-missing-key": {"id": sealed["id"], "target": sealed["target"]},
        "slot-id-not-its-target-s": {**sealed, "id": other_slot()["id"]},
        "slot-mode-unknown": {**sealed, "mode": "prepend"},
        "slot-target-not-an-object": {**sealed, "target": "list_notes"},
        "slot-not-an-object": sealed["id"],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"payload_digest": "sha256:" + "A" * 64},
        {"payload_digest": "a" * 64},
        {"placed_digest": "sha256:" + "a" * 63},
        {"placed_digest": 7},
        *({"slot": slot} for slot in _slot_changes().values()),
        {"status": "too_large"},
        {"status": "skipped_occurrence"},
    ],
    ids=[
        "digest-upper-case",
        "digest-without-prefix",
        "placed-digest-short",
        "placed-digest-not-text",
        *_slot_changes(),
        "too-large",
        "skipped-occurrence",
    ],
)
def test_a_malformed_sealed_placement_fails_the_receipt(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    snapshot = note_snapshot(**changes)

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert receipt["incomplete_reason"] == "snapshot_malformed"


def test_an_unmatched_outcome_with_a_placement_fails_the_receipt(tmp_path: Path) -> None:
    run = run_of("referral")
    snapshot = _read(run / "snapshot.json")
    applied = snapshot["function_calls"][0]["injection_outcomes"][0]
    snapshot["function_calls"][1]["injection_outcomes"][0]["payload_digest"] = applied[
        "payload_digest"
    ]

    receipt = parsed(tmp_path, run, episode=PLACED / "referral", snapshot=snapshot, delivery=None)

    assert receipt["incomplete_reason"] == "snapshot_malformed"


def test_each_placement_key_is_optional(tmp_path: Path) -> None:
    snapshot = _read(NOTE / "snapshot.json")
    del snapshot["function_calls"][0]["injection_outcomes"][0]["placed_digest"]

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", snapshot=snapshot, delivery=None)

    assert placed(receipt) == EXPECTED["note"]


# --- delivery.json from a19562e ----------------------------------------------------------


def test_a_delivery_document_with_the_placement_keys_is_read(tmp_path: Path) -> None:
    delivery = _read(NOTE / "delivery.json")
    assert PLACEMENT_FIELDS <= delivery["delivery"][0].keys()

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", delivery=delivery)

    assert receipt["episode"]["delivery"] == [
        {key: value for key, value in fact.items() if key not in PLACEMENT_FIELDS}
        for fact in delivery["delivery"]
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"payload_digest": "sha256:" + "A" * 64},
        {"slot": {"id": "sha256:" + "0" * 64, "target": {}, "mode": "append"}},
        {"status": "too_large"},
    ],
    ids=["digest-upper-case", "slot-id-not-its-target-s", "not-a-placement"],
)
def test_a_malformed_delivery_placement_fails_the_receipt(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    delivery = _read(NOTE / "delivery.json")
    delivery["delivery"][0].update(changes)

    receipt = parsed(tmp_path, NOTE, episode=PLACED / "note", delivery=delivery)

    assert receipt["incomplete_reason"] == "delivery_malformed"


def test_a_delivery_document_before_the_keys_still_reads(tmp_path: Path) -> None:
    receipt = parsed(
        tmp_path, EPISODES.parent / "regraded-13d5487" / "command-single" / "detected"
    )

    assert receipt["execution_status"] == "completed"
