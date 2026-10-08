"""The console script and its subcommand surface."""

from __future__ import annotations

import pytest

from asago_bundle_garak.cli import main


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "asago-bundle-garak" in capsys.readouterr().out
