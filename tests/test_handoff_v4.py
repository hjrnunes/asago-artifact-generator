"""The input adapter accepts scenario-handoff-v4 and validates its attack shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from asago_artifact_generator.input_adapter import (
    InputKind,
    InputSourceError,
    ShapeVersionMalformed,
    _framed_digest,
    build_scenario_handoff_view,
    load_input,
)

_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff" / "handoff-v4"
_V3_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff" / "handoff-v3"
_EXPECTED = json.loads((_KIT / "expected-violations.json").read_text(encoding="utf-8"))
_DIGESTS = json.loads((_KIT / "canonical-digests.json").read_text(encoding="utf-8"))[
    "handoff_digests"
]
_OWNERSHIP_PREFIXES = ("artifact_design_field:", "prose_hiding:")
_OWNERSHIP_MESSAGE = "handoff ownership violation: "


def _signed(tmp_path: Path, payload: dict[str, Any], name: str = "handoff.json") -> Path:
    payload = {key: value for key, value in payload.items() if key != "content_digest"}
    payload["content_digest"] = _framed_digest("scenario-handoff-v4", payload)
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid(name: str) -> dict[str, Any]:
    return json.loads((_KIT / "valid" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", sorted(_DIGESTS))
def test_every_valid_v4_fixture_is_accepted(relative: str) -> None:
    view = load_input(_KIT / relative)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V4
    assert view.input_kind == "scenario-handoff-v4"
    assert view.payload["content_digest"] == _DIGESTS[relative]
    assert view.attack_shape == view.payload["attack_shape"]


def test_the_kit_holds_the_valid_fixtures_the_loop_covers() -> None:
    assert sorted(_DIGESTS) == sorted(
        f"valid/{path.name}" for path in (_KIT / "valid").glob("*.json")
    )
    assert len(_DIGESTS) == 12


@pytest.mark.parametrize("relative", sorted(_EXPECTED))
def test_every_invalid_v4_fixture_is_rejected_with_its_expected_codes(relative: str) -> None:
    expected = _EXPECTED[relative]
    ownership = [code for code in expected if code.startswith(_OWNERSHIP_PREFIXES)]

    with pytest.raises(InputSourceError) as raised:
        load_input(_KIT / relative)

    message = str(raised.value)
    if ownership:
        assert message.startswith(_OWNERSHIP_MESSAGE)
        assert message.removeprefix(_OWNERSHIP_MESSAGE).split(", ") == ownership
    else:
        for code in expected:
            assert code in message


_SCHEMA_ONLY = sorted(
    name
    for name, codes in _EXPECTED.items()
    if not any(code.startswith(_OWNERSHIP_PREFIXES) for code in codes)
)


@pytest.mark.parametrize("relative", _SCHEMA_ONLY)
def test_a_shape_that_breaks_the_contract_is_a_typed_malformed_shape(relative: str) -> None:
    with pytest.raises(ShapeVersionMalformed) as raised:
        load_input(_KIT / relative)

    assert raised.value.code == "shape_version_malformed"
    assert raised.value.schema_path
    assert isinstance(raised.value, InputSourceError)


def test_v4_views_expose_the_shape_but_not_to_the_model_projection() -> None:
    view = load_input(_KIT / "valid" / "adversarial-direct-multi-turn.json")

    assert view.attack_shape["turn_count"] == 3
    assert [turn["purpose"] for turn in view.attack_shape["turn_plan"]] == [
        "establish_context",
        "assert_authority",
        "request_action",
    ]
    assert "attack_shape" not in build_scenario_handoff_view(view)


def test_a_functional_v4_handoff_has_no_shape() -> None:
    assert load_input(_KIT / "valid" / "functional-null-shape.json").attack_shape is None


def test_a_v3_handoff_gets_an_implicit_single_turn_direct_shape() -> None:
    view = load_input(_V3_KIT / "valid" / "adversarial-observed-record.json")

    assert "attack_shape" not in view.payload
    assert view.attack_shape == {
        "channel": "direct",
        "turn_count": 1,
        "turn_plan": [{"position": 1, "speaker": "attacker_user", "purpose": "request_action"}],
        "indirect": None,
        "threat_label": None,
        "source": "code_default",
        "downgrade_reason": None,
    }


def test_the_implicit_shape_is_a_copy() -> None:
    view = load_input(_V3_KIT / "valid" / "adversarial-observed-record.json")

    view.attack_shape["turn_count"] = 9

    assert view.attack_shape["turn_count"] == 1


def test_a_v4_handoff_is_not_a_v3_input_and_the_reverse() -> None:
    with pytest.raises(InputSourceError, match="unknown scenario handoff schema version"):
        load_input(
            _KIT / "valid" / "adversarial-direct-single.json", kind=InputKind.SCENARIO_HANDOFF_V3
        )
    with pytest.raises(InputSourceError, match="unknown scenario handoff schema version"):
        load_input(
            _V3_KIT / "valid" / "adversarial-observed-record.json",
            kind=InputKind.SCENARIO_HANDOFF_V4,
        )


def test_a_v4_handoff_signed_in_the_v3_digest_domain_is_rejected(tmp_path: Path) -> None:
    payload = _valid("adversarial-direct-single.json")
    payload.pop("content_digest")
    payload["content_digest"] = _framed_digest("scenario-handoff-v3", payload)
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(path)


def test_an_unknown_future_version_is_rejected(tmp_path: Path) -> None:
    payload = _valid("adversarial-direct-single.json")
    payload["schema_version"] = "scenario-handoff-v5"
    path = tmp_path / "handoff.json"
    path.write_text(
        json.dumps({**payload, "content_digest": _framed_digest("scenario-handoff-v5", payload)}),
        encoding="utf-8",
    )

    with pytest.raises(InputSourceError, match="authoring source must be"):
        load_input(path)


def test_a_yaml_v4_handoff_loads(tmp_path: Path) -> None:
    payload = _valid("adversarial-direct-multi-turn.json")
    path = tmp_path / "handoff.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    assert load_input(path).attack_shape["turn_count"] == 3


def test_a_shape_that_breaks_a_cross_field_rule_names_the_rule_path(tmp_path: Path) -> None:
    payload = _valid("adversarial-direct-multi-turn.json")
    payload["attack_shape"]["turn_plan"][1]["position"] = 3

    with pytest.raises(ShapeVersionMalformed, match=r"attack_shape.*positions"):
        load_input(_signed(tmp_path, payload))


def test_a_missing_shape_is_a_typed_malformed_shape(tmp_path: Path) -> None:
    payload = _valid("adversarial-direct-single.json")
    del payload["attack_shape"]

    with pytest.raises(ShapeVersionMalformed, match="schema_violation:attack_shape"):
        load_input(_signed(tmp_path, payload))
