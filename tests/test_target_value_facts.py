"""Value facts follow the producer's declarations, without inferred semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.contract_kit import framed_digest
from asago_artifact_generator.target_inputs import TargetInputError, load_target_inputs

_KIT = Path(__file__).resolve().parents[1] / "contracts" / "target-profile" / "target-profile-v1"
_IDENTITIES = {
    "arguments": {"operation_id": "process_refund", "argument": "order_id"},
    "state_fields": {"path": "orders.*.customer_id"},
    "state_keys": {"path": "orders"},
}


def _profile() -> dict:
    return json.loads((_KIT / "valid" / "minimal.json").read_text(encoding="utf-8"))


def _write_profile(tmp_path: Path, profile: dict) -> Path:
    payload = {key: value for key, value in profile.items() if key != "semantic_digest"}
    payload["semantic_digest"] = framed_digest("execution-target-profile-v1", payload)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("group", _IDENTITIES)
@pytest.mark.parametrize("declaration", [{}, {"record_kind": None, "format": None}])
def test_every_value_fact_requires_a_nonnull_declaration(
    tmp_path: Path, group: str, declaration: dict
) -> None:
    profile = _profile()
    profile["value_facts"] = {group: [{**_IDENTITIES[group], **declaration}]}

    with pytest.raises(TargetInputError, match="value_fact_without_declaration") as raised:
        load_target_inputs(_write_profile(tmp_path, profile))

    assert f"value_facts.{group}.0" in str(raised.value)


@pytest.mark.parametrize("group", _IDENTITIES)
@pytest.mark.parametrize(
    "declaration",
    [
        {"record_kind": "record_id", "format": None},
        {"record_kind": None, "format": "date"},
        {"record_kind": "record_id", "format": "date-time"},
    ],
)
def test_a_kind_a_format_or_both_are_valid_without_observations(
    tmp_path: Path, group: str, declaration: dict
) -> None:
    profile = _profile()
    profile["value_facts"] = {group: [{**_IDENTITIES[group], **declaration}]}

    inventory, provenance = load_target_inputs(_write_profile(tmp_path, profile))

    assert inventory["facts"] == []
    assert provenance["observations_sha256"] is None


@pytest.mark.parametrize("group", _IDENTITIES)
@pytest.mark.parametrize("second_kind", ["record_id", "other_id"])
def test_each_target_value_is_declared_once_per_group(
    tmp_path: Path, group: str, second_kind: str
) -> None:
    profile = _profile()
    profile["value_facts"] = {
        group: [
            {**_IDENTITIES[group], "record_kind": "record_id"},
            {**_IDENTITIES[group], "record_kind": second_kind},
        ]
    }

    with pytest.raises(TargetInputError, match="must declare each target value once") as raised:
        load_target_inputs(_write_profile(tmp_path, profile))

    assert f"value_facts.{group}.1" in str(raised.value)


@pytest.mark.parametrize("omit_arguments", [False, True])
def test_an_operation_without_arguments_uses_its_resource_declaration(
    tmp_path: Path, omit_arguments: bool
) -> None:
    profile = _profile()
    operation = profile["resources"][0]["operations"][0]
    operation["argument_names"] = []
    if omit_arguments:
        del operation["argument_names"]
    profile["value_facts"] = {
        "arguments": [{**_IDENTITIES["arguments"], "record_kind": "order_id"}]
    }

    load_target_inputs(_write_profile(tmp_path, profile))


def test_an_operations_own_argument_list_takes_precedence(tmp_path: Path) -> None:
    profile = _profile()
    profile["resources"][0]["operations"][0]["argument_names"] = ["amount"]
    profile["value_facts"] = {
        "arguments": [{**_IDENTITIES["arguments"], "record_kind": "order_id"}]
    }

    with pytest.raises(TargetInputError, match="value_fact_unknown_argument"):
        load_target_inputs(_write_profile(tmp_path, profile))


def test_inventory_names_do_not_substitute_for_profile_operations(tmp_path: Path) -> None:
    profile = _profile()
    profile["resources"] = []
    profile["value_facts"] = {
        "arguments": [{**_IDENTITIES["arguments"], "record_kind": "order_id"}]
    }

    with pytest.raises(TargetInputError, match="value_fact_unknown_argument"):
        load_target_inputs(_write_profile(tmp_path, profile))


@pytest.mark.parametrize(
    "facts", [[], {"arguments": None}, {"arguments": [None]}, {"state_fields": "orders"}]
)
def test_malformed_value_fact_containers_receive_schema_errors(
    tmp_path: Path, facts: object
) -> None:
    profile = _profile()
    profile["value_facts"] = facts

    with pytest.raises(TargetInputError, match="target profile schema invalid"):
        load_target_inputs(_write_profile(tmp_path, profile))


def test_state_paths_can_overlap_and_field_and_key_facts_are_distinct(tmp_path: Path) -> None:
    profile = _profile()
    profile["value_facts"] = {
        "state_fields": [
            {"path": "orders.*", "record_kind": "order_id"},
            {"path": "orders.selected", "record_kind": "other_id"},
        ],
        "state_keys": [{"path": "orders.*", "record_kind": "customer_id"}],
    }
    load_target_inputs(_write_profile(tmp_path, profile))
