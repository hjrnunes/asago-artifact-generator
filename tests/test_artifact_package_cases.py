"""The consumer reader runs every artifact-package contract case.

The cases live in ``contracts/artifact-package/`` and the orchestrator runs the
same files from its mirror. Each invalid case names a stable code;
``_MESSAGES`` maps the code to this reader's error wording, and
``_VERSION_MESSAGES`` names the wording a version's schema gives first.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

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
_VERSIONS = ("artifact-package-v4", "artifact-package-v5")
_SEED = "artifact-seed-v1"
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
    "authoring_field_unexpected": (
        r"^package manifest schema invalid at authoring(\.review_status)?: "
        r"Additional properties are not allowed "
    ),
    "creation_model_not_empty": r"^package manifest schema invalid at creation_model: ",
}
# The v5 schema lists the member names, so it rejects any other path before the path checks run.
_V5_MEMBER_PATH = r"^package manifest schema invalid at members\.\d+\.path: .* is not one of "
_VERSION_MESSAGES = {
    "artifact-package-v5": {
        "member_path_invalid": _V5_MEMBER_PATH,
        "member_path_escapes": _V5_MEMBER_PATH,
        "member_path_non_canonical": _V5_MEMBER_PATH,
        "member_path_unexpected": _V5_MEMBER_PATH,
    },
}


def _message(version: str, code: str) -> str:
    return _VERSION_MESSAGES.get(version, {}).get(code, _MESSAGES[code])


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

    message = _message(case.parents[1].name, code)
    assert re.search(message, str(raised.value)), (code, str(raised.value))


def _first_seed_error(document: Any) -> ValidationError | None:
    schema = _json(_ROOT / _SEED / "schema.json")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    return errors[0] if errors else None


def test_the_seed_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(_json(_ROOT / _SEED / "schema.json"))


@pytest.mark.parametrize("case", _cases(_SEED, "valid"), ids=lambda case: case.stem)
def test_every_valid_seed_case_passes_the_seed_schema(case: Path) -> None:
    assert _first_seed_error(_json(case)) is None


def test_every_invalid_seed_case_names_its_violation() -> None:
    expected = _json(_ROOT / _SEED / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases(_SEED, "invalid")}


@pytest.mark.parametrize("case", _cases(_SEED, "invalid"), ids=lambda case: case.stem)
def test_each_invalid_seed_case_fails_first_at_the_expected_location(case: Path) -> None:
    expected = _json(_ROOT / _SEED / "expected-violations.json")[f"invalid/{case.name}"]

    error = _first_seed_error(_json(case))

    assert error is not None
    assert "/" + "/".join(str(part) for part in error.path) == expected["path"]
    assert error.validator == expected["keyword"]


@pytest.mark.parametrize(
    "case",
    [case for version in _VERSIONS for case in _cases(version, "valid")],
    ids=lambda case: f"{case.parent.parent.name}/{case.stem}",
)
def test_the_seed_member_of_every_valid_package_follows_the_seed_schema(case: Path) -> None:
    seed = json.loads(_json(case)["files"]["seed.json"])

    assert _first_seed_error(seed) is None


_POLICY = _json(_ROOT / "metadata-policy.json")


def _package_with(authoring: dict[str, Any]) -> object:
    return build_package(
        package_id="usage-policy",
        scenario_id="usage-policy",
        input_kind="scenario-handoff-v5",
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
