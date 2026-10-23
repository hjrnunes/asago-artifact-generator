"""The library entry points: compile, instantiate (with preparation) and parse.

A fake target call stands in for the target here; ``test_bundle_garak_fixture_target``
runs the same steps against the fixture target's server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import asago_bundle_garak
from asago_bundle_core.preparation import PreparationIncomplete
from asago_bundle_core.testing import MOCK_TARGET
from asago_bundle_garak import (
    CompileError,
    Instantiated,
    InstantiateError,
    ParseError,
    RunSettings,
    compile,
    instantiate,
    parse,
)
from asago_bundle_garak.cli import main
from asago_bundle_garak.report_parse import read_bundle
from conftest import PIGGYBANK, WITHDRAW, write_setup_package

RUN = RunSettings(
    gateway_url="http://127.0.0.1:18999/v1",
    model="fixture-model",
    judge_url="http://127.0.0.1:18998/v1",
    judge_model="judge-model",
    mcp_url="http://127.0.0.1:18001/mcp",
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
        target=MOCK_TARGET,
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
    assert result.values["mcp_url"] == RUN.mcp_url
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
        target=MOCK_TARGET,
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
        target=MOCK_TARGET,
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
    result = instantiate(
        setup_package,
        setup_template,
        tmp_path / "bundle",
        target=PIGGYBANK / "target.yaml",
        run=RUN,
        call_target=FakeTarget(),
    )

    assert isinstance(result, Instantiated)


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
            {"run": {**vars(RUN), "mcp_url": None}}, "no MCP URL", id="no-mcp-url-anywhere"
        ),
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
    arguments = {"target": MOCK_TARGET, "run": RUN, **change}

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
            target=MOCK_TARGET,
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
            target=MOCK_TARGET,
            run=RUN,
            call_target=call,
        )

    assert call.calls == []


def test_a_package_without_an_inventory_needs_the_allowed_tools(
    command_package: Path, tmp_path: Path
) -> None:
    compile(command_package, tmp_path / "template")

    with pytest.raises(InstantiateError, match="no allowed tools"):
        instantiate(
            command_package, tmp_path / "template", tmp_path / "b", target=MOCK_TARGET, run=RUN
        )

    run = RunSettings(**{**vars(RUN), "allowed_tools": ("get_referral",)})
    result = instantiate(
        command_package, tmp_path / "template", tmp_path / "b", target=MOCK_TARGET, run=run
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
        target=MOCK_TARGET,
        run=RUN,
        call_target=FakeTarget(),
    )

    with pytest.raises(ParseError, match="bundle directory does not exist"):
        parse(tmp_path / "missing", tmp_path / "receipt.json")
    with pytest.raises(ParseError, match="records directory does not exist"):
        parse(tmp_path / "bundle", tmp_path / "receipt.json", records_dir=tmp_path / "none")


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
    # Without --port the target's MCP URL keeps its placeholder, so setup has no target call.
    run = write_run(tmp_path, {**vars(RUN), "allowed_tools": None})

    code = cli_instantiate(
        setup_package, setup_template, tmp_path / "bundle", run, PIGGYBANK / "target.yaml"
    )

    assert code == 4
    printed = json.loads(capsys.readouterr().out)
    assert (printed["status"], printed["reason"]) == ("incomplete", "setup_failed")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    ("target_text", "run_text", "message"),
    [
        pytest.param("target: production\n", None, "authorized_as", id="unauthorized"),
        pytest.param(None, "{not json", "run settings are unreadable", id="bad-run"),
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
