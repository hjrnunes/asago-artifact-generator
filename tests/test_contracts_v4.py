"""The vendored scenario-handoff-v4 kit and the consumer-owned artifact-package-v4 contract.

The input adapter reads the v4 kit (tests/test_handoff_v4.py), and the package
writer and loader handle only artifact-package-v4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_artifact_generator import input_adapter, package_io
from asago_artifact_generator.contract_kit import sha256_hex

_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
_HANDOFF = _CONTRACTS / "scenario-handoff"
_KIT = _HANDOFF / "handoff-v4"
_PACKAGE = _CONTRACTS / "artifact-package"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _kit_files() -> list[Path]:
    return sorted(path for path in _KIT.rglob("*.json"))


def test_the_handoff_lock_lists_every_v4_file_with_its_digest() -> None:
    lock = _json(_HANDOFF / "CONTRACT.lock")

    locked = {name for name in lock["files"] if name.startswith("handoff-v4/")}
    assert locked == {str(path.relative_to(_HANDOFF)) for path in _kit_files()}
    for name in locked:
        assert lock["files"][name] == sha256_hex((_HANDOFF / name).read_bytes()), name
    assert lock["handoff_schema_version"] == "scenario-handoff-v4"
    assert lock["digest_domain"] == "scenario-handoff-v4"
    assert "attack_shape" in lock["metadata_fields"]
    input_adapter._validate_handoff_kit()


def test_the_vendored_v4_kit_has_the_producer_fixture_counts() -> None:
    assert len(list((_KIT / "valid").glob("*.json"))) == 12
    assert len(list((_KIT / "invalid").glob("*.json"))) == 93
    expected = _json(_KIT / "expected-violations.json")
    assert set(expected) == {f"invalid/{p.name}" for p in (_KIT / "invalid").glob("*.json")}


@pytest.mark.parametrize("fixture", sorted((_KIT / "valid").glob("*.json")), ids=lambda p: p.name)
def test_vendored_v4_schema_accepts_the_valid_fixtures(fixture: Path) -> None:
    jsonschema.validate(_json(fixture), _json(_KIT / "schema.json"))


# --- artifact-package-v4 ---------------------------------------------------------


def _schema() -> dict[str, Any]:
    return _json(_PACKAGE / "artifact-package-v4" / "schema.json")


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": "artifact-package-v4",
        "package_id": "pkg-1",
        "scenario_id": "SCN-1",
        "input_kind": "scenario-handoff-v4",
        "source_digests": {"input": "a" * 64},
        "members": [
            {
                "path": "stimulus.json",
                "media_type": "application/json",
                "length": 2,
                "sha256": "b" * 64,
            }
        ],
        "authoring": {},
        "runtime_capabilities": {},
        "creation_model": {},
        "manifest_digest": "c" * 64,
    }
    manifest.update(overrides)
    return manifest


def test_v4_schema_accepts_only_a_v4_input() -> None:
    schema = _schema()

    assert schema["properties"]["schema_version"] == {"const": "artifact-package-v4"}
    assert schema["properties"]["input_kind"] == {"const": "scenario-handoff-v4"}
    jsonschema.validate(_manifest(input_kind="scenario-handoff-v4"), schema)
    for kind in ("scenario-handoff-v1", "scenario-handoff-v2", "scenario-handoff-v3", "other"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_manifest(input_kind=kind), schema)


def test_the_package_lock_names_v4_and_pins_its_schema() -> None:
    lock = _json(_PACKAGE / "CONTRACT.lock")

    assert lock["package_schema_version"] == "artifact-package-v4"
    assert lock["digest_domain"] == "artifact-package-v4"
    schemas = {key: digest for key, digest in lock["files"].items() if key.endswith("schema.json")}
    assert schemas == {
        "artifact-package-v4/schema.json": sha256_hex(
            (_PACKAGE / "artifact-package-v4" / "schema.json").read_bytes()
        ),
    }
    package_io.validate_artifact_package_contract()
