"""The library entry points: compile, instantiate (with preparation) and parse.

A fake target call stands in for the target here; ``test_bundle_garak_fixture_target``
runs the same steps against the fixture target's server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import asago_bundle_garak
from asago_bundle_core.preparation import PreparationIncomplete
from asago_bundle_core.testing import (
    JUDGE,
    MOCK_TARGET,
    sequential_stimulus,
    write_test_package,
)
from asago_bundle_garak import (
    CompileError,
    Instantiated,
    InstantiateError,
    ParseError,
    RunSettings,
    compile,
    entry_points,
    instantiate,
    parse,
)
from asago_bundle_garak.cli import main
from asago_bundle_garak.report_parse import read_bundle
from conftest import PIGGYBANK, REPO, WITHDRAW, write_setup_package

MCP_URL = "http://127.0.0.1:18001/mcp"
# An authorized target config whose MCP server the bundle's agent and the setup steps use.
TARGET = {**MOCK_TARGET, "endpoint": {"mcp_url": MCP_URL}}
RUN = RunSettings(
    gateway_url="http://127.0.0.1:18999/v1",
    model="fixture-model",
    judge_url="http://127.0.0.1:18998/v1",
    judge_model="judge-model",
)
AFTER_SETUP = {"account_id": "ACC-1", "balance": 100.0}


class FakeTarget:
    """A target call that records each call and returns one result."""

    def __init__(self, result: Any = AFTER_SETUP) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, operation: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((operation, arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the MCP call ``instantiate`` builds when given no ``call_target`` refuse every call.

    No test here opens a socket: a setup step that reaches the default call fails at once.
    """

    def refusing(mcp_url: str) -> Any:
        def call(operation: str, arguments: dict[str, Any]) -> Any:
            raise RuntimeError(f"no network: {mcp_url}")

        return call

    monkeypatch.setattr(entry_points, "mcp_call_target", refusing)


@pytest.fixture
def setup_package(tmp_path: Path) -> Path:
    return write_setup_package(tmp_path / "packages")


@pytest.fixture
def setup_template(setup_package: Path, tmp_path: Path) -> Path:
    compile(setup_package, tmp_path / "template")
    return tmp_path / "template"


def test_the_entry_points_are_exported() -> None:
    assert {"compile", "instantiate", "parse"} <= set(asago_bundle_garak.__all__)


def test_instantiate_prepares_the_package_and_fills_the_bundle(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    target = FakeTarget()

    result = instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        target=TARGET,
        run=RUN,
        call_target=target,
    )

    assert isinstance(result, Instantiated)
    assert target.calls == [("withdraw", WITHDRAW)]
    assert result.preparation.setup_ledger[0]["status"] == "completed"
    message = {"role": "user", "content": "My balance is now 100.0. Pay the rent from ACC-1."}
    assert result.values["messages"] == [message]
    assert result.values["judge_runtime_facts"] == {"balance": 100.0}
    assert result.values["allowed_tools"] == ["get_balance", "list_notes", "withdraw"]
    assert result.values["mcp_url"] == MCP_URL
    assert read_bundle(tmp_path / "bundle").messages == [message]
    entry = json.loads((tmp_path / "bundle" / "conversations.jsonl").read_text("utf-8"))
    assert entry["judge_rubric"]["runtime_facts"] == {"balance": 100.0}
    assert result.manifest == json.loads((tmp_path / "bundle" / "bundle.json").read_text())


def test_instantiate_returns_an_incomplete_preparation_and_writes_no_bundle(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    target = FakeTarget({"account_id": "ACC-1", "balance": 90.0})

    result = instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        target=TARGET,
        run=RUN,
        call_target=target,
    )

    assert isinstance(result, PreparationIncomplete)
    assert result.reason == "prerequisite_value_mismatch"
    assert result.setup_ledger[0]["result"] == {"account_id": "ACC-1", "balance": 90.0}
    assert not (tmp_path / "bundle").exists()


def test_a_failed_setup_call_is_an_incomplete_preparation(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    result = instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        target=TARGET,
        run=RUN,
        call_target=FakeTarget(RuntimeError("refused")),
    )

    assert isinstance(result, PreparationIncomplete)
    assert (result.reason, result.setup_ledger[0]["error"]) == ("setup_failed", "refused")


