"""The ``tool-bundle-v1`` schema: what every adapter's ``bundle.json`` must satisfy."""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

import jsonschema

SCHEMA_VERSION = "tool-bundle-v1"
SCHEMA_FILE = "schemas/tool-bundle-v1.schema.json"


@cache
def _schema_text() -> str:
    return files("asago_bundle_core").joinpath(SCHEMA_FILE).read_text(encoding="utf-8")


def load_schema() -> dict[str, Any]:
    """Return a fresh copy of the schema document."""

    return json.loads(_schema_text())


def manifest_errors(manifest: Any) -> list[str]:
    """Return one message per schema violation, ordered by location; empty when valid."""

    validator = jsonschema.Draft202012Validator(load_schema())
    errors = sorted(validator.iter_errors(manifest), key=lambda e: [str(part) for part in e.path])
    return [error.message for error in errors]


def validate_manifest(manifest: Any) -> None:
    """Raise ``jsonschema.ValidationError`` when ``manifest`` breaks the schema."""

    jsonschema.Draft202012Validator(load_schema()).validate(manifest)
