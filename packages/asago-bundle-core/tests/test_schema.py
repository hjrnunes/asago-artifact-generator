"""The installed tool-bundle schema and manifest reader rules.

``manifest_errors`` reads the contract before applying the schema; ``validate_manifest``
checks the payload without reading the envelope.
"""

from __future__ import annotations

import copy
from typing import Any

import jsonschema
import pytest
from asago_contracts_core import write_envelope

from asago_bundle_core.bundle_contract import VERSION, installed_specs
from asago_bundle_core.schema import (
    SCHEMA_VERSION,
    load_schema,
    manifest_errors,
    validate_manifest,
)

DIGEST = "ab" * 32
VERSIONS = (SCHEMA_VERSION,)


def garak_manifest(version: str = SCHEMA_VERSION) -> dict[str, Any]:
    manifest = {
        "schema_version": version,
        "tool": "garak",
        "tool_revision_range": ">=968cc224",
        "package_id": "SCN-001-SCN-001",
        "scenario_id": "SCN-001",
        "package_digest": DIGEST,
        "claim_level": "command_attempt",
        "plugins": ["injection.IndirectInjection", "toolcall.ToolCallCondition"],
        "environment": ["OPENAI_API_KEY"],
        "templates": {"conversation": "conversation.template.json", "run": "run.template.json"},
        "entrypoint": ["{tool_python}", "-m", "garak", "--config", "{bundle}/run.yaml"],
        "native_outputs": ["reports/SCN-001.report.jsonl"],
    }
    manifest["conversation_mode"] = "single"
    manifest["values_required"] = ["allowed_tools", "gateway_url", "mcp_url", "messages", "model"]
    return manifest


def with_change(**changes: Any) -> dict[str, Any]:
    manifest = garak_manifest()
    manifest.update(changes)
    return manifest


def without(key: str) -> dict[str, Any]:
    manifest = garak_manifest()
    del manifest[key]
    return manifest


def enveloped(manifest: dict[str, Any]) -> dict[str, Any]:
    spec = installed_specs().spec("tool-bundle", manifest["schema_version"])
    return write_envelope(manifest, spec, writer="tests.schema", writer_version="0.1.0")


def stamped(manifest: dict[str, Any]) -> dict[str, Any]:
    """Add the v2 envelope without validating, as a test of a broken payload needs."""

    spec = installed_specs().spec("tool-bundle", SCHEMA_VERSION)
    return {
        **manifest,
        "contract": {
            "id": spec.contract,
            "version": spec.version,
            "spec_digest": spec.spec_digest,
            "requires": [],
            "provenance": {
                "writer": "tests.schema",
                "writer_version": "0.1.0",
                "validator_version": "asago-contracts-core 0.1.0",
            },
        },
    }


def read_errors(manifest: dict[str, Any]) -> list[str]:
    """Return what the reader says about a payload, stamped as a v2 writer stamps it."""

    return manifest_errors(manifest if "contract" in manifest else stamped(manifest))


def test_the_installed_schema_loads() -> None:
    current = load_schema()

    assert current["$id"] == SCHEMA_VERSION == VERSION == "tool-bundle-v2"
    assert {"conversation_mode", "values_required"} <= current["properties"].keys()
    assert {"delivery", "requires"}.isdisjoint(current["properties"])
    jsonschema.Draft202012Validator.check_schema(current)


@pytest.mark.parametrize("version", VERSIONS)
def test_a_garak_manifest_reads(version: str) -> None:
    manifest = garak_manifest(version)

    assert manifest_errors(enveloped(manifest)) == []


def test_a_v2_manifest_without_the_envelope_is_refused() -> None:
    errors = manifest_errors(garak_manifest())

    assert errors
    assert "envelope" in errors[0]


def test_validate_manifest_checks_the_current_payload() -> None:
    validate_manifest(garak_manifest())

    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(garak_manifest("tool-bundle-v9"))


def test_a_concrete_manifest_with_a_values_digest_is_valid() -> None:
    validate_manifest(with_change(values_digest=DIGEST, templates={}))


@pytest.mark.parametrize("key", sorted(load_schema()["required"]))
def test_every_required_key_is_required(key: str) -> None:
    errors = read_errors(without(key))

    assert errors, f"a manifest without {key} must be refused"
    assert key in errors[0]


@pytest.mark.parametrize(
    "manifest",
    [
        with_change(surprise=1),
        with_change(package_digest="not-a-digest"),
        with_change(package_digest="AB" * 32),
        with_change(values_digest="short"),
        with_change(schema_version="tool-bundle-v9"),
        with_change(claim_level="tool_call"),
        with_change(conversation_mode="parallel"),
        with_change(entrypoint=[]),
        with_change(environment=["lower_case"]),
        with_change(values_required=["model", "model"]),
        with_change(tool=""),
    ],
    ids=[
        "unknown-key",
        "short-digest",
        "uppercase-digest",
        "short-values-digest",
        "other-version",
        "claim-level-not-a-value",
        "conversation-mode-not-a-value",
        "empty-entrypoint",
        "environment-name-lowercase",
        "duplicate-value-key",
        "empty-tool",
    ],
)
def test_an_invalid_manifest_is_refused(manifest: dict[str, Any]) -> None:
    assert read_errors(manifest)
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
    assert read_errors(with_change(**changes)) == []


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
    assert read_errors(with_change(**changes))


def test_the_optional_fields_stay_optional() -> None:
    required = set(load_schema()["required"])

    assert {"repeats", "serve"}.isdisjoint(required)


def test_the_table_does_not_mutate_its_input() -> None:
    manifest = garak_manifest()
    before = copy.deepcopy(manifest)

    manifest_errors(enveloped(manifest))

    assert manifest == before