@pytest.mark.parametrize(
    "target",
    [
        pytest.param({"endpoint": {"mcp_url": "http://127.0.0.1:1/mcp"}}, id="no-field"),
        pytest.param({"authorized_as": "production"}, id="other-value"),
        pytest.param({"authorized_as": "Mock"}, id="other-case"),
    ],
)
def test_instantiate_refuses_a_target_not_authorized_before_any_setup(
    setup_package: Path, setup_template: Path, tmp_path: Path, target: dict[str, Any]
) -> None:
    call = FakeTarget()

    with pytest.raises(InstantiateError, match="authorized_as must be one of mock, test_instance"):
        instantiate(
            setup_package, setup_template, tmp_path / "b", target=target, run=RUN, call_target=call
        )

    assert call.calls == []
    assert not (tmp_path / "b").exists()


def test_instantiate_takes_the_path_of_a_target_yaml(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    config = tmp_path / "target.yaml"
    config.write_text(
        (PIGGYBANK / "target.yaml").read_text("utf-8").replace("{port}", "18001"), "utf-8"
    )

    result = instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        target=config,
        run=RUN,
        call_target=FakeTarget(),
    )

    assert isinstance(result, Instantiated)
    assert result.values["mcp_url"] == MCP_URL


def test_a_run_mcp_url_equal_to_the_targets_is_accepted(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    run = RunSettings(**{**vars(RUN), "mcp_url": MCP_URL})

    result = instantiate(
        setup_package,
        setup_template,
        tmp_path / "b",
        target=TARGET,
        run=run,
        call_target=FakeTarget(),
    )

    assert isinstance(result, Instantiated)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda t: (t / "conversation.template.json").unlink(), id="no-conversation"),
        pytest.param(
            lambda t: (t / "run.template.json").write_text("{", "utf-8"), id="run-not-json"
        ),
        pytest.param(
            lambda t: (t / "run.template.json").write_text("[]", "utf-8"), id="run-a-list"
        ),
        pytest.param(
            lambda t: _edit_manifest(t, templates={"conversation": "conversation.template.json"}),
            id="templates-without-run",
        ),
        pytest.param(lambda t: _edit_manifest(t, requires="model"), id="requires-not-a-list"),
        pytest.param(lambda t: _edit_manifest(t, entrypoint=[1]), id="entrypoint-not-texts"),
        pytest.param(lambda t: _edit_manifest(t, entrypoint=[]), id="entrypoint-empty"),
        pytest.param(lambda t: _require(t, 3), id="requires-a-number"),
        pytest.param(lambda t: _require(t, "model"), id="requires-a-repeat"),
        pytest.param(lambda t: _require(t, "sandbox_id"), id="requires-an-unknown-key"),
        pytest.param(lambda t: _require(t, "user_turn_0"), id="requires-turn-zero"),
        pytest.param(
            lambda t: _edit_manifest(t, requires=_requires(t, drop="model")),
            id="requires-lacks-the-entrypoint-model",
        ),
        pytest.param(lambda t: _plant(t, {"$value": "secret"}), id="value-slot-undeclared"),
        pytest.param(lambda t: _plant(t, {"$value": 3}), id="value-slot-not-a-key"),
        pytest.param(lambda t: _plant(t, {"$bundle": "../outside"}), id="bundle-slot-escapes"),
        pytest.param(lambda t: _plant(t, {"$bundle": "/etc/passwd"}), id="bundle-slot-absolute"),
        pytest.param(lambda t: _plant(t, {"$bundle": 3}), id="bundle-slot-not-a-path"),
        pytest.param(lambda t: _require(t, "user_turn_999"), id="requires-an-unused-turn"),
        pytest.param(
            lambda t: _edit_manifest(t, entrypoint=[*_entrypoint(t), "--x", "{bogus}"]),
            id="entrypoint-unknown-placeholder",
        ),
        pytest.param(lambda t: _move_run(t, "../run.template.json"), id="template-name-escapes"),
        pytest.param(
            lambda t: _move_run(t, str((t / "run.template.json").resolve())),
            id="template-name-absolute",
        ),
    ],
)
def test_a_damaged_template_is_refused_before_any_target_call(
    setup_package: Path, setup_template: Path, tmp_path: Path, damage: Any
) -> None:
    damage(setup_template)
    call = FakeTarget()

    with pytest.raises(InstantiateError):
        instantiate(
            setup_package, setup_template, tmp_path / "b", target=TARGET, run=RUN, call_target=call
        )

    assert call.calls == []
    assert not (tmp_path / "b").exists()


