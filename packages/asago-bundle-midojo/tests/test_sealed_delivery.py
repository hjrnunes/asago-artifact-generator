"""parse from MiDojo 13d5487: the attack outcome fields, delivery facts, and ``delivery.json``.

From 13d5487 MiDojo's grade also says ``attempted``, ``blocked``, ``effect_observed`` and
``injection_delivered``, each true, false or null (unknown), and each sealed outcome
carries its delivery facts: ``selected``, ``transformation`` and ``emitted``. A runner may
write ``delivery.json`` (``midojo-delivery-v1``) beside the snapshot: MiDojo's delivery
facts of the snapshot's calls and the exposure they derive. ``fixtures/regraded-13d5487/``
holds the fixture runs as MiDojo at 13d5487 reads, grades and reports them
(``tools/fixture-generators/regraded_13d5487.py``); ``fixtures/episode/`` the 3824922
verdicts, which lack the four fields.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import EPISODES, REGRADED
from midojo_sdk.snapshot import snapshot_digest

from asago_bundle_midojo.cli import main
from asago_bundle_midojo.sealed import ATTACK_OUTCOME, delivery_shape, parse_sealed
from conftest import DELIVERY_CONTRACT, RECEIPT_SCHEMA, as_sealed

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
DELIVERY_SCHEMA = json.loads(
    (DELIVERY_CONTRACT / "midojo-delivery-v1.schema.json").read_text(encoding="utf-8")
)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


RECORDING = _read(REGRADED / "recording.json")
REPORTED = REGRADED / "reported"
RUNS = [
    pytest.param(REGRADED, case, scenario, id=f"{case}/{scenario}")
    for case, scenarios in sorted(RECORDING["scenarios"].items())
    if case != "reported"
    for scenario in sorted(scenarios)
] + [pytest.param(REPORTED, "command-single", "detected", id="reported/command-single/detected")]


def recorded(root: Path, case: str, scenario: str, name: str) -> Any:
    return _read(root / case / scenario / name)


def parsed(
    tmp_path: Path,
    case: str,
    scenario: str,
    *,
    root: Path = REGRADED,
    delivery: Any = "recorded",
    **replacements: Any,
) -> dict[str, Any]:
    """Parse a recorded run copied into ``tmp_path/run``, its files replaced as given.

    ``delivery`` is the delivery document to write (``"recorded"``: the run's own), or None
    to parse without one.
    """

    run = tmp_path / "run"
    run.mkdir()
    for name in ("snapshot", "verdict"):
        source = root / case / scenario / f"{name}.json"
        if name in replacements:
            (run / f"{name}.json").write_text(json.dumps(replacements[name]), encoding="utf-8")
        else:
            shutil.copy(source, run / f"{name}.json")
    delivery_path = None
    if delivery is not None:
        delivery_path = run / "delivery.json"
        if delivery == "recorded":
            shutil.copy(root / case / scenario / "delivery.json", delivery_path)
        else:
            delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
    receipt = parse_sealed(
        EPISODES / case,
        run / "snapshot.json",
        run / "verdict.json",
        run / "receipt.json",
        delivery_path=delivery_path,
    )
    jsonschema.validate(receipt, SCHEMA)
    return receipt


# --- the attack outcome fields of MiDojo's grade --------------------------------------


@pytest.mark.parametrize(("root", "case", "scenario"), RUNS)
def test_a_run_graded_at_13d5487_carries_its_attack_outcome_into_the_receipt(
    tmp_path: Path, root: Path, case: str, scenario: str
) -> None:
    grade = recorded(root, case, scenario, "verdict.json")["grade"]
    recording = RECORDING["scenarios"]
    if root == REPORTED:
        recording = recording["reported"]

    receipt = parsed(tmp_path, case, scenario, root=root)

    verdict = receipt["episode"]["verdict"]
    assert {name: verdict[name] for name in ATTACK_OUTCOME} == {
        name: grade[name] for name in ATTACK_OUTCOME
    }
    assert {name: grade[name] for name in ATTACK_OUTCOME} == {
        name: recording[case][scenario][name] for name in ATTACK_OUTCOME
    }
    assert receipt["incomplete_reason"] in (None, "turn_failed:1")


def test_the_fixture_runs_hold_each_of_true_false_and_unknown() -> None:
    values = {
        name: {
            RECORDING["scenarios"][case][scenario][name]
            for case, scenarios in RECORDING["scenarios"].items()
            if case != "reported"
            for scenario in scenarios
        }
        for name in ("attempted", "blocked", "effect_observed")
    }

    assert values == {name: {True, False, None} for name in values}
    reported = RECORDING["scenarios"]["reported"]["command-single"]["detected"]
    assert reported["injection_delivered"] is True


@pytest.mark.parametrize("value", [True, False, None])
def test_each_attack_outcome_field_is_kept_as_midojo_gave_it(
    tmp_path: Path, value: bool | None
) -> None:
    verdict = recorded(REGRADED, "command-single", "detected", "verdict.json")
    verdict["grade"].update(dict.fromkeys(ATTACK_OUTCOME, value))

    receipt = parsed(tmp_path, "command-single", "detected", verdict=verdict)

    assert receipt["execution_status"] == "completed"
    verdict_block = receipt["episode"]["verdict"]
    assert [verdict_block[name] for name in ATTACK_OUTCOME] == [value] * 4
    # Unknown is never made a fail, and never changes the security outcome.
    assert receipt["attempts"][0]["result"]["outcome"] == "detected"


def test_a_verdict_sealed_before_the_fields_reads_them_as_unknown(tmp_path: Path) -> None:
    verdict = recorded(EPISODES, "command-single", "detected", "verdict.json")
    assert not set(ATTACK_OUTCOME) & verdict["grade"].keys()

    receipt = parsed(tmp_path, "command-single", "detected", root=EPISODES, delivery=None)

    assert receipt["execution_status"] == "completed"
    assert {name: receipt["episode"]["verdict"][name] for name in ATTACK_OUTCOME} == dict.fromkeys(
        ATTACK_OUTCOME
    )


def _grade_variant(name: str) -> dict[str, Any]:
    verdict = recorded(REGRADED, "command-single", "detected", "verdict.json")
    grade = verdict["grade"]
    if name == "short-name":
        grade["effect"] = grade.pop("effect_observed")
    elif name == "unknown-key":
        grade["delivery"] = True
    elif name == "one-missing":
        del grade["injection_delivered"]
    elif name == "text":
        grade["attempted"] = "yes"
    elif name == "number":
        grade["blocked"] = 0
    elif name == "list":
        grade["effect_observed"] = [True]
    return verdict


@pytest.mark.parametrize(
    "name", ["short-name", "unknown-key", "one-missing", "text", "number", "list"]
)
def test_a_grade_with_an_unknown_key_or_a_partial_or_mistyped_outcome_is_malformed(
    tmp_path: Path, name: str
) -> None:
    receipt = parsed(tmp_path, "command-single", "detected", verdict=_grade_variant(name))

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "verdict_malformed",
    )
    assert receipt["episode"]["verdict"] is None


@pytest.mark.parametrize("value", ["yes", 1, [True], {"value": True}])
def test_the_receipt_schema_holds_each_attack_outcome_field_to_true_false_or_null(
    tmp_path: Path, value: Any
) -> None:
    receipt = parsed(tmp_path, "command-single", "detected")
    for name in ATTACK_OUTCOME:
        changed = copy.deepcopy(receipt)
        changed["episode"]["verdict"][name] = value

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(changed, SCHEMA)


# --- delivery facts in each sealed outcome ---------------------------------------------


@pytest.mark.parametrize(("root", "case", "scenario"), RUNS)
def test_the_port_reproduces_the_snapshot_id_midojo_checked_at_13d5487(
    root: Path, case: str, scenario: str
) -> None:
    snapshot = recorded(root, case, scenario, "snapshot.json")

    assert snapshot_digest(snapshot) == snapshot["snapshot_id"]


def _outcome_variant(name: str) -> dict[str, Any]:
    """The reported run's snapshot, its first outcome changed, renamed by its digest."""

    snapshot = recorded(REPORTED, "command-single", "detected", "snapshot.json")
    outcome = snapshot["function_calls"][0]["injection_outcomes"][0]
    if name == "facts-unreported":
        outcome.update(selected=None, transformation=None, emitted=None)
    elif name == "facts-not-emitted":
        outcome.update(selected=True, emitted=[])
    elif name == "json-string":
        outcome["transformation"] = "json_string"
    elif name == "unmatched-uncounted":
        outcome.update(
            status="unmatched", occurrence=0, selected=False, transformation=None, emitted=[]
        )
    elif name == "unmatched-uncounted-unreported":
        outcome.update(
            status="unmatched", occurrence=0, selected=None, transformation=None, emitted=None
        )
    elif name == "facts-missing-one":
        del outcome["emitted"]
    elif name == "selected-text":
        outcome["selected"] = "true"
    elif name == "transformation-unknown":
        outcome["transformation"] = "base64"
    elif name == "emitted-unknown-part":
        outcome["emitted"] = ["description"]
    elif name == "emitted-text":
        outcome["emitted"] = "structured_content"
    elif name == "uncounted-selected":
        outcome.update(status="unmatched", occurrence=0, transformation=None, emitted=[])
    elif name == "uncounted-transformed":
        outcome.update(status="unmatched", occurrence=0, selected=False, emitted=[])
    elif name == "uncounted-emitted":
        outcome.update(status="unmatched", occurrence=0, selected=False, transformation=None)
    snapshot["snapshot_id"] = snapshot_digest(snapshot)
    return snapshot


@pytest.mark.parametrize(
    "name",
    [
        "facts-unreported",
        "facts-not-emitted",
        "json-string",
        "unmatched-uncounted",
        "unmatched-uncounted-unreported",
    ],
)
def test_an_outcome_with_delivery_facts_of_midojo_s_types_is_kept(
    tmp_path: Path, name: str
) -> None:
    snapshot = _outcome_variant(name)
    verdict = recorded(REPORTED, "command-single", "detected", "verdict.json")
    verdict["snapshot_id"] = snapshot["snapshot_id"]

    receipt = parsed(
        tmp_path,
        "command-single",
        "detected",
        root=REPORTED,
        snapshot=snapshot,
        verdict=verdict,
        delivery=None,
    )

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == ("completed", None)
    sealed = snapshot["function_calls"][0]["injection_outcomes"]
    assert as_sealed(receipt["episode"]["call_injections"][0]["injection_outcomes"]) == sealed


@pytest.mark.parametrize(
    "name",
    [
        "facts-missing-one",
        "selected-text",
        "transformation-unknown",
        "emitted-unknown-part",
        "emitted-text",
        "uncounted-selected",
        "uncounted-transformed",
        "uncounted-emitted",
    ],
)
def test_an_outcome_whose_delivery_facts_are_not_midojo_s_is_malformed(
    tmp_path: Path, name: str
) -> None:
    receipt = parsed(
        tmp_path,
        "command-single",
        "detected",
        root=REPORTED,
        snapshot=_outcome_variant(name),
        delivery=None,
    )

    assert receipt["incomplete_reason"] == "snapshot_malformed"


# --- delivery.json: MiDojo's delivery facts and exposure, beside the snapshot ----------


@pytest.mark.parametrize(("root", "case", "scenario"), RUNS)
def test_a_delivery_document_is_carried_into_the_receipt_and_listed_as_native(
    tmp_path: Path, root: Path, case: str, scenario: str
) -> None:
    delivery = recorded(root, case, scenario, "delivery.json")

    receipt = parsed(tmp_path, case, scenario, root=root)

    assert receipt["episode"]["delivery"] == delivery["delivery"]
    assert receipt["episode"]["exposure"] == delivery["exposure"]
    assert [item["path"] for item in receipt["native"]] == [
        "snapshot.json",
        "verdict.json",
        "delivery.json",
    ]


def test_the_reported_run_shows_the_payload_emitted_and_nothing_past_the_interceptor(
    tmp_path: Path,
) -> None:
    receipt = parsed(tmp_path, "command-single", "detected", root=REPORTED)

    exposure = receipt["episode"]["exposure"]
    assert (exposure["emitted"], exposure["agent_tool_result"], exposure["model_input"]) == (
        "exposed",
        "unknown",
        "unknown",
    )
    (fact,) = receipt["episode"]["delivery"]
    assert (fact["selected"], fact["transformation"], fact["emitted"]) == (
        True,
        "identity",
        ["structured_content", "content_block"],
    )
    assert receipt["episode"]["verdict"]["injection_delivered"] is True


def test_without_a_delivery_document_delivery_and_exposure_are_unknown(tmp_path: Path) -> None:
    receipt = parsed(tmp_path, "command-single", "detected", delivery=None)

    assert receipt["execution_status"] == "completed"
    assert (receipt["episode"]["delivery"], receipt["episode"]["exposure"]) == (None, None)
    assert [item["path"] for item in receipt["native"]] == ["snapshot.json", "verdict.json"]


def test_a_delivery_document_named_but_absent_fails_the_receipt(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(REGRADED / "command-single" / "detected", run)
    (run / "delivery.json").unlink()

    receipt = parse_sealed(
        EPISODES / "command-single",
        run / "snapshot.json",
        run / "verdict.json",
        run / "receipt.json",
        delivery_path=run / "delivery.json",
    )

    jsonschema.validate(receipt, SCHEMA)
    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "delivery_missing",
    )
    assert receipt["attempts"] == []


def _delivery_variant(name: str) -> Any:
    document = recorded(REPORTED, "command-single", "detected", "delivery.json")
    fact, exposure = document["delivery"][0], document["exposure"]
    instruction = exposure["instructions"][0]
    if name == "not-an-object":
        return [document]
    if name == "extra-field":
        document["results"] = {}
    elif name == "missing-exposure":
        del document["exposure"]
    elif name == "other-version":
        document["schema_version"] = "midojo-delivery-v2"
    elif name == "snapshot-id-null":
        document["snapshot_id"] = None
    elif name == "facts-an-object":
        document["delivery"] = fact
    elif name == "fact-extra-field":
        fact["payload"] = "x"
    elif name == "fact-missing-plan-index":
        del fact["plan_instruction_index"]
    elif name == "fact-index-a-bool":
        fact["interceptor_instruction_index"] = False
    elif name == "fact-unknown-status":
        fact["status"] = "delivered"
    elif name == "fact-unknown-disposition":
        fact["disposition"] = "teleported"
    elif name == "fact-transformation-unknown":
        fact["transformation"] = "base64"
    elif name == "fact-emitted-unknown-part":
        fact["emitted"] = ["template"]
    elif name == "fact-uncounted-emitted":
        fact.update(status="unmatched", occurrence=0)
    elif name == "exposure-unknown-status":
        exposure["emitted"] = "delivered"
    elif name == "exposure-extra-boundary":
        exposure["agent_output"] = "unknown"
    elif name == "instruction-unknown-boundary":
        instruction["exposure"]["agent_output"] = "unknown"
    elif name == "instruction-unknown-placer":
        instruction["placed_by"] = "agent"
    elif name == "instruction-without-reason":
        del instruction["reason"]
    return document


DELIVERY_VARIANTS = [
    "not-an-object",
    "extra-field",
    "missing-exposure",
    "other-version",
    "snapshot-id-null",
    "facts-an-object",
    "fact-extra-field",
    "fact-missing-plan-index",
    "fact-index-a-bool",
    "fact-unknown-status",
    "fact-unknown-disposition",
    "fact-transformation-unknown",
    "fact-emitted-unknown-part",
    "fact-uncounted-emitted",
    "exposure-unknown-status",
    "exposure-extra-boundary",
    "instruction-unknown-boundary",
    "instruction-unknown-placer",
    "instruction-without-reason",
]


@pytest.mark.parametrize("name", DELIVERY_VARIANTS)
def test_a_delivery_document_that_is_not_midojo_s_fails_the_receipt(
    tmp_path: Path, name: str
) -> None:
    receipt = parsed(
        tmp_path, "command-single", "detected", root=REPORTED, delivery=_delivery_variant(name)
    )

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "delivery_malformed",
    )
    assert receipt["attempts"] == []
    assert (receipt["episode"]["delivery"], receipt["episode"]["exposure"]) == (None, None)


