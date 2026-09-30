from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from asago_artifact_generator.bindings import validate_bindings
from asago_artifact_generator.target_inputs import TargetInputError, load_target_inputs


def _profile() -> dict:
    return {
        "schema_version": "execution-target-profile-v1",
        "target_id": "synthetic-target",
        "authorization_scope_id": "synthetic-scope",
        "basis": "target",
        "inventory_authority": "observed",
        "semantic_authority": "inferred",
        "inventory_completeness": "observed_complete",
        "source_protocol": "mcp",
        "source_inventory_digest": "a" * 64,
        "discovery_provenance": {
            "scanner_id": "scanner",
            "interpreter_id": "interpreter",
            "verifier_id": "verifier",
        },
        "inventory": {
            "schema_version": "mcp-inventory-v1",
            "target_id": "synthetic-target",
            "authorization_scope_id": "synthetic-scope",
            "source_protocol": "mcp",
            "semantic_digest": "a" * 64,
            "tools": [
                {
                    "name": "lookup_record",
                    "title": "Lookup",
                    "description": "Look up one record.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"record_id": {"type": "string"}},
                        "required": ["record_id"],
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                    },
                    "annotations": {"readOnlyHint": True},
                    "argument_names": ["record_id"],
                    "source_observation_sha256": "b" * 64,
                }
            ],
        },
        "interpretations": [
            {
                "resource_id": "mcp:synthetic-target:lookup_record",
                "tool_name": "lookup_record",
                "disposition": "supported",
                "likely_effect": "read",
                "likely_state_effect": "none",
                "interpreter_verifier_agreement": "agree",
                "evidence_refs": ["inventory:tool:lookup_record:name"],
                "rationale": "The observed interface reads one record.",
            }
        ],
        "resources": [],
        "diagnostics": [],
    }


def _write_profile(path: Path, *, mutate: bool = False) -> None:
    profile = _profile()
    if mutate:
        profile["target_id"] = "changed-target"
    digest_payload = dict(profile)
    profile["semantic_digest"] = hashlib.sha256(
        b"execution-target-profile-v1\0"
        + json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(profile), encoding="utf-8")


