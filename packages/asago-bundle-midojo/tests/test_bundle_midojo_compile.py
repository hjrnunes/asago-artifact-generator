"""compile: a command-attempt package becomes a MiDojo bundle template."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.schema import manifest_errors, validate_manifest
from asago_bundle_core.slots import VALUE
from asago_bundle_core.testing import CONDITION, write_test_package
from asago_bundle_midojo import compiler
from asago_bundle_midojo.compiler import CompileError, compile_package


def manifest_of(template: Path) -> dict[str, Any]:
    return json.loads((template / "bundle.json").read_text(encoding="utf-8"))


def test_the_manifest_validates_against_the_core_schema(template: Path) -> None:
    manifest = manifest_of(template)

    validate_manifest(manifest)
    assert manifest_errors(manifest) == []


def test_the_manifest_names_the_tool_and_the_claim(template: Path) -> None:
    manifest = manifest_of(template)

    assert manifest["tool"] == "midojo"
    assert manifest["tool_revision_range"] == ">=9ceb22e3"
    assert manifest["claim_level"] == "command_attempt"
    assert manifest["delivery"] == "single"
    assert manifest["target_mode"] == "orch_hosted"
    assert manifest["plugins"] == ["asago_tool_call_condition", "asago_neutral"]
    assert manifest["environment"] == ["OPENAI_API_KEY"]
    assert manifest["templates"] == {"suite": "suite.template.json"}


def test_the_manifest_leaves_repeats_to_orch_and_has_no_phases_object(template: Path) -> None:
    manifest = manifest_of(template)

    assert "repeats" not in manifest
    assert "phases" not in manifest


def test_requires_lists_the_catalog_keys_and_the_service_port(template: Path) -> None:
    assert manifest_of(template)["requires"] == [
        "gateway_url",
        "mcp_url",
        "messages",
        "model",
        "service_port",
    ]


def test_the_serve_object_starts_midojo_on_loopback_and_names_a_health_url(
    template: Path,
) -> None:
    serve = manifest_of(template)["serve"]

    assert serve == {
        "argv": [
            "{tool_python}",
            "-m",
            "midojo.serve",
            "--host",
            "127.0.0.1",
            "--port",
            "{service_port}",
            "--load-suite",
            "asago_suite.suite",
        ],
        "ready_url": "http://127.0.0.1:{service_port}/health",
    }


def test_the_entrypoint_runs_the_run_script_with_the_openai_protocol(template: Path) -> None:
    entrypoint = manifest_of(template)["entrypoint"]

    assert entrypoint[:3] == ["{tool_python}", "{bundle}/run_midojo.py", "--bundle"]
    flags = entrypoint[entrypoint.index("--") + 1 :]
    assert flags == [
        "--agent-uri",
        "{gateway_url}",
        "--protocol",
        "openai",
        "--model-name",
        "{model}",
        "--mcp-server-url",
        "{mcp_url}",
        "--mcp-server-label",
        "qualification_target",
        "--suite",
        "asago_suite.suite",
        "--logdir",
        "{bundle}/midojo-logs",
    ]
    assert "http://127.0.0.1:{service_port}" in entrypoint


def test_native_outputs_are_the_files_the_run_script_and_the_verifier_write(
    template: Path,
) -> None:
    assert manifest_of(template)["native_outputs"] == [
        "control-plane/run.json",
        "control-plane/evaluation.json",
        "midojo-logs/results.json",
        "verdict.json",
    ]


def test_the_manifest_carries_the_package_identity(command_package: Path, template: Path) -> None:
    package = json.loads((command_package / "manifest.json").read_text(encoding="utf-8"))
    manifest = manifest_of(template)

    assert manifest["package_id"] == package["package_id"]
    assert manifest["scenario_id"] == package["scenario_id"]
    assert manifest["package_digest"] == package["manifest_digest"]


def test_the_suite_template_holds_the_condition_and_two_slots(template: Path) -> None:
    suite = json.loads((template / "suite.template.json").read_text(encoding="utf-8"))

    assert suite["agent_runtime"] == {"type": "unmanaged"}
    (user_task,) = suite["user_tasks"]
    (injection_task,) = suite["injection_tasks"]
    assert user_task["prompt"] == {VALUE: "messages"}
    assert user_task["utility"] == {"asago_neutral": {}}
    assert injection_task["security"] == {
        "asago_tool_call_condition": {
            "condition": CONDITION,
            "verdict_file": {"$bundle": "verdict.json"},
        }
    }
    assert "probes" not in injection_task


def test_the_bundle_code_is_copied_byte_for_byte(template: Path) -> None:
    source = files("asago_bundle_midojo").joinpath("bundle_files")
    for name, original in {
        "asago_suite/__init__.py": "suite_package.py",
        "asago_suite/suite.py": "suite_module.py",
        "asago_suite/asago_verifiers.py": "asago_verifiers.py",
        "run_midojo.py": "run_midojo.py",
    }.items():
        assert (template / name).read_bytes() == source.joinpath(original).read_bytes()


def test_compile_twice_gives_the_same_bytes(command_package: Path, tmp_path: Path) -> None:
    compile_package(command_package, tmp_path / "one")
    compile_package(command_package, tmp_path / "two")

    def tree(root: Path) -> dict[Path, bytes]:
        return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    assert tree(tmp_path / "one") == tree(tmp_path / "two")
    assert len(tree(tmp_path / "one")) == 6


def test_compile_returns_the_manifest_it_wrote(command_package: Path, tmp_path: Path) -> None:
    returned = compile_package(command_package, tmp_path / "out")

    assert returned == manifest_of(tmp_path / "out")


def test_compile_refuses_a_non_empty_output_directory(
    command_package: Path, tmp_path: Path
) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x", encoding="utf-8")

    with pytest.raises(CompileError, match="not empty"):
        compile_package(command_package, tmp_path / "out")


def test_compile_refuses_a_directory_that_is_not_a_package(tmp_path: Path) -> None:
    with pytest.raises(CompileError, match="cannot be loaded"):
        compile_package(tmp_path, tmp_path / "out")


def test_compile_refuses_a_slot_marker_in_the_package(tmp_path: Path) -> None:
    condition = json.loads(json.dumps(CONDITION))
    condition["comparisons"][0]["right"] = {"$value": "model"}
    package = write_test_package(tmp_path / "packages", condition=condition)

    with pytest.raises(CompileError, match="slot marker"):
        compile_package(package, tmp_path / "out")


def test_compile_refuses_an_invalid_condition(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", condition={"comparisons": []})

    with pytest.raises(CompileError, match="condition"):
        compile_package(package, tmp_path / "out")


def test_the_suite_module_and_the_tool_revision_are_fixed_by_compile() -> None:
    assert compiler.SUITE_MODULE == "asago_suite.suite"
    assert compiler.TOOL_REVISION_RANGE == ">=9ceb22e3"
