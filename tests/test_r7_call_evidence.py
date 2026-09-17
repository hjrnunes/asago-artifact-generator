"""R7 call-evidence regressions for the consumer provider seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from asago_artifact_generator.llm import LLMJsonParseError, llm_json_with_evidence


def _provider_response(raw: str, usage: Any = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))],
        usage=usage,
    )


def test_fenced_trailing_comma_preserves_raw_and_ordered_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '```json\n{"value": 1,}\n```'
    response = _provider_response(
        raw,
        usage=SimpleNamespace(prompt_tokens=13, completion_tokens=5),
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    monkeypatch.setattr("asago_artifact_generator.llm.get_client", lambda: fake_client)

    parsed, evidence = llm_json_with_evidence("user", "system")

    assert parsed == {"value": 1}
    assert evidence["raw_response"] == raw
    assert evidence["cleaned_response"] == parsed
    assert evidence["usage"] == {
        "status": "reported",
        "prompt_tokens": 13,
        "completion_tokens": 5,
    }
    assert [item["name"] for item in evidence["deterministic_transformations"]] == [
        "markdown_fence_removal",
        "trailing_comma_repair",
        "plain_json_decode",
    ]
    assert all(
        item["input_pin"] and item["output_pin"]
        for item in evidence["deterministic_transformations"]
    )


def test_missing_usage_is_explicitly_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"value": 1}'
    response = _provider_response(raw, usage=None)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    monkeypatch.setattr("asago_artifact_generator.llm.get_client", lambda: fake_client)

    parsed, evidence = llm_json_with_evidence("user", "system")

    assert parsed == {"value": 1}
    assert evidence["usage"]["status"] == "unavailable"
    assert evidence["usage"]["prompt_tokens"] is None
    assert evidence["usage"]["completion_tokens"] is None


def test_malformed_answer_retains_raw_and_answered_failure_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "not-json"
    response = _provider_response(
        raw,
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    monkeypatch.setattr("asago_artifact_generator.llm.get_client", lambda: fake_client)

    with pytest.raises(LLMJsonParseError) as raised:
        llm_json_with_evidence("user", "system")

    assert raised.value.evidence["raw_response"] == raw
    assert raised.value.evidence["failure_class"] == "answered_malformed"
    assert raised.value.evidence["usage"]["status"] == "reported"


def test_invalid_escape_repair_is_pinned_when_no_trailing_comma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"value": "bad\\q"}'
    response = _provider_response(raw)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    monkeypatch.setattr("asago_artifact_generator.llm.get_client", lambda: fake_client)

    parsed, evidence = llm_json_with_evidence("user", "system")

    assert parsed == {"value": r"bad\q"}
    assert [item["name"] for item in evidence["deterministic_transformations"]] == [
        "invalid_escape_repair",
        "plain_json_decode",
    ]