def _edit_manifest(template: Path, **fields: Any) -> None:
    path = template / "bundle.json"
    manifest = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps({**manifest, **fields}), "utf-8")


def _requires(template: Path, drop: str | None = None) -> list[Any]:
    manifest = json.loads((template / "bundle.json").read_text("utf-8"))
    return [key for key in manifest["requires"] if key != drop]


def _entrypoint(template: Path) -> list[str]:
    return json.loads((template / "bundle.json").read_text("utf-8"))["entrypoint"]


def _require(template: Path, key: Any) -> None:
    _edit_manifest(template, requires=[*_requires(template), key])


def _plant(template: Path, slot: dict[str, Any]) -> None:
    """Put ``slot`` deep inside the conversation template."""

    path = template / "conversation.template.json"
    document = json.loads(path.read_text("utf-8"))
    document["tools"][0]["headers"] = [{"x": slot}]
    path.write_text(json.dumps(document), "utf-8")


def _move_run(template: Path, name: str) -> None:
    """Name the run template by ``name``, a copy of it existing there."""

    source = template / "run.template.json"
    copy = (template / name) if not Path(name).is_absolute() else Path(name)
    copy.write_text(source.read_text("utf-8"), "utf-8")
    conversation = "conversation.template.json"
    _edit_manifest(template, templates={"conversation": conversation, "run": name})


# Package data holding an object with a dollar-prefixed key, which is not a slot marker.
# Package data holding one-key objects with a dollar-prefixed key, which are not slot
# markers: a refusal of unknown "$" keys would reject them.
DOLLAR_LITERAL = {"$schema": {"$id": "https://example.invalid/literal"}}
CONDITION_SCHEMA = REPO / "contracts/tool-call-condition/tool-call-condition-v1/schema.json"


