"""Conformance checks for a tool adapter: ``compile``, ``instantiate`` and ``parse``.

Each ``check_*`` function returns a list of failure messages and the empty list when the
adapter is faithful, so a test asserts ``not failures`` and a corpus script prints them.
The adapter's three steps arrive as callables (``AdapterUnderTest``) because their
signatures differ per tool; the adapter closes over any extra arguments of its own.

``check_round_trip`` runs the steps in order and stops at the first step that fails:
``compile`` writes ``<work>/template``, ``instantiate`` writes ``<work>/bundle``, and
``parse`` writes ``<work>/bundle/receipt.json`` (a tool's report must lie inside the
receipt's directory).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from asago_artifact_generator.package_io import ArtifactPackage, load_package

from .errors import BundleError
from .gap import CapabilityGap
from .schema import manifest_errors
from .slots import contains_marker, template_slots
from .text import canonical_text

BUNDLE_FILE = "bundle.json"
TEMPLATE = "template"
BUNDLE = "bundle"
RECEIPT = "receipt.json"
PROBE_RECEIPT = "receipt-without-native.json"
GAP_FIELDS = frozenset(
    {"kind", "tool", "package_id", "scenario_id", "package_digest", "delivery", "reason"}
)
GAP_TEXT_FIELDS = ("tool", "delivery", "reason")


@dataclass(frozen=True)
class AdapterUnderTest:
    """An adapter's steps as callables.

    ``compile(package_dir, out_dir)`` and ``instantiate(template_dir, values, out_dir)``
    write their directory and return the manifest; ``compile`` raises ``CapabilityGap`` for
    a package the tool cannot deliver and ``BundleError`` for one it cannot read.
    ``parse(bundle_dir, receipt_path)`` writes and returns the receipt.
    ``write_native(bundle_dir, manifest)`` stands in for the tool's run and writes its
    native outputs into a concrete bundle. ``entrypoint_keys`` are the value keys the
    entrypoint renders.
    """

    compile: Callable[[Path, Path], dict[str, Any]]
    instantiate: Callable[[Path, dict[str, Any], Path], dict[str, Any]]
    parse: Callable[[Path, Path], dict[str, Any]]
    write_native: Callable[[Path, dict[str, Any]], None]
    entrypoint_keys: tuple[str, ...] = ()


def check_round_trip(
    adapter: AdapterUnderTest,
    package_dir: Path,
    values: dict[str, Any],
    work: Path,
    receipt_schema: Path,
) -> list[str]:
    """Run compile, instantiate and parse on one package; a faithful gap also passes."""

    try:
        failures = check_compile(adapter, package_dir, work)
    except CapabilityGap as gap:
        return check_gap(gap.record, package_dir)
    except BundleError as exc:
        return [f"compile refused the package: {exc}"]
    if failures:
        return failures
    failures = check_instantiate(adapter, work / TEMPLATE, values, work)
    if failures:
        return failures
    return check_parse(adapter, work / BUNDLE, values, receipt_schema)


def check_gap(record: Any, package_dir: Path) -> list[str]:
    """Check a capability-gap record against the package it names."""

    if not isinstance(record, dict):
        return ["gap record is not an object"]
    package = load_package(package_dir)
    return [
        *_gap_field_failures(record),
        *_gap_identity_failures(record, package),
        *_gap_text_failures(record),
    ]


def check_compile(adapter: AdapterUnderTest, package_dir: Path, work: Path) -> list[str]:
    """Compile twice into ``<work>/template`` and ``<work>/template-again`` and compare."""

    first, second = work / TEMPLATE, work / f"{TEMPLATE}-again"
    returned = adapter.compile(package_dir, first)
    adapter.compile(package_dir, second)
    written = _read_json(first / BUNDLE_FILE)
    manifest = written if isinstance(written, dict) else {}
    return [
        *_manifest_failures("manifest", written, returned),
        *_identity_failures(manifest, load_package(package_dir)),
        *_bytes_failures(first, second),
        *_template_failures(first, manifest),
        *_requires_failures(first, manifest, adapter.entrypoint_keys),
    ]


def check_instantiate(
    adapter: AdapterUnderTest, template_dir: Path, values: dict[str, Any], work: Path
) -> list[str]:
    """Instantiate into ``<work>/bundle``, then refuse each required key left out."""

    bundle = work / BUNDLE
    try:
        concrete = adapter.instantiate(template_dir, values, bundle)
    except BundleError as exc:
        return [f"instantiate refused the values: {exc}"]
    template = _read_json(template_dir / BUNDLE_FILE)
    requires = template.get("requires", []) if isinstance(template, dict) else []
    return [
        *_manifest_failures("concrete manifest", _read_json(bundle / BUNDLE_FILE), concrete),
        *_values_digest_failures(bundle, values),
        *_marker_failures(bundle),
        *_refusal_failures(adapter, template_dir, requires, values, work),
    ]


def check_parse(
    adapter: AdapterUnderTest, bundle_dir: Path, values: dict[str, Any], receipt_schema: Path
) -> list[str]:
    """Parse a concrete bundle without, then with, its native output.

    ``bundle_dir`` must hold no native output yet; ``write_native`` adds it.
    """

    written = _read_json(bundle_dir / BUNDLE_FILE)
    manifest = written if isinstance(written, dict) else {}
    failures = _missing_native_failures(adapter, bundle_dir)
    adapter.write_native(bundle_dir, manifest)
    receipt = adapter.parse(bundle_dir, bundle_dir / RECEIPT)
    return [
        *failures,
        *_receipt_schema_failures(receipt, receipt_schema),
        *_receipt_package_failures(receipt, manifest),
        *_attempt_failures(receipt, manifest, values),
    ]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gap_field_failures(record: dict[str, Any]) -> list[str]:
    if set(record) == GAP_FIELDS:
        return []
    return [f"gap record fields {sorted(record)} differ from {sorted(GAP_FIELDS)}"]


def _gap_identity_failures(record: dict[str, Any], package: ArtifactPackage) -> list[str]:
    expected = {
        "kind": "capability_gap",
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
    }
    return [
        f"gap record {key} is {record.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if record.get(key) != value
    ]


def _gap_text_failures(record: dict[str, Any]) -> list[str]:
    return [
        f"gap record {key} is empty or not text"
        for key in GAP_TEXT_FIELDS
        if not (isinstance(record.get(key), str) and record[key].strip())
    ]


def _manifest_failures(label: str, written: Any, returned: Any) -> list[str]:
    failures = [f"{label} does not validate: {message}" for message in manifest_errors(written)]
    if written != returned:
        failures.append(f"{BUNDLE_FILE} on disk differs from the returned {label}")
    return failures


def _identity_failures(manifest: dict[str, Any], package: ArtifactPackage) -> list[str]:
    expected = {
        "package_digest": package.manifest.manifest_digest,
        "scenario_id": package.manifest.scenario_id,
        "package_id": package.manifest.package_id,
    }
    return [
        f"{key} {manifest.get(key)!r} differs from the package's {value!r}"
        for key, value in expected.items()
        if manifest.get(key) != value
    ]


def _bytes_failures(first: Path, second: Path) -> list[str]:
    names = sorted(
        {
            path.relative_to(root).as_posix()
            for root in (first, second)
            for path in root.rglob("*")
            if path.is_file()
        }
    )
    differing = [name for name in names if _bytes(first / name) != _bytes(second / name)]
    return [f"two compiles of one package differ in bytes: {differing}"] if differing else []


def _bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _template_failures(directory: Path, manifest: dict[str, Any]) -> list[str]:
    templates = manifest.get("templates")
    if not isinstance(templates, dict):
        return []
    return [
        f"template file {name} named by templates.{role} is missing"
        for role, name in templates.items()
        if not (directory / name).is_file()
    ]


def _requires_failures(
    directory: Path, manifest: dict[str, Any], entrypoint_keys: tuple[str, ...]
) -> list[str]:
    templates = manifest.get("templates")
    names = templates.values() if isinstance(templates, dict) else []
    documents = [document for document in (_read_json(directory / n) for n in names) if document]
    missing = (template_slots(documents) | set(entrypoint_keys)) - set(
        manifest.get("requires", [])
    )
    if not missing:
        return []
    return [f"requires lacks {sorted(missing)}: the templates or the entrypoint use them"]


def _values_digest_failures(bundle: Path, values: dict[str, Any]) -> list[str]:
    written = _read_json(bundle / BUNDLE_FILE)
    digest = written.get("values_digest") if isinstance(written, dict) else None
    expected = hashlib.sha256(canonical_text(values).encode()).hexdigest()
    if digest is None:
        return ["values_digest is missing from the concrete manifest"]
    if digest != expected:
        return [f"values_digest {digest} differs from the sha256 of the canonical values"]
    return []


def _documents(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return []


def _marker_failures(bundle: Path) -> list[str]:
    return [
        f"{path.relative_to(bundle).as_posix()} still holds a slot marker"
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and any(contains_marker(document) for document in _documents(path))
    ]


def _refusal_failures(
    adapter: AdapterUnderTest,
    template_dir: Path,
    requires: list[str],
    values: dict[str, Any],
    work: Path,
) -> list[str]:
    failures = []
    for key in requires:
        partial = {name: value for name, value in values.items() if name != key}
        try:
            adapter.instantiate(template_dir, partial, work / f"refused-{key}")
        except BundleError:
            continue
        failures.append(f"instantiate accepted values without {key}")
    return failures


def _missing_native_failures(adapter: AdapterUnderTest, bundle_dir: Path) -> list[str]:
    probe = bundle_dir / PROBE_RECEIPT
    prefix = "parse of a bundle without its native output"
    try:
        receipt = adapter.parse(bundle_dir, probe)
    except BundleError as exc:
        return [f"{prefix} raised instead of writing a failed receipt: {exc}"]
    probe.unlink(missing_ok=True)
    status = receipt.get("execution_status")
    if status != "failed":
        return [f"{prefix} did not give execution_status failed (gave {status!r})"]
    if not receipt.get("incomplete_reason"):
        return [f"{prefix} gave no incomplete_reason"]
    return []


def _receipt_schema_failures(receipt: Any, schema_path: Path) -> list[str]:
    validator = jsonschema.Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )
    errors = sorted(validator.iter_errors(receipt), key=lambda e: [str(p) for p in e.path])
    return [f"receipt does not validate: {error.message}" for error in errors]


def _receipt_package_failures(receipt: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    package = receipt.get("package")
    digest = package.get("digest") if isinstance(package, dict) else None
    expected = manifest.get("package_digest")
    if digest == expected:
        return []
    return [f"receipt names package digest {digest}, not {expected}"]


def _expected_attempts(manifest: dict[str, Any], values: dict[str, Any]) -> int | None:
    repeats = manifest.get("repeats")
    if not (isinstance(repeats, dict) and repeats.get("native")):
        return 1
    count = values.get("repeats")
    return count if type(count) is int and count >= 1 else None


def _attempt_failures(
    receipt: dict[str, Any], manifest: dict[str, Any], values: dict[str, Any]
) -> list[str]:
    if receipt.get("execution_status") != "completed":
        return []
    expected = _expected_attempts(manifest, values)
    if expected is None:
        return ["repeats.native is true but values carry no positive integer repeats count"]
    attempts = receipt.get("attempts")
    count = len(attempts) if isinstance(attempts, list) else None
    return [] if count == expected else [f"receipt holds {count} attempts, expected {expected}"]
