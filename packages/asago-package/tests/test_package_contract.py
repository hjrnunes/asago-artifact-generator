"""The artifact-package authority contract: v5, which the writer writes and the reader loads.

The v4 schema stays frozen alongside v5 for the contract cases and the orchestrator's
mirror; the reader refuses a v4 package. The writer writes only artifact-package-v1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_package import package_io
from asago_package.contract_kit import contract_dir

_PACKAGE = contract_dir("artifact-package")
_PACKAGE_VERSION = "artifact-package-v1"
_HANDOFF_INPUT = "scenario-handoff-v1"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(version: str) -> dict[str, Any]:
    return _json(_PACKAGE / version / "schema.json")


def _manifest(version: str, **overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": version,
        "package_id": "pkg-1",
        "scenario_id": "SCN-1",
        "input_kind": _HANDOFF_INPUT,
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


def test_the_schema_accepts_only_its_handoff_input() -> None:
    schema = _schema(_PACKAGE_VERSION)
    accepted = _HANDOFF_INPUT

    assert schema["properties"]["schema_version"] == {"const": _PACKAGE_VERSION}
    assert schema["properties"]["input_kind"] == {"enum": [accepted]}
    jsonschema.validate(_manifest(_PACKAGE_VERSION, input_kind=accepted), schema)
    for kind in ("scenario-handoff-v9", "other"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_manifest(_PACKAGE_VERSION, input_kind=kind), schema)


def test_the_schema_lists_exactly_the_members_the_writer_writes() -> None:
    path = _schema(_PACKAGE_VERSION)["$defs"]["member"]["properties"]["path"]

    assert set(path["enum"]) == package_io._ALLOWED_MEMBER_NAMES


@pytest.mark.parametrize(
    "member",
    ["source-hashes.json", "authoring/01-call1.json", "authoring/transformations.json"],
)
def test_a_retired_member_fails_the_schema(member: str) -> None:
    with pytest.raises(jsonschema.ValidationError, match="is not one of"):
        jsonschema.validate(
            _manifest(_PACKAGE_VERSION, members=_members(member)), _schema(_PACKAGE_VERSION)
        )


def test_the_schema_closes_the_authoring_outcome_and_empties_the_creation_model() -> None:
    schema = _schema(_PACKAGE_VERSION)
    outcome = {"status": "accepted", "terminal_status": "accepted"}

    jsonschema.validate(
        _manifest(
            _PACKAGE_VERSION, authoring={**outcome, "review_status": {"artifact": "accepted"}}
        ),
        schema,
    )
    for authoring in (
        {**outcome, "attempts": 4},
        {**outcome, "review_status": {"artifact": "accepted", "plan": "accepted"}},
    ):
        with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
            jsonschema.validate(_manifest(_PACKAGE_VERSION, authoring=authoring), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_manifest(_PACKAGE_VERSION, creation_model={"model": "m"}), schema)


def test_the_package_lock_names_and_pins_the_current_schema() -> None:
    lock = _json(_PACKAGE / "CONTRACT.lock")

    assert lock["package_schema_version"] == _PACKAGE_VERSION
    assert lock["digest_domain"] == _PACKAGE_VERSION
    assert f"{_PACKAGE_VERSION}/schema.json" in lock["files"]
    assert not any(key.startswith("artifact-package-v9/") for key in lock["files"])
    package_io.validate_artifact_package_contract()
