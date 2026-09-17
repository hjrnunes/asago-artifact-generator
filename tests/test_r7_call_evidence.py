"""R7 call-evidence regressions for the consumer provider seam."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from asago_artifact_generator.design.authoring import (
    LLMArtifactAuthor,
    _attach_author_evidence,
)
from asago_artifact_generator.llm import (
    LLMJsonParseError,
    fix_json,
    llm_json_with_evidence,
)


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


def test_fix_json_returns_cleaned_json_text() -> None:
    assert json.loads(fix_json('```json\n{"value": 1,}\n```')) == {"value": 1}


def test_non_object_answer_is_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "[]"
    response = _provider_response(raw)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    monkeypatch.setattr("asago_artifact_generator.llm.get_client", lambda: fake_client)

    with pytest.raises(LLMJsonParseError) as raised:
        llm_json_with_evidence("user", "system")

    assert raised.value.evidence["raw_response"] == raw
    assert raised.value.evidence["failure_class"] == "answered_schema_failure"
    assert raised.value.evidence["parse_error"] == ("the model response JSON was not an object")


def test_llm_author_success_keeps_returned_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = {"raw_response": '{"value": 1}'}

    monkeypatch.setattr(
        "asago_artifact_generator.llm.llm_json_with_evidence",
        lambda *_, **__: ({"value": 1}, evidence),
    )
    author = LLMArtifactAuthor()

    result = author.author({"scenario_id": "SCN-007"})

    assert result == {"value": 1}
    assert author.last_evidence is evidence


def test_llm_author_parse_error_keeps_parse_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = {"raw_response": "not-json", "failure_class": "answered_malformed"}

    def raise_parse_error(*_: Any, **__: Any) -> Any:
        raise LLMJsonParseError("the model response was not valid JSON", evidence=evidence)

    monkeypatch.setattr(
        "asago_artifact_generator.llm.llm_json_with_evidence",
        raise_parse_error,
    )
    author = LLMArtifactAuthor()

    with pytest.raises(LLMJsonParseError):
        author.author({"scenario_id": "SCN-007"})

    assert author.last_evidence is evidence


def test_llm_author_provider_failure_classifies_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_provider_failure(*_: Any, **__: Any) -> Any:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "asago_artifact_generator.llm.llm_json_with_evidence",
        raise_provider_failure,
    )
    author = LLMArtifactAuthor()

    with pytest.raises(RuntimeError):
        author.author({"scenario_id": "SCN-007"})

    assert author.last_evidence is not None
    assert author.last_evidence["failure_class"] == "provider_failure"
    assert author.last_evidence["call_error"] == "RuntimeError"


def test_attach_author_evidence_copies_transformations_and_sanitizes_errors() -> None:
    author = SimpleNamespace(
        last_evidence={
            "cleaned_response": {"value": 1},
            "usage": {"status": "unavailable", "prompt_tokens": None},
            "failure_class": "answered_malformed",
            "content_pins": {"raw_response": "pin-value"},
            "deterministic_transformations": [{"name": "plain_json_decode"}],
            "parse_error": "POST https://private.example/v1 exploded",
            "call_error": "token=sk-secret-value",
        }
    )
    attempt: dict[str, Any] = {
        "content_pins": {},
        "deterministic_transformations": [],
    }

    _attach_author_evidence(attempt, author)

    assert attempt["deterministic_transformations"] == [{"name": "plain_json_decode"}]
    assert attempt["cleaned_response"] == {"value": 1}
    assert attempt["failure_class"] == "answered_malformed"
    assert attempt["content_pins"] == {"raw_response": "pin-value"}
    assert "private.example" not in attempt["parse_error"]
    assert "sk-secret-value" not in attempt["call_error"]
