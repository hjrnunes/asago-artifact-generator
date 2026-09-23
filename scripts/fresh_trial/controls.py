"""Frozen supplied-control loading and candidate-local remapping."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asago_artifact_generator.detector_controls import ControlCase

from .errors import TrialInputError

_CONTROLS_SCHEMA_VERSION = "fresh-five-case-controls-v1"


@dataclass(frozen=True)
class ControlSuite:
    """One frozen case-control file, including unresolved non-verdict rows."""

    cases: tuple[dict[str, Any], ...]
    unresolved: tuple[dict[str, Any], ...]
    sha256: str


def load_control_suite(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> ControlSuite:
    """Load frozen control rows without converting unresolved rows to verdicts."""

    control_path = Path(path)
    try:
        control_bytes = control_path.read_bytes()
        document = json.loads(control_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(f"cannot read frozen controls: {control_path}") from exc
    actual_sha256 = hashlib.sha256(control_bytes).hexdigest()
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise TrialInputError(f"control sha256 mismatch for {control_path}")
    if not isinstance(document, dict) or document.get("schema_version") != (
        _CONTROLS_SCHEMA_VERSION
    ):
        raise TrialInputError(f"unsupported frozen controls schema: {control_path}")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise TrialInputError(f"frozen controls cases must be a list: {control_path}")
    cases: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise TrialInputError(f"control row {index} must be an object: {control_path}")
        name = raw_case.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise TrialInputError(f"control row {index} has a missing or duplicate name")
        names.add(name)
        if (
            raw_case.get("status") == "unresolved"
            or raw_case.get("expected_outcome") == "unresolved"
        ):
            unresolved.append(copy.deepcopy(raw_case))
            continue
        evidence = raw_case.get("evidence")
        expected = raw_case.get("expected_outcome")
        claim_level = raw_case.get("expected_claim_level")
        if not isinstance(evidence, dict) or expected not in {
            "detected",
            "not_detected",
            "inconclusive",
        }:
            raise TrialInputError(f"control row {name!r} is not a verdict case")
        if claim_level is not None and (
            not isinstance(claim_level, str) or not claim_level.strip()
        ):
            raise TrialInputError(f"control row {name!r} has an invalid claim level")
        cases.append(copy.deepcopy(raw_case))
    return ControlSuite(tuple(cases), tuple(unresolved), actual_sha256)


def load_control_cases(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[ControlCase, ...]:
    """Load exact frozen expectations for preflight inspection and tests."""

    suite = load_control_suite(path, expected_sha256=expected_sha256)
    return tuple(
        ControlCase(
            name=record["name"],
            evidence=copy.deepcopy(record["evidence"]),
            expected_outcome=record["expected_outcome"],
            expected_claim_level=record.get("expected_claim_level"),
        )
        for record in suite.cases
    )


def remap_control_cases(
    records: Sequence[dict[str, Any]],
    plan: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[ControlCase, ...]:
    """Apply frozen binding-name and synthetic record-ID remaps mechanically."""

    del metadata  # The current frozen remap contract derives names from Call 1 only.
    declarations = plan.get("runtime_bindings")
    if not isinstance(declarations, list):
        declarations = []
    resolved: list[ControlCase] = []
    for record in records:
        evidence = copy.deepcopy(record["evidence"])
        remap = record.get("binding_remap", {})
        if not isinstance(remap, dict):
            raise TrialInputError(f"control {record['name']!r} binding_remap must be an object")
        binding_values = evidence.get("bindings", {})
        if not isinstance(binding_values, dict):
            raise TrialInputError(f"control {record['name']!r} bindings must be an object")
        binding_names: dict[str, str] = {}
        for fixture_name, descriptor in remap.items():
            if not isinstance(fixture_name, str) or not isinstance(descriptor, dict):
                raise TrialInputError(f"control {record['name']!r} has an invalid binding remap")
            matches = [
                item
                for item in declarations
                if isinstance(item, Mapping)
                and item.get("source_kind") == descriptor.get("source_kind")
                and item.get("source_ref") == descriptor.get("source_ref")
                and item.get("selector") == descriptor.get("selector")
            ]
            if len(matches) != 1 or not isinstance(matches[0].get("name"), str):
                raise TrialInputError(
                    f"control {record['name']!r} binding source is missing or ambiguous: "
                    f"{descriptor.get('source_ref')!r}"
                )
            candidate_name = matches[0]["name"]
            if fixture_name in binding_values:
                binding_values[candidate_name] = binding_values.pop(fixture_name)
            binding_names[fixture_name] = candidate_name
        dynamic_ids = record.get("dynamic_record_ids", {})
        if not isinstance(dynamic_ids, dict):
            raise TrialInputError(
                f"control {record['name']!r} dynamic_record_ids must be an object"
            )
        for placeholder, binding_alias in dynamic_ids.items():
            if not isinstance(placeholder, str) or not isinstance(binding_alias, str):
                raise TrialInputError(
                    f"control {record['name']!r} has an invalid dynamic record ID remap"
                )
            evidence_binding = binding_names.get(binding_alias, binding_alias)
            if evidence_binding not in binding_values:
                raise TrialInputError(
                    f"control {record['name']!r} lacks synthetic binding for {binding_alias!r}"
                )
            evidence_value = binding_values[evidence_binding]
            _replace_control_marker(evidence, f"{{{{record_id:{placeholder}}}}}", evidence_value)
        resolved.append(
            ControlCase(
                name=record["name"],
                evidence=evidence,
                expected_outcome=record["expected_outcome"],
                expected_claim_level=record.get("expected_claim_level"),
            )
        )
    return tuple(resolved)


def _replace_control_marker(value: Any, marker: str, replacement: Any) -> Any:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            value[key] = _replace_control_marker(item, marker, replacement)
        return value
    if isinstance(value, list):
        return [_replace_control_marker(item, marker, replacement) for item in value]
    if isinstance(value, str) and value == marker:
        return copy.deepcopy(replacement)
    return value
