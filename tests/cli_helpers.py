"""Shared CLI invocation helper that renders Typer output without a terminal."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from typer import rich_utils
from typer.testing import CliRunner, Result

# Wide enough that Rich never wraps or truncates an option name or message.
_PLAIN_WIDTH = 200


class PlainCliRunner(CliRunner):
    """Invoke a CLI with Typer's Rich output forced to plain, wide text.

    Typer reads `GITHUB_ACTIONS`, `FORCE_COLOR`, `TERM`, and the terminal width
    once, when `typer.rich_utils` is imported, so changing the environment from
    a test has no effect. Patch the module settings for the duration of each
    invocation instead, so help and error text compare the same on a developer
    shell and on CI.
    """

    def invoke(self, *args: Any, **kwargs: Any) -> Result:
        with patch.multiple(
            rich_utils,
            FORCE_TERMINAL=False,
            COLOR_SYSTEM=None,
            MAX_WIDTH=_PLAIN_WIDTH,
        ):
            return super().invoke(*args, **kwargs)
