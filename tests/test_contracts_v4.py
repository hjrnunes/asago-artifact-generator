"""The vendored scenario-handoff-v4 kit and the consumer-owned artifact-package-v4 contract.

The input adapter reads the v4 kit (tests/test_handoff_v4.py) and the package
writer writes only artifact-package-v4; the package loader still reads v3.
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
    assert lock["handoff_schema_versions"][-1] == "scenario-handoff-v4"
    assert lock["digest_domains"]["scenario-handoff-v4"] == "scenario-handoff-v4"
    assert "attack_shape" in lock["metadata_fields"]
    input_adapter._validate_handoff_kit()


def test_the_vendored_v4_kit_has_the_producer_fixture_counts() -> None:
    assert len(list((_KIT / "valid").glob("*.json"))) == 12
    assert len(list((_KIT / "invalid").glob("*.json"))) == 91
    expected = _json(_KIT / "expected-violations.json")
    assert set(expected) == {f"invalid/{p.name}" for p in (_KIT / "invalid").glob("*.json")}


@pytest.mark.parametrize("fixture", sorted((_KIT / "valid").glob("*.json")), ids=lambda p: p.name)
def test_vendored_v4_schema_accepts_the_valid_fixtures(fixture: Path) -> None:
    jsonschema.validate(_json(fixture), _json(_KIT / "schema.json"))


def test_the_v3_handoff_kit_is_unchanged_by_the_v4_mirror() -> None:
    lock = _json(_HANDOFF / "CONTRACT.lock")

    v3 = {name: digest for name, digest in lock["files"].items() if name.startswith("handoff-v3/")}
    assert len(v3) == 78
    assert v3["handoff-v3/schema.json"] == (
        "c6fd2083ff70bf0810be58e395385d39a2ad06c3bb40c5fde998c8375b92fb2c"
    )
    assert input_adapter._HANDOFF_SCHEMA_VERSION == "scenario-handoff-v4"


# --- artifact-package-v4 ---------------------------------------------------------


def _schema(version: str) -> dict[str, Any]:
    return _json(_PACKAGE / f"artifact-package-{version}" / "schema.json")


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


def test_v4_schema_accepts_v3_and_v4_inputs_and_nothing_else() -> None:
    schema = _schema("v4")

    assert schema["properties"]["schema_version"] == {"const": "artifact-package-v4"}
    assert schema["properties"]["input_kind"] == {
        "enum": ["scenario-handoff-v3", "scenario-handoff-v4"]
    }
    for kind in ("scenario-handoff-v3", "scenario-handoff-v4"):
        jsonschema.validate(_manifest(input_kind=kind), schema)
    for kind in ("scenario-handoff-v1", "scenario-handoff-v2", "other"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_manifest(input_kind=kind), schema)


def test_v4_schema_matches_v3_except_for_its_version_and_input_kinds() -> None:
    v3, v4 = _schema("v3"), _schema("v4")

    assert v4["required"] == v3["required"]
    assert v4["additionalProperties"] is False
    assert v4["$defs"] == v3["$defs"]
    assert set(v4["properties"]) == set(v3["properties"])
    differing = {key for key in v3["properties"] if v3["properties"][key] != v4["properties"][key]}
    assert differing == {"schema_version", "input_kind"}
    assert v4["title"] == "artifact-package-v4 directory manifest"
    assert "seed.json" in v4["description"]


def test_a_v3_manifest_still_validates_under_v3_and_not_under_v4() -> None:
    manifest = _manifest(schema_version="artifact-package-v3", input_kind="scenario-handoff-v3")

    jsonschema.validate(manifest, _schema("v3"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, _schema("v4"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_manifest(), _schema("v3"))


def test_the_package_lock_adds_v4_and_keeps_the_v3_entry_and_singular_fields() -> None:
    lock = _json(_PACKAGE / "CONTRACT.lock")

    assert lock["package_schema_version"] == "artifact-package-v3"
    assert lock["digest_domain"] == "artifact-package-v3"
    assert lock["package_schema_versions"] == ["artifact-package-v3", "artifact-package-v4"]
    assert lock["digest_domains"] == {
        "artifact-package-v3": "artifact-package-v3",
        "artifact-package-v4": "artifact-package-v4",
    }
    schemas = {key: digest for key, digest in lock["files"].items() if key.endswith("schema.json")}
    assert schemas == {
        "artifact-package-v3/schema.json": (
            "af47372909554dbca706755ed3fc55bea7b164a4fe91ec74c526f0ca74859578"
        ),
        "artifact-package-v4/schema.json": sha256_hex(
            (_PACKAGE / "artifact-package-v4" / "schema.json").read_bytes()
        ),
    }
    package_io.validate_artifact_package_contract()


def test_the_package_writer_writes_only_v4() -> None:
    assert package_io.PACKAGE_SCHEMA_VERSION == "artifact-package-v4"
    assert not hasattr(package_io, "package_schema_version_for")
