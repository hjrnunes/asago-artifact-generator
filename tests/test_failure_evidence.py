from __future__ import annotations

import pytest

from asago_artifact_generator.failure_evidence import redact_metadata


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"API_Key": "k", "Base_URL": "https://x", "model": "m", 7: "n"},
            {"API_Key": "<redacted>", "Base_URL": "<redacted>", "model": "m", "7": "n"},
        ),
        ([{"token": "t"}, "plain"], [{"token": "<redacted>"}, "plain"]),
        (({"secret": "s"}, 1), [{"secret": "<redacted>"}, 1]),
        ("HTTPS://host/path", "<redacted>"),
        ("see https://host", "see https://host"),
        (3, 3),
        (None, None),
    ],
)
def test_redact_metadata_removes_secret_and_endpoint_values(
    value: object, expected: object
) -> None:
    assert redact_metadata(value) == expected
