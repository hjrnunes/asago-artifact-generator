"""A single-record carrier whose record ID is in its result only: MiDojo's ``result_record``.

The clinic fixture target (``fixtures/targets/clinic/``) declares ``get_referral`` and
``get_education``: each takes a patient's ID and returns one record whose own ID is a
property of the result. ``fixtures/result-record/<case>/`` holds what compile wrote for
each case of ``result_record_cases.CLINIC_CASES``, and each scenario under it the snapshot
and verdict MiDojo sealed and graded from that episode at c0bee12, its host interceptor in
front of a scripted upstream of the clinic's shape (``tools/fixture-generators/
result_record.py``,
which also loads the plan with MiDojo's ``InjectionPlan.from_data`` and checks MiDojo's
host interceptor declares ``result_record``). The other carrier shapes below copy the
declarations of real single-record and list carriers, cut to what placement reads.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import FIXTURES
from result_record_cases import (
    CLINIC_CASES,
    CLINIC_PLANTED,
    CLINIC_TARGET,
    EDUCATION_SLOT,
    REFERRAL_SLOT,
    RESULT_RECORDS,
    compile_clinic_case,
    write_clinic_package,
)

from asago_bundle_core.conformance import check_gap
from asago_bundle_core.gap import CapabilityGap
from asago_bundle_midojo.cli import main
from asago_bundle_midojo.declared import DeclaredTools
from asago_bundle_midojo.episode_compile import TOOL_REVISION_RANGE, compile_episode
from asago_bundle_midojo.placement import Placement, gap_reason, locate
from asago_bundle_midojo.sealed import parse_sealed
from conftest import RECEIPT_SCHEMA

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
RECORDING = json.loads((RESULT_RECORDS / "recording.json").read_text(encoding="utf-8"))
RUNS = [
    pytest.param(case, scenario, id=f"{case}/{scenario}")
    for case, scenarios in sorted(RECORDING["scenarios"].items())
    for scenario in sorted(scenarios)
]
TEXT = {"type": "string"}


def written(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in ("snapshot.json", "verdict.json")
    }


# --- compile: the clinic's referral and education slots ---------------------------------


@pytest.mark.parametrize("case", sorted(CLINIC_CASES))
def test_compile_writes_the_episode_midojo_ran(tmp_path: Path, case: str) -> None:
    compile_clinic_case(tmp_path / "packages", case, tmp_path / "out")

    assert written(tmp_path / "out") == written(RESULT_RECORDS / case)


@pytest.mark.parametrize(
    ("case", "slot", "record"),
    [
        ("referral", REFERRAL_SLOT, "REF-101"),
        ("education", EDUCATION_SLOT, "EDU-CATARACT"),
    ],
)
def test_a_record_named_in_the_result_only_is_matched_by_the_result(
    tmp_path: Path, case: str, slot: dict[str, Any], record: str
) -> None:
    document = compile_clinic_case(tmp_path / "packages", case, tmp_path / "out")

    (instruction,) = document["episode"]["plan"]["instructions"]
    assert instruction["target"] == {
        "tool": slot["carrier_operation"],
        "surface": "structured_content",
        "content_block_index": None,
        "content_block_type": None,
        "structured_content_path": [slot["field"]],
        "template_path": [],
        "template_key": None,
        "result_record": {"field": slot["record_key"], "equals": record},
    }
    assert instruction["payload"] == CLINIC_PLANTED
    assert instruction["occurrence"] == {"kind": "every", "nth": None}
    assert document["tool_revision_range"] == TOOL_REVISION_RANGE == ">=8e8208f"
    assert document["placements"] == [
        {
            "instruction": 0,
            "slot": slot["id"],
            "record": record,
            "carrier_operation": slot["carrier_operation"],
            "field": slot["field"],
            "record_key": slot["record_key"],
        }
    ]


def test_every_compiled_fixture_names_one_range() -> None:
    """Every fixture compiled now names the one range compile writes, whatever it targets.

    ``fixtures/episode-a5f81e9/`` is not swept: it keeps what compile wrote at a5f81e9 beside
    the snapshots MiDojo sealed then, so it is read as the v0 envelope and never compared with
    a compile.
    """

    assert TOOL_REVISION_RANGE == ">=8e8208f"
    roots = [FIXTURES / "episode", FIXTURES / "placed-a19562e", FIXTURES / "result-record"]
    for root in roots:
        for episode in sorted(root.glob("*/episode.json")):
            document = json.loads(episode.read_text(encoding="utf-8"))
            assert document["tool_revision_range"] == TOOL_REVISION_RANGE


def _clinic_variant(tmp_path: Path, output: dict[str, Any]) -> Path:
    """The clinic target with ``get_referral`` returning ``output``."""

    root = tmp_path / "variant"
    root.mkdir()
    for name in ("target.yaml", "runtime-spec.yaml"):
        (root / name).write_bytes((CLINIC_TARGET.parent / name).read_bytes())
    listed = json.loads((CLINIC_TARGET.parent / "tools-list.json").read_text(encoding="utf-8"))
    for tool in listed["tools"]:
        if tool["name"] == "get_referral":
            tool["outputSchema"] = output
    (root / "tools-list.json").write_text(json.dumps(listed), encoding="utf-8")
    return root / "target.yaml"


def test_a_record_named_by_neither_the_call_nor_the_result_is_a_capability_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _clinic_variant(
        tmp_path, {"type": "object", "properties": {"patient_id": TEXT, "reason": TEXT}}
    )
    package = write_clinic_package(tmp_path / "packages", "referral")
    reason = (
        "seed item 0: 'get_referral' declares no argument 'referral_id' (slot "
        "clinic.referral.reason's record_key), so no call names the record whose 'reason' it "
        "returns; 'get_referral''s result object declares no property 'referral_id' (slot "
        "clinic.referral.reason's record_key) either, by which MiDojo would match the result"
    )

    with pytest.raises(CapabilityGap) as raised:
        compile_episode(package, tmp_path / "out", target=target)

    assert raised.value.record["reason"] == reason
    assert check_gap(raised.value.record, package) == []
    out = tmp_path / "cli-out"
    assert main(["compile", str(package), "--out", str(out), "--target", str(target)]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == reason
    assert not (tmp_path / "out").exists() and not out.exists()


def test_a_result_property_of_another_type_than_the_record_s_is_a_capability_gap(
    tmp_path: Path,
) -> None:
    """MiDojo compares type for type: the text record is not coerced to match a number."""

    target = _clinic_variant(
        tmp_path,
        {"type": "object", "properties": {"referral_id": {"type": "integer"}, "reason": TEXT}},
    )
    package = write_clinic_package(tmp_path / "packages", "referral")

    with pytest.raises(CapabilityGap) as raised:
        compile_episode(package, tmp_path / "out", target=target)

    assert raised.value.record["reason"].endswith(
        "'get_referral''s result declares 'referral_id' (slot clinic.referral.reason's "
        "record_key) as 'integer', not text"
    )


# --- the shapes of real carriers -----------------------------------------------------------


def _lookup(argument: str) -> dict[str, Any]:
    return {"type": "object", "properties": {argument: TEXT}, "required": [argument]}


def _record(*names: str) -> dict[str, Any]:
    return {"type": "object", "properties": {name: TEXT for name in names}}


def _documents(*names: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"status": TEXT, "documents": {"type": "array", "items": _record(*names)}},
    }


# Name -> (carrier, its input and output schemas, the slot's field and record_key, a record).
CARRIERS: dict[str, tuple[str, dict[str, Any], dict[str, Any], str, str, str]] = {
    # A referral and an education record read by the patient's ID.
    "referral": (
        "get_referral",
        _lookup("patient_id"),
        _record("referral_id", "patient_id", "specialty", "reason", "status"),
        "reason",
        "referral_id",
        "REF-101",
    ),
    "education": (
        "get_education",
        _lookup("patient_id"),
        _record("patient_id", "template_id", "specialty", "title", "script"),
        "script",
        "template_id",
        "EDU-CATARACT",
    ),
    # An order and a listing read by their own ID.
    "order": (
        "lookup_order",
        _lookup("order_id"),
        _record("order_id", "customer_id", "merchant", "item", "status"),
        "item",
        "order_id",
        "ORD-101",
    ),
    "listing": (
        "get_listing",
        _lookup("listing_id"),
        _record("listing_id", "host_id", "title", "city", "status"),
        "title",
        "listing_id",
        "LST-101",
    ),
    # Policy documents found by a query: a list of records.
    "policy": (
        "retrieve_policy",
        _lookup("query"),
        _documents("doc_id", "title", "body"),
        "documents[].body",
        "doc_id",
        "POL-REFUND",
    ),
}


def _placed(name: str) -> Placement:
    carrier, inputs, output, field, key, record = CARRIERS[name]
    slot = {
        "id": f"slot.{name}",
        "carrier_operation": carrier,
        "field": field,
        "records": [record],
        "max_chars": 2000,
        "mode": "append",
        "record_key": key,
    }
    tool = {"name": carrier, "inputSchema": inputs, "outputSchema": output}
    declared = DeclaredTools({carrier: tool}, frozenset())
    placement = locate(Placement(0, slot, record, "text"), declared)
    assert gap_reason(placement, declared) is None
    return placement


@pytest.mark.parametrize(
    ("name", "key", "record"),
    [
        ("referral", "referral_id", "REF-101"),
        ("education", "template_id", "EDU-CATARACT"),
    ],
)
def test_a_carrier_taking_another_id_is_matched_by_its_result(
    name: str, key: str, record: str
) -> None:
    target = _placed(name).instruction()["target"]

    assert target["result_record"] == {"field": key, "equals": record}
    assert "call_arguments" not in target


@pytest.mark.parametrize(
    ("name", "key", "record"),
    [
        ("order", "order_id", "ORD-101"),
        ("listing", "listing_id", "LST-101"),
    ],
)
def test_a_carrier_taking_the_record_s_id_is_matched_by_its_call(
    name: str, key: str, record: str
) -> None:
    """Its result names the record too, but the call's argument comes first."""

    target = _placed(name).instruction()["target"]

    assert target["call_arguments"] == {key: record}
    assert "result_record" not in target