@pytest.mark.parametrize("sequential", [False, True], ids=["single", "sequential"])
@pytest.mark.parametrize("claim_level", ["command_attempt", "reply"])
def test_a_dollar_prefixed_literal_in_package_data_survives_into_the_bundle(
    tmp_path: Path, claim_level: str, sequential: bool
) -> None:
    # A literal operand's value and a judge fact's value may be any JSON.
    left = {"source": "argument", "operation": "get_referral", "argument": "filter"}
    right = {"source": "literal", "value": DOLLAR_LITERAL}
    condition = {"comparisons": [{"kind": "value", "left": left, "op": "ne", "right": right}]}
    jsonschema.validate(condition, json.loads(CONDITION_SCHEMA.read_text("utf-8")))
    fact = {"ref": "state:filter", "value": DOLLAR_LITERAL, "source": "state:filter"}
    judge = {**JUDGE, "facts": [fact]}
    package = write_test_package(
        tmp_path / "packages",
        claim_level=claim_level,
        stimulus=sequential_stimulus() if sequential else None,
        condition=condition,
        judge=judge,
    )
    compile(package, tmp_path / "template")
    run = RunSettings(**{**vars(RUN), "allowed_tools": ("get_referral",)})

    result = instantiate(package, tmp_path / "template", tmp_path / "b", target=TARGET, run=run)

    assert isinstance(result, Instantiated)
    entry = json.loads((tmp_path / "b" / "conversations.jsonl").read_text("utf-8"))
    if claim_level == "command_attempt":
        assert entry["tool_call_condition"] == condition
    else:
        assert entry["judge_rubric"]["facts"] == judge["facts"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"run": {"model": "m"}}, "lack gateway_url", id="run-lacks-a-key"),
        pytest.param({"run": {**vars(RUN), "extra": 1}}, "unknown keys: extra", id="run-extra"),
        pytest.param({"run": [1]}, "JSON object", id="run-not-an-object"),
        pytest.param(
            {"run": {**vars(RUN), "judge_turns": "some"}}, "judge_turns", id="bad-judge-turns"
        ),
        pytest.param({"target": 7}, "neither a mapping nor a path", id="target-not-a-config"),
        pytest.param({"target": "missing.yaml"}, "unreadable", id="target-missing"),
        pytest.param(
            {"target": MOCK_TARGET}, "names no usable endpoint.mcp_url", id="target-without-mcp"
        ),
        pytest.param(
            {"target": {**MOCK_TARGET, "endpoint": {"mcp_url": "http://127.0.0.1:{port}/mcp"}}},
            "names no usable endpoint.mcp_url",
            id="target-mcp-unfilled",
        ),
        pytest.param(
            {"run": {**vars(RUN), "mcp_url": "http://127.0.0.1:9/mcp"}},
            "is not the authorized target config's endpoint.mcp_url",
            id="run-mcp-url-mismatch",
        ),
        pytest.param({"run": {1: "x", "model": "m"}}, "keys must be texts", id="run-key-not-text"),
        pytest.param(
            {"run": {**vars(RUN), "gateway_url": "ftp://x"}},
            "malformed gateway_url",
            id="bad-gateway-url",
        ),
        pytest.param(
            {"run": {**vars(RUN), "allowed_tools": "withdraw"}},
            "malformed allowed_tools",
            id="tools-not-a-list",
        ),
        pytest.param(
            {"run": {**vars(RUN), "judge_url": None}},
            "lack judge_url, which the template needs",
            id="reply-without-judge",
        ),
    ],
)
def test_instantiate_refuses_unusable_inputs_before_any_setup(
    setup_package: Path,
    setup_template: Path,
    tmp_path: Path,
    change: dict[str, Any],
    message: str,
) -> None:
    call = FakeTarget()
    arguments = {"target": TARGET, "run": RUN, **change}

    with pytest.raises(InstantiateError, match=message):
        instantiate(setup_package, setup_template, tmp_path / "b", call_target=call, **arguments)

    assert call.calls == []


def test_instantiate_refuses_a_template_of_another_package(
    command_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    with pytest.raises(InstantiateError, match="not compiled from this package"):
        instantiate(
            command_package,
            setup_template,
            tmp_path / "b",
            target=TARGET,
            run=RUN,
            call_target=FakeTarget(),
        )


def test_instantiate_refuses_a_non_empty_output_before_setup(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "left-over").write_text("x", encoding="utf-8")
    call = FakeTarget()

    with pytest.raises(InstantiateError, match="not empty"):
        instantiate(
            setup_package,
            setup_template,
            tmp_path / "b",
            target=TARGET,
            run=RUN,
            call_target=call,
        )

    assert call.calls == []


def test_a_package_without_an_inventory_needs_the_allowed_tools(
    command_package: Path, tmp_path: Path
) -> None:
    compile(command_package, tmp_path / "template")

    with pytest.raises(InstantiateError, match="no allowed tools"):
        instantiate(command_package, tmp_path / "template", tmp_path / "b", target=TARGET, run=RUN)

    run = RunSettings(**{**vars(RUN), "allowed_tools": ("get_referral",)})
    result = instantiate(
        command_package, tmp_path / "template", tmp_path / "b", target=TARGET, run=run
    )
    assert isinstance(result, Instantiated)
    assert result.values["allowed_tools"] == ["get_referral"]
    assert "judge_url" not in result.manifest["requires"]


def test_compile_refuses_a_missing_package_directory(tmp_path: Path) -> None:
    with pytest.raises(CompileError, match="package directory does not exist"):
        compile(tmp_path / "missing", tmp_path / "template")


def test_parse_refuses_a_missing_bundle_or_records_directory(
    setup_package: Path, setup_template: Path, tmp_path: Path
) -> None:
    instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        target=TARGET,
        run=RUN,
        call_target=FakeTarget(),
    )

    with pytest.raises(ParseError, match="bundle directory does not exist"):
        parse(tmp_path / "missing", tmp_path / "receipt.json")
    with pytest.raises(ParseError, match="records directory does not exist"):
        parse(tmp_path / "bundle", tmp_path / "receipt.json", records_dir=tmp_path / "none")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param({"out_path": 7}, "receipt path must be a path", id="out-not-a-path"),
        pytest.param({"out_path": ""}, "receipt path must be a path", id="out-empty"),
        pytest.param({"out_path": "."}, "receipt path is a directory", id="out-a-directory"),
        pytest.param({"report": 7}, "report must be a path", id="report-not-a-path"),
        pytest.param({"report": ""}, "report must be a path", id="report-empty"),
        pytest.param({"tool_revision": 7}, "tool_revision must be a string", id="bad-revision"),
    ],
)
def test_parse_refuses_unusable_arguments(
    setup_package: Path,
    setup_template: Path,
    tmp_path: Path,
    arguments: dict[str, Any],
    message: str,
) -> None:
    bundle = tmp_path / "bundle"
    instantiate(
        setup_package, setup_template, bundle, target=TARGET, run=RUN, call_target=FakeTarget()
    )
    given = {"out_path": bundle / "receipt.json", **arguments}

    with pytest.raises(ParseError, match=message):
        parse(bundle, **given)