@pytest.mark.parametrize("name", DELIVERY_VARIANTS)
def test_the_delivery_schema_refuses_what_parse_refuses(name: str) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_delivery_variant(name), DELIVERY_SCHEMA)


@pytest.mark.parametrize(("root", "case", "scenario"), RUNS)
def test_every_recorded_delivery_document_meets_the_schema_and_the_parser(
    root: Path, case: str, scenario: str
) -> None:
    document = recorded(root, case, scenario, "delivery.json")

    jsonschema.validate(document, DELIVERY_SCHEMA)
    assert delivery_shape(document)


@pytest.mark.parametrize("field", ["evaluation_id", "snapshot_id"])
def test_a_delivery_document_of_another_snapshot_fails_the_receipt(
    tmp_path: Path, field: str
) -> None:
    other = recorded(REGRADED, "command-single", "not-detected", "delivery.json")
    document = recorded(REGRADED, "command-single", "detected", "delivery.json")
    document[field] = other[field]

    receipt = parsed(tmp_path, "command-single", "detected", delivery=document)

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "delivery_mismatch",
    )
    assert receipt["attempts"] == []
    # What the document says is kept as evidence, as a mismatched verdict is.
    assert receipt["episode"]["delivery"] == document["delivery"]


@pytest.mark.parametrize("field", ["evaluation_id", "snapshot_id"])
def test_a_delivery_document_of_another_snapshot_leaves_every_placement_unknown(
    tmp_path: Path, field: str
) -> None:
    document = recorded(REPORTED, "command-single", "detected", "delivery.json")
    for name in ("matched", "mismatched"):
        (tmp_path / name).mkdir()
    matched = parsed(tmp_path / "matched", "command-single", "detected", root=REPORTED)
    (outcome,) = matched["episode"]["call_injections"][0]["injection_outcomes"]
    # With its own delivery document the outcome emitted, and both fields are established.
    assert outcome["emitted"] and outcome["payload_sha256"] and outcome["slot"]
    document[field] = "sha256:" + "0" * 64 if field == "snapshot_id" else "another"

    receipt = parsed(
        tmp_path / "mismatched", "command-single", "detected", root=REPORTED, delivery=document
    )

    assert receipt["incomplete_reason"] == "delivery_mismatch"
    (outcome,) = receipt["episode"]["call_injections"][0]["injection_outcomes"]
    assert outcome["emitted"]
    assert (outcome["payload_sha256"], outcome["slot"]) == (None, None)


