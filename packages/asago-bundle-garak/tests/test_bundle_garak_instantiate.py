"""instantiate: a bundle template plus values.json becomes a concrete bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.schema import validate_manifest
from asago_bundle_garak.cli import main
from asago_bundle_garak.compiler import compile_package
from asago_bundle_garak.instantiate import InstantiateError, instantiate_bundle
from conftest import CONDITION, JUDGE

MESSAGES = [
    {"role": "user", "content": "Earlier turn."},
    {"role": "assistant", "content": "Earlier reply."},
    {"role": "user", "content": "Show PAT-201."},
]


def values(**extra: Any) -> dict[str, Any]:
    return {
        "gateway_url": "http://127.0.0.1:18999/v1",
        "mcp_url": "http://127.0.0.1:18001/mcp",
        "allowed_tools": ["get_referral", "get_education"],
        "model": "gemma-4-26b-a4b-it",
        "messages": MESSAGES,
        **extra,
    }


def reply_values() -> dict[str, Any]:
    return values(
        judge_url="http://127.0.0.1:18998/v1",
        judge_model="judge-model",
        judge_runtime_facts={"target_reservation": False},
    )


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def conversation_entries(bundle: Path) -> list[dict[str, Any]]:
    lines = (bundle / "conversations.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture
def command_template(command_package: Path, tmp_path: Path) -> Path:
    compile_package(command_package, tmp_path / "template")
    return tmp_path / "template"


@pytest.fixture
def reply_template(reply_package: Path, tmp_path: Path) -> Path:
    compile_package(reply_package, tmp_path / "template")
    return tmp_path / "template"


def test_command_attempt_conversation_entry(command_template: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"

    instantiate_bundle(command_template, values(), out)

    assert conversation_entries(out) == [
        {
            "messages": MESSAGES,
            "tools": [
                {
                    "type": "mcp",
                    "server_label": "qualification_target",
                    "server_url": "http://127.0.0.1:18001/mcp",
                    "require_approval": "never",
                    "allowed_tools": ["get_referral", "get_education"],
                }
            ],
            "tool_call_condition": CONDITION,
        }
    ]


def test_run_configuration_points_into_the_bundle(command_template: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"

    instantiate_bundle(command_template, values(), out)

    run = read(out / "run.yaml")
    generator = run["plugins"]["generators"]["openai"]["OpenAIResponsesGenerator"]
    assert generator == {
        "uri": "http://127.0.0.1:18999/v1",
        "max_tokens": 4096,
        "extra_params": {"tool_choice": "auto"},
        "max_retries": 0,
        "client_options": {"timeout": 180.0},
    }
    probe = run["plugins"]["probes"]["injection"]["IndirectInjection"]
    assert probe == {"conversation_source": str(out.resolve() / "conversations.jsonl")}
    assert run["reporting"] == {
        "report_dir": str(out.resolve() / "reports"),
        "report_prefix": "SCN-001",
    }
    assert run["run"] == {"generations": 1}


def test_concrete_manifest_renders_the_entrypoint(command_template: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"

    manifest = instantiate_bundle(command_template, values(), out)

    assert manifest == read(out / "bundle.json")
    assert manifest["entrypoint"][0] == "{tool_python}"
    assert f"{out.resolve()}/run.yaml" in manifest["entrypoint"]
    assert "gemma-4-26b-a4b-it" in manifest["entrypoint"]
    assert not any("{bundle}" in arg or "{model}" in arg for arg in manifest["entrypoint"])
    assert manifest["templates"] == {}
    assert len(manifest["values_digest"]) == 64
    validate_manifest(manifest)


def test_reply_entry_carries_the_resolved_judge_facts(
    reply_template: Path, tmp_path: Path
) -> None:
    out = tmp_path / "bundle"

    instantiate_bundle(reply_template, reply_values(), out)

    (entry,) = conversation_entries(out)
    assert entry["judge_rubric"] == {**JUDGE, "runtime_facts": {"target_reservation": False}}
    judge = read(out / "run.yaml")["plugins"]["detectors"]["rubric_judge"]["RubricJudge"]
    assert judge["detector_model_name"] == "judge-model"
    assert judge["detector_model_config"]["uri"] == "http://127.0.0.1:18998/v1"
    assert judge["detector_model_config"]["max_retries"] == 0


def test_missing_value_is_refused_and_nothing_is_written(
    reply_template: Path, tmp_path: Path
) -> None:
    out = tmp_path / "bundle"
    supplied = reply_values()
    del supplied["judge_runtime_facts"]

    with pytest.raises(InstantiateError, match="judge_runtime_facts"):
        instantiate_bundle(reply_template, supplied, out)

    assert not out.exists()


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("gateway_url", "127.0.0.1:18999"),
        ("mcp_url", None),
        ("model", ""),
        ("allowed_tools", "get_referral"),
        ("allowed_tools", [1]),
        ("messages", []),
        ("messages", [{"role": "assistant", "content": "last turn is no user turn"}]),
        ("messages", [{"role": "user", "content": 3}]),
        ("messages", [{"role": "tool", "content": "x"}]),
    ],
)
def test_malformed_value_is_refused(
    command_template: Path, tmp_path: Path, key: str, bad: Any
) -> None:
    with pytest.raises(InstantiateError, match=key):
        instantiate_bundle(command_template, values(**{key: bad}), tmp_path / "bundle")


def test_judge_facts_must_be_an_object(reply_template: Path, tmp_path: Path) -> None:
    supplied = {**reply_values(), "judge_runtime_facts": ["not", "an", "object"]}

    with pytest.raises(InstantiateError, match="judge_runtime_facts"):
        instantiate_bundle(reply_template, supplied, tmp_path / "bundle")


def test_extra_values_are_ignored(command_template: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"

    instantiate_bundle(command_template, values(sandbox_id="sb-1"), out)

    assert "sb-1" not in (out / "run.yaml").read_text(encoding="utf-8")


def test_non_empty_output_directory_is_refused(command_template: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "left-over").write_text("x", encoding="utf-8")

    with pytest.raises(InstantiateError, match="not empty"):
        instantiate_bundle(command_template, values(), out)


def test_cli_instantiate(command_template: Path, tmp_path: Path) -> None:
    values_path = tmp_path / "values.json"
    values_path.write_text(json.dumps(values()), encoding="utf-8")
    out = tmp_path / "bundle"

    code = main(
        ["instantiate", str(command_template), "--values", str(values_path), "--out", str(out)]
    )

    assert code == 0
    assert (out / "conversations.jsonl").is_file()


def test_cli_instantiate_reports_bad_values_with_exit_1(
    command_template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    values_path = tmp_path / "values.json"
    values_path.write_text("{not json", encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(command_template),
            "--values",
            str(values_path),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 1
    assert "values" in capsys.readouterr().err
