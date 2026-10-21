"""The openshell runtime: MiDojo runs the mini-agents target in a sandbox from the base."""

from __future__ import annotations

import copy
import json
from functools import partial
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_bundle_core.conformance import AdapterUnderTest, check_round_trip
from asago_bundle_core.schema import manifest_errors
from asago_bundle_core.testing import sequential_stimulus, write_test_package
from asago_bundle_midojo import cli
from asago_bundle_midojo.compiler import (
    OPENSHELL_ENTRYPOINT_VALUES,
    CompileError,
    compile_package,
)
from asago_bundle_midojo.instantiate import InstantiateError, instantiate_bundle
from asago_bundle_midojo.parse import EVIDENCE_FILE, parse_bundle
from conftest import (
    BASE,
    CALL,
    IMAGE_ID,
    OPENSHELL_SEQUENTIAL_VALUES,
    OPENSHELL_VALUES,
    PROMPT,
    RECEIPT_SCHEMA,
    VALUES,
    compile_options,
    openshell_evaluation,
    write_native,
)

VALIDATOR = jsonschema.Draft202012Validator(json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
ASAGO = "/sandbox/workdir/.asago/"
SERVER_LOG, OGX_LOG, RUN_SUMMARY = (
    f"{ASAGO}{name}" for name in ("mcp-calls.jsonl", "ogx.log", "run-summary.json")
)
CALL_ID = "mini-agents:0b6f4c8e-1d2a-4c3b-9e8f-7a6b5c4d3e21"
RUNTIME_LISTS = (
    "processes",
    "network_calls_allowed",
    "network_calls_blocked",
    "files_created",
    "files_modified",
    "files_deleted",
    "security_findings",
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compile_openshell(package: Path, out: Path, base: Path = BASE) -> dict[str, Any]:
    return compile_package(package, out, **{**compile_options("openshell"), "midojo_base": base})


@pytest.fixture
def openshell_template(command_package: Path, tmp_path: Path) -> Path:
    out = tmp_path / "openshell-template"
    compile_openshell(command_package, out)
    return out


@pytest.fixture
def openshell_bundle(openshell_template: Path, tmp_path: Path) -> Path:
    out = tmp_path / "openshell-bundle"
    instantiate_bundle(openshell_template, OPENSHELL_VALUES, out)
    return out


def sequential_bundle(tmp_path: Path) -> Path:
    package = write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())
    compile_openshell(package, tmp_path / "template")
    instantiate_bundle(tmp_path / "template", OPENSHELL_SEQUENTIAL_VALUES, tmp_path / "bundle")
    return tmp_path / "bundle"


# --- compile ---------------------------------------------------------------------------------


def test_the_suite_runs_the_bases_sandbox_with_its_slots_as_values(
    openshell_template: Path,
) -> None:
    runtime = read_json(openshell_template / "suite.template.json")["agent_runtime"]
    base_policy = read_json(BASE)["runtime"]["policy"]
    rules = runtime["policy"]["networkPolicies"]

    assert runtime["type"] == "openshell"
    assert runtime["image"] == {"$value": "image_id"}
    assert runtime["agent_command"] == [
        "/opt/app-root/.venv/bin/mini-agents-sandbox-agent",
        "--domain",
        "klarna",
    ]
    assert rules["control_plane"]["endpoints"][0]["port"] == {"$value": "service_port"}
    assert rules["model_relay"]["endpoints"][0]["port"] == {"$value": "relay_port"}
    assert {key: runtime["policy"][key] for key in ("filesystem", "landlock", "process")} == {
        key: base_policy[key] for key in ("filesystem", "landlock", "process")
    }
    assert runtime["files"] == {}
    assert runtime["env_vars"] == {
        "AGENT_MODEL": {"$value": "model"},
        "AGENT_MODEL_BASE_URL": {"$value": "relay_url"},
        "AGENT_MODEL_API_KEY": "${env.OPENAI_API_KEY}",
        "AGENT_INSTRUCTIONS": {"$value": "instructions"},
        "AGENT_ALLOWED_TOOLS": {"$value": "allowed_tools"},
    }


def test_the_manifest_runs_midojo_on_the_gateway_with_the_agent_timeout(
    openshell_template: Path,
) -> None:
    manifest = read_json(openshell_template / "bundle.json")

    assert manifest_errors(manifest) == []
    assert manifest["tool_revision_range"] == ">=b0fb4576"
    assert manifest["environment"] == ["OPENAI_API_KEY"]
    assert manifest["requires"] == [
        "agent_timeout",
        "allowed_tools",
        "gateway",
        "image_id",
        "instructions",
        "messages",
        "model",
        "relay_port",
        "relay_url",
        "service_port",
    ]
    flags = manifest["entrypoint"][manifest["entrypoint"].index("--") + 1 :]
    assert flags == [
        "--gateway",
        "{gateway}",
        "--agent-timeout",
        "{agent_timeout}",
        "--suite",
        "asago_suite.suite",
        "--logdir",
        "{bundle}/midojo-logs",
    ]
    assert manifest["serve"]["argv"][-2:] == ["--load-suite", "asago_suite.suite"]


def test_a_sequential_package_sends_its_turns_under_openshell(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())
    manifest = compile_openshell(package, tmp_path / "template")
    suite = read_json(tmp_path / "template" / "suite.template.json")

    assert manifest["delivery"] == "sequential"
    assert manifest["tool_revision_range"] == ">=b0fb4576"
    assert suite["user_tasks"][0]["turns"] == [
        {"$value": "user_turn_1"},
        {"$value": "user_turn_2"},
    ]
    assert suite["agent_runtime"]["type"] == "openshell"


def test_the_default_runtime_compiles_the_unmanaged_bundle_unchanged(
    command_package: Path, tmp_path: Path
) -> None:
    options = {key: value for key, value in compile_options().items() if key != "runtime"}
    compile_package(command_package, tmp_path / "default", **options)
    compile_package(command_package, tmp_path / "unmanaged", **compile_options("unmanaged"))

    def files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    assert files(tmp_path / "default") == files(tmp_path / "unmanaged")
    assert read_json(tmp_path / "default" / "suite.template.json")["agent_runtime"] == {
        "type": "unmanaged"
    }


def runtime_base(tmp_path: Path, change: Any) -> Path:
    base = read_json(BASE)
    change(base)
    path = tmp_path / "base.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def drop_runtime(base: dict[str, Any]) -> None:
    del base["runtime"]


def tag_image(base: dict[str, Any]) -> None:
    base["runtime"]["image"] = "asago-mini-agents-sandbox:latest"


def fixed_domain(base: dict[str, Any]) -> None:
    base["runtime"]["agent_command"] = ["/opt/app-root/.venv/bin/mini-agents-sandbox-agent"]


def fixed_relay_port(base: dict[str, Any]) -> None:
    base["runtime"]["policy"]["networkPolicies"]["model_relay"]["endpoints"][0]["port"] = 18006


def marker(base: dict[str, Any]) -> None:
    base["runtime"]["policy"]["note"] = {"$value": "model"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (drop_runtime, "no runtime block"),
        (tag_image, "not the <image_id> slot"),
        (fixed_domain, "agent_command"),
        (fixed_relay_port, "lacks port slots: ['<relay_port>']"),
        (marker, "slot marker"),
    ],
)
def test_a_base_without_a_usable_runtime_block_is_refused(
    command_package: Path, tmp_path: Path, change: Any, message: str
) -> None:
    with pytest.raises(CompileError, match=message.replace("[", r"\[").replace("]", r"\]")):
        compile_openshell(command_package, tmp_path / "template", runtime_base(tmp_path, change))


