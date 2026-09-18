"""Atomic, contained artifact-package-v1 persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from asago_artifact_generator.package_io import (
    ArtifactPackage,
    PackageIntegrityError,
    PackagePathError,
    build_package,
    load_package,
    write_package,
)


def _package() -> ArtifactPackage:
    return build_package(
        package_id="pkg-1",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v1",
        source_digests={"scenario.json": "a" * 64},
        members={
            "plan.json": b'{"plan":"exact"}\n',
            "stimulus.json": b'{"user_text":"hello"}\n',
            "detector.py": (
                b"def evaluate(evidence: dict) -> dict:\n"
                b"    return {'outcome': 'inconclusive', 'reason': 'no evidence', "
                b"'evidence_refs': [], 'claim_level': 'reply'}\n"
            ),
            "authoring/raw-response.json": b'{"raw":true}\n',
        },
    )


def test_package_round_trip_reloads_and_preserves_raw_member_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "package"
    package = _package()

    written = write_package(destination, package)
    loaded = load_package(written)

    assert loaded.manifest.schema_version == "artifact-package-v1"
    assert loaded.manifest.package_id == "pkg-1"
    assert loaded.members["authoring/raw-response.json"] == b'{"raw":true}\n'
    assert loaded.members["detector.py"] == package.members["detector.py"]


@pytest.mark.parametrize("name", ["/absolute.json", "../escape.json", "a/../../escape.json"])
def test_package_rejects_absolute_and_traversal_members(tmp_path: Path, name: str) -> None:
    with pytest.raises(PackagePathError):
        write_package(
            tmp_path / "package",
            build_package(
                package_id="pkg-1",
                scenario_id="scenario-1",
                input_kind="scenario-handoff-v1",
                source_digests={"source": "a" * 64},
                members={name: b"x"},
            ),
        )


def test_loader_rejects_member_tampering_before_exposing_content(tmp_path: Path) -> None:
    destination = write_package(tmp_path / "package", _package())
    (destination / "detector.py").write_bytes(b"def evaluate(evidence): return {}\n")

    with pytest.raises(PackageIntegrityError, match="mismatch"):
        load_package(destination)


def test_writer_rejects_unexpected_member_declared_in_manifest(tmp_path: Path) -> None:
    package = _package()
    package.manifest.members.append(
        {"path": "unexpected.txt", "media_type": "text/plain", "length": 1, "sha256": "x"}
    )

    with pytest.raises(PackageIntegrityError, match="mismatch"):
        write_package(tmp_path / "package", package)


def test_writer_rejects_secret_bearing_manifest_metadata(tmp_path: Path) -> None:
    with pytest.raises(PackageIntegrityError, match="secret"):
        write_package(
            tmp_path / "package",
            build_package(
                package_id="pkg-1",
                scenario_id="scenario-1",
                input_kind="scenario-handoff-v1",
                source_digests={"source": "a" * 64},
                members={"detector.py": b"source\n"},
                creation_model={"api_key": "not persisted"},
            ),
        )


def test_interrupted_write_leaves_no_partial_package_and_preserves_previous(
    tmp_path: Path,
) -> None:
    destination = write_package(tmp_path / "package", _package())
    original = load_package(destination).members["detector.py"]
    replacement = build_package(
        package_id="pkg-1",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v1",
        source_digests={"scenario.json": "a" * 64},
        members={
            **_package().members,
            "detector.py": b"replacement\n",
        },
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        write_package(destination, replacement, fail_after_members=1)

    assert load_package(destination).members["detector.py"] == original
    assert not list(tmp_path.glob(".package.*.tmp"))
