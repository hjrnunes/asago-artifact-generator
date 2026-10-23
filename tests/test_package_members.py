"""An accepted package holds only the members a reader uses.

Each request's prompt, reply, ledger record, and review stay in the sidecar
failure evidence; the package keeps no copy. A package written before then
(``fixtures/package_with_request_copies``, written by c18e46d's writer) still
loads: its copies are verified and dropped. The writer refuses them, and the
loader refuses any other member.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.package_io import (
    PackagePathError,
    _manifest_digest,
    _member_record,
    build_package,
    load_package,
)

from .support import review_response, scripted_orchestrator, world_builders

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)
_orchestrator = partial(scripted_orchestrator, task_id="members")
_OLD_PACKAGE = Path(__file__).parent / "fixtures" / "package_with_request_copies" / "package"
# What c18e46d's writer put in a package beside the members still written: per
# request ``authoring/NN-<stage>`` with ``.json``, ``.prompt`` and ``.raw``, for the
# stages call1, call2, correction, plan_review and artifact_review.
_RETIRED_MEMBERS = (
    "source-hashes.json",
    "authoring/ledger.json",
    "authoring/reviews.json",
    "authoring/transformations.json",
    "authoring/01-call1.json",
    "authoring/02-plan_review.prompt",
    "authoring/03-call2.raw",
    "authoring/04-correction.json",
    "authoring/05-artifact_review.raw",
    "authoring/100-correction.prompt",
)
_UNKNOWN_MEMBERS = (
    "extra.json",
    "source-hashes.yaml",
    "authoring/raw-response.json",
    "authoring/notes.json",
    "authoring/01-call1.txt",
    "authoring/1-call1.json",
    "authoring/01-review.json",
    "authoring/01-call1.json/inner.json",
)


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    orchestrator, _ = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), review_response(), _framed(), review_response()],
        policy=AuthoringPolicy(),
    )
    result = orchestrator.run(_view(), _inventory(), _runtime_contract())
    assert result.status == "accepted"
    return tmp_path / "package"


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _add_member(package_dir: Path, member: str) -> None:
    """Write ``member`` into the package and reseal the manifest around it."""

    content = b"{}\n"
    target = package_dir / member
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["members"] = sorted(
        [*manifest["members"], _member_record(member, content)], key=lambda item: item["path"]
    )
    manifest["manifest_digest"] = _manifest_digest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_an_accepted_package_holds_only_the_read_members(package_dir: Path) -> None:
    assert _files(package_dir) == [
        "bindings.json",
        "checks.json",
        "examples.json",
        "explanation.json",
        "inputs.json",
        "manifest.json",
        "observations.json",
        "plan.json",
        "prerequisites.json",
        "setup.json",
        "stimulus.json",
        "tool_call_condition.json",
    ]


def test_the_kept_members_have_the_bytes_c18e46d_wrote(package_dir: Path) -> None:
    # inputs.json lost its copies of the handoff; the manifest lost the ledger.
    for member in _files(package_dir):
        if member not in {"inputs.json", "manifest.json"}:
            assert (package_dir / member).read_bytes() == (_OLD_PACKAGE / member).read_bytes()


def test_the_manifest_keeps_only_the_authoring_outcome(package_dir: Path) -> None:
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["authoring"] == {
        "status": "accepted",
        "terminal_status": "accepted",
        "review_status": {"artifact": "accepted"},
    }
    # Kept, empty: the artifact-package-v4 schema and downstream loaders require the key.
    assert manifest["creation_model"] == {}


def test_inputs_keep_the_runtime_inputs_and_their_provenance(package_dir: Path) -> None:
    inputs = json.loads((package_dir / "inputs.json").read_text(encoding="utf-8"))
    checks = json.loads((package_dir / "checks.json").read_text(encoding="utf-8"))

    assert sorted(inputs) == [
        "authoring_input_pins",
        "discovery_provenance",
        "inventory",
        "runtime_contract",
    ]
    assert inputs["authoring_input_pins"]["input_sha256"] == _view().source_sha256
    assert checks == {"interface": "artifact-authoring-v2", "status": "structurally_valid"}


def test_a_package_written_by_c18e46d_loads_without_its_request_copies(
    package_dir: Path,
) -> None:
    loaded = load_package(_OLD_PACKAGE)

    current = [name for name in _files(package_dir) if name != "manifest.json"]
    assert sorted(loaded.members) == current
    assert sorted(set(_files(_OLD_PACKAGE)) - {"manifest.json", *current}) == [
        "authoring/01-call1.json",
        "authoring/01-call1.prompt",
        "authoring/01-call1.raw",
        "authoring/02-plan_review.json",
        "authoring/02-plan_review.prompt",
        "authoring/02-plan_review.raw",
        "authoring/03-call2.json",
        "authoring/03-call2.prompt",
        "authoring/03-call2.raw",
        "authoring/04-artifact_review.json",
        "authoring/04-artifact_review.prompt",
        "authoring/04-artifact_review.raw",
        "authoring/ledger.json",
        "authoring/reviews.json",
        "authoring/transformations.json",
        "source-hashes.json",
    ]
    # The manifest is the one c18e46d sealed, ledger and creation model included.
    assert loaded.manifest.authoring["attempts"] == 4
    assert len(loaded.manifest.authoring["ledger"]) == 4
    assert loaded.manifest.creation_model["model"] == "configured-private-authoring"
    assert len(loaded.manifest.members) == len(_files(_OLD_PACKAGE)) - 1


@pytest.mark.parametrize("member", _RETIRED_MEMBERS)
def test_the_loader_verifies_and_drops_a_retired_member(package_dir: Path, member: str) -> None:
    expected = sorted(load_package(package_dir).members)
    _add_member(package_dir, member)

    assert sorted(load_package(package_dir).members) == expected


@pytest.mark.parametrize("member", _UNKNOWN_MEMBERS)
def test_the_loader_rejects_any_other_member(package_dir: Path, member: str) -> None:
    _add_member(package_dir, member)

    with pytest.raises(PackagePathError, match="unexpected package member path"):
        load_package(package_dir)


@pytest.mark.parametrize("member", _RETIRED_MEMBERS)
def test_the_writer_refuses_a_retired_member(member: str) -> None:
    with pytest.raises(PackagePathError, match="unexpected package member path"):
        build_package(
            package_id="pkg-1",
            scenario_id="SCN-1",
            input_kind="scenario-handoff-v5",
            source_digests={"input": "a" * 64},
            members={"plan.json": b"{}\n", member: b"{}\n"},
        )
