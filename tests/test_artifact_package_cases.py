"""The consumer reader runs every artifact-package contract case.

The cases live in ``contracts/artifact-package/`` and orch runs the same files
from its mirror. Each invalid case names a stable code; ``_MESSAGES`` maps the
code to this reader's error wording.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.core import AuthoringError
from asago_artifact_generator.authoring.prompt_safety import assert_no_secrets
from asago_artifact_generator.metadata_policy import secret_metadata_paths
from asago_artifact_generator.package_io import (
    PackageIntegrityError,
    PackagePathError,
    build_package,
    load_package,
    validate_artifact_package_contract,
)

_REPO = Path(__file__).resolve().parents[1]
_ROOT = _REPO / "contracts" / "artifact-package"
_VERSIONS = ("artifact-package-v3", "artifact-package-v4")
_MESSAGES = {
    "manifest_not_object": r"^package manifest must be an object$",
    "manifest_fields_invalid": r"^package manifest fields invalid ",
    "identity_blank": r"^manifest field is blank: ",
    "source_digests_invalid": r"^manifest source_digests must contain SHA-256 strings$",
    "manifest_digest_mismatch": r"^manifest digest mismatch$",
    "members_empty": r"^package manifest schema invalid at members: \[\] should be non-empty$",
    "metadata_not_object": r"^manifest metadata must be objects$",
    "metadata_secret": r"^manifest contains secret-bearing metadata$",
    "input_kind_unsupported": r"^unsupported package input kind: ",
    "schema_version_unknown": r"^unknown artifact package schema version$",
    "member_length_mismatch": r"^length mismatch for package member: ",
    "member_media_type_mismatch": r"^member digest or metadata mismatch: ",
    # The vendored schema's member path pattern rejects these before the path checks run.
    "member_path_invalid": r"^package manifest schema invalid at members\.\d+\.path: ",
    "member_path_escapes": r"^package manifest schema invalid at members\.\d+\.path: ",
    "member_path_non_canonical": r"^non-canonical package member path: ",
    "member_path_unexpected": r"^unexpected package member path: ",
    "member_duplicate": r"^duplicate package member: ",
    "member_set_mismatch": r"^package member set does not match manifest$",
    "member_digest_mismatch": r"^digest mismatch for package member: ",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases(version: str, kind: str) -> list[Path]:
    return sorted((_ROOT / version / kind).glob("*.json"))


def _materialize(case: Path, destination: Path) -> Path:
    data = _json(case)
    destination.mkdir()
    for name, text in data["files"].items():
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        (destination / name).write_text(text, encoding="utf-8")
    (destination / "manifest.json").write_text(json.dumps(data["manifest"]), encoding="utf-8")
    return destination


def _generator() -> Any:
    path = _REPO / "scripts" / "gen_artifact_package_cases.py"
    spec = importlib.util.spec_from_file_location("gen_artifact_package_cases", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_cases_and_lock_are_the_generator_output() -> None:
    generator = _generator()
    files = generator.expected_files()
    on_disk = {
        path.relative_to(_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in _ROOT.rglob("*.json")
        if path.name != "schema.json"
    }
    lock = _json(_ROOT / "CONTRACT.lock")

    assert on_disk == files
    assert lock == generator.expected_lock(lock, files)
    validate_artifact_package_contract()


@pytest.mark.parametrize("version", _VERSIONS)
def test_every_invalid_case_names_a_code_this_reader_maps(version: str) -> None:
    expected = _json(_ROOT / version / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases(version, "invalid")}
    assert set(expected.values()) <= set(_MESSAGES)


def test_every_mapped_code_has_a_case() -> None:
    codes = {
        code
        for version in _VERSIONS
        for code in _json(_ROOT / version / "expected-violations.json").values()
    }

    assert codes == set(_MESSAGES)


@pytest.mark.parametrize(
    "case",
    [case for version in _VERSIONS for case in _cases(version, "valid")],
    ids=lambda case: f"{case.parent.parent.name}/{case.stem}",
)
def test_the_reader_loads_every_valid_case(tmp_path: Path, case: Path) -> None:
    package = load_package(_materialize(case, tmp_path / "package"))

    assert package.manifest.to_dict() == _json(case)["manifest"]
    assert {name: content.decode() for name, content in package.members.items()} == _json(case)[
        "files"
    ]


@pytest.mark.parametrize(
    "case",
    [case for version in _VERSIONS for case in _cases(version, "invalid")],
    ids=lambda case: f"{case.parent.parent.name}/{case.stem}",
)
def test_the_reader_rejects_every_invalid_case_with_its_code(tmp_path: Path, case: Path) -> None:
    code = _json(case.parents[1] / "expected-violations.json")[f"invalid/{case.name}"]

    with pytest.raises((PackageIntegrityError, PackagePathError)) as raised:
        load_package(_materialize(case, tmp_path / "package"))

    assert re.search(_MESSAGES[code], str(raised.value)), (code, str(raised.value))


_POLICY = _json(_ROOT / "metadata-policy.json")


def _package_with(authoring: dict[str, Any]) -> object:
    return build_package(
        package_id="usage-policy",
        scenario_id="usage-policy",
        input_kind="scenario-handoff-v4",
        source_digests={"input": "a" * 64},
        members={"explanation.json": b"{}\n"},
        authoring=authoring,
    )


@pytest.mark.parametrize("entry", _POLICY["allowed"], ids=lambda entry: entry["name"])
def test_the_metadata_policy_accepts_every_allowed_case(entry: dict[str, Any]) -> None:
    metadata = entry["metadata"]

    assert secret_metadata_paths(metadata) == []
    assert_no_secrets({"authoring": metadata})
    assert _package_with(metadata).manifest.authoring == metadata


@pytest.mark.parametrize("entry", _POLICY["rejected"], ids=lambda entry: entry["name"])
def test_the_metadata_policy_rejects_every_rejected_case_at_its_paths(
    entry: dict[str, Any],
) -> None:
    metadata = entry["metadata"]

    assert secret_metadata_paths(metadata) == entry["paths"]
    with pytest.raises(AuthoringError):
        assert_no_secrets({"authoring": metadata})
    with pytest.raises(PackageIntegrityError, match="secret-bearing metadata"):
        _package_with(metadata)


def test_the_metadata_policy_rejects_a_non_string_usage_key() -> None:
    # JSON cannot carry a non-string key, so this rule has no case file.
    assert secret_metadata_paths({"usage": {1: 1}}) == ["usage.1", "usage"]
    with pytest.raises(PackageIntegrityError, match="secret-bearing metadata"):
        _package_with({"usage": {1: 1}})
