"""Producer scenario-handoff-v2 acceptance and authoring pass-through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from asago_artifact_generator.authoring.contracts import (
    NEUTRAL_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_PLAN_OUTCOME_EXAMPLE,
    PLAN_FIELD_MEANINGS,
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
from tests.test_versioned_prompt_roles import (
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _source,
)

_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff" / "handoff-v2"
_V1_HANDOFF = _KIT.parent / "handoff-v1" / "valid" / "adversarial-refund.json"
_OBSERVED = _KIT / "valid" / "adversarial-observed-record.json"
_EXPECTED_VIOLATIONS = json.loads((_KIT / "expected-violations.json").read_text(encoding="utf-8"))
_HANDOFF_DIGESTS = json.loads((_KIT / "canonical-digests.json").read_text(encoding="utf-8"))[
    "handoff_digests"
]
_V2_FIELDS = ("discriminating_condition", "condition_check")


def _write_signed(tmp_path: Path, payload: dict, domain: str) -> Path:
    payload = {key: value for key, value in payload.items() if key != "content_digest"}
    payload["content_digest"] = _framed_digest(domain, payload)
    path = tmp_path / "handoff.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("relative", sorted(_HANDOFF_DIGESTS))
def test_valid_v2_fixtures_load_with_their_digest_domain(relative: str) -> None:
    view = load_input(_KIT / relative)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V1
    assert view.handoff_schema_version == "scenario-handoff-v2"
    assert view.payload["content_digest"] == _HANDOFF_DIGESTS[relative]
    handoff = build_scenario_handoff_view(view)
    for key in _V2_FIELDS:
        if view.payload.get(key) is None:
            assert key not in handoff
        else:
            assert handoff[key] == view.payload[key]


@pytest.mark.parametrize("relative", sorted(_EXPECTED_VIOLATIONS))
def test_invalid_v2_fixtures_report_expected_violations(relative: str) -> None:
    with pytest.raises(InputSourceError) as raised:
        load_input(_KIT / relative, kind=InputKind.SCENARIO_HANDOFF_V1)

    for code in _EXPECTED_VIOLATIONS[relative]:
        assert code in str(raised.value)


def test_v1_handoff_still_loads_as_v1() -> None:
    view = load_input(_V1_HANDOFF)

    assert view.handoff_schema_version == "scenario-handoff-v1"
    assert not set(_V2_FIELDS) & set(build_scenario_handoff_view(view))


def test_v1_document_rejects_v2_fields(tmp_path: Path) -> None:
    payload = json.loads(_V1_HANDOFF.read_text(encoding="utf-8"))
    payload["discriminating_condition"] = None
    path = _write_signed(tmp_path, payload, "scenario-handoff-v1")

    with pytest.raises(InputSourceError, match="discriminating_condition"):
        load_input(path)


def test_v2_document_signed_in_v1_domain_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(_OBSERVED.read_text(encoding="utf-8"))
    path = _write_signed(tmp_path, payload, "scenario-handoff-v1")

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(path)


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(_OBSERVED.read_text(encoding="utf-8"))
    payload["schema_version"] = "scenario-handoff-v3"
    path = _write_signed(tmp_path, payload, "scenario-handoff-v3")

    with pytest.raises(InputSourceError, match="scenario-handoff-v2"):
        load_input(path)


def test_condition_and_guidance_reach_plan_artifact_and_review_prompts() -> None:
    view = load_input(_OBSERVED)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "call2": build_call2_packet_v2(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, _metadata(), _source(), [], inventory, runtime
        ),
    }
    statement = view.payload["discriminating_condition"]["statement"]
    guidance = "must check the scenario's discriminating_condition on captured evidence"
    check_reason = view.payload["condition_check"]["comparisons"][0]["reason"]

    for name, packet in packets.items():
        assert packet.user.count(statement) == 1, name
        assert "statement_location" in packet.user, name
        assert "argument_values" in packet.user, name
        assert '"condition_check"' in packet.user, name
        assert '"satisfied"' in packet.user, name
        assert check_reason not in packet.user, name
        assert packet.user.count(guidance) == (0 if name == "call2" else 1), name


@pytest.mark.parametrize("source", [_V1_HANDOFF, _KIT / "valid" / "analytical-only.json"])
def test_condition_guidance_is_omitted_without_a_condition(source: Path) -> None:
    view = load_input(source)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = (
        build_call1_packet_v2(view, inventory, runtime),
        build_plan_review_packet(view, plan, inventory, runtime),
        build_artifact_review_packet(view, plan, _metadata(), _source(), [], inventory, runtime),
    )

    for packet in packets:
        assert "discriminating_condition" not in packet.user, packet.stage
        assert "condition_check" not in packet.user, packet.stage


def test_condition_statement_stays_in_the_view_when_gherkin_omits_it(tmp_path: Path) -> None:
    payload = json.loads(_OBSERVED.read_text(encoding="utf-8"))
    payload["gherkin"]["given"] = [
        step
        for step in payload["gherkin"]["given"]
        if "the discriminating condition holds" not in step
    ]
    path = _write_signed(tmp_path, payload, "scenario-handoff-v2")
    view = load_input(path)
    statement = view.payload["discriminating_condition"]["statement"]

    packet = build_call1_packet_v2(view, _inventory(), _runtime_contract())

    assert packet.user.count(statement) == 1
    assert "statement_location" not in packet.user
    assert build_scenario_handoff_view(view)["condition_check"] == view.payload["condition_check"]


def test_call1_renders_runtime_contract_and_binding_rules_once() -> None:
    view = load_input(_OBSERVED)
    runtime = _runtime_contract()
    packet = build_call1_packet_v2(view, _inventory(), runtime)
    binding = packet.payload["response_contract"]["binding_declaration"]

    runtime_text = json.dumps(runtime, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert packet.user.count(runtime_text) == 1
    for rule in ("selector_rule", "consumer_rule", "source_ref_rule"):
        assert packet.user.count(json.dumps(binding[rule], ensure_ascii=False)) == 1, rule
        assert f"See binding_declaration.{rule}." in packet.user, rule


_NOT_CALLED_HANDOFF = _KIT / "valid" / "functional-not-called.json"


def test_not_called_condition_adds_omission_guidance_to_plan_prompts() -> None:
    view = load_input(_NOT_CALLED_HANDOFF)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, _metadata(), _source(), [], inventory, runtime
        ),
    }
    omission_sentence = "A not_called comparison is an omission"

    for name, packet in packets.items():
        assert packet.user.count(omission_sentence) == 1, name
    for name in ("call1", "plan_review"):
        assert packets[name].user.count(NEUTRAL_OMISSION_OUTCOME_EXAMPLE) == 1, name
        assert packets[name].user.count(NEUTRAL_PLAN_OUTCOME_EXAMPLE) == 1, name


@pytest.mark.parametrize("source", [_OBSERVED, _V1_HANDOFF])
def test_omission_guidance_is_omitted_without_a_not_called_comparison(source: Path) -> None:
    view = load_input(source)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    packets = (
        build_call1_packet_v2(view, inventory, runtime),
        build_plan_review_packet(view, plan, inventory, runtime),
    )

    for packet in packets:
        assert "A not_called comparison is an omission" not in packet.user, packet.stage
        assert NEUTRAL_OMISSION_OUTCOME_EXAMPLE not in packet.user, packet.stage


def test_command_interface_shows_detected_omission_with_container_refs() -> None:
    packet = build_call2_packet_v2(
        load_input(_NOT_CALLED_HANDOFF), _plan(), _inventory(), _runtime_contract()
    )
    omission = {
        "claim_level": "command_attempt",
        "evidence_refs": ["tool_calls", "completeness.tool_calls"],
        "outcome": "detected",
        "reason": (
            "Complete relevant tool-call capture contains no call to the required operation."
        ),
    }

    rendered = json.dumps(omission, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert f'"complete_omission_example":{rendered}' in packet.user
    assert "omission" in PLAN_FIELD_MEANINGS