def test_an_unknown_runtime_is_refused(command_package: Path, tmp_path: Path) -> None:
    with pytest.raises(CompileError, match="unknown agent runtime"):
        compile_package(command_package, tmp_path / "t", **compile_options("docker"))


def test_the_cli_compiles_an_openshell_bundle(command_package: Path, tmp_path: Path) -> None:
    argv = ["compile", str(command_package), "--out", str(tmp_path / "t")]
    argv += ["--midojo-base", str(BASE), "--domain", "klarna", "--runtime", "openshell"]

    assert cli.main(argv) == 0
    assert read_json(tmp_path / "t" / "suite.template.json")["agent_runtime"]["type"] == (
        "openshell"
    )


def test_the_cli_refuses_an_unknown_runtime(command_package: Path, tmp_path: Path) -> None:
    argv = ["compile", str(command_package), "--out", str(tmp_path / "t")]
    argv += ["--midojo-base", str(BASE), "--domain", "klarna", "--runtime", "docker"]

    with pytest.raises(SystemExit) as raised:
        cli.main(argv)

    assert raised.value.code == 2


# --- instantiate -----------------------------------------------------------------------------


def test_instantiate_fills_the_sandbox_and_renders_the_commands(openshell_bundle: Path) -> None:
    runtime = read_json(openshell_bundle / "asago_suite/suite.yaml")["agent_runtime"]
    manifest = read_json(openshell_bundle / "bundle.json")
    rules = runtime["policy"]["networkPolicies"]

    assert runtime["image"] == IMAGE_ID
    assert rules["control_plane"]["endpoints"] == [
        {"host": "host.openshell.internal", "port": 18123}
    ]
    assert rules["model_relay"]["endpoints"] == [
        {"host": "host.openshell.internal", "port": 18006}
    ]
    assert runtime["env_vars"] == {
        "AGENT_MODEL": "fixture-model",
        "AGENT_MODEL_BASE_URL": "http://host.openshell.internal:18006/v1",
        "AGENT_MODEL_API_KEY": "${env.OPENAI_API_KEY}",
        "AGENT_INSTRUCTIONS": "You are the support agent.",
        "AGENT_ALLOWED_TOOLS": "lookup_order,get_account_details",
    }
    assert manifest_errors(manifest) == []
    flags = manifest["entrypoint"][manifest["entrypoint"].index("--") + 1 :]
    assert flags[:4] == ["--gateway", "openshell", "--agent-timeout", "300"]
    assert manifest["serve"]["ready_url"] == "http://127.0.0.1:18123/health"
    assert not any("{" in argument for argument in manifest["entrypoint"][1:])


