"""The console script and its subcommand surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ipi_cases import (
    SEQUENTIAL_TURNS,
    TARGET,
    planted_stimulus,
    write_planted_package,
)

from asago_bundle_garak.cli import main


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "asago-bundle-garak" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        pytest.param([], "--package, --target, --run", id="none"),
        pytest.param(["--port", "8010"], "--package, --target, --run", id="port-only"),
        pytest.param(["--package", "p", "--target", "t.yaml"], "--run", id="no-run"),
        pytest.param(
            ["--values", "v.json", "--package", "p", "--target", "t.yaml", "--run", "r.json"],
            "unrecognized arguments: --values",
            id="values",
        ),
    ],
)
def test_instantiate_without_package_target_and_run_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["instantiate", str(tmp_path / "template"), "--out", str(tmp_path / "b"), *extra])

    assert raised.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


def test_compile_help_lists_the_judge_turns_choice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["compile", "--help"])

    assert raised.value.code == 0
    assert "--judge-turns" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flag", "judge_turns", "timeout"),
    [
        pytest.param(None, "final", 190, id="default"),
        pytest.param("all", "all", 370, id="all"),
    ],
)
def test_compile_writes_the_turns_the_judge_grades(
    tmp_path: Path, flag: str | None, judge_turns: str, timeout: int
) -> None:
    package = write_planted_package(
        tmp_path / "packages",
        scenario_id="SCN-052",
        claim_level="reply",
        stimulus=planted_stimulus(SEQUENTIAL_TURNS),
    )
    out = tmp_path / "out"

    assert (
        main(
            [
                "compile",
                str(package),
                "--out",
                str(out),
                "--target",
                str(TARGET),
                *(("--judge-turns", flag) if flag is not None else ()),
            ]
        )
        == 0
    )

    (entry,) = json.loads((out / "ipi-episodes.json").read_text(encoding="utf-8"))
    reply = entry["injection_spec"]["security"]["reply_judge"]
    assert reply["rubric"]["judge_turns"] == judge_turns
    assert reply["timeout_seconds"] == timeout
