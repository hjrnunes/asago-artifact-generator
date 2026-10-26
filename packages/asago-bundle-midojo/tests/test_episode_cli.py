"""The adapter's command line: compile an episode, parse its sealed result."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from episode_cases import EPISODES, TARGET, write_case_package, write_planted_package

from asago_bundle_core.testing import single_stimulus
from asago_bundle_midojo.cli import main


def compile_args(package: Path, out: Path, *extra: str) -> list[str]:
    return [
        "compile",
        str(package),
        "--out",
        str(out),
        "--target",
        str(TARGET),
        *extra,
    ]


def test_compile_writes_the_episode(tmp_path: Path) -> None:
    package = write_case_package(tmp_path / "packages", "reply-single")

    assert main(compile_args(package, tmp_path / "out", "--judge-turns", "all")) == 0

    document = json.loads((tmp_path / "out/episode.json").read_text(encoding="utf-8"))
    (spec,) = document["episode"]["security"].values()
    assert spec["judge_turns"] == "all"


def test_compile_blocks_each_tool_block_names(tmp_path: Path) -> None:
    package = write_case_package(tmp_path / "packages", "command-single")

    assert main(compile_args(package, tmp_path / "out", "--block", "withdraw")) == 0

    document = json.loads((tmp_path / "out/episode.json").read_text(encoding="utf-8"))
    assert document["tools"]["write_disposition"] == {
        "default": "forward",
        "tools": {"withdraw": "block"},
    }


def test_compile_requires_the_target_config(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["compile", "p", "--out", "o"])

    assert raised.value.code == 2
    assert "--target" in capsys.readouterr().err


def test_a_target_config_not_authorized_for_a_run_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = write_case_package(tmp_path / "packages", "command-single")
    target = tmp_path / "target" / "target.yaml"
    target.parent.mkdir()
    target.write_text("target: piggybank\n", encoding="utf-8")

    code = main(["compile", str(package), "--out", str(tmp_path / "out"), "--target", str(target)])

    assert code == 1
    assert "not authorized for a run" in capsys.readouterr().err


def test_a_package_midojo_cannot_run_exits_3_with_the_gap_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = write_planted_package(tmp_path / "packages", stimulus=single_stimulus())

    assert main(compile_args(package, tmp_path / "out")) == 3

    record = json.loads(capsys.readouterr().out)
    assert record["kind"] == "capability_gap"
    assert record["reason"].startswith("MiDojo runs planted_item packages only")
    assert not (tmp_path / "out").exists()


def test_a_package_that_cannot_be_read_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(compile_args(tmp_path / "missing", tmp_path / "out")) == 1

    assert capsys.readouterr().err.startswith("compile: package cannot be loaded")


def test_parse_writes_the_receipt(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(EPISODES / "command-single/detected", run)

    code = main(
        [
            "parse",
            str(EPISODES / "command-single"),
            "--snapshot",
            str(run / "snapshot.json"),
            "--verdict",
            str(run / "verdict.json"),
            "--out",
            str(run / "receipt.json"),
            "--tool-revision",
            "3824922f",
            "--route",
            "midojo",
        ]
    )

    assert code == 0
    receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["tool"]["revision"] == "3824922f"
    assert receipt["episode"]["route"] == "midojo"
    assert receipt["attempts"][0]["result"]["outcome"] == "detected"


def test_parse_without_an_episode_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run"
    shutil.copytree(EPISODES / "command-single/detected", run)

    code = main(
        [
            "parse",
            str(tmp_path),
            "--snapshot",
            str(run / "snapshot.json"),
            "--verdict",
            str(run / "verdict.json"),
            "--out",
            str(run / "receipt.json"),
        ]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("parse: ")


def test_parse_offers_only_the_routes_that_exist(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["parse", "e", "--snapshot", "s", "--verdict", "v", "--out", "o", "--route", "garak"])

    assert raised.value.code == 2
    assert "invalid choice: 'garak'" in capsys.readouterr().err