def _write_observations(path: Path, profile_digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "state": {
                    "record": {
                        "owner": {"id": "owner-1"},
                        "record_id": "record-1",
                    },
                    "enabled": True,
                    "audit_log": [{"event": "capture"}],
                },
                "target_profile_digest": profile_digest,
                "read_observations": [
                    {
                        "profile_digest": profile_digest,
                        "tool_name": "lookup_record",
                        "arguments": {"record_id": "record-1"},
                        "result": {"status": "ok", "values": [1, "two"]},
                        "status": {"transport": "verified", "content": "untrusted"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _profile_digest(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["semantic_digest"]


def test_target_discovery_maps_operations_facts_and_handles(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    observations = tmp_path / "runtime-context.json"
    _write_profile(profile)
    _write_observations(observations, _profile_digest(profile))

    inventory, provenance = load_target_inputs(profile, observations)

    assert [operation["name"] for operation in inventory["operations"]] == ["lookup_record"]
    operation = inventory["operations"][0]
    assert operation["arguments"]["properties"]["record_id"]["type"] == "string"
    assert operation["result_schema"]["properties"]["result"]["type"] == "string"
    assert operation["interpretation"] == {
        "disposition": "supported",
        "likely_effect": "read",
        "likely_state_effect": "none",
        "interpreter_verifier_agreement": "agree",
    }
    assert [fact["ref"] for fact in inventory["facts"]] == [
        "state:enabled",
        "state:record",
        "observation:lookup_record:0",
    ]
    assert inventory["facts"][0]["schema"] == {"type": "boolean"}
    assert inventory["facts"][1]["schema"]["properties"]["owner"]["properties"]["id"] == {
        "type": "string"
    }
    assert inventory["facts"][2]["schema"] == {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "values": {
                "type": "array",
                "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            },
        },
    }
    refs = {handle["ref"] for handle in inventory["source_handles"]}
    assert {
        "tool:lookup_record",
        "target-profile",
        "runtime-context",
    } <= refs
    assert provenance["profile_sha256"] == hashlib.sha256(profile.read_bytes()).hexdigest()
    assert (
        provenance["observations_sha256"] == hashlib.sha256(observations.read_bytes()).hexdigest()
    )
    assert provenance["discovery_provenance"]["scanner_id"] == "scanner"


def test_target_discovery_output_is_deterministic_and_bindable(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    observations = tmp_path / "runtime-context.json"
    _write_profile(profile)
    _write_observations(observations, _profile_digest(profile))

    first = load_target_inputs(profile, observations)
    second = load_target_inputs(profile, observations)

    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second,
        sort_keys=True,
        separators=(",", ":"),
    )
    validated = validate_bindings(
        [
            {
                "name": "owner_id",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:record",
                "selector": "value.owner.id",
                "consumers": ["prerequisites.owner_id"],
                "on_missing": "stop",
            }
        ],
        inventory=first[0],
        runtime_contract={"setup_permissions": []},
    )
    assert validated[0].name == "owner_id"


def test_target_discovery_rejects_digest_mismatch_and_bad_schema(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    observations = tmp_path / "runtime-context.json"
    _write_profile(profile)
    _write_observations(observations, "c" * 64)

    with pytest.raises(TargetInputError, match="does not match"):
        load_target_inputs(profile, observations)

    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"target_id": "missing-required-fields"}), encoding="utf-8")
    with pytest.raises(TargetInputError, match="schema invalid"):
        load_target_inputs(malformed)


def _write_keyed_observations(path: Path, profile_digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "state": {
                    "orders": {
                        "ORD-1": {"status": "open", "record_key": "collides-by-name"},
                        "ORD-2": {"status": "closed", "record_key": "collides-by-name"},
                    },
                    "tags": {"a": "not-a-record"},
                },
                "target_profile_digest": profile_digest,
                "read_observations": [],
            }
        ),
        encoding="utf-8",
    )


def test_keyed_map_state_gains_a_derived_record_key_companion(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    observations = tmp_path / "runtime-context.json"
    _write_profile(profile)
    _write_keyed_observations(observations, _profile_digest(profile))

    inventory, _ = load_target_inputs(profile, observations)
    facts = {fact["ref"]: fact for fact in inventory["facts"]}

    assert list(facts) == ["state:orders", "state:orders:records", "state:tags"]
    assert facts["state:orders"]["value"]["ORD-1"] == {
        "status": "open",
        "record_key": "collides-by-name",
    }
    companion = facts["state:orders:records"]
    assert companion["value"] == {
        "ORD-1": {"record_key": "ORD-1"},
        "ORD-2": {"record_key": "ORD-2"},
    }
    assert companion["provenance"]["derived_from"] == "state:orders"
    assert companion["provenance"]["derivation"] == "keyed_map_record_key"
    assert companion["provenance"]["source"] == "runtime-context"
    assert "state:orders" in companion["meaning"]

    validated = validate_bindings(
        [
            {
                "name": "target_order_id",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:orders:records",
                "selector": "value.ORD-2.record_key",
                "consumers": ["prerequisites.target_order_id"],
                "on_missing": "stop",
            },
            {
                "name": "target_order_status",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:orders",
                "selector": "value.ORD-2.status",
                "consumers": ["prerequisites.target_order_status"],
                "on_missing": "stop",
            },
        ],
        inventory=inventory,
        runtime_contract={"setup_permissions": []},
    )
    assert [binding.name for binding in validated] == [
        "target_order_id",
        "target_order_status",
    ]


@pytest.mark.parametrize(
    ("selector", "expected_type"),
    [("value.ORD-2", "object"), ("value", "object")],
)
def test_record_key_companion_bindings_must_select_the_key_string(
    tmp_path: Path, selector: str, expected_type: str
) -> None:
    # The companion's records are {"record_key": key} wrappers; binding one
    # hands a detector that expects the full record nothing but the key.
    from asago_artifact_generator.bindings import BindingValidationError

    profile = tmp_path / "profile.json"
    observations = tmp_path / "runtime-context.json"
    _write_profile(profile)
    _write_keyed_observations(observations, _profile_digest(profile))
    inventory, _ = load_target_inputs(profile, observations)

    with pytest.raises(BindingValidationError) as raised:
        validate_bindings(
            [
                {
                    "name": "target_order",
                    "expected_type": expected_type,
                    "source_kind": "supplied_input",
                    "source_ref": "facts:state:orders:records",
                    "selector": selector,
                    "consumers": ["detector.target_order"],
                    "on_missing": "stop",
                }
            ],
            inventory=inventory,
            runtime_contract={"setup_permissions": []},
        )
    message = str(raised.value)
    assert "target_order" in message
    assert "value.<key>.record_key" in message
    assert "facts:state:orders" in message
