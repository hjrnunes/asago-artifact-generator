"""Derive authoring inputs from producer target-discovery artifacts.

This module is deliberately mechanical.  It validates the producer-owned
profile, copies its observed tool interface, and turns captured JSON values
into typed facts without interpreting names or descriptions.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .contract_kit import (
    canonical_json,
    first_schema_error,
    framed_digest,
    load_json_file,
    parse_document,
    sha256_hex,
    verify_contract_lock,
)
from .value_checks import SHA256_HEX_LENGTH, is_sha256_hex

_CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "target-profile"
_PROFILE_SCHEMA_VERSION = "execution-target-profile-v1"
_PROFILE_DIGEST_DOMAIN = _PROFILE_SCHEMA_VERSION


class TargetInputError(ValueError):
    """Raised when producer target-discovery input is invalid."""


def load_target_inputs(
    profile_path: str | Path,
    observations_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a profile and derive an inventory plus immutable provenance.

    The returned inventory contains operations from the profile.  When a
    runtime-context file is supplied, it also contains one fact for each
    captured state key and read observation.
    """

    profile_file = Path(profile_path)
    profile_bytes = _read_file(profile_file, "target profile")
    profile = _parse_document(profile_file, profile_bytes, "target profile")
    _validate_profile_contract(profile)
    _validate_profile_digest(profile)

    observations: dict[str, Any] | None = None
    observations_bytes: bytes | None = None
    observations_file: Path | None = None
    if observations_path is not None:
        observations_file = Path(observations_path)
        observations_bytes = _read_file(observations_file, "target observations")
        observations = _parse_document(
            observations_file,
            observations_bytes,
            "target observations",
        )
        _validate_observations(observations, profile=profile)
        if observations.get("target_profile_digest") != profile["semantic_digest"]:
            raise TargetInputError(
                "target observations profile digest does not match target profile semantic_digest"
            )

    inventory = _build_inventory(
        profile,
        observations,
        observations_file=observations_file,
        observations_bytes=observations_bytes,
    )
    # Record content digests, not local paths: packages must not depend on the
    # machine or run directory that supplied the discovery files.
    provenance = {
        "profile_sha256": sha256_hex(profile_bytes),
        "observations_sha256": (
            sha256_hex(observations_bytes) if observations_bytes is not None else None
        ),
        "semantic_digest": profile["semantic_digest"],
        "source_inventory_digest": profile.get("source_inventory_digest"),
        "target_id": profile["target_id"],
        "discovery_provenance": deepcopy(profile.get("discovery_provenance")),
    }
    return inventory, provenance


def _build_inventory(
    profile: dict[str, Any],
    observations: dict[str, Any] | None,
    *,
    observations_file: Path | None,
    observations_bytes: bytes | None,
) -> dict[str, Any]:
    inventory: dict[str, Any] = {
        "operations": _operations(profile),
        "facts": [],
        "source_handles": _source_handles(profile),
    }
    if observations is not None:
        inventory["facts"] = _facts(
            observations,
            observations_file=observations_file,
            observations_bytes=observations_bytes,
        )
    # Keep source handles in a deterministic namespace while retaining the
    # exact file paths in the returned provenance record.
    inventory["source_handles"].append(
        {
            "ref": "target-profile",
            "meaning": "Producer execution target profile.",
        }
    )
    if observations_file is not None:
        inventory["source_handles"].append(
            {
                "ref": "runtime-context",
                "meaning": "Producer captured runtime context paired with the target profile.",
            }
        )
    inventory["source_handles"].sort(key=lambda item: item["ref"])
    return inventory


def _operations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    inventory = profile.get("inventory")
    tools = inventory.get("tools", []) if isinstance(inventory, dict) else []
    interpretation_by_tool = _interpretations_by_tool(profile)
    operations: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise TargetInputError("target profile inventory contains an invalid tool")
        if tool["name"] in seen_names:
            raise TargetInputError(f"target profile inventory duplicates tool: {tool['name']}")
        seen_names.add(tool["name"])
        operations.append(_operation_record(tool, interpretation_by_tool.get(tool["name"], [])))
    return sorted(operations, key=lambda item: item["name"])