def test_an_empty_system_message_is_accepted(openshell_template: Path, tmp_path: Path) -> None:
    # MiDojo's unmanaged runs send an empty system message when the suite has none;
    # an openshell bundle gets the same.
    instantiate_bundle(
        openshell_template, {**OPENSHELL_VALUES, "instructions": ""}, tmp_path / "b"
    )


@pytest.mark.parametrize("bad", [[], ["lookup order"], [""], "lookup_order", None, [1]])
def test_a_malformed_tool_list_is_refused(
    openshell_template: Path, tmp_path: Path, bad: Any
) -> None:
    with pytest.raises(InstantiateError, match="malformed allowed_tools: expected "):
        instantiate_bundle(
            openshell_template, {**OPENSHELL_VALUES, "allowed_tools": bad}, tmp_path / "b"
        )


@pytest.mark.parametrize(
    "key,bad",
    [
        ("instructions", None),
        ("instructions", "Answer as ${env.USER}."),
    ],
)
def test_a_malformed_system_message_is_refused(
    openshell_template: Path, tmp_path: Path, key: str, bad: Any
) -> None:
    with pytest.raises(InstantiateError, match=f"malformed {key}: expected "):
        instantiate_bundle(openshell_template, {**OPENSHELL_VALUES, key: bad}, tmp_path / "b")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("image_id", "asago-mini-agents-sandbox@sha256:" + "4f" * 32),
        ("image_id", "sha256:4f4f"),
        ("image_id", "sha256:" + "4F" * 32),
        ("image_id", "asago-mini-agents-sandbox:728b731"),
        ("relay_port", 0),
        ("relay_port", 65536),
        ("relay_port", "18006"),
        ("relay_port", True),
        ("service_port", 70000),
        ("relay_url", "ftp://host.openshell.internal:18006/v1"),
        ("relay_url", "http://"),
        ("relay_url", "http://${env.RELAY_HOST}:18006/v1"),
        ("model", ""),
        ("model", "${env.MODEL}"),
        ("instructions", "Answer as ${env.USER}."),
        ("instructions", None),
        ("gateway", ""),
        ("agent_timeout", 0),
        ("agent_timeout", -5),
        ("agent_timeout", "300"),
        ("agent_timeout", float("inf")),
        ("agent_timeout", True),
    ],
)
def test_a_malformed_sandbox_value_is_refused_with_what_it_must_be(
    openshell_template: Path, tmp_path: Path, key: str, bad: Any
) -> None:
    with pytest.raises(InstantiateError, match=f"malformed {key}: expected "):
        instantiate_bundle(openshell_template, {**OPENSHELL_VALUES, key: bad}, tmp_path / "b")


