"""tool-bundle-v1: the schema loads from core and decides which manifests are valid."""

from __future__ import annotations

import copy
from typing import Any

import jsonschema
import pytest

from asago_bundle_core.schema import (
    SCHEMA_VERSION,
    load_schema,
    manifest_errors,
    validate_manifest,
)

DIGEST = "ab" * 32


def garak_manifest() -> dict[str, Any]:
    return {
        "schema_version": "tool-bundle-v1",
        "tool": "garak",
        "tool_revision_range": ">=968cc224",
        "package_id": "SCN-001-SCN-001",
        "scenario_id": "SCN-001",
        "package_digest": DIGEST,
        "claim_level": "command_attempt",
        "delivery": "single",
        "target_mode": "orch_hosted",
        "plugins": ["injection.IndirectInjection", "toolcall.ToolCallCondition"],
        "requires": ["allowed_tools", "gateway_url", "mcp_url", "messages", "model"],
        "environment": ["OPENAI_API_KEY"],
        "templates": {"conversation": "conversation.template.json", "run": "run.template.json"},
        "entrypoint": ["{tool_python}", "-m", "garak", "--config", "{bundle}/run.yaml"],
        "native_outputs": ["reports/SCN-001.report.jsonl"],
    }


def with_change(**changes: Any) -> dict[str, Any]:
    manifest = garak_manifest()
    manifest.update(changes)
    return manifest


def without(key: str) -> dict[str, Any]:
    manifest = garak_manifest()
    del manifest[key]
    return manifest


def test_schema_loads_from_core_and_names_its_version() -> None:
    schema = load_schema()

    assert schema["$id"] == SCHEMA_VERSION == "tool-bundle-v1"
    jsonschema.Draft202012Validator.check_schema(schema)


def test_a_garak_manifest_is_valid() -> None:
    validate_manifest(garak_manifest())

    assert manifest_errors(garak_manifest()) == []


def test_a_concrete_manifest_with_a_values_digest_is_valid() -> None:
    validate_manifest(with_change(values_digest=DIGEST, templates={}))


@pytest.mark.parametrize("key", sorted(load_schema()["required"]))
def test_every_required_key_is_required(key: str) -> None:
    errors = manifest_errors(without(key))

    assert errors, f"a manifest without {key} must be refused"
    assert key in errors[0]


@pytest.mark.parametrize(
    "manifest",
    [
        with_change(surprise=1),
        with_change(package_digest="not-a-digest"),
        with_change(package_digest="AB" * 32),
        with_change(values_digest="short"),
        with_change(schema_version="tool-bundle-v2"),
        with_change(claim_level="tool_call"),
        with_change(delivery="parallel"),
        with_change(target_mode="remote"),
        with_change(entrypoint=[]),
        with_change(environment=["lower_case"]),
        with_change(requires=["model", "model"]),
        with_change(tool=""),
    ],
    ids=[
        "unknown-key",
        "short-digest",
        "uppercase-digest",
        "short-values-digest",
        "other-version",
        "claim-level-not-a-value",
        "delivery-not-a-value",
        "target-mode-not-a-value",
        "empty-entrypoint",
        "environment-name-lowercase",
        "duplicate-require",
        "empty-tool",
    ],
)
def test_an_invalid_manifest_is_refused(manifest: dict[str, Any]) -> None:
    assert manifest_errors(manifest)
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(manifest)


SERVE = {
    "argv": ["{tool_python}", "-m", "serve", "--port", "{service_port}"],
    "ready_url": "http://127.0.0.1:{service_port}/health",
}


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_level": "state_effect"},
        {"repeats": {"native": True}},
        {"repeats": {"native": False}},
        {"serve": SERVE},
        {"claim_level": "state_effect", "repeats": {"native": True}, "serve": SERVE},
    ],
    ids=["state-effect", "repeats-native", "repeats-not-native", "serve", "all-three"],
)
def test_the_optional_fields_are_valid(changes: dict[str, Any]) -> None:
    assert manifest_errors(with_change(**changes)) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"repeats": 0},
        {"repeats": 3},
        {"repeats": True},
        {"repeats": {}},
        {"repeats": {"native": "yes"}},
        {"repeats": {"native": True, "count": 3}},
        {"serve": {"ready_url": SERVE["ready_url"]}},
        {"serve": {"argv": SERVE["argv"]}},
        {"serve": {**SERVE, "entrypoint": ["x"]}},
        {"serve": {"argv": [], "ready_url": SERVE["ready_url"]}},
        {"serve": {"argv": SERVE["argv"], "ready_url": ""}},
        {"serve": "midojo-serve"},
        {"phases": {"serve": SERVE}},
    ],
    ids=[
        "repeats-zero",
        "repeats-integer",
        "repeats-boolean",
        "repeats-without-native",
        "repeats-native-not-boolean",
        "repeats-unknown-key",
        "serve-without-argv",
        "serve-without-ready-url",
        "serve-unknown-key",
        "serve-empty-argv",
        "serve-empty-ready-url",
        "serve-not-an-object",
        "phases-is-not-a-field",
    ],
)
def test_a_malformed_optional_field_is_refused(changes: dict[str, Any]) -> None:
    assert manifest_errors(with_change(**changes))


def test_the_optional_fields_stay_optional() -> None:
    required = set(load_schema()["required"])

    assert {"repeats", "serve"}.isdisjoint(required)


def test_the_table_does_not_mutate_its_input() -> None:
    manifest = garak_manifest()
    before = copy.deepcopy(manifest)

    manifest_errors(manifest)

    assert manifest == before