def test_a_list_carrier_is_matched_by_its_record_selector() -> None:
    target = _placed("policy").instruction()["target"]

    assert target["structured_content_path"] == [
        "documents",
        {"field": "doc_id", "equals": "POL-REFUND"},
        "body",
    ]
    assert "call_arguments" not in target and "result_record" not in target


# --- parse: what MiDojo sealed for a result_record target --------------------------------


def _parsed(tmp_path: Path, case: str, scenario: str) -> dict[str, Any]:
    run = tmp_path / "run"
    run.mkdir()
    for name in ("snapshot.json", "verdict.json"):
        shutil.copy(RESULT_RECORDS / case / scenario / name, run / name)
    out = run / "receipt.json"
    receipt = parse_sealed(
        RESULT_RECORDS / case,
        run / "snapshot.json",
        run / "verdict.json",
        out,
        tool_revision="c0bee126",
        adapter_revision="abc123",
    )
    assert receipt == json.loads(out.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, SCHEMA)
    return receipt


@pytest.mark.parametrize(("case", "scenario"), RUNS)
def test_a_sealed_result_record_run_gives_a_completed_receipt_with_its_unmatched_call(
    tmp_path: Path, case: str, scenario: str
) -> None:
    run = RECORDING["scenarios"][case][scenario]

    receipt = _parsed(tmp_path, case, scenario)

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == ("completed", None)
    (attempt,) = receipt["attempts"]
    outcome = {True: "detected", False: "not_detected"}[run["security"]]
    assert attempt["result"]["outcome"] == outcome
    sealed = [
        item
        for call in receipt["episode"]["call_injections"]
        for item in call["injection_outcomes"] or []
    ]
    assert [(item["status"], item["occurrence"]) for item in sealed] == [
        (item["status"], item["occurrence"]) for item in run["injection_outcomes"]
    ]
    (missed,) = [item for item in sealed if item["status"] == "unmatched"]
    assert missed["occurrence"] == 0
    assert (missed["selected"], missed["transformation"], missed["emitted"]) == (False, None, [])
    assert missed["detail"].startswith("result field ")


def test_midojo_placed_the_payload_only_in_the_result_of_the_planted_record() -> None:
    snapshot = json.loads(
        (RESULT_RECORDS / "referral" / "detected" / "snapshot.json").read_text(encoding="utf-8")
    )

    reasons = {
        call["structured_content"]["referral_id"]: call["structured_content"]["reason"]
        for call in snapshot["function_calls"]
        if call["function"] == "get_referral"
    }
    assert reasons["REF-101"].endswith(CLINIC_PLANTED)
    assert CLINIC_PLANTED not in reasons["REF-104"]


def test_the_fixture_names_the_revision_midojo_sealed_it_at() -> None:
    assert RECORDING["midojo_revision"] == "c0bee1266f51a18a9383fb8edd4cbb7b55217d9a"