@pytest.mark.parametrize("key", sorted(set(OPENSHELL_VALUES)))
def test_a_missing_sandbox_value_is_refused(
    openshell_template: Path, tmp_path: Path, key: str
) -> None:
    values = {name: value for name, value in OPENSHELL_VALUES.items() if name != key}

    with pytest.raises(InstantiateError, match=f"values lack {key}"):
        instantiate_bundle(openshell_template, values, tmp_path / "b")


@pytest.mark.parametrize(
    "url", ["http://host.openshell.internal:18007/v1", "http://host.openshell.internal/v1"]
)
def test_a_relay_url_off_the_relay_port_is_refused(
    openshell_template: Path, tmp_path: Path, url: str
) -> None:
    with pytest.raises(InstantiateError, match="not relay_port 18006"):
        instantiate_bundle(
            openshell_template, {**OPENSHELL_VALUES, "relay_url": url}, tmp_path / "b"
        )


def test_a_relay_url_on_its_scheme_port_is_accepted(
    openshell_template: Path, tmp_path: Path
) -> None:
    values = {**OPENSHELL_VALUES, "relay_url": "https://relay.example/v1", "relay_port": 443}

    instantiate_bundle(openshell_template, values, tmp_path / "b")

    suite = read_json(tmp_path / "b" / "asago_suite/suite.yaml")
    assert suite["agent_runtime"]["policy"]["networkPolicies"]["model_relay"]["endpoints"] == [
        {"host": "host.openshell.internal", "port": 443}
    ]


def test_a_fractional_agent_timeout_is_passed_as_it_is(
    openshell_template: Path, tmp_path: Path
) -> None:
    manifest = instantiate_bundle(
        openshell_template, {**OPENSHELL_VALUES, "agent_timeout": 90.5}, tmp_path / "b"
    )

    assert manifest["entrypoint"][manifest["entrypoint"].index("--agent-timeout") + 1] == "90.5"


def test_an_unmanaged_template_keeps_its_values_and_messages(
    template: Path, tmp_path: Path
) -> None:
    with pytest.raises(InstantiateError, match=r"^values carry a malformed service_port$"):
        instantiate_bundle(template, {**VALUES, "service_port": 0}, tmp_path / "b")


# --- parse -----------------------------------------------------------------------------------


def receipt_of(bundle: Path, **options: Any) -> dict[str, Any]:
    receipt = parse_bundle(bundle, bundle / "receipt.json", **options)
    assert list(VALIDATOR.iter_errors(receipt)) == []
    return receipt


def parsed(bundle: Path, evaluation: dict[str, Any], **options: Any) -> dict[str, Any]:
    write_native(bundle, evaluation=evaluation)
    return receipt_of(bundle, **options)


def reason_of(bundle: Path, evaluation: dict[str, Any]) -> str | None:
    receipt = parsed(bundle, evaluation)
    if receipt["incomplete_reason"] is not None:
        assert receipt["execution_status"] == "failed"
        assert receipt["attempts"] == []
    return receipt["incomplete_reason"]


WORKDIR_FILES = {"server": SERVER_LOG, "ogx": OGX_LOG, "summary": RUN_SUMMARY}


def with_files(evaluation: dict[str, Any], **changes: str | None) -> dict[str, Any]:
    """The evaluation with ``server``, ``ogx`` or ``summary`` replaced; ``None`` removes it."""

    changed = copy.deepcopy(evaluation)
    files = changed["observations"]["openshell"]["workdir_new_file_contents"]
    for name, content in changes.items():
        path = WORKDIR_FILES[name]
        if content is None:
            files.pop(path)
        else:
            files[path] = content
    return changed


def summary_text(**changes: Any) -> str:
    summary = json.loads(
        openshell_evaluation()["observations"]["openshell"]["workdir_new_file_contents"][
            RUN_SUMMARY
        ]
    )
    return json.dumps({**summary, **changes})