def test_a_delivery_mismatch_outranks_an_unanswered_turn(tmp_path: Path) -> None:
    document = recorded(REGRADED, "command-single", "interrupted", "delivery.json")
    document["snapshot_id"] = "sha256:" + "0" * 64

    receipt = parsed(tmp_path, "command-single", "interrupted", delivery=document)

    assert receipt["incomplete_reason"] == "delivery_mismatch"


def test_parse_reads_the_delivery_document_it_is_given(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(REPORTED / "command-single" / "detected", run)
    args = [
        "parse",
        str(EPISODES / "command-single"),
        "--snapshot",
        str(run / "snapshot.json"),
        "--verdict",
        str(run / "verdict.json"),
    ]

    delivered = [*args, "--delivery", str(run / "delivery.json"), "--out", str(run / "r.json")]
    assert main(delivered) == 0
    assert main([*args, "--out", str(run / "bare.json")]) == 0

    with_delivery = _read(run / "r.json")
    bare = _read(run / "bare.json")
    assert with_delivery["episode"]["exposure"]["emitted"] == "exposed"
    assert (bare["episode"]["delivery"], bare["episode"]["exposure"]) == (None, None)


@pytest.mark.parametrize(
    ("case", "accepted"),
    [
        *[
            (path, True)
            for path in sorted((DELIVERY_CONTRACT / "midojo-delivery-v1/valid").glob("*.json"))
        ],
        *[
            (path, False)
            for path in sorted((DELIVERY_CONTRACT / "midojo-delivery-v1/invalid").glob("*.json"))
        ],
    ],
    ids=lambda value: value.stem if isinstance(value, Path) else str(value),
)
def test_parse_accepts_exactly_the_contract_s_valid_cases(case: Path, accepted: bool) -> None:
    assert delivery_shape(_read(case)) is accepted


# --- a named file whose bytes are not UTF-8 is malformed, not a crash ------------------


def _undecodable_run(tmp_path: Path, name: str) -> Path:
    """The reported run copied into ``tmp_path/run``, ``name`` replaced by a byte that is not
    UTF-8."""

    run = tmp_path / "run"
    shutil.copytree(REPORTED / "command-single" / "detected", run)
    (run / name).write_bytes(b"\xff")
    return run


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("delivery.json", "delivery_malformed"),
        ("snapshot.json", "snapshot_malformed"),
        ("verdict.json", "verdict_malformed"),
    ],
)
def test_a_file_that_is_not_utf_8_fails_the_receipt_as_malformed(
    tmp_path: Path, name: str, reason: str
) -> None:
    run = _undecodable_run(tmp_path, name)

    receipt = parse_sealed(
        EPISODES / "command-single",
        run / "snapshot.json",
        run / "verdict.json",
        run / "receipt.json",
        delivery_path=run / "delivery.json",
    )

    jsonschema.validate(receipt, SCHEMA)
    assert (receipt["execution_status"], receipt["incomplete_reason"]) == ("failed", reason)
    assert receipt["attempts"] == []


def test_parse_writes_a_failed_receipt_for_a_delivery_document_that_is_not_utf_8(
    tmp_path: Path,
) -> None:
    run = _undecodable_run(tmp_path, "delivery.json")

    code = main(
        [
            "parse",
            str(EPISODES / "command-single"),
            "--snapshot",
            str(run / "snapshot.json"),
            "--verdict",
            str(run / "verdict.json"),
            "--delivery",
            str(run / "delivery.json"),
            "--out",
            str(run / "receipt.json"),
        ]
    )

    assert code == 0
    receipt = _read(run / "receipt.json")
    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "delivery_malformed",
    )
