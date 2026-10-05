import typer

from asago_artifact_generator import cli


def test_cli_exposes_generate_as_its_only_command():
    group = typer.main.get_command(cli.app)

    assert set(group.commands) == {"generate"}
