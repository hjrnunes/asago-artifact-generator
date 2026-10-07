"""A v4 handoff yields an artifact-package-v4 whose stimulus states its turns; v3 stays v3."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_artifact_generator.authoring.core import _json_bytes
from asago_artifact_generator.input_adapter import InputView
from asago_artifact_generator.package_io import (
    PACKAGE_SCHEMA_VERSION,
    PackageIntegrityError,
    _manifest_digest,
    build_package,
    load_package,
    write_package,
)

from .support import ScriptedAuthoringTransport, stage_local_orchestrator, world
from .test_sequential_turns import (
    runtime,
    sequential_metadata,
    sequential_plan,
    v4_view,
)

_SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "artifact-package"


def _author(tmp_path: Path, view: InputView, turns: int):
    refund = world("refund")
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(sequential_plan(refund["plan"], turns)),
            json.dumps(sequential_metadata(refund["metadata"], turns)),
        ]
    )
    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(view, refund["inventory"], runtime(refund["runtime_contract"]))
    return result, transport


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "package" / "manifest.json").read_text(encoding="utf-8"))


def _stimulus(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "package" / "stimulus.json").read_text(encoding="utf-8"))


def test_a_three_turn_v4_handoff_becomes_a_sequential_v4_package(tmp_path: Path) -> None:
    result, transport = _author(tmp_path, v4_view(tmp_path, 3), 3)

    assert result.status == "accepted", result.findings
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    manifest = _manifest(tmp_path)
    assert manifest["schema_version"] == "artifact-package-v4"
    assert manifest["input_kind"] == "scenario-handoff-v4"
    stimulus = _stimulus(tmp_path)
    assert stimulus["mode"] == "sequential"
    assert stimulus["turn_count"] == 3
    assert stimulus["delivery"] == "sequential_user_turns"
    assert len(stimulus["history"]) == 2
    assert {item["role"] for item in stimulus["history"]} == {"user"}
    assert stimulus["user_text"]
    schema = json.loads(
        (_SCHEMAS / "artifact-package-v4" / "schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(manifest, schema)
    assert load_package(tmp_path / "package").manifest.schema_version == "artifact-package-v4"


def test_a_one_turn_v4_handoff_costs_the_same_requests_and_states_a_single_turn(
    tmp_path: Path,
) -> None:
    result, transport = _author(tmp_path, v4_view(tmp_path, 1), 1)

    assert result.status == "accepted", result.findings
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    stimulus = _stimulus(tmp_path)
    assert stimulus["mode"] == "single"
    assert stimulus["turn_count"] == 1
    assert stimulus["history"] == []
    assert _manifest(tmp_path)["schema_version"] == "artifact-package-v4"


def test_a_v3_handoff_still_writes_the_v3_package_byte_for_byte(tmp_path: Path) -> None:
    refund = world("refund")
    transport = ScriptedAuthoringTransport(
        [json.dumps(refund["plan"]), json.dumps(refund["metadata"])]
    )

    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(refund["view"], refund["inventory"], refund["runtime_contract"])

    assert result.status == "accepted", result.findings
    manifest = _manifest(tmp_path)
    assert manifest["schema_version"] == PACKAGE_SCHEMA_VERSION == "artifact-package-v3"
    assert manifest["input_kind"] == "scenario-handoff-v3"
    assert (tmp_path / "package" / "stimulus.json").read_bytes() == _json_bytes(
        refund["metadata"]["stimulus"]
    )
    assert set(_stimulus(tmp_path)) == {"user_text", "delivery", "history", "slots"}


def _build(input_kind: str, **changes: Any):
    return build_package(
        package_id="p",
        scenario_id="s",
        input_kind=input_kind,
        source_digests={"input": "a" * 64},
        members={"plan.json": b"{}"},
        **changes,
    )


@pytest.mark.parametrize(
    ("input_kind", "version"),
    [
        ("scenario-handoff-v3", "artifact-package-v3"),
        ("scenario-handoff-v4", "artifact-package-v4"),
    ],
)
def test_the_package_version_follows_the_input_kind(input_kind: str, version: str) -> None:
    assert _build(input_kind).manifest.schema_version == version


def test_an_unknown_input_kind_is_refused() -> None:
    with pytest.raises(PackageIntegrityError, match="unsupported package input kind"):
        _build("scenario-handoff-v9")


def test_a_v4_package_round_trips_and_a_version_swap_is_caught(tmp_path: Path) -> None:
    package = _build("scenario-handoff-v4")
    destination = write_package(tmp_path / "out", package)

    assert load_package(destination).manifest.to_dict() == package.manifest.to_dict()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = "artifact-package-v3"
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PackageIntegrityError):
        load_package(destination)


def test_a_v3_manifest_cannot_carry_a_v4_input_kind(tmp_path: Path) -> None:
    package = _build("scenario-handoff-v4")
    destination = write_package(tmp_path / "out", package)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = "artifact-package-v3"
    manifest.pop("manifest_digest")
    manifest["manifest_digest"] = _manifest_digest(manifest)
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="unsupported package input kind"):
        load_package(destination)