def server_text(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def server_line(**changes: Any) -> dict[str, Any]:
    return {
        "sequence": 1,
        "call_id": CALL_ID,
        "function": "get_referral",
        "load": "ok",
        "start": "ok",
        "push": "ok",
        "report": "ok",
        "failure": None,
        **changes,
    }


def test_a_single_openshell_run_gives_a_completed_receipt(openshell_bundle: Path) -> None:
    evaluation = openshell_evaluation()
    receipt = parsed(openshell_bundle, evaluation)
    (attempt,) = receipt["attempts"]
    observed = evaluation["observations"]["openshell"]

    assert receipt["execution_status"] == "completed"
    assert attempt["result"]["outcome"] == "detected"
    assert attempt["generation"]["request_count"] == 1
    assert attempt["observation"]["source"] == "tool_native"
    assert [call["name"] for call in attempt["observation"]["tool_calls"]] == ["get_referral"]
    assert attempt["observation"]["assistant_messages"] == ["Done."]
    assert attempt["turns"][0]["request"]["messages"] == [{"role": "user", "content": PROMPT}]
    assert attempt["runtime_observations"] == {
        "source": "openshell",
        **{name: observed[name] for name in RUNTIME_LISTS},
    }


def test_the_evidence_file_holds_what_the_receipt_cannot(openshell_bundle: Path) -> None:
    receipt = parsed(openshell_bundle, openshell_evaluation())

    evidence = read_json(openshell_bundle / EVIDENCE_FILE)

    assert evidence == {
        "schema_version": "midojo-openshell-evidence-v1",
        "package": receipt["package"],
        "bundle_digest": receipt["bundle_digest"],
        "evaluation_id": "eval-1",
        "image": IMAGE_ID,
        "image_digest": None,
        "policy_digest": "sha256:" + "a1" * 32,
        "network_calls_interrupted": [],
        "network_callers": [
            "/opt/app-root/.venv/bin/python-mcp",
            "/opt/app-root/.venv/bin/python-ogx",
        ],
    }


def test_a_failed_run_still_writes_the_evidence_it_has(openshell_bundle: Path) -> None:
    evaluation = openshell_evaluation()
    del evaluation["observations"]

    assert reason_of(openshell_bundle, evaluation) == "openshell_observations_missing"
    evidence = read_json(openshell_bundle / EVIDENCE_FILE)
    assert evidence["evaluation_id"] == "eval-1"
    assert evidence["image"] is None
    assert evidence["network_callers"] is None


def test_an_unmanaged_receipt_has_no_evidence_file(bundle: Path) -> None:
    write_native(bundle)

    receipt = receipt_of(bundle)

    assert receipt["attempts"][0]["runtime_observations"]["source"] is None
    assert not (bundle / EVIDENCE_FILE).exists()


def test_a_two_turn_openshell_run_takes_each_calls_turn_from_midojo(tmp_path: Path) -> None:
    bundle = sequential_bundle(tmp_path)

    receipt = parsed(bundle, openshell_evaluation("two-turn"))
    (attempt,) = receipt["attempts"]

    assert receipt["execution_status"] == "completed"
    assert attempt["generation"]["request_count"] == 2
    assert [turn["response"]["tool_call_indices"] for turn in attempt["turns"]] == [[], [0]]
    assert [turn["response"]["assistant_messages"] for turn in attempt["turns"]] == [
        ["Hi."],
        ["Done."],
    ]
    assert attempt["observation"]["tool_calls"][0]["turn_index"] == 1
    assert attempt["runtime_observations"]["source"] == "openshell"


def test_a_failed_turn_keeps_the_sandboxs_evidence(tmp_path: Path) -> None:
    bundle = sequential_bundle(tmp_path)
    evaluation = openshell_evaluation("two-turn")
    evaluation["turns"][1]["reply"] = None
    evaluation.update(completed=False, security=None, utility=None, agent_output=None)

    receipt = parsed(bundle, evaluation)
    (attempt,) = receipt["attempts"]

    assert receipt["incomplete_reason"] == "turn_failed:2"
    assert attempt["generation"]["request_count"] == 2
    assert attempt["runtime_observations"]["source"] == "openshell"


def test_a_failed_turn_without_its_evidence_shows_no_attempt(tmp_path: Path) -> None:
    bundle = sequential_bundle(tmp_path)
    evaluation = with_files(openshell_evaluation("two-turn"), ogx=None)
    evaluation["turns"][1]["reply"] = None
    evaluation.update(completed=False, security=None, utility=None, agent_output=None)

    receipt = parsed(bundle, evaluation)

    assert receipt["incomplete_reason"] == "turn_failed:2"
    assert receipt["attempts"] == []


def malformed_observations(evaluation: dict[str, Any]) -> None:
    evaluation["observations"]["openshell"]["processes"] = None


def undecoded_file(evaluation: dict[str, Any]) -> None:
    evaluation["observations"]["openshell"]["workdir_new_file_contents"][OGX_LOG] = 7


def no_openshell_source(evaluation: dict[str, Any]) -> None:
    evaluation["observations"] = {"other": {}}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (no_openshell_source, "openshell_observations_missing"),
        (malformed_observations, "openshell_observations_malformed"),
        (undecoded_file, "openshell_observations_malformed"),
    ],
)
def test_unusable_observations_fail_the_receipt(
    openshell_bundle: Path, change: Any, reason: str
) -> None:
    evaluation = openshell_evaluation()
    change(evaluation)

    assert reason_of(openshell_bundle, evaluation) == reason


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("summary", "workdir_file_missing:run-summary.json"),
        ("ogx", "workdir_file_missing:ogx.log"),
        ("server", "workdir_file_missing:mcp-calls.jsonl"),
    ],
)
def test_a_missing_workdir_file_fails_the_receipt(
    openshell_bundle: Path, missing: str, reason: str
) -> None:
    evaluation = with_files(openshell_evaluation(), **{missing: None})

    assert reason_of(openshell_bundle, evaluation) == reason


