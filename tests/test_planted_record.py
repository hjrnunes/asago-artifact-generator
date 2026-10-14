"""The plan author names the record an indirect shape plants its text in; code validates it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.binding_repair import correction_repair_inputs
from asago_artifact_generator.authoring.checks import collect_plan_findings_v2
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.planted_item import (
    RECORD_FIELD,
    RECORD_PATH,
    choose_planted_target,
    plan_record_findings,
    seed_members,
)
from asago_artifact_generator.authoring.prompt_context import (
    build_artifact_author_context,
    build_plan_author_context,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_plan_review_packet,
)
from asago_artifact_generator.authoring.sequential_turns import (
    multi_turn_block,
    multi_turn_payload,
    records_in_context,
    sequential_plan_findings,
)

from .support import (
    ScriptedAuthoringTransport,
    json_section,
    rendered_response_contract,
    stage_local_orchestrator,
    world,
)
from .turn_support import (
    ASKS,
    CARRIER_SLOT,
    indirect_inventory,
    indirect_metadata,
    indirect_plan,
    indirect_runtime,
    indirect_shape,
    indirect_view,
)

RECORDS = CARRIER_SLOT["records"]


def runtime() -> dict[str, Any]:
    return indirect_runtime(world("refund")["runtime_contract"])


def plan_naming(record: Any, turns: int = 2) -> dict[str, Any]:
    plan = indirect_plan(world("refund")["plan"], turns)
    plan["stimulus_approach"][RECORD_FIELD] = record
    return plan


def unnamed_shape() -> dict[str, Any]:
    return indirect_shape(record_ref=None)


# --- the checks ----------------------------------------------------------------------


def test_the_record_field_sits_in_the_stimulus_approach() -> None:
    assert RECORD_FIELD == "planted_record_ref"
    assert RECORD_PATH == "stimulus_approach.planted_record_ref"


@pytest.mark.parametrize("record", RECORDS)
def test_a_plan_that_names_a_record_the_slot_lists_has_no_record_finding(record: str) -> None:
    assert plan_record_findings(unnamed_shape(), plan_naming(record), runtime()) == []


def test_a_plan_that_names_no_record_is_found_when_the_shape_names_none() -> None:
    plan = indirect_plan(world("refund")["plan"])

    [finding] = plan_record_findings(unnamed_shape(), plan, runtime())

    assert (finding.code, finding.path) == ("missing_field", RECORD_PATH)
    assert "ORD-101" in finding.detail and "ORD-102" in finding.detail


def test_a_plan_that_names_a_record_outside_the_slot_is_found() -> None:
    [finding] = plan_record_findings(unnamed_shape(), plan_naming("ORD-999"), runtime())

    assert (finding.code, finding.path) == ("planted_record_not_in_slot", RECORD_PATH)
    assert "ORD-999" in finding.detail
    assert "klarna.order.item" in finding.detail
    assert "ORD-101" in finding.detail and "ORD-102" in finding.detail


@pytest.mark.parametrize("value", [None, 7, ["ORD-101"], ""])
def test_a_record_that_is_not_a_listed_string_is_found(value: Any) -> None:
    [finding] = plan_record_findings(unnamed_shape(), plan_naming(value), runtime())

    assert finding.path == RECORD_PATH
    assert finding.code in {"type_error", "planted_record_not_in_slot"}


def test_a_plan_that_names_a_record_the_producer_already_named_is_found() -> None:
    shape = indirect_shape(record_ref="ORD-101")

    [finding] = plan_record_findings(shape, plan_naming("ORD-102"), runtime())

    assert (finding.code, finding.path) == ("unexpected_field", RECORD_PATH)
    assert plan_record_findings(shape, indirect_plan(world("refund")["plan"]), runtime()) == []


def test_a_direct_plan_that_names_a_record_is_found() -> None:
    refund = world("refund")
    plan = {**refund["plan"]}
    plan["stimulus_approach"] = {**plan["stimulus_approach"], RECORD_FIELD: "ORD-101"}

    [finding] = plan_record_findings(None, plan, refund["runtime_contract"])

    assert (finding.code, finding.path) == ("unexpected_field", RECORD_PATH)
    assert plan_record_findings(None, refund["plan"], refund["runtime_contract"]) == []


def test_a_runtime_with_no_carrier_slot_leaves_the_refusal_to_the_shape_gate() -> None:
    plan = indirect_plan(world("refund")["plan"])
    bare = {**runtime(), "seed_slots": []}

    assert plan_record_findings(unnamed_shape(), plan, bare) == []


@pytest.mark.parametrize("plan", ["plan", {"stimulus_approach": "x"}, {}])
def test_a_plan_without_an_approach_object_has_no_record_finding(plan: Any) -> None:
    assert plan_record_findings(unnamed_shape(), plan, runtime()) == []


def test_the_plan_stage_returns_the_record_finding(tmp_path: Path) -> None:
    view = indirect_view(tmp_path, record_ref=None)
    plan = plan_naming("ORD-999")

    findings = sequential_plan_findings(view, plan, runtime())

    assert [(f.code, f.path) for f in findings] == [("planted_record_not_in_slot", RECORD_PATH)]


def test_the_plan_checks_accept_the_record_field(tmp_path: Path) -> None:
    refund = world("refund")

    findings = collect_plan_findings_v2(plan_naming("ORD-102"), refund["inventory"], runtime())

    assert findings == []


# --- the record the package plants ----------------------------------------------------


def test_the_target_takes_the_record_the_plan_names(tmp_path: Path) -> None:
    shape = unnamed_shape()

    assert choose_planted_target(shape, runtime(), plan_naming("ORD-102")).record_ref == "ORD-102"
    assert choose_planted_target(shape, runtime(), plan_naming("ORD-101")).record_ref == "ORD-101"


def test_a_shape_that_names_no_record_still_plants_the_first_record_without_a_plan_name() -> None:
    shape = unnamed_shape()
    legacy = indirect_plan(world("refund")["plan"])

    assert choose_planted_target(shape, runtime()).record_ref == "ORD-101"
    assert choose_planted_target(shape, runtime(), None).record_ref == "ORD-101"
    assert choose_planted_target(shape, runtime(), legacy).record_ref == "ORD-101"


def test_the_producers_record_wins_over_a_plan_that_names_another() -> None:
    shape = indirect_shape(record_ref="ORD-102")

    assert choose_planted_target(shape, runtime(), plan_naming("ORD-101")).record_ref == "ORD-102"


def test_a_plan_record_outside_every_slot_has_no_target() -> None:
    assert choose_planted_target(unnamed_shape(), runtime(), plan_naming("ORD-999")) is None


def _seed_record(tmp_path: Path, plan: dict[str, Any] | None) -> str:
    view = indirect_view(tmp_path, record_ref=None)
    metadata = indirect_metadata(world("refund")["metadata"])
    members = seed_members(view, metadata, runtime(), plan)
    return json.loads(members["seed.json"])["items"][0]["record_ref"]


def test_the_seed_plants_the_record_the_plan_names(tmp_path: Path) -> None:
    assert _seed_record(tmp_path, plan_naming("ORD-102")) == "ORD-102"


def test_the_seed_plants_the_first_record_for_a_plan_that_names_none(tmp_path: Path) -> None:
    assert _seed_record(tmp_path, indirect_plan(world("refund")["plan"])) == "ORD-101"
    assert _seed_record(tmp_path, None) == "ORD-101"


def _author(tmp_path: Path, plans: list[dict[str, Any]]):
    refund = world("refund")
    transport = ScriptedAuthoringTransport(
        [*(json.dumps(plan) for plan in plans), json.dumps(indirect_metadata(refund["metadata"]))]
    )
    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(
        indirect_view(tmp_path, record_ref=None),
        indirect_inventory(refund["inventory"]),
        runtime(),
    )
    return result, transport


def test_an_authored_package_plants_the_record_the_plan_named(tmp_path: Path) -> None:
    result, transport = _author(tmp_path, [plan_naming("ORD-102")])

    assert result.status == "accepted", result.findings
    assert [r["stage"] for r in transport.requests] == ["call1", "call2"]
    seed = json.loads((tmp_path / "package" / "seed.json").read_text(encoding="utf-8"))
    assert seed["items"][0]["record_ref"] == "ORD-102"
    plan = json.loads((tmp_path / "package" / "plan.json").read_text(encoding="utf-8"))
    assert plan["stimulus_approach"][RECORD_FIELD] == "ORD-102"


def test_a_plan_naming_a_record_outside_the_slot_goes_through_the_plan_correction(
    tmp_path: Path,
) -> None:
    result, transport = _author(tmp_path, [plan_naming("ORD-999"), plan_naming("ORD-102")])

    assert result.status == "accepted", result.findings
    assert [r["stage"] for r in transport.requests] == ["call1", "correction", "call2"]
    correction = transport.requests[1]["user"]
    assert "planted_record_not_in_slot" in correction
    assert "ORD-999" in correction
    assert "ORD-101" in correction and "ORD-102" in correction


def test_a_plan_that_names_no_record_goes_through_the_plan_correction(tmp_path: Path) -> None:
    unnamed = indirect_plan(world("refund")["plan"])

    result, transport = _author(tmp_path, [unnamed, plan_naming("ORD-101")])

    assert result.status == "accepted", result.findings
    assert [r["stage"] for r in transport.requests] == ["call1", "correction", "call2"]
    assert "missing_field" in transport.requests[1]["user"]


def test_the_plan_contract_asks_for_the_record_only_when_the_plan_names_it(
    tmp_path: Path,
) -> None:
    refund = world("refund")
    inventory = indirect_inventory(refund["inventory"])

    def properties(view: Any) -> dict[str, Any]:
        packet = build_call1_packet_v2(view, inventory, runtime())
        schema = rendered_response_contract(packet)["schema"]
        return schema["properties"]["stimulus_approach"]

    unnamed = properties(indirect_view(tmp_path, record_ref=None))
    named = properties(indirect_view(tmp_path, record_ref="ORD-101"))

    assert RECORD_FIELD in unnamed["properties"]
    assert unnamed["properties"][RECORD_FIELD]["type"] == "string"
    assert unnamed["properties"][RECORD_FIELD]["enum"] == RECORDS
    assert unnamed["properties"][RECORD_FIELD]["description"]
    assert RECORD_FIELD in unnamed["required"]
    assert RECORD_FIELD not in named["properties"]
    assert RECORD_FIELD not in named["required"]


# --- what the plan stages show and ask ---------------------------------------------------


def test_the_plan_stage_block_lists_the_slots_records_and_asks_for_the_choice(
    tmp_path: Path,
) -> None:
    block = multi_turn_block(indirect_view(tmp_path, record_ref=None), runtime())

    item = block["planted_item"]
    assert item["records"] == RECORDS
    assert "record_ref" not in item
    assert RECORD_FIELD in item["record_choice"]
    assert "records" in item["record_choice"]
    entry = block["fields"][RECORD_PATH]
    assert entry["meaning"]
    assert entry["example"] in RECORDS


def test_the_plan_stage_says_the_text_goes_in_a_listed_record_and_why(tmp_path: Path) -> None:
    block = multi_turn_block(indirect_view(tmp_path, record_ref=None), runtime())

    choice = block["planted_item"]["record_choice"]
    assert "only in a record that records lists" in choice
    assert "the carrier returns to the agent" in choice
    assert "target record" in choice
    assert "never name the target record" in choice
    assert "benign turns name the planted record" in choice
    meaning = block["fields"][RECORD_PATH]["meaning"]
    assert "planted_item.records" in meaning
    assert "failure criterion" in meaning


def test_a_shape_that_names_the_record_shows_it_and_asks_for_no_choice(tmp_path: Path) -> None:
    block = multi_turn_block(indirect_view(tmp_path, record_ref="ORD-102"), runtime())

    assert block["planted_item"]["record_ref"] == "ORD-102"
    assert "records" not in block["planted_item"]
    assert "record_choice" not in block["planted_item"]
    assert RECORD_PATH not in block["fields"]
    assert records_in_context({"multi_turn_shape": block}) is None


def test_every_plan_stage_request_carries_the_slots_records(tmp_path: Path) -> None:
    refund = world("refund")
    view = indirect_view(tmp_path, record_ref=None)
    inventory = indirect_inventory(refund["inventory"])
    plan = plan_naming("ORD-102")
    context = {
        **build_plan_author_context(view, inventory, runtime()),
        **multi_turn_payload(view, runtime()),
    }
    correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call1", original_context=context, current_output=b"{}", findings=[]
        ),
        correction_repair_inputs(view, inventory, runtime()),
    )
    packets = {
        "call1": build_call1_packet_v2(view, inventory, runtime()),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime()),
        "plan_correction": correction,
    }

    for stage, packet in packets.items():
        shape = json_section(packet.user, "INDIRECT SHAPE") if stage != "plan_correction" else None
        text = packet.user
        assert "ORD-101" in text and "ORD-102" in text, stage
        assert RECORD_PATH in text, stage
        if shape is not None:
            assert shape["planted_item"]["records"] == RECORDS, stage


def test_a_plan_correction_keeps_the_record_field_in_its_contract(tmp_path: Path) -> None:
    refund = world("refund")
    view = indirect_view(tmp_path, record_ref=None)
    inventory = indirect_inventory(refund["inventory"])
    context = {
        **build_plan_author_context(view, inventory, runtime()),
        **multi_turn_payload(view, runtime()),
    }

    payload = build_correction_context(
        failed_stage="call1", original_context=context, current_output=b"{}", findings=[]
    )

    approach = payload["response_contract"]["schema"]["properties"]["stimulus_approach"]
    assert RECORD_FIELD in approach["properties"]
    assert approach["properties"][RECORD_FIELD]["enum"] == RECORDS
    assert RECORD_FIELD in approach["required"]


def test_the_records_a_recorded_plan_context_offers_are_the_slots_records(tmp_path: Path) -> None:
    view = indirect_view(tmp_path, record_ref=None)

    assert records_in_context(multi_turn_payload(view, runtime())) == RECORDS
    assert records_in_context({}) is None
    assert records_in_context({"multi_turn_shape": {"planted_item": None}}) is None


def test_the_artifact_stages_show_the_record_the_plan_fixed(tmp_path: Path) -> None:
    refund = world("refund")
    view = indirect_view(tmp_path, record_ref=None)
    inventory = indirect_inventory(refund["inventory"])
    plan = plan_naming("ORD-102")
    metadata = indirect_metadata(refund["metadata"])
    packets = {
        "call2": build_call2_packet_v2(view, plan, inventory, runtime()),
        "artifact_review": build_artifact_review_packet(
            view, plan, metadata, inventory, runtime()
        ),
    }

    for stage, packet in packets.items():
        item = json_section(packet.user, "INDIRECT SHAPE")["planted_item"]
        assert item["record_ref"] == "ORD-102", stage
        assert "records" not in item, stage
        assert RECORD_PATH not in json_section(packet.user, "INDIRECT SHAPE")["fields"], stage
    review = packets["artifact_review"].user.split("PLANTED TEXT", 1)[1]
    assert '"record_ref": "ORD-102"' in review


# --- the reason the artifact stages are given -----------------------------------------------


def _artifact_packets(view: Any, plan: dict[str, Any]) -> dict[str, Any]:
    refund = world("refund")
    inventory = indirect_inventory(refund["inventory"])
    metadata = indirect_metadata(refund["metadata"])
    return {
        "call2": build_call2_packet_v2(view, plan, inventory, runtime()),
        "artifact_review": build_artifact_review_packet(
            view, plan, metadata, inventory, runtime()
        ),
    }


def _fixed_by(packet: Any) -> str:
    return json_section(packet.user, "INDIRECT SHAPE")["planted_item"]["record_ref_fixed_by"]


@pytest.mark.parametrize(
    ("record_ref", "plan", "reason"),
    [
        (None, plan_naming("ORD-102"), ["accepted plan", RECORD_PATH, "cannot change"]),
        (None, indirect_plan(world("refund")["plan"]), ["first record", "cannot revise the plan"]),
        ("ORD-102", indirect_plan(world("refund")["plan"]), ["producer's attack shape"]),
    ],
)
def test_the_artifact_stages_say_why_the_record_is_fixed(
    tmp_path: Path, record_ref: str | None, plan: dict[str, Any], reason: list[str]
) -> None:
    packets = _artifact_packets(indirect_view(tmp_path, record_ref=record_ref), plan)

    for stage, packet in packets.items():
        for phrase in reason:
            assert phrase in _fixed_by(packet), (stage, phrase)
        assert "artifact has no field that names a record" in _fixed_by(packet), stage


def test_the_planted_text_section_carries_the_same_reason_and_the_check(tmp_path: Path) -> None:
    packets = _artifact_packets(indirect_view(tmp_path, record_ref=None), plan_naming("ORD-102"))
    review = packets["artifact_review"].user.split("PLANTED TEXT", 1)[1]
    section = json_section("\n" + "PLANTED TEXT" + review, "PLANTED TEXT")

    planted_in = section["planted_in"]
    assert planted_in["record_ref"] == "ORD-102"
    assert planted_in["record_ref_fixed_by"] == _fixed_by(packets["artifact_review"])
    assert "record_ref_fixed_by" in section["check"]
    assert "another record" in section["check"]
    assert "benign turns" in section["check"]


def test_the_plan_stage_gives_no_fixed_reason_while_the_plan_author_chooses(
    tmp_path: Path,
) -> None:
    block = multi_turn_block(indirect_view(tmp_path, record_ref=None), runtime())

    assert "record_ref_fixed_by" not in block["planted_item"]


def test_the_artifact_correction_keeps_the_fixed_record_and_its_reason(tmp_path: Path) -> None:
    refund = world("refund")
    view = indirect_view(tmp_path, record_ref=None)
    inventory = indirect_inventory(refund["inventory"])
    plan = plan_naming("ORD-102")
    context = {
        **build_artifact_author_context(view, plan, inventory, runtime()),
        **multi_turn_payload(view, runtime(), plan),
    }

    packet = _render_correction_packet(
        build_correction_context(
            failed_stage="call2", original_context=context, current_output=b"{}", findings=[]
        ),
        correction_repair_inputs(view, inventory, runtime()),
    )

    assert '"record_ref":"ORD-102"' in packet.user
    assert "record_ref_fixed_by" in packet.user
    assert "records" not in json.dumps(context["multi_turn_shape"]["planted_item"])


def test_an_artifact_correction_after_a_failed_call2_shows_the_plans_record(
    tmp_path: Path,
) -> None:
    refund = world("refund")
    quoting = indirect_metadata(refund["metadata"], planted=ASKS[0])
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(plan_naming("ORD-102")),
            json.dumps(quoting),
            json.dumps(indirect_metadata(refund["metadata"])),
        ]
    )

    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(
        indirect_view(tmp_path, record_ref=None),
        indirect_inventory(refund["inventory"]),
        runtime(),
    )

    assert result.status == "accepted", result.findings
    assert [r["stage"] for r in transport.requests] == ["call1", "call2", "correction"]
    correction = transport.requests[2]["user"]
    assert '"record_ref":"ORD-102"' in correction
    assert "record_ref_fixed_by" in correction
