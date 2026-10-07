from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from asago_artifact_generator import target_inputs
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
                    "consumers": ["judge.target_order"],
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


def _observations(profile_digest: str) -> dict:
    return {
        "state": {"enabled": True},
        "target_profile_digest": profile_digest,
        "read_observations": [
            {
                "profile_digest": profile_digest,
                "tool_name": "lookup_record",
                "arguments": {"record_id": "record-1"},
                "result": {"status": "ok"},
                "status": {"transport": "verified", "content": "untrusted"},
            }
        ],
    }


def _read(**changes: object):
    def mutate(observations: dict) -> None:
        observations["read_observations"][0].update(changes)

    return mutate


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda o: o.update(bogus=1, extra=2), "unsupported fields: bogus, extra"),
        (lambda o: o.update(target_profile_digest="abc"), "require target_profile_digest"),
        (lambda o: o.update(state=[]), "state must be an object"),
        (lambda o: o.update(read_observations={}), "read_observations must be a list"),
        (
            lambda o: o.update(read_observations=o["read_observations"] * 16),
            "cannot contain more than 15 reads",
        ),
        (lambda o: o.update(read_observations=["x"]), r"read_observations\[0\] must be an object"),
        (_read(profile_digest="d" * 64), r"read_observations\[0\] profile digest does not match"),
        (_read(profile_digest=None), r"read_observations\[0\] profile digest does not match"),
        (_read(tool_name=""), r"read_observations\[0\] requires tool_name"),
        (_read(tool_name="delete_record"), r"read_observations\[0\] names an unknown tool"),
        (_read(arguments={"record_id": 1}), "arguments must be a string mapping"),
        (_read(arguments=["record-1"]), "arguments must be a string mapping"),
        (_read(status={"transport": "unverified", "content": "untrusted"}), "verified and"),
        (_read(status={"transport": "verified", "content": "trusted"}), "verified and"),
        (_read(status="verified"), "must be verified and untrusted"),
        (_read(result={"isError": True}), r"read_observations\[0\] result must be successful"),
        (_read(result=["ok"]), r"read_observations\[0\] result must be successful"),
    ],
)
def test_target_observations_are_rejected_at_the_first_invalid_field(
    tmp_path: Path, mutate, message: str
) -> None:
    profile = tmp_path / "profile.json"
    _write_profile(profile)
    observations = _observations(_profile_digest(profile))
    mutate(observations)
    observations_path = tmp_path / "runtime-context.json"
    observations_path.write_text(json.dumps(observations), encoding="utf-8")

    with pytest.raises(TargetInputError, match=message):
        load_target_inputs(profile, observations_path)


