"""The artifact-package-v1 values, writer and reader, exercised with hand-built data.

Every case writes the manifest and member bytes itself: the writer takes a hand-built
``PackageManifest`` and the reader takes a hand-built package directory, so the cases come
from the contract's own rules rather than from the module's builders. Only the digest is
computed here, over the canonical JSON the contract defines, which makes the expected value
independent of the module.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from asago_package.package_io import (
    PACKAGE_SCHEMA_VERSION,
    ArtifactPackage,
    PackageIntegrityError,
    PackageManifest,
    PackagePathError,
    build_package,
    load_package,
    write_package,
)

V5 = "artifact-package-v1"
EXPLANATION = b'{"text":"hand built"}\n'
REPLY_PLAN = b'{"observation_claim":{"claim_level":"reply"}}\n'


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _record(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "media_type": "application/json",
        "length": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _digest(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _manifest(**changes: Any) -> dict[str, Any]:
    """A hand-built v5 manifest, sealed the way the contract says a manifest is sealed."""

    manifest: dict[str, Any] = {
        "schema_version": V5,
        "package_id": "pkg-hand-built",
        "scenario_id": "SCN-HAND-1",
        "input_kind": "scenario-handoff-v1",
        "source_digests": {"handoff.json": "a" * 64},
        "members": [_record("explanation.json", EXPLANATION)],
        "authoring": {"status": "produced"},
        "runtime_capabilities": {"delivery": ["direct_user_message"]},
        "creation_model": {},
    }
    manifest.update(changes)
    manifest["manifest_digest"] = _digest(manifest)
    return manifest


def _manifest_object(manifest: dict[str, Any]) -> PackageManifest:
    return PackageManifest(
        package_id=manifest["package_id"],
        scenario_id=manifest["scenario_id"],
        input_kind=manifest["input_kind"],
        source_digests=manifest["source_digests"],
        members=manifest["members"],
        authoring=manifest["authoring"],
        runtime_capabilities=manifest["runtime_capabilities"],
        creation_model=manifest["creation_model"],
        manifest_digest=manifest["manifest_digest"],
        schema_version=manifest["schema_version"],
    )


def _package(
    *, manifest: dict[str, Any] | None = None, members: dict[str, bytes] | None = None
) -> ArtifactPackage:
    manifest = _manifest() if manifest is None else manifest
    members = {"explanation.json": EXPLANATION} if members is None else members
    return ArtifactPackage(_manifest_object(manifest), members)


def _materialize(directory: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> Path:
    """Write a package directory by hand: the manifest and each member, nothing else."""

    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return directory


def _written(tmp_path: Path) -> Path:
    return write_package(tmp_path / "package", _package())


def test_a_hand_built_package_round_trips_through_the_writer_and_the_reader(
    tmp_path: Path,
) -> None:
    manifest = _manifest()

    written = write_package(tmp_path / "package", _package(manifest=manifest))
    loaded = load_package(written)

    assert PACKAGE_SCHEMA_VERSION == manifest["schema_version"]
    assert loaded.manifest.to_dict() == manifest
    assert loaded.members == {"explanation.json": EXPLANATION}


def test_the_reader_loads_a_hand_built_package_directory(tmp_path: Path) -> None:
    manifest = _manifest()

    loaded = load_package(
        _materialize(tmp_path / "package", manifest, {"explanation.json": EXPLANATION})
    )

    assert loaded.manifest.package_id == "pkg-hand-built"
    assert loaded.manifest.manifest_digest == manifest["manifest_digest"]
    assert loaded.members["explanation.json"] == EXPLANATION


def test_build_package_seals_the_same_manifest_the_hand_built_case_states() -> None:
    package = build_package(
        package_id="pkg-hand-built",
        scenario_id="SCN-HAND-1",
        input_kind="scenario-handoff-v1",
        source_digests={"handoff.json": "a" * 64},
        members={"explanation.json": EXPLANATION},
        authoring={"status": "produced"},
        runtime_capabilities={"delivery": ["direct_user_message"]},
    )

    assert package.manifest.to_dict() == _manifest()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"manifest_digest": "c" * 64}, "manifest digest mismatch"),
        ({"schema_version": "artifact-package-v9"}, "the writer writes only"),
    ],
)
def test_the_writer_refuses_a_hand_built_manifest_it_would_not_load(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    manifest = {**_manifest(), **overrides}

    with pytest.raises(PackageIntegrityError, match=message):
        write_package(tmp_path / "package", _package(manifest=manifest))


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("/absolute.json", PackagePathError),
        ("../escape.json", PackagePathError),
        ("a/../escape.json", PackagePathError),
        ("a//b.json", PackagePathError),
        ("source-hashes.json", PackagePathError),
    ],
)
def test_the_writer_refuses_a_hand_built_member_path_it_does_not_own(
    tmp_path: Path, name: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        write_package(
            tmp_path / "package",
            build_package(
                package_id="pkg-hand-built",
                scenario_id="SCN-HAND-1",
                input_kind="scenario-handoff-v1",
                source_digests={"handoff.json": "a" * 64},
                members={"explanation.json": EXPLANATION, name: b"{}\n"},
            ),
        )


def test_the_writer_refuses_a_hand_built_member_that_is_not_bytes(tmp_path: Path) -> None:
    with pytest.raises(PackageIntegrityError, match="package member is not bytes"):
        build_package(
            package_id="pkg-hand-built",
            scenario_id="SCN-HAND-1",
            input_kind="scenario-handoff-v1",
            source_digests={"handoff.json": "a" * 64},
            members={"explanation.json": "text"},  # type: ignore[dict-item]
        )


def test_the_reader_refuses_a_manifest_missing_a_required_field(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.pop("authoring")
    manifest["manifest_digest"] = _digest(manifest)

    with pytest.raises(PackageIntegrityError, match="package manifest fields invalid"):
        load_package(
            _materialize(tmp_path / "package", manifest, {"explanation.json": EXPLANATION})
        )


def test_the_reader_refuses_a_manifest_digest_that_does_not_cover_its_fields(
    tmp_path: Path,
) -> None:
    directory = _written(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scenario_id"] = "SCN-CHANGED"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="manifest digest mismatch"):
        load_package(directory)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"package_id": ""}, "manifest field is blank: package_id"),
        ({"input_kind": "scenario-handoff-v9"}, "unsupported package input kind"),
        (
            {"schema_version": "artifact-package-v9"},
            "unsupported artifact package schema version: artifact-package-v9",
        ),
        (
            {"runtime_capabilities": {"api_key": "not persisted"}},
            "manifest runtime_capabilities contains secret-bearing metadata",
        ),
        (
            {"authoring": {"status": "produced", "api_key": "not persisted"}},
            "manifest authoring contains secret-bearing metadata",
        ),
        ({"authoring": None}, "package manifest schema invalid at authoring"),
    ],
)
def test_the_reader_refuses_hand_built_manifest_values_the_schema_closes(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    directory = _materialize(
        tmp_path / "package", _manifest(**changes), {"explanation.json": EXPLANATION}
    )

    with pytest.raises(PackageIntegrityError, match=message):
        load_package(directory)


def test_the_reader_refuses_a_package_member_the_manifest_does_not_declare(
    tmp_path: Path,
) -> None:
    directory = _written(tmp_path)
    (directory / "checks.json").write_bytes(b"{}\n")

    with pytest.raises(PackageIntegrityError, match="package member set does not match manifest"):
        load_package(directory)


def test_the_reader_refuses_a_member_whose_bytes_changed(tmp_path: Path) -> None:
    directory = _written(tmp_path)
    (directory / "explanation.json").write_bytes(b'{"text":"tampered"}\n')

    with pytest.raises(PackageIntegrityError, match="digest mismatch for package member"):
        load_package(directory)


def test_the_reader_refuses_a_member_that_is_missing(tmp_path: Path) -> None:
    directory = _written(tmp_path)
    (directory / "explanation.json").unlink()

    with pytest.raises(PackageIntegrityError, match="missing package member: explanation.json"):
        load_package(directory)


def test_the_reader_refuses_a_symlink_inside_the_package(tmp_path: Path) -> None:
    directory = _written(tmp_path)
    target = tmp_path / "outside.json"
    target.write_bytes(b"{}\n")
    os.symlink(target, directory / "checks.json")

    with pytest.raises(PackageIntegrityError, match="symlink is not allowed in package"):
        load_package(directory)


def test_the_reader_refuses_a_reply_plan_without_its_judge_member(tmp_path: Path) -> None:
    manifest = _manifest(members=[_record("plan.json", REPLY_PLAN)])
    directory = _materialize(tmp_path / "package", manifest, {"plan.json": REPLY_PLAN})

    with pytest.raises(PackageIntegrityError, match="reply package requires member: judge.json"):
        load_package(directory)


def test_the_writer_refuses_a_reply_plan_without_its_judge_member(tmp_path: Path) -> None:
    package = build_package(
        package_id="pkg-hand-built",
        scenario_id="SCN-HAND-1",
        input_kind="scenario-handoff-v1",
        source_digests={"handoff.json": "a" * 64},
        members={"plan.json": REPLY_PLAN},
    )

    with pytest.raises(PackageIntegrityError, match="reply package requires member: judge.json"):
        write_package(tmp_path / "package", package)


def test_the_reader_refuses_a_directory_that_is_not_a_package(tmp_path: Path) -> None:
    directory = tmp_path / "empty"
    directory.mkdir()

    with pytest.raises(PackageIntegrityError, match="invalid package manifest"):
        load_package(directory)
