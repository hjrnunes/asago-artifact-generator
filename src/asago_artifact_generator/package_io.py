"""Contained, atomic persistence for the consumer-owned artifact package."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_SCHEMA_VERSION = "artifact-package-v1"
DETECTOR_INTERFACE_VERSION = "evaluate(evidence: dict) -> dict"
_SECRET_TERMS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "endpoint",
    "password",
    "secret",
    "token",
)
_ALLOWED_MEMBER_NAMES = {
    "plan.json",
    "stimulus.json",
    "setup.json",
    "bindings.json",
    "prerequisites.json",
    "detector.py",
    "checks.json",
    "inputs.json",
    "source-hashes.json",
    "observations.json",
    "judge.json",
}
_CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "artifact-package"
_INPUT_KINDS = {"scenario-handoff-v1", "native-semantic-yaml", "reference-task"}


class PackagePathError(ValueError):
    """Raised when a package member escapes its code-owned directory."""


class PackageIntegrityError(ValueError):
    """Raised when a package is incomplete, unexpected, or tampered."""


@dataclass
class PackageManifest:
    """Canonical manifest fields that bind the immutable package contents."""

    package_id: str
    scenario_id: str
    input_kind: str
    source_digests: dict[str, str]
    members: list[dict[str, Any]]
    reference_task: dict[str, Any] | None = None
    authoring: dict[str, Any] = field(default_factory=dict)
    detector_interface: str = DETECTOR_INTERFACE_VERSION
    runtime_capabilities: dict[str, Any] = field(default_factory=dict)
    creation_model: dict[str, Any] = field(default_factory=dict)
    manifest_digest: str = ""
    schema_version: str = PACKAGE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "package_id": self.package_id,
            "scenario_id": self.scenario_id,
            "input_kind": self.input_kind,
            "source_digests": self.source_digests,
            "members": self.members,
            "authoring": self.authoring,
            "detector_interface": self.detector_interface,
            "runtime_capabilities": self.runtime_capabilities,
            "creation_model": self.creation_model,
        }
        if self.manifest_digest:
            result["manifest_digest"] = self.manifest_digest
        if self.reference_task is not None:
            result["reference_task"] = self.reference_task
        return result


@dataclass
class ArtifactPackage:
    """An in-memory package whose members have not yet been persisted."""

    manifest: PackageManifest
    members: dict[str, bytes]


def build_package(
    *,
    package_id: str,
    scenario_id: str,
    input_kind: str,
    source_digests: dict[str, str],
    members: dict[str, bytes],
    reference_task: dict[str, Any] | None = None,
    authoring: dict[str, Any] | None = None,
    runtime_capabilities: dict[str, Any] | None = None,
    creation_model: dict[str, Any] | None = None,
) -> ArtifactPackage:
    """Build a package and derive its canonical member manifest."""

    normalized_members = _normalize_members(members)
    manifest = PackageManifest(
        package_id=package_id,
        scenario_id=scenario_id,
        input_kind=input_kind,
        source_digests=dict(source_digests),
        members=[_member_record(path, value) for path, value in normalized_members.items()],
        reference_task=reference_task,
        authoring=authoring or {},
        runtime_capabilities=runtime_capabilities or {},
        creation_model=creation_model or {},
    )
    manifest.manifest_digest = _manifest_digest(manifest.to_dict())
    _validate_manifest_fields(manifest)
    return ArtifactPackage(manifest, normalized_members)


def write_package(
    destination: str | Path,
    package: ArtifactPackage,
    *,
    fail_after_members: int | None = None,
) -> Path:
    """Atomically replace ``destination`` with a complete verified package.

    ``fail_after_members`` is a deterministic fault-injection hook used by
    tests.  It raises before the destination is replaced and never leaves its
    temporary directory behind.
    """

    destination = Path(destination)
    validate_artifact_package_contract()
    _validate_package(package)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent))
    try:
        for index, (relative, content) in enumerate(package.members.items(), start=1):
            target = _contained_path(temporary, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if fail_after_members is not None and index >= fail_after_members:
                raise RuntimeError("interrupted package write")
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(_canonical_json(package.manifest.to_dict()) + b"\n")
        loaded = load_package(temporary)
        if loaded.manifest.to_dict() != package.manifest.to_dict():
            raise PackageIntegrityError("package changed while writing")
        backup: Path | None = None
        if destination.exists() or destination.is_symlink():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.backup.", suffix=".tmp", dir=parent)
            )
            backup.rmdir()
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
        except Exception:
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if backup is not None and (backup.exists() or backup.is_symlink()):
                os.replace(backup, destination)
            raise
        if backup is not None and (backup.exists() or backup.is_symlink()):
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_package(path: str | Path) -> ArtifactPackage:
    """Load and verify every package member before returning any content."""

    validate_artifact_package_contract()
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise PackageIntegrityError(f"package directory is unavailable: {root}")
    manifest_path = root / "manifest.json"
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageIntegrityError(f"invalid package manifest: {exc}") from exc
    manifest = _manifest_from_dict(manifest_data)
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise PackageIntegrityError(f"symlink is not allowed in package: {candidate}")
    members: dict[str, bytes] = {}
    expected_paths: set[str] = set()
    for record in manifest.members:
        relative = _validate_member_path(record.get("path"))
        if relative in expected_paths:
            raise PackageIntegrityError(f"duplicate package member: {relative}")
        expected_paths.add(relative)
        member_path = _contained_path(root, relative)
        if not member_path.is_file() or member_path.is_symlink():
            raise PackageIntegrityError(f"missing package member: {relative}")
        content = member_path.read_bytes()
        if record.get("sha256") != _sha256(content):
            raise PackageIntegrityError(f"digest mismatch for package member: {relative}")
        if record.get("length") != len(content):
            raise PackageIntegrityError(f"length mismatch for package member: {relative}")
        members[relative] = content
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual_paths != expected_paths:
        raise PackageIntegrityError("package member set does not match manifest")
    expected_directories = {
        parent.as_posix()
        for relative in expected_paths
        for parent in Path(relative).parents
        if str(parent) != "."
    }
    actual_directories = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
    }
    if actual_directories != expected_directories:
        raise PackageIntegrityError("package directory set does not match manifest")
    package = ArtifactPackage(manifest, members)
    _validate_package(package)
    return package


def _normalize_members(members: dict[str, bytes]) -> dict[str, bytes]:
    if not isinstance(members, dict):
        raise PackageIntegrityError("package members must be a mapping")
    result: dict[str, bytes] = {}
    for name, value in members.items():
        relative = _validate_member_path(name)
        if not isinstance(value, bytes):
            raise PackageIntegrityError(f"package member is not bytes: {relative}")
        if relative in result:
            raise PackageIntegrityError(f"duplicate package member: {relative}")
        result[relative] = value
    return dict(sorted(result.items()))


def _validate_member_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PackagePathError(f"invalid package member path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackagePathError(f"package member path escapes package: {value!r}")
    relative = path.as_posix()
    if relative != value:
        raise PackagePathError(f"non-canonical package member path: {value!r}")
    if relative not in _ALLOWED_MEMBER_NAMES and not relative.startswith("authoring/"):
        raise PackagePathError(f"unexpected package member path: {relative}")
    return relative


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise PackagePathError(f"package member escapes package: {relative}")
    return candidate


def _member_record(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "media_type": _media_type(path),
        "length": len(content),
        "sha256": _sha256(content),
    }


def _media_type(path: str) -> str:
    if path.endswith(".py"):
        return "text/x-python"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".yaml") or path.endswith(".yml"):
        return "application/yaml"
    return "application/octet-stream"


def _manifest_from_dict(value: Any) -> PackageManifest:
    if not isinstance(value, dict):
        raise PackageIntegrityError("package manifest must be an object")
    required = {
        "schema_version",
        "package_id",
        "scenario_id",
        "input_kind",
        "source_digests",
        "members",
        "authoring",
        "detector_interface",
        "runtime_capabilities",
        "creation_model",
    }
    unknown = set(value) - required - {"reference_task", "manifest_digest"}
    missing = required - set(value)
    if unknown or missing:
        raise PackageIntegrityError(
            f"package manifest fields invalid (missing={missing}, unknown={unknown})"
        )
    manifest = PackageManifest(
        package_id=value["package_id"],
        scenario_id=value["scenario_id"],
        input_kind=value["input_kind"],
        source_digests=value["source_digests"],
        members=value["members"],
        reference_task=value.get("reference_task"),
        authoring=value["authoring"],
        detector_interface=value["detector_interface"],
        runtime_capabilities=value["runtime_capabilities"],
        creation_model=value["creation_model"],
        manifest_digest=value.get("manifest_digest", ""),
        schema_version=value["schema_version"],
    )
    _validate_manifest_fields(manifest)
    return manifest


def _validate_manifest_fields(manifest: PackageManifest) -> None:
    if manifest.schema_version != PACKAGE_SCHEMA_VERSION:
        raise PackageIntegrityError("unknown artifact package schema version")
    if not manifest.manifest_digest:
        raise PackageIntegrityError("manifest digest is missing")
    if manifest.manifest_digest != _manifest_digest(manifest.to_dict()):
        raise PackageIntegrityError("manifest digest mismatch")
    for name in ("package_id", "scenario_id", "input_kind", "detector_interface"):
        if not isinstance(getattr(manifest, name), str) or not getattr(manifest, name):
            raise PackageIntegrityError(f"manifest field is blank: {name}")
    if manifest.input_kind not in _INPUT_KINDS:
        raise PackageIntegrityError(f"unsupported package input kind: {manifest.input_kind}")
    if (
        not isinstance(manifest.source_digests, dict)
        or not manifest.source_digests
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for key, value in manifest.source_digests.items()
        )
    ):
        raise PackageIntegrityError("manifest source_digests must contain SHA-256 strings")
    for value in (
        manifest.reference_task,
        manifest.authoring,
        manifest.runtime_capabilities,
        manifest.creation_model,
    ):
        if value is not None and not isinstance(value, dict):
            raise PackageIntegrityError("manifest metadata must be objects")
        if value is not None and _contains_secret_key(value):
            raise PackageIntegrityError("manifest contains secret-bearing metadata")
    if not isinstance(manifest.members, list):
        raise PackageIntegrityError("manifest members must be a list")


def _validate_package(package: ArtifactPackage) -> None:
    _validate_manifest_fields(package.manifest)
    normalized = _normalize_members(package.members)
    declared = {
        record.get("path") for record in package.manifest.members if isinstance(record, dict)
    }
    if declared != set(normalized):
        raise PackageIntegrityError("package member set does not match manifest")
    for record in package.manifest.members:
        if not isinstance(record, dict):
            raise PackageIntegrityError("package member record must be an object")
        relative = _validate_member_path(record.get("path"))
        expected = _member_record(relative, normalized[relative])
        if record != expected:
            raise PackageIntegrityError(f"member digest or metadata mismatch: {relative}")


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(term in lowered for term in _SECRET_TERMS):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest_digest(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("manifest_digest", None)
    return _sha256(_canonical_json(payload))


def validate_artifact_package_contract() -> None:
    """Verify the consumer-owned contract lock before package IO."""

    lock_path = _CONTRACT_ROOT / "CONTRACT.lock"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageIntegrityError(f"cannot read artifact package contract lock: {exc}") from exc
    if lock.get("authority") != "asago-artifact-generator":
        raise PackageIntegrityError("artifact package contract authority mismatch")
    for relative, expected in lock.get("files", {}).items():
        member = _CONTRACT_ROOT / relative
        if not member.is_file() or _sha256(member.read_bytes()) != expected:
            raise PackageIntegrityError(f"artifact package contract digest mismatch: {relative}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


__all__ = [
    "ArtifactPackage",
    "DETECTOR_INTERFACE_VERSION",
    "PACKAGE_SCHEMA_VERSION",
    "PackageIntegrityError",
    "PackageManifest",
    "PackagePathError",
    "build_package",
    "load_package",
    "write_package",
    "validate_artifact_package_contract",
]

# Descriptive aliases keep the persistence seam readable at call sites.
read_artifact_package = load_package
write_artifact_package = write_package
ArtifactPackageError = PackageIntegrityError

__all__ += ["ArtifactPackageError", "read_artifact_package", "write_artifact_package"]
