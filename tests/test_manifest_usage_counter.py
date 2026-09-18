"""Closed manifest usage metadata policy regressions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import AuthoringError, assert_no_secrets
from asago_artifact_generator.package_io import (
    PackageIntegrityError,
    build_package,
    load_package,
)

FRESH_EVIDENCE = (
    Path(__file__).resolve().parents[1]
    / "runs"
    / "authoring"
    / "g07-fresh-20260918"
    / "G07-fresh-20260918.failure-evidence.json"
)


def _package(*, authoring: dict) -> object:
    return build_package(
        package_id="usage-policy",
        scenario_id="G07",
        input_kind="reference-task",
        source_digests={"input": "a" * 64},
        members={"detector.py": b"source\n"},
        authoring=authoring,
    )


def _fresh_usage() -> dict:
    evidence = json.loads(FRESH_EVIDENCE.read_text(encoding="utf-8"))
    return {
        "interface": "artifact-authoring-v1",
        "usage": [attempt["usage"] for attempt in evidence["attempts"]],
    }


def test_exact_captured_provider_usage_metadata_is_allowed() -> None:
    metadata = _fresh_usage()

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


def test_pinned_fresh_responses_replay_to_immutable_package_without_contact() -> None:
    root = FRESH_EVIDENCE.parents[3]
    script = root / "scripts" / "replay_g07_fresh_package.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    package_path = FRESH_EVIDENCE.parent / "G07-fresh-20260918"
    package = load_package(package_path)
    replay = json.loads(
        (FRESH_EVIDENCE.parent / "G07-fresh-20260918.offline-replay.json").read_text()
    )
    assembly = json.loads(
        (FRESH_EVIDENCE.parent / "G07-fresh-20260918.offline-assembly.json").read_text()
    )

    assert replay["outputs_replaced_or_edited"] is False
    assert replay["private_model_client_constructed"] is False
    assert replay["transport_constructed"] is False
    assert set(replay["contacts"].values()) == {0}
    assert [attempt["raw_response_sha256"] for attempt in replay["attempts"]] == [
        "3bbbb50f7ed4396f12ef628ea648e81a8255e250779ad9531d6febec0ea83d10",
        "b9796b620993f46165a7e29ca74fa7156ff00c4cb2a89c998153fb1b1354a636",
    ]
    assert package.manifest.manifest_digest == assembly["package_manifest_digest"]
    assert assembly["manifest_reload"] == "passed"
    assert assembly["secret_scan"] == "passed"
    assert assembly["source_and_input_pins"] == "passed"
    assert assembly["tamper_check"]["status"] == "rejected"
    assert assembly["original_failure_evidence_unchanged"] is True
    assert assembly["original_terminal_record_unchanged"] is True
