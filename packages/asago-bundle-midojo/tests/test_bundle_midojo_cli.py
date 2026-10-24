"""The console script: subcommands and exit codes (0 done, 1 input, 2 usage, 3 gap)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_bundle_core.testing import write_test_package
from asago_bundle_midojo.cli import main
from conftest import BASE, DOMAIN, REPO, TARGET_CONFIG, VALUES, write_native

FLAGS = ["--midojo-base", str(BASE), "--domain", DOMAIN]
# Written by c18e46d's package writer (see its generate.py).
PACKAGE_WITH_REQUEST_COPIES = REPO / "tests/fixtures/package_with_request_copies/package"


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "asago-bundle-midojo" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["compile"],
        ["unknown"],
        ["compile", "x", "--out"],
        ["compile", "x", "--out", "o"],
        ["compile", "x", "--out", "o", "--midojo-base", "b"],
        ["compile", "x", "--out", "o", "--domain", "klarna"],
    ],
)
def test_a_usage_error_exits_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2


def test_compile_writes_the_template_and_exits_zero(command_package: Path, tmp_path: Path) -> None:
    code = main(["compile", str(command_package), "--out", str(tmp_path / "template"), *FLAGS])

    assert code == 0
    assert (tmp_path / "template" / "bundle.json").is_file()


def test_compile_takes_a_package_written_before_the_request_copies_were_retired(
    tmp_path: Path,
) -> None:
    # Its authoring/ request copies and source-hashes.json are verified, then ignored.
    template = tmp_path / "template"

    code = main(["compile", str(PACKAGE_WITH_REQUEST_COPIES), "--out", str(template), *FLAGS])

    assert code == 0
    manifest = json.loads((template / "bundle.json").read_text(encoding="utf-8"))
    package = json.loads((PACKAGE_WITH_REQUEST_COPIES / "manifest.json").read_text("utf-8"))
    assert manifest["package_digest"] == package["manifest_digest"]
    assert manifest["claim_level"] == "command_attempt"


def test_compile_of_a_sequential_package_writes_the_template_and_exits_zero(
    sequential_package: Path, tmp_path: Path
) -> None:
    code = main(["compile", str(sequential_package), "--out", str(tmp_path / "template"), *FLAGS])

    assert code == 0
    manifest = json.loads((tmp_path / "template" / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["delivery"] == "sequential"


def test_compile_of_a_gap_prints_the_record_and_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = write_test_package(tmp_path / "packages", claim_level="state_effect")

    code = main(["compile", str(package), "--out", str(tmp_path / "template"), *FLAGS])

    assert code == 3
    record = json.loads(capsys.readouterr().out)
    assert record["tool"] == "midojo"
    assert record["delivery"] == "single"
    assert "state_effect" in record["reason"]
    assert not (tmp_path / "template").exists()


def test_compile_of_an_unknown_domain_exits_one(
    command_package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["compile", str(command_package), "--out", str(tmp_path / "t")]
    code = main([*argv, "--midojo-base", str(BASE), "--domain", "nowhere"])

    assert code == 1
    assert "nowhere" in capsys.readouterr().err


def test_compile_of_an_unreadable_package_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["compile", str(tmp_path / "missing"), "--out", str(tmp_path / "template"), *FLAGS]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("compile: ")


def test_instantiate_writes_the_bundle_and_exits_zero(template: Path, tmp_path: Path) -> None:
    values = tmp_path / "values.json"
    values.write_text(json.dumps(VALUES), encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(values),
            "--target",
            str(TARGET_CONFIG),
            "--out",
            str(tmp_path / "b"),
        ]
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
        [
            "instantiate",
            str(template),
            "--values",
            str(values),
            "--target",
            str(TARGET_CONFIG),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("instantiate: ")


@pytest.mark.parametrize(
    "values_text",
    [
        pytest.param(json.dumps(VALUES), id="usable-values"),
        # The target config is checked before the values are read.
        pytest.param("{not json", id="unreadable-values"),
    ],
)
def test_instantiate_refuses_a_target_not_authorized_for_a_run(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], values_text: str
) -> None:
    values = tmp_path / "values.json"
    values.write_text(values_text, encoding="utf-8")
    target = tmp_path / "target.yaml"
    target.write_text("target: production\n", encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(values),
            "--target",
            str(target),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 1
    assert "authorized_as" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        pytest.param([], "--values, --target", id="neither"),
        pytest.param(["--target", str(TARGET_CONFIG)], "--values", id="no-values"),
        pytest.param(["--values", "values.json"], "--target", id="no-target"),
    ],
)
def test_instantiate_without_values_and_target_is_a_usage_error(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    missing: str,
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["instantiate", str(template), "--out", str(tmp_path / "b"), *extra])

    assert raised.value.code == 2
    assert f"the following arguments are required: {missing}" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


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
