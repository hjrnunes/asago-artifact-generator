"""Contained, atomic persistence for the consumer-owned artifact package."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .contract_kit import (
    ClaimLevel,
    canonical_json,
    first_schema_error,
    sha256_hex,
    verify_contract_lock,
)
from .metadata_policy import secret_metadata_paths
from .value_checks import is_sha256_hex

PACKAGE_SCHEMA_VERSION = "artifact-package-v3"
TOOL_CALL_CONDITION_MEMBER = "tool_call_condition.json"
_ALLOWED_MEMBER_NAMES = {
    "plan.json",
    "stimulus.json",
    "setup.json",
    "bindings.json",
    "prerequisites.json",
    "checks.json",
    "inputs.json",
    "source-hashes.json",
    "observations.json",
    "judge.json",
    "explanation.json",
    "examples.json",
    TOOL_CALL_CONDITION_MEMBER,
}
# Each claim level names the member that downstream detection reads.
_CLAIM_LEVEL_MEMBERS = {
    ClaimLevel.COMMAND_ATTEMPT.value: TOOL_CALL_CONDITION_MEMBER,
    ClaimLevel.REPLY.value: "judge.json",
}
_CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "artifact-package"
_INPUT_KINDS = {"scenario-handoff-v3"}


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
    authoring: dict[str, Any] = field(default_factory=dict)
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
            "runtime_capabilities": self.runtime_capabilities,
            "creation_model": self.creation_model,
        }
        if self.manifest_digest:
            result["manifest_digest"] = self.manifest_digest
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
) -> Path:
    """Atomically replace ``destination`` with a complete verified package.

    A failure before the swap removes the temporary directory and leaves any
    existing package in place.
    """

    destination = Path(destination)
    validate_artifact_package_contract()
    _validate_package(package)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent))
    try:
        _write_package_files(temporary, package)
        loaded = load_package(temporary)
        if loaded.manifest.to_dict() != package.manifest.to_dict():
            raise PackageIntegrityError("package changed while writing")
        backup = _move_existing_aside(destination, parent)
        _replace_or_restore(temporary, destination, backup)
        if backup is not None and _path_present(backup):
            _remove_path(backup)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _write_package_files(temporary: Path, package: ArtifactPackage) -> None:
    for relative, content in package.members.items():
        target = _contained_path(temporary, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest_path = temporary / "manifest.json"
    manifest_path.write_bytes(canonical_json(package.manifest.to_dict()).encode("utf-8") + b"\n")


def _move_existing_aside(destination: Path, parent: Path) -> Path | None:
    if not _path_present(destination):
        return None
    backup = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.backup.", suffix=".tmp", dir=parent)
    )
    backup.rmdir()
    os.replace(destination, backup)
    return backup


def _replace_or_restore(temporary: Path, destination: Path, backup: Path | None) -> None:
    try:
        os.replace(temporary, destination)
    except Exception:
        if _path_present(destination):
            _remove_path(destination)
        if backup is not None and _path_present(backup):
            os.replace(backup, destination)
        raise


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


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
    for record in manifest.members:
        relative, content = _load_member(root, record, members)
        members[relative] = content
    _verify_package_layout(root, set(members))
    package = ArtifactPackage(manifest, members)
    _validate_package(package)
    return package


def _load_member(root: Path, record: Any, loaded: dict[str, bytes]) -> tuple[str, bytes]:
    """Read one manifest member and verify its path, digest, and length."""

    relative = _validate_member_path(record.get("path"))
    if relative in loaded:
        raise PackageIntegrityError(f"duplicate package member: {relative}")
    member_path = _contained_path(root, relative)
    if not member_path.is_file() or member_path.is_symlink():
        raise PackageIntegrityError(f"missing package member: {relative}")
    content = member_path.read_bytes()
    if record.get("sha256") != sha256_hex(content):
        raise PackageIntegrityError(f"digest mismatch for package member: {relative}")
    if record.get("length") != len(content):
        raise PackageIntegrityError(f"length mismatch for package member: {relative}")
    return relative, content


def _verify_package_layout(root: Path, expected_paths: set[str]) -> None:
    """Require the package's files and directories to be exactly the manifest's."""

    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual_paths != expected_paths:
        raise PackageIntegrityError("package member set does not match manifest")
    actual_directories = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
    }
    if actual_directories != _member_directories(expected_paths):
        raise PackageIntegrityError("package directory set does not match manifest")


def _member_directories(paths: set[str]) -> set[str]:
    return {
        parent.as_posix()
        for relative in paths
        for parent in Path(relative).parents
        if str(parent) != "."
    }


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
        "sha256": sha256_hex(content),
    }


def _media_type(path: str) -> str:
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
        "runtime_capabilities",
        "creation_model",
    }
    unknown = set(value) - required - {"manifest_digest"}
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
        authoring=value["authoring"],
        runtime_capabilities=value["runtime_capabilities"],
        creation_model=value["creation_model"],
        manifest_digest=value.get("manifest_digest", ""),
        schema_version=value["schema_version"],
    )
    _validate_manifest_fields(manifest)
    schema_error = first_schema_error(_manifest_schema(), value)
    if schema_error is not None:
        raise PackageIntegrityError(f"package manifest schema invalid {schema_error}")
    return manifest


def _manifest_schema() -> dict[str, Any]:
    schema_path = _CONTRACT_ROOT / PACKAGE_SCHEMA_VERSION / "schema.json"
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageIntegrityError(f"cannot read artifact package schema: {exc}") from exc


def _validate_manifest_fields(manifest: PackageManifest) -> None:
    if manifest.schema_version != PACKAGE_SCHEMA_VERSION:
        raise PackageIntegrityError("unknown artifact package schema version")
    if not manifest.manifest_digest:
        raise PackageIntegrityError("manifest digest is missing")
    if manifest.manifest_digest != _manifest_digest(manifest.to_dict()):
        raise PackageIntegrityError("manifest digest mismatch")
    _validate_manifest_names(manifest)
    if not _is_source_digest_map(manifest.source_digests):
        raise PackageIntegrityError("manifest source_digests must contain SHA-256 strings")
    for value in (
        manifest.authoring,
        manifest.runtime_capabilities,
        manifest.creation_model,
    ):
        _validate_manifest_metadata(value)
    if not isinstance(manifest.members, list):
        raise PackageIntegrityError("manifest members must be a list")


def _validate_manifest_names(manifest: PackageManifest) -> None:
    for name in ("package_id", "scenario_id", "input_kind"):
        if not isinstance(getattr(manifest, name), str) or not getattr(manifest, name):
            raise PackageIntegrityError(f"manifest field is blank: {name}")
    if manifest.input_kind not in _INPUT_KINDS:
        raise PackageIntegrityError(f"unsupported package input kind: {manifest.input_kind}")


def _is_source_digest_map(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(key, str) and bool(key) and is_sha256_hex(digest)
            for key, digest in value.items()
        )
    )


def _validate_manifest_metadata(value: Any) -> None:
    if value is not None and not isinstance(value, dict):
        raise PackageIntegrityError("manifest metadata must be objects")
    if value is not None and secret_metadata_paths(value):
        raise PackageIntegrityError("manifest contains secret-bearing metadata")


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
    _validate_claim_level_member(normalized)


def _validate_claim_level_member(members: dict[str, bytes]) -> None:
    plan_bytes = members.get("plan.json")
    if plan_bytes is None:
        return
    try:
        plan = json.loads(plan_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageIntegrityError(f"package plan.json is not JSON: {exc}") from exc
    claim = plan.get("observation_claim") if isinstance(plan, dict) else None
    level = claim.get("claim_level") if isinstance(claim, dict) else None
    required = _CLAIM_LEVEL_MEMBERS.get(level)
    if required is not None and required not in members:
        raise PackageIntegrityError(f"{level} package requires member: {required}")


def tool_call_condition_bytes(condition: dict[str, Any]) -> bytes:
    """Serialize the handoff tool-call condition as the package member bytes."""

    text = json.dumps(condition, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def _manifest_digest(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("manifest_digest", None)
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def validate_artifact_package_contract() -> None:
    """Verify the consumer-owned contract lock before package IO."""

    verify_contract_lock(
        _CONTRACT_ROOT,
        PackageIntegrityError,
        lock_label="artifact package contract lock",
        member_label="artifact package contract",
        metadata={"authority": "asago-artifact-generator"},
        metadata_message="artifact package contract authority mismatch",
    )


__all__ = [
    "ArtifactPackage",
    "PACKAGE_SCHEMA_VERSION",
    "PackageIntegrityError",
    "PackageManifest",
    "PackagePathError",
    "TOOL_CALL_CONDITION_MEMBER",
    "build_package",
    "load_package",
    "tool_call_condition_bytes",
    "write_package",
    "validate_artifact_package_contract",
]