def test_a_run_without_calls_needs_no_server_log(openshell_bundle: Path) -> None:
    evaluation = with_files(openshell_evaluation(), server=None)
    evaluation.update(function_calls=[], security=False, security_reason=None)

    receipt = parsed(openshell_bundle, evaluation)

    assert receipt["execution_status"] == "completed"
    assert receipt["attempts"][0]["result"]["outcome"] == "not_detected"


@pytest.mark.parametrize(
    "summary",
    [
        "not json",
        "[]",
        summary_text(schema="mini-agents-run-summary-v0"),
        summary_text(gateway_request_count="1"),
        summary_text(gateway_request_count=-1),
        summary_text(rejected=None),
    ],
)
def test_a_malformed_run_summary_fails_the_receipt(openshell_bundle: Path, summary: str) -> None:
    evaluation = with_files(openshell_evaluation(), summary=summary)

    assert reason_of(openshell_bundle, evaluation) == "run_summary_malformed"


@pytest.mark.parametrize("log", ["not json\n", "[1]\n", server_text(server_line()) + "{\n"])
def test_a_malformed_server_log_fails_the_receipt(openshell_bundle: Path, log: str) -> None:
    evaluation = with_files(openshell_evaluation(), server=log)

    assert reason_of(openshell_bundle, evaluation) == "server_log_malformed"


def without(document: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in document.items() if name != key}


@pytest.mark.parametrize(
    "line",
    [
        *(
            without(server_line(), key)
            for key in ("sequence", "call_id", "function", "load", "start", "push", "report")
        ),
        without(server_line(), "failure"),
        {"call_id": CALL_ID, "function": "get_referral"},
        server_line(sequence=0),
        server_line(sequence="1"),
        server_line(sequence=True),
        server_line(call_id=7),
        server_line(function=None),
        server_line(load="unfinished"),
        server_line(start="unfinished"),
        server_line(push="unfinished"),
        server_line(report="done"),
        server_line(failure=["refused"]),
    ],
)
def test_a_server_log_line_unlike_its_writers_fails_the_receipt(
    openshell_bundle: Path, line: dict[str, Any]
) -> None:
    evaluation = with_files(openshell_evaluation(), server=server_text(line))

    assert reason_of(openshell_bundle, evaluation) == "server_log_malformed"


@pytest.mark.parametrize(
    "line",
    [
        server_line(load=None),
        server_line(load="failed", call_id=None, start=None, push=None, report=None),
        server_line(failure="PUT /agent/environment: HTTP 500"),
    ],
)
def test_a_server_log_line_the_writer_can_write_is_read(
    openshell_bundle: Path, line: dict[str, Any]
) -> None:
    evaluation = with_files(openshell_evaluation(), server=server_text(line))

    assert reason_of(openshell_bundle, evaluation) != "server_log_malformed"


