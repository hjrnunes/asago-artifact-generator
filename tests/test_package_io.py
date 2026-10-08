"""Atomic, contained artifact-package persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator import package_io
from asago_artifact_generator.package_io import (
    ArtifactPackage,
    PackageIntegrityError,
    PackagePathError,
    build_package,
    load_package,
    tool_call_condition_bytes,
    write_package,
)


def _package() -> ArtifactPackage:
    return build_package(
        package_id="pkg-1",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v4",
        source_digests={"scenario.json": "a" * 64},
        members={
            "plan.json": b'{"plan":"exact"}\n',
            "stimulus.json": b'{"user_text":"hello"}\n',
            "explanation.json": b'{"text":"exact explanation"}\n',
            "authoring/raw-response.json": b'{"raw":true}\n',
        },
    )


def test_package_round_trip_reloads_and_preserves_raw_member_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "package"
    package = _package()

    written = write_package(destination, package)
    loaded = load_package(written)

    assert loaded.manifest.schema_version == "artifact-package-v4"
    assert loaded.manifest.package_id == "pkg-1"
    assert loaded.members["authoring/raw-response.json"] == b'{"raw":true}\n'
    assert loaded.members["explanation.json"] == package.members["explanation.json"]


@pytest.mark.parametrize("name", ["/absolute.json", "../escape.json", "a/../../escape.json"])
def test_package_rejects_absolute_and_traversal_members(tmp_path: Path, name: str) -> None:
    with pytest.raises(PackagePathError):
        write_package(
            tmp_path / "package",
            build_package(
                package_id="pkg-1",
                scenario_id="scenario-1",
                input_kind="scenario-handoff-v4",
                source_digests={"source": "a" * 64},
                members={name: b"x"},
            ),
        )


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
                input_kind="scenario-handoff-v4",
                source_digests={"source": "a" * 64},
                members={"explanation.json": b"{}\n"},
                creation_model={"api_key": "not persisted"},
            ),
        )


def test_interrupted_write_leaves_no_partial_package_and_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = write_package(tmp_path / "package", _package())
    original = load_package(destination).members["explanation.json"]
    replacement = build_package(
        package_id="pkg-1",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v4",
        source_digests={"scenario.json": "a" * 64},
        members={
            **_package().members,
            "explanation.json": b"replacement\n",
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

    assert load_package(destination).members["explanation.json"] == original
    assert not list(tmp_path.glob(".package.*.tmp"))


def _replacement_package() -> ArtifactPackage:
    return build_package(
        package_id="pkg-2",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v4",
        source_digests={"scenario.json": "b" * 64},
        members={**_package().members, "explanation.json": b"replacement\n"},
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
                (Path(target) / "explanation.json").write_bytes(b"partial\n")
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
    assert loaded.members["explanation.json"] == b"replacement\n"
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


def _rewrite_manifest(destination: Path, mutate, *, redigest: bool = True) -> None:
    import json

    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    if redigest:
        manifest["manifest_digest"] = package_io._manifest_digest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _set_field(name: str, value: object):
    return lambda manifest: manifest.__setitem__(name, value)


@pytest.mark.parametrize(
    ("mutate", "redigest", "message"),
    [
        (_set_field("manifest_digest", ""), False, "manifest digest is missing"),
        (_set_field("scenario_id", ""), True, "manifest field is blank: scenario_id"),
        (_set_field("members", "plan.json"), True, "manifest members must be a list"),
        (lambda manifest: manifest.pop("authoring"), True, "package manifest fields invalid"),
    ],
)
def test_loader_rejects_an_invalid_manifest(
    tmp_path: Path, mutate, redigest: bool, message: str
) -> None:
    destination = write_package(tmp_path / "package", _package())
    _rewrite_manifest(destination, mutate, redigest=redigest)

    with pytest.raises(PackageIntegrityError, match=message):
        load_package(destination)


def test_loader_rejects_a_manifest_that_violates_the_vendored_schema(tmp_path: Path) -> None:
    destination = write_package(tmp_path / "package", _package())
    _rewrite_manifest(destination, _set_field("authoring", None))

    with pytest.raises(PackageIntegrityError) as raised:
        load_package(destination)

    assert str(raised.value) == (
        "package manifest schema invalid at authoring: None is not of type 'object'"
    )


def test_writer_rejects_a_package_without_members(tmp_path: Path) -> None:
    empty = build_package(
        package_id="pkg-1",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v4",
        source_digests={"scenario.json": "a" * 64},
        members={},
    )

    with pytest.raises(
        PackageIntegrityError, match="^package manifest schema invalid at members: "
    ):
        write_package(tmp_path / "package", empty)
    assert not (tmp_path / "package").exists()


def test_loader_rejects_a_package_whose_layout_differs_from_the_manifest(
    tmp_path: Path,
) -> None:
    extra_directory = write_package(tmp_path / "extra-directory", _package())
    (extra_directory / "authoring" / "empty").mkdir()
    missing_member = write_package(tmp_path / "missing-member", _package())
    (missing_member / "plan.json").unlink()
    linked = write_package(tmp_path / "linked", _package())
    (linked / "authoring" / "link.json").symlink_to(linked / "plan.json")
    unreadable = write_package(tmp_path / "unreadable", _package())
    (unreadable / "manifest.json").write_text("{", encoding="utf-8")

    cases = [
        (extra_directory, "package directory set does not match manifest"),
        (missing_member, "missing package member: plan.json"),
        (linked, "symlink is not allowed in package"),
        (unreadable, "invalid package manifest"),
        (tmp_path / "absent", "package directory is unavailable"),
    ]
    for destination, message in cases:
        with pytest.raises(PackageIntegrityError, match=message):
            load_package(destination)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("a\\b.json", "invalid package member path: 'a\\\\b.json'"),
        ("./plan.json", "non-canonical package member path: './plan.json'"),
        ("notes.txt", "unexpected package member path: notes.txt"),
    ],
)
def test_build_package_names_the_rejected_member_path(name: str, message: str) -> None:
    with pytest.raises(PackagePathError) as caught:
        build_package(
            package_id="pkg-1",
            scenario_id="scenario-1",
            input_kind="scenario-handoff-v4",
            source_digests={"source": "a" * 64},
            members={name: b"x"},
        )
    assert str(caught.value) == message


def _claim_package(claim_level: str, extra: dict[str, bytes]) -> ArtifactPackage:
    plan = {"observation_claim": {"claim_level": claim_level}}
    return build_package(
        package_id="pkg-claim",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v4",
        source_digests={"scenario.json": "a" * 64},
        members={"plan.json": json.dumps(plan).encode() + b"\n", **extra},
    )


def test_manifest_carries_no_detector_interface_and_rejects_a_detector_member(
    tmp_path: Path,
) -> None:
    destination = write_package(tmp_path / "package", _package())
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "artifact-package-v4"
    assert "detector_interface" not in manifest
    assert set(manifest) == {
        "schema_version",
        "package_id",
        "scenario_id",
        "input_kind",
        "source_digests",
        "authoring",
        "runtime_capabilities",
        "creation_model",
        "members",
        "manifest_digest",
    }
    with pytest.raises(PackagePathError, match="unexpected package member path: detector.py"):
        _claim_package("reply", {"judge.json": b"{}\n", "detector.py": b"x\n"})


def test_tool_call_condition_bytes_are_sorted_indented_json_with_a_newline() -> None:
    condition = {"comparisons": [{"op": "gt", "kind": "value", "right": {"value": "é"}}]}

    assert tool_call_condition_bytes(condition) == (
        b'{\n  "comparisons": [\n    {\n      "kind": "value",\n      "op": "gt",\n'
        b'      "right": {\n        "value": "\xc3\xa9"\n      }\n    }\n  ]\n}\n'
    )


@pytest.mark.parametrize(
    ("claim_level", "member"),
    [("command_attempt", "tool_call_condition.json"), ("reply", "judge.json")],
)
def test_claim_level_requires_its_scoring_member(
    tmp_path: Path, claim_level: str, member: str
) -> None:
    with pytest.raises(
        PackageIntegrityError, match=f"{claim_level} package requires member: {member}"
    ):
        write_package(tmp_path / "missing", _claim_package(claim_level, {}))

    written = write_package(tmp_path / "present", _claim_package(claim_level, {member: b"{}\n"}))
    assert load_package(written).members[member] == b"{}\n"


def test_written_manifest_matches_the_locked_v4_schema(tmp_path: Path) -> None:
    import jsonschema

    root = Path(package_io.__file__).resolve().parents[2] / "contracts" / "artifact-package"
    lock = json.loads((root / "CONTRACT.lock").read_text(encoding="utf-8"))
    schema = json.loads((root / "artifact-package-v4" / "schema.json").read_text(encoding="utf-8"))
    destination = write_package(tmp_path / "package", _package())
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))

    assert lock["package_schema_version"] == package_io.PACKAGE_SCHEMA_VERSION
    assert lock["digest_domain"] == "artifact-package-v3"
    assert "artifact-package-v4/schema.json" in lock["files"]
    assert "artifact-package-v2/schema.json" not in lock["files"]
    jsonschema.validate(manifest, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**manifest, "detector_interface": "x"}, schema)


@pytest.mark.parametrize("schema_text", [None, "{not json"])
def test_unreadable_manifest_schema_is_an_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_text: str | None
) -> None:
    monkeypatch.setattr(package_io, "_CONTRACT_ROOT", tmp_path)
    if schema_text is not None:
        schema = tmp_path / package_io.PACKAGE_SCHEMA_VERSION / "schema.json"
        schema.parent.mkdir()
        schema.write_text(schema_text, encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="^cannot read artifact package schema: "):
        package_io._manifest_schema()
