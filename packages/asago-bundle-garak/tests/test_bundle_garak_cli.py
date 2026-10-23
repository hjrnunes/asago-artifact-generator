"""The console script and its subcommand surface, and the legacy ``instantiate`` form."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_garak.cli import main
from asago_bundle_garak.compiler import compile_package
from asago_bundle_garak.instantiate import InstantiateError, fill_template, read_template

VALUES = {
    "gateway_url": "http://127.0.0.1:18999/v1",
    "mcp_url": "http://127.0.0.1:18001/mcp",
    "allowed_tools": ["get_referral", "get_education"],
    "model": "gemma-4-26b-a4b-it",
    "messages": [{"role": "user", "content": "Show PAT-201."}],
}


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "asago-bundle-garak" in capsys.readouterr().out


def _refuse(*_: Any, **__: Any) -> Any:
    raise AssertionError("the legacy form read a target config or prepared the package")


@pytest.fixture
def no_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything reads a target config, prepares a package or calls a target."""

    for module, name in [
        ("asago_bundle_core.target", "load_target_config"),
        ("asago_bundle_core.target", "authorized_target"),
        ("asago_bundle_core.target", "mcp_call_target"),
        ("asago_bundle_core.preparation", "prepare_package"),
        ("asago_bundle_garak.cli", "authorized_target"),
        ("asago_bundle_garak.cli", "instantiate"),
        ("asago_bundle_garak.entry_points", "authorized_target"),
        ("asago_bundle_garak.entry_points", "mcp_call_target"),
        ("asago_bundle_garak.entry_points", "prepare_package"),
        # The package's ``instantiate`` function shadows this module as an attribute.
        ("asago_bundle_garak.instantiate", "authorized_target"),
    ]:
        monkeypatch.setattr(importlib.import_module(module), name, _refuse)


@pytest.fixture
def template(command_package: Path, tmp_path: Path) -> Path:
    compile_package(command_package, tmp_path / "template")
    return tmp_path / "template"


def write_values(tmp_path: Path, content: Any) -> Path:
    path = tmp_path / "values.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), "utf-8")
    return path


def files(bundle: Path) -> dict[str, str]:
    return {
        str(path.relative_to(bundle)): path.read_text(encoding="utf-8") if path.is_file() else ""
        for path in sorted(bundle.rglob("*"))
    }


def test_the_legacy_form_fills_the_template_with_the_values_and_touches_no_target(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_target: None
) -> None:
    bundle = tmp_path / "bundle"

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(write_values(tmp_path, VALUES)),
            "--out",
            str(bundle),
        ]
    )

    assert code == 0
    assert capsys.readouterr() == ("", "")
    legacy = files(bundle)
    bundle.rename(tmp_path / "legacy")
    # The filler the prepared form uses writes the same bundle from the same values.
    fill_template(read_template(template), VALUES, bundle)
    assert legacy == files(bundle)
    assert set(legacy) == {"bundle.json", "conversations.jsonl", "reports", "run.yaml"}


def test_the_legacy_form_checks_the_template_only_as_it_did_before_preparation(
    template: Path, tmp_path: Path, no_target: None
) -> None:
    # ``read_template`` refuses a requires key it has no check for; the legacy form checks
    # any ``user_turn_`` key as a text and fills the template.
    manifest = json.loads((template / "bundle.json").read_text(encoding="utf-8"))
    manifest["requires"].append("user_turn_x")
    (template / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    supplied = {**VALUES, "user_turn_x": "Hello."}

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(write_values(tmp_path, supplied)),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 0
    assert (tmp_path / "b" / "conversations.jsonl").is_file()
    with pytest.raises(InstantiateError, match="no check for"):
        read_template(template)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param("{not json", "values are unreadable", id="unreadable"),
        pytest.param({"model": "m"}, "values lack", id="incomplete"),
        pytest.param({**VALUES, "user_turn_x": " "}, "malformed user_turn_x", id="blank-turn"),
    ],
)
def test_the_legacy_form_refuses_unusable_values_with_exit_1(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    no_target: None,
    content: Any,
    message: str,
) -> None:
    manifest = json.loads((template / "bundle.json").read_text(encoding="utf-8"))
    if isinstance(content, dict) and "user_turn_x" in content:
        manifest["requires"].append("user_turn_x")
        (template / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(write_values(tmp_path, content)),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--package", "p"],
        ["--target", "target.yaml"],
        ["--port", "8010"],
        ["--run", "run.json"],
        ["--package", "p", "--target", "target.yaml", "--run", "run.json"],
    ],
)
def test_values_with_an_option_of_the_prepared_form_is_a_usage_error(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    no_target: None,
    extra: list[str],
) -> None:
    values = write_values(tmp_path, VALUES)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "instantiate",
                str(template),
                "--values",
                str(values),
                "--out",
                str(tmp_path / "b"),
                *extra,
            ]
        )

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "--values (the legacy form, values already resolved) cannot be combined" in err
    assert all(option in err for option in extra if option.startswith("--"))
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        pytest.param([], "missing --package, --target, --run", id="neither-form"),
        pytest.param(["--port", "8010"], "missing --package, --target, --run", id="port-only"),
        pytest.param(["--package", "p", "--target", "t.yaml"], "missing --run", id="no-run"),
    ],
)
def test_a_call_without_either_form_is_a_usage_error(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    no_target: None,
    extra: list[str],
    missing: str,
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["instantiate", str(template), "--out", str(tmp_path / "b"), *extra])

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "give either --package, --target and --run, or, in the legacy form, --values" in err
    assert missing in err
    assert not (tmp_path / "b").exists()
