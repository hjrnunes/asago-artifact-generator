"""Atomic, contained artifact-package-v2 persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from asago_artifact_generator import package_io
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

    assert loaded.manifest.schema_version == "artifact-package-v2"
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    real_write_bytes = Path.write_bytes
    written: list[Path] = []

    def write_one_member_then_fail(self: Path, data: bytes) -> int:
        if written:
            raise RuntimeError("interrupted package write")
        written.append(self)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_one_member_then_fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        write_package(destination, replacement)
    monkeypatch.undo()

    assert len(written) == 1

    assert load_package(destination).members["detector.py"] == original
    assert not list(tmp_path.glob(".package.*.tmp"))


def _replacement_package() -> ArtifactPackage:
    return build_package(
        package_id="pkg-2",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v1",
        source_digests={"scenario.json": "b" * 64},
        members={**_package().members, "detector.py": b"replacement\n"},
    )


def _leftovers(parent: Path) -> list[Path]:
    return sorted(parent.glob(".package.*.tmp"))


def _fail_install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    partial: str | None = None,
) -> None:
    """Make the move of the written package onto its destination fail.

    ``partial`` leaves a directory or file at the destination first, as an
    interrupted rename could.
    """

    real_replace = package_io.os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        if Path(source).name.startswith(".package.") and ".backup." not in Path(source).name:
            if partial == "dir":
                Path(target).mkdir()
                (Path(target) / "detector.py").write_bytes(b"partial\n")
            elif partial == "file":
                Path(target).write_bytes(b"partial\n")
            raise OSError("install failed")
        real_replace(source, target)

    monkeypatch.setattr(package_io.os, "replace", replace)


def test_overwrite_replaces_previous_package_and_removes_its_backup(tmp_path: Path) -> None:
    destination = write_package(tmp_path / "package", _package())

    write_package(destination, _replacement_package())

    loaded = load_package(destination)
    assert loaded.manifest.package_id == "pkg-2"
    assert loaded.members["detector.py"] == b"replacement\n"
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("partial", [None, "dir", "file"])
def test_failed_install_restores_previous_package_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: str | None
) -> None:
    destination = write_package(tmp_path / "package", _package())
    _fail_install(monkeypatch, partial=partial)

    with pytest.raises(OSError, match="install failed"):
        write_package(destination, _replacement_package())

    restored = load_package(destination)
    assert restored.manifest.package_id == "pkg-1"
    assert restored.members == _package().members
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("partial", [None, "dir", "file"])
def test_failed_first_install_leaves_no_package_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: str | None
) -> None:
    _fail_install(monkeypatch, partial=partial)

    with pytest.raises(OSError, match="install failed"):
        write_package(tmp_path / "package", _package())

    assert not (tmp_path / "package").exists()
    assert _leftovers(tmp_path) == []


def test_overwrite_replaces_a_symlinked_destination_without_touching_its_target(
    tmp_path: Path,
) -> None:
    target = write_package(tmp_path / "target", _package())
    destination = tmp_path / "package"
    destination.symlink_to(target, target_is_directory=True)

    write_package(destination, _replacement_package())

    assert not destination.is_symlink()
    assert load_package(destination).manifest.package_id == "pkg-2"
    assert load_package(target).manifest.package_id == "pkg-1"
    assert _leftovers(tmp_path) == []
