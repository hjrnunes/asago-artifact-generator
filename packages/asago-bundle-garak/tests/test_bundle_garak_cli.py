"""The console script and its subcommand surface."""

from __future__ import annotations

from pathlib import Path

import pytest

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