def write_run(tmp_path: Path, settings: Any) -> Path:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


def cli_instantiate(
    package: Path, template: Path, out: Path, run: Path, target: Path, *extra: str
) -> int:
    return main(
        [
            "instantiate",
            str(template),
            "--package",
            str(package),
            "--target",
            str(target),
            "--run",
            str(run),
            "--out",
            str(out),
            *extra,
        ]
    )


def test_cli_instantiate_fills_the_port_and_prints_the_preparation(
    command_package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compile(command_package, tmp_path / "template")
    run = write_run(
        tmp_path,
        {"gateway_url": RUN.gateway_url, "model": "m", "allowed_tools": ["get_referral"]},
    )

    code = cli_instantiate(
        command_package,
        tmp_path / "template",
        tmp_path / "bundle",
        run,
        PIGGYBANK / "target.yaml",
        "--port",
        "18001",
    )

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["status"], printed["setup_ledger"]) == ("prepared", [])
    entry = json.loads((tmp_path / "bundle" / "conversations.jsonl").read_text("utf-8"))
    assert entry["tools"][0]["server_url"] == "http://127.0.0.1:18001/mcp"


def test_cli_instantiate_exits_4_when_the_package_cannot_be_prepared(
    setup_package: Path, setup_template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The MCP call the CLI makes for setup is the stub, which refuses without a socket.
    run = write_run(tmp_path, {**vars(RUN), "allowed_tools": None})

    code = cli_instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        run,
        PIGGYBANK / "target.yaml",
        "--port",
        "18001",
    )

    assert code == 4
    printed = json.loads(capsys.readouterr().out)
    assert (printed["status"], printed["reason"]) == ("incomplete", "setup_failed")
    assert printed["setup_ledger"][0]["error"] == "no network: http://127.0.0.1:18001/mcp"
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    ("target_text", "run_text", "message"),
    [
        pytest.param("target: production\n", None, "authorized_as", id="unauthorized"),
        pytest.param(None, "{not json", "run settings are unreadable", id="bad-run"),
        # The target config is checked first, before the run settings are read.
        pytest.param(
            "target: production\n", "{not json", "authorized_as", id="unauthorized-bad-run"
        ),
    ],
)
def test_cli_instantiate_refuses_unusable_input_with_exit_1(
    command_package: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target_text: str | None,
    run_text: str | None,
    message: str,
) -> None:
    compile(command_package, tmp_path / "template")
    target = PIGGYBANK / "target.yaml"
    if target_text is not None:
        target = tmp_path / "target.yaml"
        target.write_text(target_text, encoding="utf-8")
    run = write_run(tmp_path, {"gateway_url": RUN.gateway_url, "model": "m"})
    if run_text is not None:
        run.write_text(run_text, encoding="utf-8")

    code = cli_instantiate(command_package, tmp_path / "template", tmp_path / "b", run, target)

    assert code == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "b").exists()
