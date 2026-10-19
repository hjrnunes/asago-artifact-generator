"""The consumer-owned artifact-package-v4 contract.

The input adapter reads only the vendored scenario-handoff-v5 kit
(tests/test_handoff_v5.py), and the package writer and loader handle only
artifact-package-v4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_artifact_generator import package_io

_PACKAGE = Path(__file__).resolve().parents[1] / "contracts" / "artifact-package"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return _json(_PACKAGE / "artifact-package-v4" / "schema.json")


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": "artifact-package-v4",
        "package_id": "pkg-1",
        "scenario_id": "SCN-1",
        "input_kind": "scenario-handoff-v5",
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


def test_v4_schema_accepts_only_a_v5_input() -> None:
    schema = _schema()

    assert schema["properties"]["schema_version"] == {"const": "artifact-package-v4"}
    assert schema["properties"]["input_kind"] == {"enum": ["scenario-handoff-v5"]}
    jsonschema.validate(_manifest(input_kind="scenario-handoff-v5"), schema)
    for kind in ("scenario-handoff-v3", "scenario-handoff-v4", "other"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_manifest(input_kind=kind), schema)


def test_the_package_lock_names_v4_and_pins_its_schema() -> None:
    lock = _json(_PACKAGE / "CONTRACT.lock")

    assert lock["package_schema_version"] == "artifact-package-v4"
    assert lock["digest_domain"] == "artifact-package-v4"
    package_io.validate_artifact_package_contract()