def _interpretations_by_tool(profile: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    interpretations = profile.get("interpretations", [])
    interpretation_by_tool: dict[str, list[dict[str, Any]]] = {}
    if isinstance(interpretations, list):
        for interpretation in interpretations:
            if isinstance(interpretation, dict) and isinstance(
                interpretation.get("tool_name"), str
            ):
                interpretation_by_tool.setdefault(interpretation["tool_name"], []).append(
                    interpretation
                )
    return interpretation_by_tool


def _operation_record(
    tool: dict[str, Any], interpretations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Map one inventory tool to an operation, with its first interpretation's summary."""

    operation: dict[str, Any] = {
        "name": tool["name"],
        "title": tool.get("title"),
        "description": tool.get("description"),
        "arguments": deepcopy(tool.get("input_schema", {})),
        "annotations": deepcopy(tool.get("annotations")),
    }
    if tool.get("output_schema") is not None:
        operation["result_schema"] = deepcopy(tool["output_schema"])
    if interpretations:
        interpretation = interpretations[0]
        selected = {
            key: interpretation[key]
            for key in (
                "disposition",
                "likely_effect",
                "likely_state_effect",
                "interpreter_verifier_agreement",
            )
            if key in interpretation
        }
        if selected:
            operation["interpretation"] = selected
    return operation


def _source_handles(profile: dict[str, Any]) -> list[dict[str, Any]]:
    inventory = profile.get("inventory")
    tools = inventory.get("tools", []) if isinstance(inventory, dict) else []
    handles = [
        {
            "ref": f"tool:{tool['name']}",
            "meaning": tool.get("description"),
        }
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    return handles


def _facts(
    observations: dict[str, Any],
    *,
    observations_file: Path | None,
    observations_bytes: bytes | None,
) -> list[dict[str, Any]]:
    if observations_file is None or observations_bytes is None:
        raise TargetInputError("target observations provenance is unavailable")
    provenance_base = {
        "source": "runtime-context",
        "sha256": sha256_hex(observations_bytes),
    }
    facts: list[dict[str, Any]] = []
    # The producer's runtime-context parser drops ``audit_log`` as capture
    # telemetry rather than target state; mirror that contract here.
    state = {
        key: value for key, value in observations.get("state", {}).items() if key != "audit_log"
    }
    state_refs = {f"state:{key}" for key in state}
    for key in sorted(state):
        ref = f"state:{key}"
        facts.append(
            {
                "ref": ref,
                "value": deepcopy(state[key]),
                "schema": _infer_schema(state[key]),
                "provenance": deepcopy(provenance_base),
                "meaning": "Captured target state at discovery time.",
            }
        )
        companion = _keyed_records_fact(ref, state[key], provenance_base, state_refs)
        if companion is not None:
            facts.append(companion)

    read_observations = observations.get("read_observations") or []
    facts.extend(_read_facts(read_observations, provenance_base))
    return facts


def _read_facts(
    read_observations: list[dict[str, Any]], provenance_base: dict[str, Any]
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for index, observation in enumerate(read_observations):
        tool_name = observation["tool_name"]
        observation_provenance = {
            **provenance_base,
            "tool_name": tool_name,
            "arguments": deepcopy(observation.get("arguments")),
            "status": deepcopy(observation.get("status")),
        }
        facts.append(
            {
                "ref": f"observation:{tool_name}:{index}",
                "value": deepcopy(observation.get("result")),
                "schema": _infer_schema(observation.get("result")),
                "provenance": observation_provenance,
                "meaning": (
                    f"Captured result of the {tool_name} read call; content is untrusted."
                ),
            }
        )
    return facts


KEYED_RECORDS_SUFFIX = ":records"
_RECORD_KEY_FIELD = "record_key"


def _keyed_records_fact(
    ref: str,
    value: Any,
    provenance_base: dict[str, Any],
    existing_refs: set[str],
) -> dict[str, Any] | None:
    """Return a derived fact that exposes each keyed-map record key as a field.

    A captured map such as ``{"LN-1": {...}}`` documents its records only
    under their keys, so no selector can yield the key itself as a string.
    The companion maps each key to ``{"record_key": key}``; record fields stay
    selectable only through the unchanged original fact, which also keeps the
    companion small in rendered prompts.
    """

    if not isinstance(value, dict) or not value:
        return None
    if not all(isinstance(record, dict) for record in value.values()):
        return None
    companion_ref = f"{ref}{KEYED_RECORDS_SUFFIX}"
    if companion_ref in existing_refs:
        return None
    records = {key: {_RECORD_KEY_FIELD: key} for key in value}
    return {
        "ref": companion_ref,
        "value": records,
        "schema": _infer_schema(records),
        "provenance": {
            **deepcopy(provenance_base),
            "derived_from": ref,
            "derivation": "keyed_map_record_key",
            "key_field": _RECORD_KEY_FIELD,
        },
        "meaning": (
            f"Derived from {ref}: each record key of {ref} as a string, selected with "
            f"value.<key>.{_RECORD_KEY_FIELD}. Record fields remain selectable only "
            f"from {ref} with value.<key>.<field>."
        ),
    }


# bool precedes int because bool is an int subclass.
_SCALAR_SCHEMA_TYPES = (
    (type(None), "null"),
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
)


def _infer_schema(value: Any) -> dict[str, Any]:
    for kind, name in _SCALAR_SCHEMA_TYPES:
        if isinstance(value, kind):
            return {"type": name}
    if isinstance(value, list):
        return {"type": "array", "items": _array_items_schema(value)}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {key: _infer_schema(value[key]) for key in sorted(value)},
        }
    raise TargetInputError(f"cannot infer JSON schema for value of type {type(value).__name__}")


def _array_items_schema(value: list[Any]) -> dict[str, Any]:
    schemas = [_infer_schema(item) for item in value]
    unique = {canonical_json(schema, allow_nan=False): schema for schema in schemas}
    if not unique:
        return {}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return {"anyOf": [unique[key] for key in sorted(unique)]}


def _validate_observations(value: Any, *, profile: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise TargetInputError("target observations must be an object")
    allowed = {
        "state",
        "read_observations",
        "target_profile_digest",
        "read_observation_input",
        "read_observation_diagnostics",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TargetInputError(
            "target observations contain unsupported fields: " + ", ".join(unknown)
        )
    profile_digest = value.get("target_profile_digest")
    if not is_sha256_hex(profile_digest):
        raise TargetInputError("target observations require target_profile_digest")
    state = value.get("state")
    if not isinstance(state, dict):
        raise TargetInputError("target observations state must be an object")
    read_observations = _read_observation_list(value)
    inventory = profile.get("inventory")
    profile_tool_names = (
        {tool.get("name") for tool in inventory.get("tools", []) if isinstance(tool, dict)}
        if isinstance(inventory, dict)
        else set()
    )
    for index, observation in enumerate(read_observations):
        _validate_read_observation(
            index,
            observation,
            profile_digest=profile_digest,
            profile_tool_names=profile_tool_names,
        )


def _read_observation_list(value: dict[str, Any]) -> list[Any]:
    read_observations = value.get("read_observations", [])
    if read_observations is None:
        read_observations = []
    if not isinstance(read_observations, list):
        raise TargetInputError("target observations read_observations must be a list")
    if len(read_observations) > 15:
        raise TargetInputError("target observations cannot contain more than 15 reads")
    return read_observations


def _validate_read_observation(
    index: int,
    observation: Any,
    *,
    profile_digest: str,
    profile_tool_names: set[Any],
) -> None:
    if not isinstance(observation, dict):
        raise TargetInputError(f"target observations read_observations[{index}] must be an object")
    if not isinstance(observation.get("profile_digest"), str) or (
        observation["profile_digest"] != profile_digest
    ):
        raise TargetInputError(
            f"target observations read_observations[{index}] profile digest does not match"
        )
    if not isinstance(observation.get("tool_name"), str) or not observation["tool_name"]:
        raise TargetInputError(
            f"target observations read_observations[{index}] requires tool_name"
        )
    if observation["tool_name"] not in profile_tool_names:
        raise TargetInputError(
            f"target observations read_observations[{index}] names an unknown tool"
        )
    _validate_read_outcome(index, observation)


def _validate_read_outcome(index: int, observation: dict[str, Any]) -> None:
    if not _is_string_mapping_or_none(observation.get("arguments")):
        raise TargetInputError(
            f"target observations read_observations[{index}] arguments must be a string mapping"
        )
    status = observation.get("status")
    if (
        not isinstance(status, dict)
        or status.get("transport") != "verified"
        or status.get("content") != "untrusted"
    ):
        raise TargetInputError(
            f"target observations read_observations[{index}] must be verified and untrusted"
        )
    result = observation.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        raise TargetInputError(
            f"target observations read_observations[{index}] result must be successful"
        )


def _is_string_mapping_or_none(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _validate_profile_contract(profile: Any) -> None:
    if not isinstance(profile, dict):
        raise TargetInputError("target profile must be an object")
    contract_root = _validate_contract_lock()
    schema = load_json_file(
        contract_root / "target-profile-v1" / "schema.json",
        TargetInputError,
        "cannot validate target profile contract",
    )
    try:
        error = first_schema_error(schema, profile)
    except Exception as exc:  # noqa: BLE001 - normalize validator failures
        raise TargetInputError(f"cannot validate target profile contract: {exc}") from exc
    if error is not None:
        raise TargetInputError(f"target profile schema invalid {error}")


def _validate_contract_lock() -> Path:
    verify_contract_lock(
        _CONTRACT_ROOT,
        TargetInputError,
        lock_label="target profile contract lock",
        member_label="target profile contract",
        metadata={
            "authority": "asago-scenario-generator",
            "contract": "target-profile",
            "schema_version": _PROFILE_SCHEMA_VERSION,
            "digest_domain": _PROFILE_DIGEST_DOMAIN,
        },
        metadata_message="target profile contract lock metadata is invalid",
    )
    return _CONTRACT_ROOT


def _validate_profile_digest(profile: dict[str, Any]) -> None:
    digest = profile.get("semantic_digest")
    if not isinstance(digest, str) or len(digest) != SHA256_HEX_LENGTH:
        raise TargetInputError("target profile semantic_digest is required")
    payload = deepcopy(profile)
    payload.pop("semantic_digest", None)
    try:
        expected = framed_digest(_PROFILE_DIGEST_DOMAIN, payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TargetInputError(
            "target profile contains a value that cannot be canonically encoded"
        ) from exc
    if digest != expected:
        raise TargetInputError(
            "target profile semantic_digest does not match canonical profile content"
        )
    inventory = profile.get("inventory")
    if (
        isinstance(inventory, dict)
        and isinstance(inventory.get("semantic_digest"), str)
        and profile.get("source_inventory_digest") != inventory["semantic_digest"]
    ):
        raise TargetInputError(
            "target profile source_inventory_digest does not match inventory semantic_digest"
        )


def _parse_document(path: Path, content: bytes, label: str) -> dict[str, Any]:
    try:
        value = parse_document(path, content, parse_constant=_reject_non_json_number)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
        raise TargetInputError(f"cannot parse {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TargetInputError(f"{label} must be an object")
    return value


def _read_file(path: Path, label: str) -> bytes:
    if not path.is_file():
        raise TargetInputError(f"{label} is not a file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TargetInputError(f"cannot read {label} {path}: {exc}") from exc


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


__all__ = ["TargetInputError", "load_target_inputs"]
