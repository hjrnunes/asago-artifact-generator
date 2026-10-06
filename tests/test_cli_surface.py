import pytest
import typer

from asago_artifact_generator import cli


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
