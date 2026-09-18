"""Source-pinned, target-free input adapters for artifact authoring.

The producer handoff is the semantic authority.  Native scenario YAML and
labeled reference tasks are development adapters; neither adapter changes the
meaning of the supplied source.  This module deliberately returns plain,
deterministic views so authoring code cannot accidentally reach a target.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .extract import behavior_spec_text

_HANDOFF_SCHEMA_VERSION = "scenario-handoff-v1"
_HANDOFF_DIGEST_DOMAIN = "scenario-handoff-v1"
_HANDOFF_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "scenario-handoff"


class InputKind(StrEnum):
    """The three supported design-time input representations."""

    SCENARIO_HANDOFF_V1 = "scenario-handoff-v1"
    NATIVE_SEMANTIC_YAML = "native-semantic-yaml"
    REFERENCE_TASK = "reference-task"


class InputSourceError(ValueError):
    """Raised when an input or its vendored contract is not trustworthy."""


@dataclass(frozen=True)
class SourceSnapshot:
    """A hash-verified source and optional immutable working snapshot."""

    source_path: str
    sha256: str
    length: int
    snapshot_path: str | None = None

    @property
    def digest(self) -> str:
        """Compatibility name for callers that use digest terminology."""

        return self.sha256


@dataclass(frozen=True)
class InputView:
    """The complete deterministic view supplied to authoring."""

    kind: InputKind
    scenario_id: str
    payload: dict[str, Any]
    source: SourceSnapshot
    source_bytes: bytes
    narrative: Any
    narrative_bytes: bytes
    gherkin: Any
    gherkin_text: str
    gherkin_bytes: bytes
    source_digests: dict[str, str] = field(default_factory=dict)
    reference_label: str | None = None
    reference_id: str | None = None
    benchmark_context: dict[str, Any] = field(default_factory=dict)

    @property
    def source_sha256(self) -> str:
        """Return the exact source-file digest."""

        return self.source.sha256

    @property
    def input_kind(self) -> str:
        """Return the wire value used in package manifests."""

        return self.kind.value

    @property
    def source_hashes(self) -> dict[str, str]:
        """Return source hashes in a package-friendly mapping."""

        return dict(self.source_digests)

    @property
    def narrative_text(self) -> str:
        """Return narrative content without changing its supplied wording."""

        if isinstance(self.narrative, str):
            return self.narrative
        return self.narrative_bytes.decode("utf-8")


def snapshot_input(source_path: str | Path, snapshot_dir: str | Path) -> SourceSnapshot:
    """Read one source and copy its exact bytes into a hash-addressed snapshot.

    The source is opened read-only.  The snapshot is written through a
    temporary file and renamed only after its bytes have been flushed.
    """

    path = Path(source_path)
    source_bytes = _read_source(path)
    digest = _sha256(source_bytes)
    directory = Path(snapshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}-{path.name}"
    if destination.exists() and destination.read_bytes() != source_bytes:
        raise InputSourceError(f"snapshot collision for {path}")
    if not destination.exists():
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with open(fd, "wb", closefd=True) as handle:
                handle.write(source_bytes)
                handle.flush()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return SourceSnapshot(str(path), digest, len(source_bytes), str(destination))


def snapshot_inputs(
    source_paths: list[str | Path] | tuple[str | Path, ...],
    snapshot_dir: str | Path,
) -> tuple[SourceSnapshot, ...]:
    """Snapshot a supplied collection in caller order."""

    return tuple(snapshot_input(path, snapshot_dir) for path in source_paths)


def load_input(
    source_path: str | Path,
    *,
    kind: InputKind | str | None = None,
    input_kind: InputKind | str | None = None,
    reference_label: str | None = None,
    reference_id: str | None = None,
    benchmark_source_path: str | Path | None = None,
    snapshot_dir: str | Path | None = None,
) -> InputView:
    """Load one approved input and produce a source-pinned authoring view."""

    path = Path(source_path)
    try:
        explicit_kind = _coerce_kind(kind) if kind is not None else None
        alternate_kind = _coerce_kind(input_kind) if input_kind is not None else None
    except ValueError as exc:
        raise InputSourceError(f"unsupported input kind: {kind or input_kind}") from exc
    if (
        explicit_kind is not None
        and alternate_kind is not None
        and explicit_kind != alternate_kind
    ):
        raise InputSourceError("kind and input_kind disagree")
    source_bytes = _read_source(path)
    source = (
        snapshot_input(path, snapshot_dir)
        if snapshot_dir is not None
        else SourceSnapshot(str(path), _sha256(source_bytes), len(source_bytes))
    )
    selected_kind = explicit_kind or alternate_kind or _infer_kind(path, source_bytes)
    if selected_kind is InputKind.SCENARIO_HANDOFF_V1:
        return _handoff_view(path, source_bytes, source)
    if selected_kind is InputKind.NATIVE_SEMANTIC_YAML:
        return _native_view(path, source_bytes, source)
    if selected_kind is InputKind.REFERENCE_TASK:
        view = _reference_view(
            path,
            source_bytes,
            source,
            reference_label=reference_label,
            reference_id=reference_id,
        )
        if benchmark_source_path is not None:
            benchmark_bytes = _read_source(Path(benchmark_source_path))
            benchmark_context = _benchmark_context(
                benchmark_bytes,
                reference_id=view.reference_id or view.scenario_id,
            )
            view = replace(
                view,
                source_digests={
                    **view.source_digests,
                    "benchmark": _sha256(benchmark_bytes),
                },
                benchmark_context=benchmark_context,
            )
        return view
    raise InputSourceError(f"unsupported input kind: {selected_kind}")


def build_reference_task_view(view: InputView) -> dict[str, Any]:
    """Project a reference task into meaning-bearing model-facing context.

    The projection intentionally has no answer-shaped stimulus, oracle
    implementation, scoring hint, verdict, or whole source payload.  Those
    values remain available through :func:`build_comparison_inputs` for
    offline comparison and provenance, never through an authoring prompt.
    Native handoffs retain their producer-owned narrative and Gherkin rather
    than passing through this reference-specific projection.
    """

    if view.kind is not InputKind.REFERENCE_TASK:
        return {
            "kind": view.kind.value,
            "scenario_id": view.scenario_id,
            "narrative": deepcopy(view.narrative),
            "gherkin": view.gherkin_text,
            "semantic_failure_condition": _handoff_failure_condition(view.payload),
            "safe_alternative": view.payload.get("safe_alternative", ""),
            "supplied_facts": _handoff_facts(view.payload),
            "environment_documentation": _handoff_environment(view.payload),
            "source_references": dict(view.source_digests),
        }

    source = view.payload
    if not isinstance(source, dict):
        raise InputSourceError("reference task payload must be an object")
    oracle = source.get("oracle") if isinstance(source.get("oracle"), dict) else {}
    benchmark_oracle = (
        view.benchmark_context.get("oracle")
        if isinstance(view.benchmark_context.get("oracle"), dict)
        else {}
    )
    failure = source.get("constraint_meaning")
    unsafe_when = benchmark_oracle.get("unsafe_when", oracle.get("unsafe_when", ""))
    if not isinstance(failure, str) or not failure.strip():
        failure = unsafe_when
    elif isinstance(unsafe_when, str) and unsafe_when.strip():
        failure = f"{failure} {unsafe_when}"
    safe = source.get("safe_alternative")
    if safe is None:
        safe = source.get("safe_behaviors")
    if safe is None:
        safe = oracle.get("safe_behaviors", [])
    if benchmark_oracle.get("safe_behaviors"):
        safe = benchmark_oracle["safe_behaviors"]
    facts: list[Any] = []
    for key in ("facts", "state_facts", "policy_facts"):
        supplied = source.get(key)
        if isinstance(supplied, dict):
            facts.append(deepcopy(supplied))
        elif isinstance(supplied, list):
            facts.extend(deepcopy(supplied))
    benchmark = _reference_benchmark_view(source)
    benchmark.update(
        {
            key: deepcopy(value)
            for key, value in view.benchmark_context.items()
            if key in {"benchmark_version", "record_conditions"}
        }
    )
    return {
        "kind": view.kind.value,
        "scenario_id": view.scenario_id,
        "functional_or_adversarial": source.get("kind", source.get("family", "")),
        "title": source.get("title", ""),
        "semantic_failure_condition": failure,
        "safe_alternative": deepcopy(safe),
        "supplied_facts": deepcopy(facts),
        "relevant_policy_meaning": deepcopy(source.get("constraint_meaning", "")),
        "benchmark_interpretation": benchmark,
        "environment_documentation": {
            "target_environment": source.get("target_environment"),
            "documented_operations": deepcopy(source.get("documented_operations", [])),
            "supported_observations": deepcopy(source.get("supported_observations", [])),
        },
        "source_references": dict(view.source_digests),
        "reference_label": view.reference_label,
    }


def build_comparison_inputs(view: InputView) -> dict[str, Any]:
    """Return sealed answer-bearing evidence for offline comparison only."""

    return {
        "source_snapshot": {
            "path": view.source.source_path,
            "sha256": view.source.sha256,
            "length": view.source.length,
        },
        "source_digests": dict(view.source_digests),
        "complete_source": deepcopy(view.payload),
        "stimulus": deepcopy(view.payload.get("stimulus")),
        "oracle": deepcopy(view.payload.get("oracle")),
        "scoring_hints": deepcopy(
            view.payload.get("scoring_hints", view.payload.get("scoring_hint"))
        ),
        "verdicts": deepcopy(view.payload.get("verdicts", view.payload.get("verdict"))),
    }


def _handoff_failure_condition(payload: dict[str, Any]) -> Any:
    return payload.get("semantic_failure_criterion", "")


def _handoff_facts(payload: dict[str, Any]) -> list[Any]:
    facts = payload.get("sourced_facts", [])
    return deepcopy(facts) if isinstance(facts, list) else []


def _handoff_environment(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "documented_operations": deepcopy(payload.get("documented_operations", [])),
        "governing_rules": deepcopy(payload.get("governing_rules", [])),
        "assumptions_and_unknowns": deepcopy(payload.get("assumptions_and_unknowns", [])),
    }


def _reference_benchmark_view(source: dict[str, Any]) -> dict[str, Any]:
    """Keep benchmark amendments as meaning, not as an answer encoding."""

    result: dict[str, Any] = {}
    for key in (
        "benchmark_version",
        "benchmark_revision",
        "record_conditions",
        "eligibility",
        "ownership",
        "balance_requirement",
    ):
        if key in source:
            result[key] = deepcopy(source[key])
    return result


def _benchmark_context(source_bytes: bytes, *, reference_id: str) -> dict[str, Any]:
    """Resolve one benchmark revision into meaning-only reference context."""

    document = yaml.safe_load(source_bytes)
    if not isinstance(document, dict):
        raise InputSourceError("benchmark source must be an object")
    selected = next(
        (
            item
            for item in document.get("cases", [])
            if isinstance(item, dict) and item.get("gold_id", item.get("id")) == reference_id
        ),
        None,
    )
    if not isinstance(selected, dict):
        raise InputSourceError(f"benchmark case is not present: {reference_id}")
    context: dict[str, Any] = {
        "benchmark_version": document.get("benchmark_version"),
    }
    if isinstance(selected.get("record_conditions"), dict):
        context["record_conditions"] = deepcopy(selected["record_conditions"])
    if isinstance(selected.get("oracle"), dict):
        context["oracle"] = deepcopy(selected["oracle"])
    return context


def _read_source(path: Path) -> bytes:
    if not path.is_file():
        raise InputSourceError(f"input source is not a file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InputSourceError(f"cannot read input source {path}: {exc}") from exc


def _coerce_kind(value: InputKind | str) -> InputKind:
    return value if isinstance(value, InputKind) else InputKind(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_document(path: Path, source_bytes: bytes) -> Any:
    try:
        if path.suffix.lower() == ".json":
            return json.loads(source_bytes)
        return yaml.safe_load(source_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InputSourceError(f"cannot parse input source {path}: {exc}") from exc


def _handoff_view(path: Path, source_bytes: bytes, source: SourceSnapshot) -> InputView:
    _validate_handoff_kit()
    payload = _parse_document(path, source_bytes)
    if not isinstance(payload, dict):
        raise InputSourceError("scenario handoff must be an object")
    _validate_handoff_payload(payload)
    expected_digest = payload.get("content_digest", "")
    digest_payload = {key: value for key, value in payload.items() if key != "content_digest"}
    if expected_digest != _framed_digest(_HANDOFF_DIGEST_DOMAIN, digest_payload):
        raise InputSourceError("scenario handoff content_digest does not match source")
    gherkin = payload["gherkin"]
    gherkin_bytes = _canonical_json(gherkin)
    companion = path.with_suffix(".feature")
    if companion.is_file():
        gherkin_bytes = _read_source(companion)
        try:
            gherkin_text = gherkin_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputSourceError(f"Gherkin companion is not UTF-8: {companion}") from exc
    else:
        gherkin_text = _render_handoff_gherkin(gherkin)
    return InputView(
        kind=InputKind.SCENARIO_HANDOFF_V1,
        scenario_id=payload["scenario_id"],
        payload=payload,
        source=source,
        source_bytes=source_bytes,
        narrative=payload["narrative"],
        narrative_bytes=payload["narrative"].encode("utf-8"),
        gherkin=gherkin,
        gherkin_text=gherkin_text,
        gherkin_bytes=gherkin_bytes,
        source_digests={
            "input": source.sha256,
            "gherkin": _sha256(gherkin_bytes),
        },
    )


def _native_view(path: Path, source_bytes: bytes, source: SourceSnapshot) -> InputView:
    payload = _parse_document(path, source_bytes)
    if not isinstance(payload, dict) or not isinstance(payload.get("scenario_id"), str):
        raise InputSourceError("native semantic YAML requires a scenario_id")
    try:
        gherkin_text = behavior_spec_text(payload.get("behavior_spec", ""))
    except ValueError as exc:
        raise InputSourceError(str(exc)) from exc
    companion = path.with_suffix(".feature")
    if companion.is_file():
        gherkin_bytes = _read_source(companion)
        try:
            gherkin_text = gherkin_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputSourceError(f"Gherkin companion is not UTF-8: {companion}") from exc
    else:
        gherkin_bytes = gherkin_text.encode("utf-8")
    narrative = payload.get("narrative", {})
    return InputView(
        kind=InputKind.NATIVE_SEMANTIC_YAML,
        scenario_id=payload["scenario_id"],
        payload=payload,
        source=source,
        source_bytes=source_bytes,
        narrative=narrative,
        narrative_bytes=(
            narrative.encode("utf-8") if isinstance(narrative, str) else _canonical_json(narrative)
        ),
        gherkin=payload.get("behavior_spec", ""),
        gherkin_text=gherkin_text,
        gherkin_bytes=gherkin_bytes,
        source_digests={
            "input": source.sha256,
            "gherkin": _sha256(gherkin_bytes),
        },
    )


def _reference_view(
    path: Path,
    source_bytes: bytes,
    source: SourceSnapshot,
    *,
    reference_label: str | None,
    reference_id: str | None,
) -> InputView:
    document = _parse_document(path, source_bytes)
    selected = document
    if isinstance(document, dict) and isinstance(document.get("gold_cases"), list):
        cases = document["gold_cases"]
        if reference_id is not None:
            matches = [
                item for item in cases if isinstance(item, dict) and item.get("id") == reference_id
            ]
            if len(matches) != 1:
                raise InputSourceError(f"reference task id is not unique: {reference_id}")
            selected = matches[0]
        elif len(cases) == 1:
            selected = cases[0]
    if not isinstance(selected, dict):
        raise InputSourceError("reference task must resolve to an object")
    if reference_id is not None and selected.get("id") != reference_id:
        raise InputSourceError(f"reference task id is not present: {reference_id}")
    scenario_id = str(selected.get("id") or reference_id or path.stem)
    stimulus = selected.get("stimulus") or {}
    turns = stimulus.get("turns") if isinstance(stimulus, dict) else None
    gherkin_text = ""
    if isinstance(selected.get("gherkin_text"), str):
        gherkin_text = selected["gherkin_text"]
    return InputView(
        kind=InputKind.REFERENCE_TASK,
        scenario_id=scenario_id,
        payload=selected,
        source=source,
        source_bytes=source_bytes,
        narrative=selected.get("constraint_meaning", ""),
        narrative_bytes=str(selected.get("constraint_meaning", "")).encode("utf-8"),
        gherkin=turns or [],
        gherkin_text=gherkin_text,
        gherkin_bytes=gherkin_text.encode("utf-8"),
        source_digests={"input": source.sha256},
        reference_label=reference_label,
        reference_id=reference_id or (scenario_id if scenario_id else None),
    )


def _infer_kind(path: Path, source_bytes: bytes) -> InputKind:
    document = _parse_document(path, source_bytes)
    if isinstance(document, dict) and document.get("schema_version") == _HANDOFF_SCHEMA_VERSION:
        return InputKind.SCENARIO_HANDOFF_V1
    if isinstance(document, dict) and "gold_cases" in document:
        return InputKind.REFERENCE_TASK
    return InputKind.NATIVE_SEMANTIC_YAML


def _validate_handoff_kit() -> None:
    lock_path = _HANDOFF_ROOT / "CONTRACT.lock"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputSourceError(f"cannot read vendored handoff lock: {exc}") from exc
    if lock.get("authority") != "asago-scenario-generator":
        raise InputSourceError("vendored handoff kit authority mismatch")
    for relative, expected in lock.get("files", {}).items():
        member = _HANDOFF_ROOT / relative
        if not member.is_file() or _sha256(member.read_bytes()) != expected:
            raise InputSourceError(f"vendored handoff kit digest mismatch: {relative}")


def validate_vendored_handoff_kit() -> None:
    """Validate every producer-owned handoff-kit member before use."""

    _validate_handoff_kit()


def _validate_handoff_payload(payload: dict[str, Any]) -> None:
    required = {
        "scenario_id",
        "kind",
        "hypothesis_framing",
        "narrative",
        "attack_tree",
        "gherkin",
        "semantic_failure_criterion",
        "safe_alternative",
        "lineage",
    }
    allowed = {
        "schema_version",
        "scenario_id",
        "scenario_version",
        "kind",
        "hypothesis_framing",
        "semantic_failure_criterion",
        "safe_alternative",
        "governing_rules",
        "lineage",
        "documented_operations",
        "sourced_facts",
        "assumptions_and_unknowns",
        "content_digest",
        "narrative",
        "attack_tree",
        "gherkin",
    }
    missing = required - payload.keys()
    unknown = set(payload) - allowed
    if missing or unknown:
        raise InputSourceError(
            f"handoff schema invalid (missing={sorted(missing)}, unknown={sorted(unknown)})"
        )
    if payload.get("schema_version", _HANDOFF_SCHEMA_VERSION) != _HANDOFF_SCHEMA_VERSION:
        raise InputSourceError("unknown scenario handoff schema version")
    if payload.get("kind") not in {"adversarial", "functional"}:
        raise InputSourceError("handoff kind is invalid")
    for key in (
        "scenario_id",
        "hypothesis_framing",
        "narrative",
        "semantic_failure_criterion",
        "safe_alternative",
    ):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise InputSourceError(f"handoff field is blank or mistyped: {key}")
    if not isinstance(payload["attack_tree"], dict) or not isinstance(payload["lineage"], dict):
        raise InputSourceError("handoff attack_tree and lineage must be objects")
    gherkin = payload["gherkin"]
    if (
        not isinstance(gherkin, dict)
        or not isinstance(gherkin.get("feature"), str)
        or not isinstance(gherkin.get("scenario"), str)
    ):
        raise InputSourceError("handoff Gherkin is invalid")
    for key, value in gherkin.items():
        if key not in {
            "feature",
            "scenario",
            "given",
            "when",
            "then_expected",
            "then_unsafe_alternative",
        }:
            raise InputSourceError(f"handoff Gherkin has unknown field: {key}")
        if key not in {"feature", "scenario"} and not isinstance(value, list):
            raise InputSourceError(f"handoff Gherkin field is not a list: {key}")
    violations = _ownership_violations(payload)
    if violations:
        raise InputSourceError(f"handoff ownership violation: {', '.join(violations)}")


def _ownership_violations(payload: dict[str, Any]) -> list[str]:
    forbidden = {
        "prepared_user_text",
        "stimulus_turns",
        "turns",
        "turn",
        "role",
        "roles",
        "delivery",
        "delivery_class",
        "delivery_route",
        "route",
        "execution_route",
        "oracle",
        "detector",
        "judge",
        "judge_prompt",
        "prompt",
        "prompts",
        "setup",
        "execution_setup",
        "execution_contract",
        "projection",
        "bundle",
        "payload",
        "messages",
        "message",
        "replay",
        "unsafe_outcome_condition",
        "operands",
        "reference_tool",
        "reference_argument",
        "applies_when",
        "trigger_evidence",
    }
    patterns = (
        ("prepared_message_field", re.compile(r"prepared_user_text", re.I)),
        ("judge_prompt", re.compile(r"\bjudge prompt\b", re.I)),
        (
            "detector_expression",
            re.compile(r"\bgreater_than\b|\bless_than\b|\bamount\s*[<>=]", re.I),
        ),
        (
            "ready_to_send_instruction",
            re.compile(r"\bsend this message\b|\bexecute the following\b", re.I),
        ),
    )
    found: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                leaf = str(key).lower()
                if leaf in forbidden and f"artifact_design_field:{leaf}" not in found:
                    found.append(f"artifact_design_field:{leaf}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str):
            for slug, pattern in patterns:
                code = f"prose_hiding:{slug}"
                if pattern.search(value) and code not in found:
                    found.append(code)

    walk(payload)
    return found


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _normalize_unicode(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _normalize_unicode(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize_unicode(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_unicode(item) for item in value]
    return value


def _framed_digest(domain: str, value: Any) -> str:
    return _sha256(domain.encode("utf-8") + b"\0" + _canonical_json(value))


def _render_handoff_gherkin(gherkin: dict[str, Any]) -> str:
    lines = [f"Feature: {gherkin['feature']}", f"Scenario: {gherkin['scenario']}"]
    for key in ("given", "when", "then_expected"):
        lines.extend(f"  {line}" for line in gherkin.get(key, []))
    lines.extend(
        f"  # Unsafe alternative (non-executable): {line}"
        for line in gherkin.get("then_unsafe_alternative", [])
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "InputKind",
    "InputSourceError",
    "InputView",
    "SourceSnapshot",
    "load_input",
    "snapshot_input",
    "snapshot_inputs",
    "validate_vendored_handoff_kit",
]
