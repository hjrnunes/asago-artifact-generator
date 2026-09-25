"""Closed manifest usage metadata policy regressions."""

from __future__ import annotations

import pytest

from asago_artifact_generator.authoring import AuthoringError, assert_no_secrets
from asago_artifact_generator.package_io import (
    PackageIntegrityError,
    build_package,
)


def _package(*, authoring: dict) -> object:
    return build_package(
        package_id="usage-policy",
        scenario_id="usage-policy",
        input_kind="scenario-handoff-v1",
        source_digests={"input": "a" * 64},
        members={"detector.py": b"source\n"},
        authoring=authoring,
    )


def test_exact_captured_provider_usage_metadata_is_allowed() -> None:
    metadata = {
        "interface": "artifact-authoring-v1",
        "usage": [
            {
                "availability": "available",
                "value": {
                    "prompt_tokens": 4,
                    "completion_tokens": 3,
                    "total_tokens": 7,
                },
            }
        ],
    }

    assert_no_secrets({"authoring": metadata})
    assert _package(authoring=metadata).manifest.authoring == metadata


def test_numeric_nested_token_detail_maps_are_allowed() -> None:
    metadata = {
        "usage": [
            {
                "availability": "available",
                "value": {
                    "prompt_tokens": 4,
                    "completion_tokens": 3,
                    "total_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": 1, "nested": {"audio_tokens": 0}},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            }
        ]
    }

    assert_no_secrets({"authoring": metadata})
    assert _package(authoring=metadata).manifest.authoring == metadata


def test_recorded_model_control_metadata_is_allowed() -> None:
    metadata = {
        "ledger": [
            {
                "controls": {
                    "context_window_tokens": 32_768,
                    "max_completion_tokens": 8_192,
                },
                "review": {
                    "effective_controls": {
                        "context_window_tokens": 32_768,
                        "max_completion_tokens": 8_192,
                    }
                },
            }
        ]
    }

    assert_no_secrets({"authoring": metadata})
    assert _package(authoring=metadata).manifest.authoring == metadata


@pytest.mark.parametrize(
    "metadata",
    [
        {"usage": [{"availability": "available", "value": {"prompt_tokens": "4"}}]},
        {"usage": [{"availability": "available", "value": {"prompt_tokens": -1}}]},
        {
            "usage": [
                {
                    "availability": "available",
                    "value": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens_extra": 2},
                }
            ]
        },
        {
            "usage": [
                {
                    "availability": "available",
                    "value": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                        "prompt_tokens_details": {"api_key": 1},
                    },
                }
            ]
        },
        {
            "usage": [
                {
                    "availability": "available",
                    "value": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                        "prompt_tokens_details": {"api_tokens": 1},
                    },
                }
            ]
        },
        {"usage": [{"availability": "available", "value": {"prompt_tokens": 1, "unexpected": 2}}]},
        {"usage": [{"availability": "available", "value": 7}]},
        {"usage": {}},
        {"usage": {1: 1}},
        {"usage": "not-a-list"},
        {"usage": [{"availability": "available", "value": {"prompt_tokens": True}}]},
        {"max_completion_tokens_extra": 8_192},
        {"context_window_tokens_extra": 32_768},
        {"auth_token": "secret"},
        {"auth": "secret"},
        {"session_token": "secret"},
        {"session": "secret"},
        {"access_token": "secret"},
        {"access": "secret"},
        {"bearer_token": "secret"},
        {"bearer": "secret"},
        {"api_key": "secret"},
        {"credential": "secret"},
        {"password": "secret"},
        {"authorization_header": "secret"},
        {"endpoint": "https://example.invalid"},
        {"base_url": "https://example.invalid"},
    ],
)
def test_preflight_and_package_reject_secret_or_invalid_usage_metadata(
    metadata: dict,
) -> None:
    with pytest.raises(AuthoringError):
        assert_no_secrets({"authoring": metadata})
    with pytest.raises(PackageIntegrityError):
        _package(authoring=metadata)


def test_unavailable_usage_marker_is_allowed() -> None:
    metadata = {
        "usage": [{"availability": "unavailable", "reason": "provider did not report usage"}]
    }

    assert_no_secrets({"authoring": metadata})
    assert _package(authoring=metadata).manifest.authoring == metadata

    direct_marker = {"usage": {"availability": "unavailable", "reason": "not reported"}}
    assert_no_secrets({"authoring": direct_marker})
    assert _package(authoring=direct_marker).manifest.authoring == direct_marker