SUMMARY_KEYS = (
    "schema",
    "domain",
    "turn_count",
    "turns_run",
    "final",
    "served_model",
    "gateway_request_count",
    "exit_reasons",
    "turns",
    "rejected",
)


@pytest.mark.parametrize(
    "summary",
    [
        *(json.dumps(without(json.loads(summary_text()), key)) for key in SUMMARY_KEYS),
        summary_text(domain=None),
        summary_text(turn_count="1"),
        summary_text(turn_count=True),
        summary_text(turn_count=1.0),
        summary_text(turns_run=-1),
        summary_text(final="true"),
        summary_text(served_model=3),
        summary_text(exit_reasons="ok"),
        summary_text(exit_reasons=[None]),
        summary_text(turns={}),
        summary_text(turns=["ok"]),
        summary_text(rejected=[1]),
    ],
)
def test_a_run_summary_unlike_its_writers_fails_the_receipt(
    openshell_bundle: Path, summary: str
) -> None:
    evaluation = with_files(openshell_evaluation(), summary=summary)

    assert reason_of(openshell_bundle, evaluation) == "run_summary_malformed"


def test_a_summary_with_no_turn_count_or_served_model_is_read(openshell_bundle: Path) -> None:
    summary = summary_text(turn_count=None, served_model=None)
    evaluation = with_files(openshell_evaluation(), summary=summary)

    assert parsed(openshell_bundle, evaluation)["execution_status"] == "completed"


@pytest.mark.parametrize("count", [0, 2])
def test_a_summary_count_other_than_the_ogx_logs_fails_the_receipt(
    openshell_bundle: Path, count: int
) -> None:
    evaluation = with_files(
        openshell_evaluation(), summary=summary_text(gateway_request_count=count)
    )

    assert reason_of(openshell_bundle, evaluation) == "request_count_mismatch"


def test_the_ogx_log_counts_only_responses_posts(openshell_bundle: Path) -> None:
    log = (
        '127.0.0.1:1 - "POST /v1/responses HTTP/1.1" 200\n'
        '127.0.0.1:1 - "POST /v1/responses?stream=false HTTP/1.1" 500\n'
        '127.0.0.1:1 - "GET /v1/responses/resp_1 HTTP/1.1" 200\n'
        '127.0.0.1:1 - "GET /v1/models HTTP/1.1" 200\n'
        "DEBUG some detail mentioning POST /v1/responses\n"
    )
    evaluation = with_files(
        openshell_evaluation(), ogx=log, summary=summary_text(gateway_request_count=2)
    )

    receipt = parsed(openshell_bundle, evaluation)

    assert receipt["attempts"][0]["generation"]["request_count"] == 2


REJECTED = {
    "index": 1,
    "reply": None,
    "response_id": None,
    "served_model": None,
    "gateway_requests": 0,
    "exit_reason": "turn-out-of-order",
    "failure": "turn 1 does not follow turn 0",
}


def test_a_rejected_agent_run_fails_the_receipt(openshell_bundle: Path) -> None:
    evaluation = with_files(openshell_evaluation(), summary=summary_text(rejected=[REJECTED]))

    assert reason_of(openshell_bundle, evaluation) == "agent_run_rejected"


@pytest.mark.parametrize("count", [-1, 0, None])
def test_a_run_rejected_for_its_turn_count_is_a_rejection_not_a_malformed_summary(
    openshell_bundle: Path, count: int | None
) -> None:
    # mini-agents writes MIDOJO_TURN_COUNT as it parsed it: -1 and 0 are integers
    # read_turn refuses, and a count that is not an integer is null (cli.py:114-143).
    rejected = {
        **REJECTED,
        "index": 0,
        "exit_reason": "input-invalid",
        "failure": f"MIDOJO_TURN_INDEX=0 is not a turn of MIDOJO_TURN_COUNT={count}",
    }
    summary = summary_text(
        turn_count=count,
        turns_run=0,
        final=False,
        served_model=None,
        gateway_request_count=0,
        exit_reasons=[],
        turns=[],
        rejected=[rejected],
    )
    log = "INFO     ogx.cli.stack.run:222 Listening on   category=cli\n"
    evaluation = with_files(openshell_evaluation(), summary=summary, ogx=log)

    assert reason_of(openshell_bundle, evaluation) == "agent_run_rejected"


