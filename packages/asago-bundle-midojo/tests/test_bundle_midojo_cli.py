"""The console script: subcommands and exit codes (0 done, 1 input, 2 usage, 3 gap)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_bundle_midojo.cli import main
from conftest import VALUES, write_native


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "asago-bundle-midojo" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["compile"], ["unknown"], ["compile", "x", "--out"]])
def test_a_usage_error_exits_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2


def test_compile_writes_the_template_and_exits_zero(command_package: Path, tmp_path: Path) -> None:
    code = main(["compile", str(command_package), "--out", str(tmp_path / "template")])

    assert code == 0
    assert (tmp_path / "template" / "bundle.json").is_file()


def test_compile_of_a_gap_prints_the_record_and_exits_three(
    sequential_package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["compile", str(sequential_package), "--out", str(tmp_path / "template")])

    assert code == 3
    record = json.loads(capsys.readouterr().out)
    assert record["tool"] == "midojo"
    assert record["delivery"] == "sequential"
    assert not (tmp_path / "template").exists()


def test_compile_of_an_unreadable_package_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["compile", str(tmp_path / "missing"), "--out", str(tmp_path / "template")])

    assert code == 1
    assert capsys.readouterr().err.startswith("compile: ")


def test_instantiate_writes_the_bundle_and_exits_zero(template: Path, tmp_path: Path) -> None:
    values = tmp_path / "values.json"
    values.write_text(json.dumps(VALUES), encoding="utf-8")

    code = main(
        ["instantiate", str(template), "--values", str(values), "--out", str(tmp_path / "b")]
    )

    assert code == 0
    assert (tmp_path / "b" / "asago_suite" / "suite.yaml").is_file()


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"model": "m"})])
def test_instantiate_with_unusable_values_exits_one(
    template: Path, tmp_path: Path, content: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    values = tmp_path / "values.json"
    if content is not None:
        values.write_text(content, encoding="utf-8")

    code = main(
        ["instantiate", str(template), "--values", str(values), "--out", str(tmp_path / "b")]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("instantiate: ")


def test_parse_writes_the_receipt_and_exits_zero(bundle: Path) -> None:
    write_native(bundle)

    code = main(
        [
            "parse",
            str(bundle),
            "--out",
            str(bundle / "receipt.json"),
            "--tool-revision",
            "9ceb22e3",
            "--adapter-revision",
            "abc1234",
        ]
    )

    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert receipt["tool"] == {
        "id": "midojo",
        "revision": "9ceb22e3",
        "adapter_revision": "abc1234",
    }


def test_parse_of_a_run_without_records_still_exits_zero(bundle: Path) -> None:
    code = main(["parse", str(bundle), "--out", str(bundle / "receipt.json")])

    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert receipt["execution_status"] == "failed"


def test_parse_takes_the_boundary_records(bundle: Path, tmp_path: Path) -> None:
    write_native(bundle)
    records = tmp_path / "records"
    (records / "mcp_capture").mkdir(parents=True)
    (records / "mcp_capture" / "calls.jsonl").write_text("", encoding="utf-8")

    code = main(
        ["parse", str(bundle), "--out", str(bundle / "receipt.json"), "--records", str(records)]
    )

    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert receipt["incomplete_reason"] == "tool_call_count_mismatch"


def test_parse_of_an_unreadable_bundle_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["parse", str(tmp_path), "--out", str(tmp_path / "receipt.json")])

    assert code == 1
    assert capsys.readouterr().err.startswith("parse: ")
