from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.value_checks import is_nonblank_str, is_sha256_hex


@pytest.mark.parametrize(
    ("value", "expected"),
    [("x", True), (" x ", True), ("", False), (" \n\t", False), (None, False), (7, False)],
)
def test_is_nonblank_str(value: Any, expected: bool) -> None:
    assert is_nonblank_str(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0123456789abcdef" * 4, True),
        ("a" * 63, False),
        ("a" * 65, False),
        ("A" * 64, False),
        ("g" * 64, False),
        ("a" * 63 + "\n", False),
        (b"a" * 64, False),
        (None, False),
    ],
)
def test_is_sha256_hex(value: Any, expected: bool) -> None:
    assert is_sha256_hex(value) is expected