def test_target_observations_must_be_an_object(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    _write_profile(profile)
    observations_path = tmp_path / "runtime-context.json"
    observations_path.write_text("[]", encoding="utf-8")

    with pytest.raises(TargetInputError, match="target observations must be an object"):
        load_target_inputs(profile, observations_path)


def test_read_observations_may_be_null_or_absent_for_the_validator() -> None:
    from asago_artifact_generator.target_inputs import _validate_observations

    digest = "a" * 64
    _validate_observations(
        {"state": {}, "target_profile_digest": digest, "read_observations": None},
        profile={},
    )
    _validate_observations({"state": {}, "target_profile_digest": digest}, profile={})
    with pytest.raises(TargetInputError, match="names an unknown tool"):
        _validate_observations(
            {
                "state": {},
                "target_profile_digest": digest,
                "read_observations": [
                    {
                        "profile_digest": digest,
                        "tool_name": "lookup_record",
                        "arguments": None,
                        "result": {},
                        "status": {"transport": "verified", "content": "untrusted"},
                    }
                ],
            },
            profile={"inventory": "not-an-object"},
        )


def test_null_read_observations_load_as_an_empty_list(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    _write_profile(profile)
    observations = _observations(_profile_digest(profile))
    observations["read_observations"] = None
    observations_path = tmp_path / "runtime-context.json"
    observations_path.write_text(json.dumps(observations), encoding="utf-8")

    inventory, _ = load_target_inputs(profile, observations_path)

    assert [fact["ref"] for fact in inventory["facts"]] == ["state:enabled"]


def test_state_fact_schemas_cover_every_json_type(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    _write_profile(profile)
    observations = _observations(_profile_digest(profile))
    observations["state"] = {
        "nothing": None,
        "ratio": 0.5,
        "count": 3,
        "empty": [],
        "names": ["a", "b"],
        "mixed": [1, "one", None, 1],
    }
    observations_path = tmp_path / "runtime-context.json"
    observations_path.write_text(json.dumps(observations), encoding="utf-8")

    inventory, _ = load_target_inputs(profile, observations_path)

    schemas = {fact["ref"]: fact["schema"] for fact in inventory["facts"]}
    assert schemas["state:nothing"] == {"type": "null"}
    assert schemas["state:ratio"] == {"type": "number"}
    assert schemas["state:count"] == {"type": "integer"}
    assert schemas["state:empty"] == {"type": "array", "items": {}}
    assert schemas["state:names"] == {"type": "array", "items": {"type": "string"}}
    assert schemas["state:mixed"] == {
        "type": "array",
        "items": {"anyOf": [{"type": "integer"}, {"type": "null"}, {"type": "string"}]},
    }


def test_schema_inference_rejects_non_json_values() -> None:
    from asago_artifact_generator.target_inputs import _infer_schema

    with pytest.raises(TargetInputError, match="cannot infer JSON schema for value of type set"):
        _infer_schema({"tags": {"a"}})


def test_operations_reject_invalid_and_duplicate_tools_and_use_the_first_interpretation() -> None:
    from asago_artifact_generator.target_inputs import _operations

    profile = _profile()
    tool = profile["inventory"]["tools"][0]
    profile["interpretations"].append(
        {**profile["interpretations"][0], "disposition": "unsupported"}
    )
    profile["interpretations"].append("not-an-interpretation")
    profile["inventory"]["tools"].append({**tool, "name": "archive_record", "output_schema": None})

    operations = _operations(profile)

    assert [operation["name"] for operation in operations] == [
        "archive_record",
        "lookup_record",
    ]
    assert "result_schema" not in operations[0]
    assert "interpretation" not in operations[0]
    assert operations[1]["interpretation"]["disposition"] == "supported"
    with pytest.raises(TargetInputError, match="duplicates tool: lookup_record"):
        _operations({"inventory": {"tools": [tool, tool]}})
    with pytest.raises(TargetInputError, match="contains an invalid tool"):
        _operations({"inventory": {"tools": [{"title": "unnamed"}]}})
    without_interpretations = _operations(
        {"inventory": {"tools": [tool]}, "interpretations": "none"}
    )
    assert "interpretation" not in without_interpretations[0]


def _digested(profile: dict) -> dict:
    payload = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(b"execution-target-profile-v1\0" + payload).hexdigest()
    return {**profile, "semantic_digest": digest}


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"semantic_digest": "short"}, "semantic_digest is required"),
        ({"semantic_digest": "a" * 64, "x": float("nan")}, "cannot be canonically encoded"),
        ({"semantic_digest": "a" * 64}, "semantic_digest does not match canonical"),
        (
            _digested(
                {"source_inventory_digest": "b" * 64, "inventory": {"semantic_digest": "c" * 64}}
            ),
            "source_inventory_digest does not match inventory",
        ),
    ],
)
def test_profile_digest_rejections(profile: dict, message: str) -> None:
    with pytest.raises(TargetInputError, match=message):
        target_inputs._validate_profile_digest(profile)


_LOCK_METADATA = {
    "authority": "asago-scenario-generator",
    "contract": "target-profile",
    "schema_version": "execution-target-profile-v1",
    "digest_domain": "execution-target-profile-v1",
}


@pytest.mark.parametrize(
    ("lock", "message"),
    [
        (None, "cannot read target profile contract lock"),
        ({**_LOCK_METADATA, "authority": "other"}, "contract lock metadata is invalid"),
        (
            {**_LOCK_METADATA, "files": {"schema.json": "0" * 64}},
            "contract digest mismatch: schema.json",
        ),
    ],
)
def test_contract_lock_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: dict | None, message: str
) -> None:
    monkeypatch.setattr(target_inputs, "_CONTRACT_ROOT", tmp_path)
    if lock is not None:
        (tmp_path / "CONTRACT.lock").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(TargetInputError, match=message):
        target_inputs._validate_contract_lock()


def test_facts_need_the_observations_file_and_bytes() -> None:
    for file, data in ((None, b"{}"), (Path("runtime-context.json"), None), (None, None)):
        with pytest.raises(TargetInputError) as raised:
            target_inputs._facts({}, observations_file=file, observations_bytes=data)

        assert str(raised.value) == "target observations provenance is unavailable"


def test_profile_must_be_an_object(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text("[]", encoding="utf-8")

    with pytest.raises(TargetInputError) as raised:
        load_target_inputs(profile)
    assert str(raised.value) == "target profile must be an object"

    with pytest.raises(TargetInputError) as raised:
        target_inputs._validate_profile_contract(["not", "an", "object"])
    assert str(raised.value) == "target profile must be an object"


@pytest.mark.parametrize("schema_text", [None, "{not json"])
def test_unreadable_profile_schema_is_a_target_input_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_text: str | None
) -> None:
    monkeypatch.setattr(target_inputs, "_validate_contract_lock", lambda: tmp_path)
    if schema_text is not None:
        schema = tmp_path / "target-profile-v1" / "schema.json"
        schema.parent.mkdir()
        schema.write_text(schema_text, encoding="utf-8")

    with pytest.raises(TargetInputError, match="^cannot validate target profile contract: "):
        target_inputs._validate_profile_contract({})


def test_validator_failure_is_a_target_input_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(schema: object, instance: object) -> None:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(target_inputs, "first_schema_error", explode)

    with pytest.raises(TargetInputError) as raised:
        target_inputs._validate_profile_contract({})

    assert str(raised.value) == "cannot validate target profile contract: validator exploded"
