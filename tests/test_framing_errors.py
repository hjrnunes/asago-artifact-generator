"""The three response framing errors share one base and keep their messages."""

from __future__ import annotations

import pytest

from asago_artifact_generator.authoring.core import (
    AuthoringError,
    Call1FramingError,
    Call2FramingError,
    Finding,
    FramingError,
    ReviewResponseError,
)


@pytest.mark.parametrize(
    ("error", "path", "empty_message"),
    [
        (Call1FramingError, "call1", "invalid Call 1 framing"),
        (Call2FramingError, "call2", "invalid Call 2 framing"),
        (ReviewResponseError, "review", "invalid reviewer response"),
    ],
)
def test_framing_errors_share_one_base(error, path, empty_message):
    findings = [
        Finding("a", "first problem", "x"),
        Finding("b", "second problem", "y"),
    ]

    raised = error(findings)
    empty = error([])

    assert isinstance(raised, FramingError)
    assert isinstance(raised, AuthoringError)
    assert raised.findings == findings
    assert raised.path == path
    assert raised.message == "first problem; second problem"
    assert empty.message == empty_message
    assert empty.path == path
