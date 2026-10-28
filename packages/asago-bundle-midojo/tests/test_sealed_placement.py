"""parse: each injection outcome's ``payload_sha256`` and ``slot``.

MiDojo seals neither the payload text it placed nor its digest nor the seed slot, so parse
reads both from the episode's plan when the plan is static (no generator permissions): the
outcome's instruction is the plan's tool-output instruction MiDojo's ``delivery_facts`` maps
it to, ``payload_sha256`` its payload's digest when the outcome emitted it, and ``slot`` its
``placements`` entry with its mode when the outcome also selected its place. Anything that
does not establish them leaves them null.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import EPISODES, PLANTED, REGRADED
from result_record_cases import CLINIC_PLANTED, RESULT_RECORDS

from asago_bundle_midojo.sealed import UNPLACED, parse_sealed
from asago_bundle_midojo.snapshot_digest import snapshot_digest
from conftest import RECEIPT_SCHEMA, REPO, as_sealed

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
REPORTED = REGRADED / "reported" / "command-single" / "detected"
REFERRAL = RESULT_RECORDS / "referral" / "detected"
NOTE_SLOT = {"id": "piggybank.note.body", "record": "NOTE-1", "mode": "append"}
REFERRAL_SLOT = {"id": "clinic.referral.reason", "record": "REF-101", "mode": "append"}
CONTRACT_CASE = (
    REPO / "contracts/execution-receipt/execution-receipt-v1/valid/midojo-episode-placement.json"
)
RESULT_RECORD_REVISION = json.loads((RESULT_RECORDS / "recording.json").read_text())[
    "midojo_revision"
]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parsed(
    tmp_path: Path,
    run: Path = REPORTED,
    *,
    episode: Path = EPISODES / "command-single",
    snapshot: dict[str, Any] | None = None,
    verdict: dict[str, Any] | None = None,
    delivery: Any = "recorded",
    plan: Any = None,
) -> dict[str, Any]:
    """Parse ``run`` copied into ``tmp_path/run``; ``snapshot`` is resealed and the verdict
    renamed to it, ``plan`` edits a copy of the episode, and ``delivery`` is the document to
    write (``"recorded"``: the run's own; None: parse without one)."""

    work = tmp_path / "run"
    work.mkdir()
    if snapshot is not None:
        snapshot = copy.deepcopy(snapshot)
        snapshot["snapshot_id"] = snapshot_digest(snapshot)
        verdict = _read(run / "verdict.json") if verdict is None else verdict
        verdict["snapshot_id"] = snapshot["snapshot_id"]
    for name, document in (("snapshot", snapshot), ("verdict", verdict)):
        if document is None:
            shutil.copy(run / f"{name}.json", work / f"{name}.json")
        else:
            (work / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
    delivery_path = None
    if delivery is not None:
        delivery_path = work / "delivery.json"
        if delivery == "recorded":
            shutil.copy(run / "delivery.json", delivery_path)
        else:
            delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
    if plan is not None:
        copied = tmp_path / "episode"
        shutil.copytree(episode, copied)
        document = _read(copied / "episode.json")
        plan(document)
        (copied / "episode.json").write_text(json.dumps(document), encoding="utf-8")
        episode = copied
    receipt = parse_sealed(
        episode,
        work / "snapshot.json",
        work / "verdict.json",
        work / "receipt.json",
        delivery_path=delivery_path,
    )
    jsonschema.validate(receipt, SCHEMA)
    return receipt


def outcomes(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        outcome
        for call in receipt["episode"]["call_injections"]
        for outcome in call["injection_outcomes"] or []
    ]


def placed(receipt: dict[str, Any]) -> list[tuple[str | None, dict[str, str] | None]]:
    return [(outcome["payload_sha256"], outcome["slot"]) for outcome in outcomes(receipt)]


def reported_snapshot(**changes: Any) -> dict[str, Any]:
    """The reported run's snapshot, its one outcome changed as given."""

    snapshot = _read(REPORTED / "snapshot.json")
    snapshot["function_calls"][0]["injection_outcomes"][0].update(changes)
    return snapshot


# --- an emitted outcome: the payload's digest and its slot -----------------------------


@pytest.mark.parametrize("delivery", ["recorded", None], ids=["with-delivery", "without"])
def test_an_emitted_outcome_carries_the_digest_of_the_payload_placed_and_its_slot(
    tmp_path: Path, delivery: Any
) -> None:
    receipt = parsed(tmp_path, delivery=delivery)

    assert receipt["execution_status"] == "completed"
    assert placed(receipt) == [(sha256(PLANTED), NOTE_SLOT)]


def test_the_digest_is_of_the_text_midojo_placed_in_the_slot_s_record() -> None:
    episode = _read(EPISODES / "command-single" / "episode.json")
    (instruction,) = episode["episode"]["plan"]["instructions"]
    snapshot = _read(REPORTED / "snapshot.json")
    (note,) = [
        note
        for note in snapshot["function_calls"][0]["structured_content"]["notes"]
        if note["note_id"] == NOTE_SLOT["record"]
    ]

    assert instruction["payload"] == PLANTED
    # Placed by appending, with the identity transformation: the record ends with the text.
    assert note["body"].endswith(PLANTED)


def test_a_result_record_run_places_its_emitted_outcome_and_not_its_unmatched_one(
    tmp_path: Path,
) -> None:
    receipt = parsed(tmp_path, REFERRAL, episode=RESULT_RECORDS / "referral", delivery=None)

    assert [(outcome["status"], outcome["occurrence"]) for outcome in outcomes(receipt)] == [
        ("applied", 1),
        ("unmatched", 0),
    ]
    assert placed(receipt) == [(sha256(CLINIC_PLANTED), REFERRAL_SLOT), (None, None)]


def test_a_fallback_placement_has_its_digest_and_no_slot(tmp_path: Path) -> None:
    snapshot = reported_snapshot(status="fallback", selected=False)

    receipt = parsed(tmp_path, snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(sha256(PLANTED), None)]


def test_a_transformed_placement_digests_the_text_before_its_transformation(
    tmp_path: Path,
) -> None:
    snapshot = reported_snapshot(transformation="json_string")

    receipt = parsed(tmp_path, snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(sha256(PLANTED), NOTE_SLOT)]


def test_the_slot_carries_the_mode_the_instruction_placed_with(tmp_path: Path) -> None:
    def replace(document: dict[str, Any]) -> None:
        document["episode"]["plan"]["instructions"][0]["mode"] = "replace"

    receipt = parsed(tmp_path, plan=replace)

    assert placed(receipt) == [(sha256(PLANTED), {**NOTE_SLOT, "mode": "replace"})]


def test_a_plan_without_a_placements_entry_gives_the_digest_and_no_slot(tmp_path: Path) -> None:
    def unplaced(document: dict[str, Any]) -> None:
        document["placements"] = []

    receipt = parsed(tmp_path, plan=unplaced)

    assert placed(receipt) == [(sha256(PLANTED), None)]


# --- what leaves both null --------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"selected": True, "emitted": []},
        {"status": "too_large", "selected": True, "transformation": "identity", "emitted": []},
        {"status": "unmatched", "occurrence": 0, "selected": False, "transformation": None},
    ],
    ids=["overwritten", "too-large", "unmatched"],
)
def test_an_outcome_that_emitted_nothing_has_neither(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    changes.setdefault("emitted", [])
    snapshot = reported_snapshot(**changes)

    receipt = parsed(tmp_path, snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(None, None)]


def test_an_outcome_that_does_not_say_what_it_emitted_has_neither(tmp_path: Path) -> None:
    snapshot = reported_snapshot(selected=None, transformation=None, emitted=None)

    receipt = parsed(tmp_path, snapshot=snapshot, delivery=None)

    assert placed(receipt) == [(None, None)]


def test_an_outcome_sealed_before_its_delivery_facts_has_neither(tmp_path: Path) -> None:
    receipt = parsed(tmp_path, EPISODES / "command-single" / "detected", delivery=None)

    (outcome,) = outcomes(receipt)
    assert "emitted" not in as_sealed([outcome])[0]  # type: ignore[index]
    assert placed(receipt) == [(None, None)]


@pytest.mark.parametrize(
    "permissions",
    [
        {"tools": ["list_notes"], "fields": [], "modes": []},
        {"tools": [], "fields": [["notes", "body"]], "modes": ["append"]},
        "all",
    ],
    ids=["tools", "fields-and-modes", "not-an-object"],
)
def test_a_plan_that_may_generate_payloads_establishes_neither(
    tmp_path: Path, permissions: Any
) -> None:
    def generative(document: dict[str, Any]) -> None:
        document["episode"]["plan"]["generator_permissions"] = permissions

    receipt = parsed(tmp_path, plan=generative)

    assert receipt["execution_status"] == "completed"
    assert placed(receipt) == [(None, None)]


@pytest.mark.parametrize(
    "edit",
    ["probe-key", "tool", "index", "channel", "no-plan"],
)
def test_an_instruction_that_does_not_resolve_establishes_neither(
    tmp_path: Path, edit: str
) -> None:
    def changed(document: dict[str, Any]) -> None:
        plan = document["episode"]["plan"]
        instruction = plan["instructions"][0]
        if edit == "probe-key":
            instruction["probe_key"] = "planted_item:seed_1"
        elif edit == "tool":
            instruction["target"]["tool"] = "get_note"
        elif edit == "index":
            plan["instructions"] = [{**instruction, "channel": "user_prompt"}]
        elif edit == "channel":
            plan["instructions"] = [{**instruction, "channel": "tool_description"}]
        elif edit == "no-plan":
            del document["episode"]["plan"]

    receipt = parsed(tmp_path, plan=changed)

    assert placed(receipt) == [(None, None)]


def test_a_payload_that_is_not_utf_8_has_no_digest(tmp_path: Path) -> None:
    def surrogate(document: dict[str, Any]) -> None:
        document["episode"]["plan"]["instructions"][0]["payload"] = "\ud800"

    receipt = parsed(tmp_path, plan=surrogate)

    assert placed(receipt) == [(None, NOTE_SLOT)]


@pytest.mark.parametrize("index", [None, 1])
def test_a_delivery_fact_naming_another_instruction_establishes_neither(
    tmp_path: Path, index: int | None
) -> None:
    delivery = _read(REPORTED / "delivery.json")
    delivery["delivery"][0]["plan_instruction_index"] = index

    receipt = parsed(tmp_path, delivery=delivery)

    assert receipt["execution_status"] == "completed"
    assert placed(receipt) == [(None, None)]


def test_a_delivery_document_without_the_outcome_s_fact_establishes_neither(
    tmp_path: Path,
) -> None:
    delivery = _read(REPORTED / "delivery.json")
    delivery["delivery"] = []

    receipt = parsed(tmp_path, delivery=delivery)

    assert placed(receipt) == [(None, None)]


def test_a_sealed_result_that_does_not_hold_together_establishes_neither(
    tmp_path: Path,
) -> None:
    verdict = _read(REPORTED / "verdict.json")
    verdict["evaluation_id"] = "another"

    receipt = parsed(tmp_path, verdict=verdict)

    assert receipt["incomplete_reason"] == "evaluation_mismatch"
    assert placed(receipt) == [(None, None)]


def test_an_unanswered_turn_keeps_the_placement(tmp_path: Path) -> None:
    snapshot = _read(REPORTED / "snapshot.json")
    snapshot["turns"][0]["reply"] = None

    receipt = parsed(tmp_path, snapshot=snapshot, delivery=None)

    assert receipt["incomplete_reason"] == "turn_failed:1"
    assert placed(receipt) == [(sha256(PLANTED), NOTE_SLOT)]


# --- the receipt schema -----------------------------------------------------------------


def test_every_outcome_parse_writes_has_both_fields(tmp_path: Path) -> None:
    receipt = parsed(tmp_path)

    assert all(UNPLACED.keys() <= outcome.keys() for outcome in outcomes(receipt))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload_sha256", "A" * 64),
        ("payload_sha256", "a" * 63),
        ("payload_sha256", 7),
        ("slot", {"id": "piggybank.note.body", "record": "NOTE-1"}),
        ("slot", {**NOTE_SLOT, "mode": "prepend"}),
        ("slot", {**NOTE_SLOT, "record": ""}),
        ("slot", {**NOTE_SLOT, "extra": 1}),
        ("slot", "piggybank.note.body"),
    ],
)
def test_the_receipt_schema_refuses_a_malformed_placement(
    tmp_path: Path, field: str, value: Any
) -> None:
    receipt = parsed(tmp_path)
    receipt["episode"]["call_injections"][0]["injection_outcomes"][0][field] = value

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, SCHEMA)


def test_a_receipt_without_the_fields_still_validates(tmp_path: Path) -> None:
    receipt = parsed(tmp_path)
    for outcome in outcomes(receipt):
        del outcome["payload_sha256"], outcome["slot"]

    jsonschema.validate(receipt, SCHEMA)


def test_the_contract_s_placement_case_is_parse_s_receipt_of_the_result_record_run(
    tmp_path: Path,
) -> None:
    work = tmp_path / "run"
    shutil.copytree(REFERRAL, work)

    receipt = parse_sealed(
        RESULT_RECORDS / "referral",
        work / "snapshot.json",
        work / "verdict.json",
        work / "receipt.json",
        tool_revision=RESULT_RECORD_REVISION,
    )

    assert receipt == _read(CONTRACT_CASE)