@pytest.mark.parametrize(
    "line",
    [
        server_line(call_id="mini-agents:another"),
        server_line(call_id=None),
        server_line(function="get_order"),
    ],
)
def test_a_server_log_naming_other_calls_fails_the_receipt(
    openshell_bundle: Path, line: dict[str, Any]
) -> None:
    evaluation = with_files(openshell_evaluation(), server=server_text(line))

    assert reason_of(openshell_bundle, evaluation) == "call_id_mismatch"


def test_a_server_log_with_another_count_fails_the_receipt(openshell_bundle: Path) -> None:
    log = server_text(server_line(), server_line(sequence=2, call_id="mini-agents:second"))
    evaluation = with_files(openshell_evaluation(), server=log)

    assert reason_of(openshell_bundle, evaluation) == "tool_call_count_mismatch"


def test_a_failed_push_in_the_sandbox_is_a_sink_failure(openshell_bundle: Path) -> None:
    evaluation = with_files(openshell_evaluation(), server=server_text(server_line(push="failed")))

    assert reason_of(openshell_bundle, evaluation) == "sink_failed"


def test_an_unfinished_call_in_the_sandbox_makes_the_attempt_inconclusive(
    openshell_bundle: Path,
) -> None:
    evaluation = with_files(
        openshell_evaluation(), server=server_text(server_line(push=None, report="unfinished"))
    )
    evaluation["function_calls"][0].update(complete=False, result=None)

    receipt = parsed(openshell_bundle, evaluation)

    assert receipt["execution_status"] == "completed"
    assert receipt["attempts"][0]["result"] == {
        "outcome": "inconclusive",
        "claim_level": "command_attempt",
        "reason": "unfinished_call:get_referral",
    }


def test_an_unfinished_call_the_server_log_finished_fails_the_receipt(
    openshell_bundle: Path,
) -> None:
    evaluation = openshell_evaluation()
    evaluation["function_calls"][0].update(complete=False, result=None)

    assert reason_of(openshell_bundle, evaluation) == "unfinished_call_mismatch"


def test_the_boundary_judge_count_is_kept_and_its_calls_are_not_read(
    openshell_bundle: Path, tmp_path: Path
) -> None:
    records = tmp_path / "records"
    (records / "mcp_capture").mkdir(parents=True)
    (records / "mcp_capture/calls.jsonl").write_text(
        json.dumps({"name": "get_order", "arguments": {}}) + "\n", encoding="utf-8"
    )
    (records / "boundary").mkdir()
    (records / "boundary/gateway-accounting.json").write_text(
        json.dumps({"responses_request_count": 9}), encoding="utf-8"
    )

    receipt = parsed(openshell_bundle, openshell_evaluation(), records_dir=records)
    (attempt,) = receipt["attempts"]

    assert attempt["generation"]["request_count"] == 1
    assert attempt["observation"]["source"] == "tool_native"
    assert attempt["observation"]["tool_calls"][0]["name"] == CALL["function"]


# --- the round trip --------------------------------------------------------------------------


def openshell_adapter() -> AdapterUnderTest:
    def write_run(bundle: Path, manifest: dict[str, Any]) -> None:
        name = "two-turn" if manifest["delivery"] == "sequential" else "single"
        write_native(bundle, evaluation=openshell_evaluation(name))

    return AdapterUnderTest(
        compile=partial(compile_package, **compile_options("openshell")),
        instantiate=instantiate_bundle,
        parse=lambda bundle, out: parse_bundle(bundle, out),
        write_native=write_run,
        entrypoint_keys=OPENSHELL_ENTRYPOINT_VALUES,
    )


@pytest.mark.parametrize(
    ("stimulus", "values"),
    [(None, OPENSHELL_VALUES), (sequential_stimulus(), OPENSHELL_SEQUENTIAL_VALUES)],
)
def test_an_openshell_package_conforms(
    tmp_path: Path, stimulus: dict[str, Any] | None, values: dict[str, Any]
) -> None:
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)

    assert (
        check_round_trip(openshell_adapter(), package, values, tmp_path / "work", RECEIPT_SCHEMA)
        == []
    )
