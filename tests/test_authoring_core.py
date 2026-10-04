from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.authoring.core import (
    ArtifactValidationError,
    PlanValidationError,
    _findings_from_error,
    _is_json_value,
    _matches_schema_type,
)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PlanValidationError("plain"), "plan_validation"),
        (ArtifactValidationError("plain"), "artifact_validation"),
        (PlanValidationError("unknown_reference: x"), "unknown_reference"),
        (PlanValidationError("plan_conflict: x"), "plan_conflict"),
        (PlanValidationError("undocumented selector x"), "undocumented_selector"),
        (PlanValidationError("an argument type mismatch"), "schema_type_mismatch"),
        (PlanValidationError("schema_type_mismatch: x"), "schema_type_mismatch"),
        (PlanValidationError("operation not permitted"), "unpermitted_setup"),
        (PlanValidationError("non_user_history: x"), "non_user_history"),
        (PlanValidationError("see unknown_reference: x"), "plan_validation"),
    ],
)
def test_findings_from_error_codes(exc: Exception, code: str) -> None:
    [finding] = _findings_from_error(exc)
    assert (finding.code, finding.detail) == (code, str(exc))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("a", True),
        (1.5, True),
        (float("nan"), False),
        ([1, {"a": [None]}], True),
        ([1, float("nan")], False),
        ({"a": 1}, True),
        ({1: "a"}, False),
        ({"a": object()}, False),
        (object(), False),
    ],
)
def test_is_json_value(value: Any, expected: bool) -> None:
    assert _is_json_value(value) is expected


@pytest.mark.parametrize(
    ("value", "schema_type", "expected"),
    [
        ("x", "string", True),
        (1, "string", False),
        (True, "boolean", True),
        (1, "boolean", False),
        (1, "integer", True),
        (True, "integer", False),
        (1.5, "integer", False),
        (1.5, "number", True),
        (True, "number", False),
        ({}, "object", True),
        ([], "object", False),
        ([], "array", True),
        ({}, "array", False),
        ("{{slot}}", "integer", True),
        (1, "unknown", True),
        (1, ["string", "null"], True),
    ],
)
def test_matches_schema_type(value: Any, schema_type: Any, expected: bool) -> None:
    assert _matches_schema_type(value, schema_type) is expected
