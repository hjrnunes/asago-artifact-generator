"""Producer scenario-handoff-v3 acceptance and authoring pass-through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from asago_artifact_generator.authoring.contracts import (
    NEUTRAL_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_PLAN_OUTCOME_EXAMPLE,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_plan_review_packet,
)
from asago_artifact_generator.input_adapter import (
    InputKind,
    InputSourceError,
    _framed_digest,
    build_scenario_handoff_view,
    load_input,
)
from tests.support import (
    HANDOFF_V3_KIT,
    NO_CONDITION_HANDOFF,
    NOT_CALLED_HANDOFF,
    OBSERVED_HANDOFF,
    rendered_response_contract,
    world_builders,
)

_inventory, _metadata, _plan, _runtime_contract = world_builders(
    "ehr", "inventory", "metadata", "plan", "runtime_contract"
)

# The schema-* cases run in tests/test_handoff_schema_cases.py with this reader's wording.
_EXPECTED_VIOLATIONS = {
    relative: codes
    for relative, codes in json.loads(
        (HANDOFF_V3_KIT / "expected-violations.json").read_text(encoding="utf-8")
    ).items()
    if not relative.startswith("invalid/schema-")
}
_HANDOFF_DIGESTS = json.loads(
    (HANDOFF_V3_KIT / "canonical-digests.json").read_text(encoding="utf-8")
)["handoff_digests"]
_V2_FIELDS = ("discriminating_condition", "condition_check")


def _write_signed(tmp_path: Path, payload: dict, domain: str) -> Path:
    payload = {key: value for key, value in payload.items() if key != "content_digest"}
    payload["content_digest"] = _framed_digest(domain, payload)
    path = tmp_path / "handoff.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("relative", sorted(_HANDOFF_DIGESTS))
def test_valid_v3_fixtures_load_with_their_digest_domain(relative: str) -> None:
    view = load_input(HANDOFF_V3_KIT / relative)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V3
    assert view.payload["content_digest"] == _HANDOFF_DIGESTS[relative]
    handoff = build_scenario_handoff_view(view)
    for key in _V2_FIELDS:
        if view.payload.get(key) is None:
            assert key not in handoff
        else:
            assert handoff[key] == view.payload[key]


@pytest.mark.parametrize("relative", sorted(_EXPECTED_VIOLATIONS))
def test_invalid_v3_fixtures_report_expected_violations(relative: str) -> None:
    with pytest.raises(InputSourceError) as raised:
        load_input(HANDOFF_V3_KIT / relative)

    for code in _EXPECTED_VIOLATIONS[relative]:
        assert code in str(raised.value)


@pytest.mark.parametrize("relative", sorted(_EXPECTED_VIOLATIONS))
def test_invalid_v3_fixtures_report_exactly_the_producers_ownership_codes(relative: str) -> None:
    prefix = "handoff ownership violation: "
    with pytest.raises(InputSourceError) as raised:
        load_input(HANDOFF_V3_KIT / relative)

    message = str(raised.value)
    reported = message.removeprefix(prefix).split(", ") if message.startswith(prefix) else []
    expected = [
        code
        for code in _EXPECTED_VIOLATIONS[relative]
        if code.startswith(("artifact_design_field:", "prose_hiding:"))
    ]
    assert reported == expected


def test_v3_document_signed_in_v2_domain_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(OBSERVED_HANDOFF.read_text(encoding="utf-8"))
    path = _write_signed(tmp_path, payload, "scenario-handoff-v2")

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(path)


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(OBSERVED_HANDOFF.read_text(encoding="utf-8"))
    payload["schema_version"] = "scenario-handoff-v5"
    path = _write_signed(tmp_path, payload, "scenario-handoff-v5")

    with pytest.raises(InputSourceError, match="scenario-handoff-v3"):
        load_input(path)


def test_condition_statement_reaches_plan_artifact_and_review_prompts() -> None:
    view = load_input(OBSERVED_HANDOFF)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "call2": build_call2_packet_v2(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, _metadata(), inventory, runtime
        ),
    }
    statement = view.payload["discriminating_condition"]["statement"]
    check_reason = view.payload["condition_check"]["comparisons"][0]["reason"]

    for name, packet in packets.items():
        assert packet.user.count(statement) == 1, name
        assert check_reason not in packet.user, name


def test_condition_guidance_is_omitted_for_an_analytical_only_scenario() -> None:
    view = load_input(HANDOFF_V3_KIT / "valid" / "analytical-only.json")
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = (
        build_call1_packet_v2(view, inventory, runtime),
        build_plan_review_packet(view, plan, inventory, runtime),
        build_artifact_review_packet(view, plan, _metadata(), inventory, runtime),
    )

    for packet in packets:
        assert "discriminating_condition" not in packet.user, packet.stage
        assert "condition_check" not in packet.user, packet.stage


def test_condition_statement_stays_in_the_view_when_gherkin_omits_it(tmp_path: Path) -> None:
    payload = json.loads(OBSERVED_HANDOFF.read_text(encoding="utf-8"))
    payload["gherkin"]["given"] = [
        step
        for step in payload["gherkin"]["given"]
        if "the discriminating condition holds" not in step
    ]
    path = _write_signed(tmp_path, payload, "scenario-handoff-v3")
    view = load_input(path)
    statement = view.payload["discriminating_condition"]["statement"]

    packet = build_call1_packet_v2(view, _inventory(), _runtime_contract())

    assert packet.user.count(statement) == 1
    assert "statement_location" not in packet.user
    assert build_scenario_handoff_view(view)["condition_check"] == view.payload["condition_check"]


def test_call1_renders_runtime_contract_and_binding_rules_once() -> None:
    view = load_input(OBSERVED_HANDOFF)
    runtime = _runtime_contract()
    packet = build_call1_packet_v2(view, _inventory(), runtime)
    binding = rendered_response_contract(packet)["binding_declaration"]

    runtime_text = json.dumps(runtime, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert packet.user.count(runtime_text) == 1
    for rule in ("selector_rule", "consumer_rule", "source_ref_rule"):
        assert packet.user.count(json.dumps(binding[rule], ensure_ascii=False)) == 1, rule
        assert f"See binding_declaration.{rule}." in packet.user, rule


def test_not_called_condition_adds_the_omission_examples_to_plan_prompts() -> None:
    view = load_input(NOT_CALLED_HANDOFF)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, _metadata(), inventory, runtime
        ),
    }

    for name in ("call1", "plan_review"):
        assert packets[name].user.count(NEUTRAL_OMISSION_OUTCOME_EXAMPLE) == 1, name
        assert packets[name].user.count(NEUTRAL_PLAN_OUTCOME_EXAMPLE) == 1, name


@pytest.mark.parametrize("source", [OBSERVED_HANDOFF, NO_CONDITION_HANDOFF])
def test_the_omission_example_is_omitted_without_a_not_called_comparison(source: Path) -> None:
    view = load_input(source)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = (
        build_call1_packet_v2(view, inventory, runtime),
        build_plan_review_packet(view, plan, inventory, runtime),
    )

    for packet in packets:
        assert NEUTRAL_OMISSION_OUTCOME_EXAMPLE not in packet.user, packet.stage
