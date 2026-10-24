"""The consumer-owned artifact-package contract: v5, which the writer writes, and v4.

The input adapter reads only the vendored scenario-handoff-v5 kit
(tests/test_handoff_v5.py). The package writer writes only artifact-package-v5;
the loader reads artifact-package-v5 and artifact-package-v4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_artifact_generator import package_io

_PACKAGE = Path(__file__).resolve().parents[1] / "contracts" / "artifact-package"
_V4 = "artifact-package-v4"
_V5 = "artifact-package-v5"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(version: str) -> dict[str, Any]:
    return _json(_PACKAGE / version / "schema.json")


def _manifest(version: str, **overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": version,
        "package_id": "pkg-1",
        "scenario_id": "SCN-1",
        "input_kind": "scenario-handoff-v5",
        "source_digests": {"input": "a" * 64},
        "members": _members("stimulus.json"),
        "authoring": {},
        "runtime_capabilities": {},
        "creation_model": {},
        "manifest_digest": "c" * 64,
    }
    manifest.update(overrides)
    return manifest


def _members(path: str) -> list[dict[str, Any]]:
    return [{"path": path, "media_type": "application/json", "length": 2, "sha256": "b" * 64}]


@pytest.mark.parametrize("version", [_V4, _V5])
def test_each_schema_accepts_only_a_v5_input(version: str) -> None:
    schema = _schema(version)

    assert schema["properties"]["schema_version"] == {"const": version}
    assert schema["properties"]["input_kind"] == {"enum": ["scenario-handoff-v5"]}
    jsonschema.validate(_manifest(version, input_kind="scenario-handoff-v5"), schema)
    for kind in ("scenario-handoff-v3", "scenario-handoff-v4", "other"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_manifest(version, input_kind=kind), schema)


def test_the_v5_schema_lists_exactly_the_members_the_writer_writes() -> None:
    path = _schema(_V5)["$defs"]["member"]["properties"]["path"]

    assert set(path["enum"]) == package_io._ALLOWED_MEMBER_NAMES


@pytest.mark.parametrize(
    "member",
    ["source-hashes.json", "authoring/01-call1.json", "authoring/transformations.json"],
)
def test_a_retired_member_passes_the_v4_schema_and_fails_the_v5_schema(member: str) -> None:
    jsonschema.validate(_manifest(_V4, members=_members(member)), _schema(_V4))
    with pytest.raises(jsonschema.ValidationError, match="is not one of"):
        jsonschema.validate(_manifest(_V5, members=_members(member)), _schema(_V5))


def test_the_v5_schema_closes_the_authoring_outcome_and_empties_the_creation_model() -> None:
    schema = _schema(_V5)
    outcome = {"status": "accepted", "terminal_status": "accepted"}

    jsonschema.validate(
        _manifest(_V5, authoring={**outcome, "review_status": {"artifact": "accepted"}}), schema
    )
    for authoring in (
        {**outcome, "attempts": 4},
        {**outcome, "review_status": {"artifact": "accepted", "plan": "accepted"}},
    ):
        jsonschema.validate(_manifest(_V4, authoring=authoring), _schema(_V4))
        with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
            jsonschema.validate(_manifest(_V5, authoring=authoring), schema)
    jsonschema.validate(_manifest(_V4, creation_model={"model": "m"}), _schema(_V4))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_manifest(_V5, creation_model={"model": "m"}), schema)


def test_the_package_lock_names_v5_and_pins_both_schemas() -> None:
    lock = _json(_PACKAGE / "CONTRACT.lock")

    assert lock["package_schema_version"] == _V5
    assert lock["digest_domain"] == _V5
    assert {f"{_V4}/schema.json", f"{_V5}/schema.json"} <= set(lock["files"])
    package_io.validate_artifact_package_contract()
