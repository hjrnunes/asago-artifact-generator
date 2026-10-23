"""An accepted package holds only the members a reader uses.

Each request's prompt, reply, ledger record, and review stay in the sidecar
failure evidence; the package keeps no copy, and the loader refuses one.
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
    load_package,
)

from .support import review_response, scripted_orchestrator, world_builders

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)
_orchestrator = partial(scripted_orchestrator, task_id="members")
_REMOVED_MEMBERS = (
    "source-hashes.json",
    "observations.json",
    "authoring/transformations.json",
    "authoring/ledger.json",
    "authoring/reviews.json",
    "authoring/01-call1.json",
    "authoring/01-call1.prompt",
    "authoring/01-call1.raw",
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


def test_an_accepted_package_holds_only_the_read_members(package_dir: Path) -> None:
    files = sorted(path.relative_to(package_dir).as_posix() for path in package_dir.rglob("*"))

    assert files == [
        "bindings.json",
        "checks.json",
        "examples.json",
        "explanation.json",
        "inputs.json",
        "manifest.json",
        "plan.json",
        "prerequisites.json",
        "setup.json",
        "stimulus.json",
        "tool_call_condition.json",
    ]


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


@pytest.mark.parametrize("member", _REMOVED_MEMBERS)
def test_the_loader_rejects_a_package_carrying_a_removed_member(
    package_dir: Path, member: str
) -> None:
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

    with pytest.raises(PackagePathError, match="unexpected package member path"):
        load_package(package_dir)
