"""Canonical text, the error base, the capability-gap record and the value helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from asago_bundle_core.errors import BundleError
from asago_bundle_core.gap import (
    EXIT_CAPABILITY_GAP,
    EXIT_INPUT,
    EXIT_OK,
    CapabilityGap,
    capability_gap_record,
)
from asago_bundle_core.text import canonical_text
from asago_bundle_core.values import check_values, render_entrypoint


class AdapterError(BundleError):
    """A stand-in for an adapter's own error."""


def is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


CHECKS = {"model": is_text}


def check_for(key: str) -> Any:
    return CHECKS.get(key)


def test_canonical_text_sorts_keys_indents_and_keeps_unicode() -> None:
    assert canonical_text({"b": 1, "a": "é"}) == '{\n  "a": "é",\n  "b": 1\n}\n'


def test_the_error_base_is_a_value_error() -> None:
    assert issubclass(BundleError, ValueError)
    assert isinstance(AdapterError("x"), BundleError)


def test_exit_codes_keep_orchs_numbers() -> None:
    assert (EXIT_OK, EXIT_INPUT, EXIT_CAPABILITY_GAP) == (0, 1, 3)


def test_the_gap_record_has_the_seven_fields_orch_reads() -> None:
    package = SimpleNamespace(
        manifest=SimpleNamespace(
            package_id="SCN-001-SCN-001", scenario_id="SCN-001", manifest_digest="ab" * 32
        )
    )

    record = capability_gap_record("garak", package, "sequential", "the reason")

    assert record == {
        "kind": "capability_gap",
        "tool": "garak",
        "package_id": "SCN-001-SCN-001",
        "scenario_id": "SCN-001",
        "package_digest": "ab" * 32,
        "delivery": "sequential",
        "reason": "the reason",
    }


def test_a_capability_gap_carries_its_record_and_names_the_reason() -> None:
    record = {"kind": "capability_gap", "reason": "cannot deliver"}

    gap = CapabilityGap(record)

    assert gap.record is record
    assert str(gap) == "cannot deliver"


def test_check_values_accepts_valid_values() -> None:
    check_values(["model"], {"model": "m", "extra": 1}, check_for, AdapterError)


def test_check_values_names_the_missing_key() -> None:
    with pytest.raises(AdapterError, match="values lack model"):
        check_values(["model"], {}, check_for, AdapterError)


def test_check_values_names_the_malformed_key() -> None:
    with pytest.raises(AdapterError, match="values carry a malformed model"):
        check_values(["model"], {"model": " "}, check_for, AdapterError)


def test_check_values_requires_an_object() -> None:
    with pytest.raises(AdapterError, match="values must be a JSON object"):
        check_values([], ["model"], check_for, AdapterError)


def test_check_values_skips_a_key_without_a_check() -> None:
    check_values(["free"], {"free": object()}, check_for, AdapterError)


def test_render_entrypoint_fills_bundle_and_listed_keys_and_leaves_tool_python() -> None:
    rendered = render_entrypoint(
        ["{tool_python}", "-m", "tool", "--config", "{bundle}/run.yaml", "--name", "{model}"],
        Path("/out/b"),
        {"model": "gemma", "other": "x"},
        ("model",),
    )

    assert rendered == [
        "{tool_python}",
        "-m",
        "tool",
        "--config",
        "/out/b/run.yaml",
        "--name",
        "gemma",
    ]


def test_render_entrypoint_leaves_an_unlisted_key() -> None:
    rendered = render_entrypoint(["{other}"], Path("/out"), {"other": "x"}, ())

    assert rendered == ["{other}"]
