from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner, Result

from asago_artifact_generator import cli

_HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "scenario-handoff"
    / "handoff-v3"
    / "valid"
    / "refund-bound.json"
)


def test_cli_exposes_generate_as_its_only_command():
    group = typer.main.get_command(cli.app)

    assert set(group.commands) == {"generate"}


def test_run_hands_the_transport_factory_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(cli, "app", lambda **options: calls.append(options))

    def factory(**options: object) -> object:
        return options

    cli.run(("generate", "x"), prog_name="prog", transport_factory=factory)

    assert calls == [{"args": ["generate", "x"], "prog_name": "prog", "obj": factory}]


def _invoke_generate(source: Path, target_profile: Path) -> Result:
    return CliRunner().invoke(
        cli.app,
        [
            "generate",
            str(source),
            "--target-profile",
            str(target_profile),
            "--runtime-contract",
            str(source.parent / "unused-runtime-contract.json"),
            "--profile",
            "unused",
        ],
    )


def test_generate_rejects_a_source_that_is_not_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = _invoke_generate(Path("missing.json"), Path("profile.json"))

    assert result.exit_code == 2
    assert "input source is not a file" in result.output


def test_generate_rejects_a_target_profile_it_cannot_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("p.json").write_text("[]", encoding="utf-8")

    result = _invoke_generate(_HANDOFF, Path("p.json"))

    assert result.exit_code == 2
    assert "Invalid value for p.json: target profile must be an object" in result.output
