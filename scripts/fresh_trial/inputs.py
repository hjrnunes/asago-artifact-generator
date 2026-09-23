"""Frozen trial input loading, pin verification, and supplied controls."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from asago_artifact_generator.detector_controls import ControlCase
from asago_artifact_generator.input_adapter import InputKind, InputView, load_input
from asago_artifact_generator.qualification_inputs import (
    PreparedAuthoringInputs,
    prepare_o03_authoring_inputs,
    prepare_o04_authoring_inputs,
    prepare_scn030_authoring_inputs,
)

from .controls import load_control_suite
from .errors import TrialInputError

_CASE_ORDER = ("G07", "A03", "O03", "O04", "SCN-030")

CASE_ORDER = _CASE_ORDER

_EXPECTED_SOURCES = {
    "G07": {"gold_cases", "benchmark", "saved_inputs"},
    "A03": {"gold_cases", "seeded_state", "executor_evidence", "saved_inputs"},
    "O03": {"gold_cases", "prepared_draft_state", "source_evidence"},
    "O04": {"gold_cases", "seed_state", "source_evidence"},
    "SCN-030": {"gold_cases", "selection", "handoff", "handoff_feature"},
}

_INPUT_INDEX_SCHEMA_VERSION = "fresh-five-case-input-index-v1"


@dataclass(frozen=True)
class TrialCaseInputs:
    """Validated source-derived inputs and offline controls for one case."""

    case_id: str
    input_view: InputView
    inventory: dict[str, Any]
    runtime_contract: dict[str, Any]
    authoring_input_pins: dict[str, Any]
    control_cases: tuple[ControlCase, ...] = ()
    control_records: tuple[dict[str, Any], ...] = ()
    unresolved_controls: tuple[dict[str, Any], ...] = ()
    controls_sha256: str = ""


def load_trial_case_inputs(run_dir: str | Path) -> tuple[TrialCaseInputs, ...]:
    """Read and hash-verify every source in the frozen index before any client exists."""

    root = Path(run_dir).resolve()
    index_path = root / "input-index.json"
    try:
        index_bytes = index_path.read_bytes()
        index = json.loads(index_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(f"cannot read frozen input index: {index_path}") from exc
    if not isinstance(index, dict) or index.get("schema_version") != _INPUT_INDEX_SCHEMA_VERSION:
        raise TrialInputError(f"unsupported frozen input index schema: {index_path}")
    entries = _case_entries(index)
    if set(entries) != set(_CASE_ORDER):
        raise TrialInputError("frozen input index must contain exactly the five trial cases")

    loaded: list[TrialCaseInputs] = []
    for case_id in _CASE_ORDER:
        entry = entries[case_id]
        sources = _verify_sources(root, case_id, entry)
        if case_id in {"G07", "A03"}:
            prepared = _load_reference_case(case_id, entry, sources)
        else:
            prepared = _prepare_qualification_case(case_id, entry, sources)
        _verify_prepared_inputs(case_id, entry, prepared, sources)
        owner_scope = _load_owner_scope(case_id, entry)
        if owner_scope is not None:
            prepared = replace(
                prepared,
                input_view=replace(prepared.input_view, owner_scope=owner_scope),
            )
        controls_path = root / "controls" / f"{case_id}.json"
        control_pin = _control_digest_from_index(case_id, entry)
        suite = load_control_suite(
            controls_path,
            expected_sha256=control_pin,
        )
        loaded.append(
            TrialCaseInputs(
                case_id=case_id,
                input_view=prepared.input_view,
                inventory=prepared.inventory,
                runtime_contract=prepared.runtime_contract,
                authoring_input_pins=prepared.authoring_input_pins,
                control_cases=tuple(
                    ControlCase(
                        name=record["name"],
                        evidence=copy.deepcopy(record["evidence"]),
                        expected_outcome=record["expected_outcome"],
                        expected_claim_level=record.get("expected_claim_level"),
                    )
                    for record in suite.cases
                ),
                control_records=tuple(copy.deepcopy(suite.cases)),
                unresolved_controls=suite.unresolved,
                controls_sha256=suite.sha256,
            )
        )
    return tuple(loaded)


def _case_entries(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_cases = index.get("cases")
    if isinstance(raw_cases, dict):
        result = raw_cases
    elif isinstance(raw_cases, list):
        result = {
            item.get("case_id"): item
            for item in raw_cases
            if isinstance(item, dict) and isinstance(item.get("case_id"), str)
        }
    else:
        raise TrialInputError("frozen input index cases must be a mapping or list")
    if any(not isinstance(value, dict) for value in result.values()):
        raise TrialInputError("every frozen input index case must be an object")
    return result


def _verify_sources(
    root: Path,
    case_id: str,
    entry: dict[str, Any],
) -> dict[str, tuple[Path, bytes]]:
    raw_sources = entry.get("sources")
    if not isinstance(raw_sources, dict):
        raise TrialInputError(f"{case_id} sources must be an object")
    if set(raw_sources) != _EXPECTED_SOURCES[case_id]:
        raise TrialInputError(
            f"{case_id} sources must be exactly {sorted(_EXPECTED_SOURCES[case_id])}"
        )
    verified: dict[str, tuple[Path, bytes]] = {}
    for source_name, descriptor in raw_sources.items():
        if not isinstance(descriptor, dict):
            raise TrialInputError(f"{case_id} source {source_name!r} must be an object")
        raw_path = descriptor.get("path")
        expected = descriptor.get("sha256")
        if not isinstance(raw_path, str) or not raw_path.strip() or not _is_sha256(expected):
            raise TrialInputError(f"{case_id} source {source_name!r} lacks path or sha256")
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = root / source_path
            try:
                source_path.resolve().relative_to(root)
            except ValueError as exc:
                raise TrialInputError(
                    f"{case_id} relative source escapes the run directory: {source_name}"
                ) from exc
        try:
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise TrialInputError(
                f"{case_id} source is unavailable: {source_name} ({source_path})"
            ) from exc
        if _sha256(source_bytes) != expected:
            raise TrialInputError(f"{case_id} {source_name} sha256 mismatch")
        verified[source_name] = (source_path, source_bytes)
    return verified


def _load_reference_case(
    case_id: str,
    entry: dict[str, Any],
    sources: dict[str, tuple[Path, bytes]],
) -> PreparedAuthoringInputs:
    source_name = "gold_cases"
    if source_name not in sources or "saved_inputs" not in sources:
        raise TrialInputError(f"{case_id} requires gold_cases and saved_inputs sources")
    required_source_names = {
        "gold_cases",
        "saved_inputs",
        "benchmark" if case_id == "G07" else "seeded_state",
        "benchmark" if case_id == "G07" else "executor_evidence",
    }
    missing_sources = required_source_names - sources.keys()
    if missing_sources:
        raise TrialInputError(
            f"{case_id} is missing original pinned sources: {sorted(missing_sources)}"
        )
    reference_id = entry.get("reference_id", case_id)
    if reference_id != case_id:
        raise TrialInputError(f"{case_id} reference_id must match its fixed case ID")
    input_kind = entry.get("input_kind")
    if input_kind != InputKind.REFERENCE_TASK.value:
        raise TrialInputError(f"{case_id} input_kind must be reference-task")
    benchmark = sources.get("benchmark")
    if case_id == "G07" and benchmark is None:
        raise TrialInputError("G07 requires its pinned benchmark source")
    try:
        saved = json.loads(sources["saved_inputs"][1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(f"{case_id} saved inputs are invalid JSON") from exc
    if not isinstance(saved, dict):
        raise TrialInputError(f"{case_id} saved inputs must be an object")
    inventory = saved.get("inventory")
    runtime_contract = saved.get("runtime_contract")
    pins = saved.get("authoring_input_pins", {})
    if not isinstance(inventory, dict) or not isinstance(runtime_contract, dict):
        raise TrialInputError(f"{case_id} saved inventory and runtime contract must be objects")
    if not isinstance(pins, dict):
        raise TrialInputError(f"{case_id} saved authoring input pins must be an object")
    original_source_sha = _sha256(sources[source_name][1])
    source_digest_pins = pins.get("source_digests")
    if (
        pins.get("scenario_id") != case_id
        or pins.get("input_sha256") != original_source_sha
        or not isinstance(source_digest_pins, dict)
        or source_digest_pins.get("input") != original_source_sha
    ):
        raise TrialInputError(f"{case_id} saved input pin does not match the original gold source")
    if case_id == "G07" and source_digest_pins.get("benchmark") != _sha256(benchmark[1]):
        raise TrialInputError("G07 saved input pin does not match its original benchmark source")
    actual_inventory_sha = _mapping_sha256(inventory)
    actual_runtime_sha = _mapping_sha256(runtime_contract)
    if pins.get("inventory_sha256") != actual_inventory_sha:
        raise TrialInputError(f"{case_id} saved inventory pin does not match extracted bytes")
    if pins.get("runtime_contract_sha256") != actual_runtime_sha:
        raise TrialInputError(
            f"{case_id} saved runtime contract pin does not match extracted bytes"
        )
    _verify_mapping_digest(case_id, entry, "inventory", actual_inventory_sha)
    _verify_mapping_digest(case_id, entry, "runtime_contract", actual_runtime_sha)
    _verify_saved_source_handles(case_id, inventory, sources)
    try:
        view = load_input(
            sources[source_name][0],
            kind=InputKind.REFERENCE_TASK,
            reference_label=entry.get("reference_label"),
            reference_id=reference_id,
            benchmark_source_path=benchmark[0] if benchmark else None,
        )
    except Exception as exc:
        raise TrialInputError(f"{case_id} reference source cannot be loaded") from exc
    return PreparedAuthoringInputs(
        input_view=view,
        inventory=copy.deepcopy(inventory),
        runtime_contract=copy.deepcopy(runtime_contract),
        authoring_input_pins=copy.deepcopy(pins),
    )


def _verify_saved_source_handles(
    case_id: str,
    inventory: dict[str, Any],
    sources: dict[str, tuple[Path, bytes]],
) -> None:
    expected_sources = (
        {
            "source:gold-cases.yaml": "gold_cases",
            "source:benchmark-v4.yaml": "benchmark",
        }
        if case_id == "G07"
        else {
            "source:gold-cases.yaml": "gold_cases",
            "source:seeded-state.json": "seeded_state",
            "source:executor-evidence.json": "executor_evidence",
        }
    )
    handles = inventory.get("source_handles")
    if not isinstance(handles, list):
        raise TrialInputError(f"{case_id} saved inventory source_handles must be a list")
    found: set[str] = set()
    for handle in handles:
        if not isinstance(handle, dict):
            continue
        source_name = expected_sources.get(handle.get("ref"))
        if source_name is None:
            continue
        expected = handle.get("sha256")
        actual_source = sources.get(source_name)
        if (
            actual_source is None
            or not _is_sha256(expected)
            or expected != _sha256(actual_source[1])
        ):
            raise TrialInputError(f"{case_id} source handle does not match {source_name}")
        found.add(source_name)
    if found != set(expected_sources.values()):
        raise TrialInputError(f"{case_id} saved inventory omits original source pins")


def _prepare_qualification_case(
    case_id: str,
    entry: dict[str, Any],
    sources: dict[str, tuple[Path, bytes]],
) -> PreparedAuthoringInputs:
    expected_kind = (
        InputKind.SCENARIO_HANDOFF_V1.value
        if case_id == "SCN-030"
        else InputKind.REFERENCE_TASK.value
    )
    if entry.get("input_kind") != expected_kind:
        raise TrialInputError(f"{case_id} input_kind must be {expected_kind}")

    def source_path(*names: str) -> Path:
        for name in names:
            if name in sources:
                return sources[name][0]
        raise TrialInputError(f"{case_id} is missing a required pinned source: {names[0]}")

    try:
        if case_id == "O03":
            return prepare_o03_authoring_inputs(
                gold_cases_path=source_path("gold_cases"),
                prepared_draft_path=source_path("prepared_draft_state"),
                source_evidence_path=source_path("source_evidence"),
            )
        if case_id == "O04":
            return prepare_o04_authoring_inputs(
                gold_cases_path=source_path("gold_cases"),
                seed_state_path=source_path("seed_state"),
                source_evidence_path=source_path("source_evidence"),
            )
        if case_id == "SCN-030":
            handoff_path = source_path("handoff")
            feature_path = source_path("handoff_feature")
            if handoff_path.with_suffix(".feature").resolve() != feature_path.resolve():
                raise TrialInputError("SCN-030 handoff feature must be adjacent to its handoff")
            return prepare_scn030_authoring_inputs(
                selection_path=source_path("selection"),
                handoff_source_path=handoff_path,
                gold_cases_path=source_path("gold_cases"),
            )
    except TrialInputError:
        raise
    except Exception as exc:
        raise TrialInputError(f"{case_id} qualification inputs cannot be prepared") from exc
    raise TrialInputError(f"unsupported trial case: {case_id}")


def _verify_prepared_inputs(
    case_id: str,
    entry: dict[str, Any],
    prepared: PreparedAuthoringInputs,
    sources: dict[str, tuple[Path, bytes]],
) -> None:
    digests = entry.get("digests")
    if not isinstance(digests, dict):
        raise TrialInputError(f"{case_id} digests must be an object")
    expected_input = _digest_value(digests, "input")
    if expected_input is None or expected_input != prepared.input_view.source_sha256:
        raise TrialInputError(f"{case_id} loaded input sha256 does not match its index")
    inventory_sha = _mapping_sha256(prepared.inventory)
    runtime_sha = _mapping_sha256(prepared.runtime_contract)
    _verify_mapping_digest(case_id, entry, "inventory", inventory_sha)
    _verify_mapping_digest(case_id, entry, "runtime_contract", runtime_sha)
    expected_pins = _digest_value(digests, "authoring_input_pins")
    if expected_pins is not None and expected_pins != _mapping_sha256(
        prepared.authoring_input_pins
    ):
        raise TrialInputError(f"{case_id} authoring input pins do not match their index")
    aliases = {
        "input": "handoff" if case_id == "SCN-030" else "gold_cases",
        "gherkin": "handoff_feature",
    }
    for name, actual in prepared.input_view.source_digests.items():
        expected = _digest_value(digests, name)
        source_name = aliases.get(name, name)
        pinned_source = sources.get(source_name)
        if expected is None and pinned_source is not None:
            expected = _sha256(pinned_source[1])
        if expected is None or expected != actual:
            raise TrialInputError(f"{case_id} source digest {name!r} does not match its index")
        if pinned_source is not None and actual != _sha256(pinned_source[1]):
            raise TrialInputError(f"{case_id} loaded {source_name} source differs from its index")


def _load_owner_scope(
    case_id: str, entry: dict[str, Any]
) -> dict[str, list[dict[str, str]]] | None:
    if case_id != "O04":
        if entry.get("owner_scope") is not None:
            raise TrialInputError("owner_scope is allowed only for O04")
        return None
    scope_record = entry.get("owner_scope")
    if not isinstance(scope_record, dict):
        raise TrialInputError("O04 owner_scope must be recorded in the input index")
    scope = {
        key: copy.deepcopy(scope_record.get(key))
        for key in ("scenario_premises", "evaluation_instructions")
    }
    if any(not isinstance(value, list) for value in scope.values()):
        raise TrialInputError("O04 owner_scope categories must be lists")
    record_expected = scope_record.get("sha256")
    index_expected = _digest_value(entry.get("digests", {}), "owner_scope")
    actual = _mapping_sha256(scope)
    if record_expected != actual or index_expected != actual:
        raise TrialInputError("O04 owner_scope sha256 mismatch")
    return scope


def _verify_mapping_digest(case_id: str, entry: dict[str, Any], name: str, actual: str) -> None:
    digests = entry.get("digests")
    expected = _digest_value(digests, name) if isinstance(digests, dict) else None
    if expected is None or expected != actual:
        raise TrialInputError(f"{case_id} {name} sha256 does not match its index")


def _digest_value(digests: Any, name: str) -> str | None:
    if not isinstance(digests, Mapping):
        return None
    aliases = {
        "input": ("input_sha256", "input"),
        "inventory": ("inventory_sha256", "inventory"),
        "runtime_contract": ("runtime_contract_sha256", "runtime_contract"),
        "authoring_input_pins": ("authoring_input_pins_sha256", "authoring_input_pins"),
        "owner_scope": ("owner_scope_sha256", "owner_scope"),
        "controls": ("controls_sha256", "controls"),
    }
    for key in aliases.get(name, (f"{name}_sha256", name)):
        value = digests.get(key)
        if isinstance(value, dict):
            value = value.get("sha256")
        if _is_sha256(value):
            return value
    return None


def _control_digest_from_index(case_id: str, entry: dict[str, Any]) -> str:
    raw = entry.get("control_sha256")
    if _is_sha256(raw):
        return raw
    digests = entry.get("digests")
    if isinstance(digests, dict):
        digest = _digest_value(digests, "controls")
        if digest is not None:
            return digest
    raise TrialInputError(f"{case_id} input index must pin its control file sha256")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mapping_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _sha256(encoded)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
