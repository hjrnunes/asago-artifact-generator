"""The Garak adapter builds on the shared core instead of carrying its own copies."""

from __future__ import annotations

import pytest

from asago_bundle_core import gap
from asago_bundle_core.errors import BundleError
from asago_bundle_garak import cli, compiler
from asago_bundle_garak.compiler import CompileError
from asago_bundle_garak.instantiate import InstantiateError
from asago_bundle_garak.report_parse import ParseError


@pytest.mark.parametrize("error", [CompileError, InstantiateError, ParseError])
def test_each_adapter_error_is_a_bundle_error(error: type[Exception]) -> None:
    assert issubclass(error, BundleError)


def test_the_gap_exception_is_the_cores() -> None:
    assert compiler.CapabilityGap is gap.CapabilityGap


def test_the_exit_codes_are_the_cores() -> None:
    assert (cli.EXIT_OK, cli.EXIT_INPUT, cli.EXIT_CAPABILITY_GAP) == (
        gap.EXIT_OK,
        gap.EXIT_INPUT,
        gap.EXIT_CAPABILITY_GAP,
    )
