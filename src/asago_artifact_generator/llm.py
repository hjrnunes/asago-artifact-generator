"""Shared OpenAI-compatible client and JSON helpers.

Providers (via ``REDTEAM_PROVIDER`` or auto-detect):

- ``ollama`` — local OpenAI-compatible server (default)
- ``gemini`` — Google Gemini OpenAI-compatible API (``GEMINI_API_KEY`` from ``.env``)
- ``openai`` — OpenAI Chat Completions (``OPENAI_API_KEY``)
- ``huggingface`` — Hugging Face router (``HF_TOKEN`` / ``OPENAI_API_KEY``)
- ``openrouter`` — OpenRouter (``OPENROUTER_API_KEY`` / ``OPENAI_API_KEY``)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from .models._base import compute_framed_digest

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_HF_BASE_URL = "https://router.huggingface.co/v1"
DEFAULT_HF_MODEL = "meta-llama/Meta-Llama-3-8B-Instruct"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-3.5-sonnet"

_OPENAI_COMPAT_PRESETS = {
    "openai": (DEFAULT_OPENAI_BASE_URL, DEFAULT_OPENAI_MODEL),
    "huggingface": (DEFAULT_HF_BASE_URL, DEFAULT_HF_MODEL),
    "openrouter": (DEFAULT_OPENROUTER_BASE_URL, DEFAULT_OPENROUTER_MODEL),
}

PROVIDER = "ollama"
OLLAMA_BASE_URL = DEFAULT_OLLAMA_BASE_URL
BASE_URL = DEFAULT_OLLAMA_BASE_URL
MODEL = DEFAULT_OLLAMA_MODEL
_client: OpenAI | None = None
AUTHOR_CALL_EVIDENCE_SCHEMA_VERSION = "artifact-author-call-evidence-v1"


class LLMJsonParseError(ValueError):
    """A provider response could not be decoded as JSON.

    The exception carries the credential-free call evidence so the design
    boundary can preserve the raw response even when parsing fails.
    """

    def __init__(self, message: str, *, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _load_dotenv() -> None:
    """Load repo-root and cwd ``.env`` without overriding existing env vars."""
    paths = (_REPO_ROOT / ".env", Path.cwd() / ".env")
    try:
        from dotenv import load_dotenv

        for path in paths:
            load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


_load_dotenv()


def _gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _looks_like_ollama_model(model: str) -> bool:
    return ":" in model


def _resolve_provider(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get("REDTEAM_PROVIDER") or "").strip().lower()
    if raw:
        return raw
    if _gemini_api_key():
        return "gemini"
    return "ollama"


def _apply_provider(provider: str) -> None:
    global PROVIDER, OLLAMA_BASE_URL, BASE_URL, MODEL
    PROVIDER = provider
    explicit_model = os.environ.get("REDTEAM_MODEL")
    if provider == "gemini":
        BASE_URL = os.environ.get("GEMINI_BASE_URL", GEMINI_OPENAI_BASE_URL).rstrip("/")
        if explicit_model and not _looks_like_ollama_model(explicit_model):
            MODEL = explicit_model
        else:
            MODEL = DEFAULT_GEMINI_MODEL
    elif provider in _OPENAI_COMPAT_PRESETS:
        default_url, default_model = _OPENAI_COMPAT_PRESETS[provider]
        env_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("HF_BASE_URL")
        BASE_URL = (env_url or default_url).rstrip("/")
        MODEL = explicit_model or default_model
    else:
        BASE_URL = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
        MODEL = explicit_model or DEFAULT_OLLAMA_MODEL
    OLLAMA_BASE_URL = BASE_URL


_apply_provider(_resolve_provider())


def configure_llm(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    global OLLAMA_BASE_URL, BASE_URL, MODEL, _client
    if provider is not None:
        os.environ["REDTEAM_PROVIDER"] = provider
        _apply_provider(_resolve_provider(provider))
    if base_url is not None:
        BASE_URL = base_url.rstrip("/")
        if PROVIDER == "ollama":
            OLLAMA_BASE_URL = BASE_URL
            os.environ["OLLAMA_BASE_URL"] = BASE_URL
        elif PROVIDER == "gemini":
            os.environ["GEMINI_BASE_URL"] = BASE_URL
        else:
            os.environ["OPENAI_BASE_URL"] = BASE_URL
    if api_key is not None:
        if PROVIDER == "gemini":
            os.environ["GEMINI_API_KEY"] = api_key
        elif PROVIDER == "huggingface":
            os.environ["HF_TOKEN"] = api_key
            os.environ["OPENAI_API_KEY"] = api_key
        elif PROVIDER == "openrouter":
            os.environ["OPENROUTER_API_KEY"] = api_key
            os.environ["OPENAI_API_KEY"] = api_key
        else:
            os.environ["OPENAI_API_KEY"] = api_key
    if model is not None:
        MODEL = model
        os.environ["REDTEAM_MODEL"] = model
    _client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if PROVIDER == "gemini":
            api_key = _gemini_api_key()
            if not api_key:
                raise RuntimeError(
                    "Gemini selected but no API key found. "
                    "Set GEMINI_API_KEY in .env (see .env.example)."
                )
        elif PROVIDER == "huggingface":
            api_key = os.environ.get("HF_TOKEN") or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("Hugging Face selected but no HF_TOKEN / OPENAI_API_KEY found.")
        elif PROVIDER == "openrouter":
            api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OpenRouter selected but no OPENROUTER_API_KEY / OPENAI_API_KEY found."
                )
        elif PROVIDER == "openai":
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OpenAI selected but no OPENAI_API_KEY found.")
        else:
            api_key = os.environ.get("OPENAI_API_KEY") or "ollama"
        _client = OpenAI(base_url=BASE_URL, api_key=api_key)
        log.info(
            "LLM provider=%s model=%s endpoint_configured=%s",
            PROVIDER,
            MODEL,
            bool(BASE_URL),
        )
    return _client


def _content_pin(value: Any, frame: str) -> str:
    """Pin one evidence value without exposing connection metadata."""
    return compute_framed_digest(frame, value)


def _transformation(
    name: str,
    input_value: Any,
    output_value: Any,
    *,
    detail: str,
) -> dict[str, Any]:
    """Build one ordered cleanup record with before/after pins."""
    return {
        "name": name,
        "status": "applied",
        "input_pin": _content_pin(input_value, "artifact-author-cleanup-input-v1"),
        "output_pin": _content_pin(output_value, "artifact-author-cleanup-output-v1"),
        "detail": detail,
    }


def _apply_fence_removal(
    current: str,
    transformations: list[dict[str, Any]],
) -> str:
    """Remove one exact outer Markdown fence when the text is fenced."""
    lines = current.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        fenced = "\n".join(lines[1:-1])
        transformations.append(
            _transformation(
                "markdown_fence_removal",
                current,
                fenced,
                detail="removed one exact outer Markdown fence",
            )
        )
        return fenced
    return current


def _apply_trailing_comma_repair(
    current: str,
    transformations: list[dict[str, Any]],
) -> str:
    """Remove commas immediately before object/array closure."""
    repaired = re.sub(r",\s*([}\]])", r"\1", current)
    if repaired != current:
        transformations.append(
            _transformation(
                "trailing_comma_repair",
                current,
                repaired,
                detail="removed commas immediately before object/array closure",
            )
        )
    return repaired


def _apply_invalid_escape_repair(
    current: str,
    transformations: list[dict[str, Any]],
    initial_error: json.JSONDecodeError,
) -> str:
    """Escape invalid JSON backslashes; unrecoverable text re-raises."""
    escaped = re.sub(r"\\(?![\"\\\/bfnrtu])", r"\\\\", current)
    if escaped == current:
        raise initial_error from None
    transformations.append(
        _transformation(
            "invalid_escape_repair",
            current,
            escaped,
            detail="escaped invalid JSON backslashes",
        )
    )
    return escaped


def _decode_json_with_evidence(
    raw_text: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """Decode provider JSON and retain only cleanups that actually applied."""
    current = raw_text.strip()
    transformations: list[dict[str, Any]] = []
    try:
        parsed = json.loads(current)
    except json.JSONDecodeError as initial_error:
        current = _apply_fence_removal(current, transformations)
        try:
            parsed = json.loads(current)
        except json.JSONDecodeError:
            current = _apply_trailing_comma_repair(current, transformations)
            try:
                parsed = json.loads(current)
            except json.JSONDecodeError:
                current = _apply_invalid_escape_repair(current, transformations, initial_error)
                parsed = json.loads(current)
    transformations.append(
        _transformation(
            "plain_json_decode",
            current,
            parsed,
            detail="decoded the cleaned JSON text",
        )
    )
    return parsed, transformations


def fix_json(text: str) -> str:
    """Return cleaned JSON text using the evidence-producing decoder."""
    parsed, _transformations = _decode_json_with_evidence(text)
    return json.dumps(parsed, ensure_ascii=False)


def _author_call_evidence(
    *,
    prompt: str,
    system: str,
    raw_response: Any | None,
    temperature: float,
    usage: Any | None = None,
) -> dict[str, Any]:
    """Build inspectable author-call evidence without connection secrets."""

    rendered_prompt = {"system": system, "user": prompt}
    controls = {
        "provider": PROVIDER,
        "model": MODEL,
        "temperature": temperature,
        "response_parser": "json",
        "credentials_recorded": False,
    }
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage is not None else None
    usage_record = {
        "status": (
            "reported"
            if prompt_tokens is not None and completion_tokens is not None
            else "unavailable"
        ),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    return {
        "schema_version": AUTHOR_CALL_EVIDENCE_SCHEMA_VERSION,
        "rendered_prompt": rendered_prompt,
        "raw_response": raw_response,
        "model_controls": controls,
        "usage": usage_record,
        "content_pins": {
            "rendered_prompt": compute_framed_digest(
                "artifact-author-rendered-prompt-v1", rendered_prompt
            ),
            "raw_response": compute_framed_digest("artifact-author-raw-response-v1", raw_response),
        },
    }


def llm_json_with_evidence(
    prompt: str,
    system: str,
    *,
    temperature: float = 0.2,
) -> tuple[dict, dict[str, Any]]:
    """Call the provider and return parsed JSON plus inspectable evidence.

    Evidence contains the exact rendered system/user prompts and raw provider
    text before parsing. Controls intentionally exclude base URLs, API keys,
    tokens, and other connection material.
    """

    response = get_client().chat.completions.create(
        model=MODEL,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.choices[0].message.content
    evidence = _author_call_evidence(
        prompt=prompt,
        system=system,
        raw_response=raw,
        temperature=temperature,
        usage=getattr(response, "usage", None),
    )
    try:
        parsed, transformations = _decode_json_with_evidence(raw if isinstance(raw, str) else "")
        evidence["deterministic_transformations"] = transformations
        evidence["cleaned_response"] = parsed
        evidence["content_pins"]["cleaned_response"] = _content_pin(
            parsed, "artifact-author-cleaned-response-v1"
        )
    except (TypeError, json.JSONDecodeError) as exc:
        evidence["parse_error"] = str(exc)
        evidence["failure_class"] = "answered_malformed"
        raise LLMJsonParseError(
            "the model response was not valid JSON",
            evidence=evidence,
        ) from exc
    if not isinstance(parsed, dict):
        evidence["parse_error"] = "the model response JSON was not an object"
        evidence["failure_class"] = "answered_schema_failure"
        raise LLMJsonParseError(
            "the model response JSON was not an object",
            evidence=evidence,
        )
    evidence["content_pins"]["parsed_response"] = compute_framed_digest(
        "artifact-author-parsed-response-v1", parsed
    )
    return parsed, evidence


def llm_json(prompt: str, system: str, *, temperature: float = 0.2) -> dict:
    """Call the provider and return only parsed JSON for legacy callers."""

    parsed, _ = llm_json_with_evidence(prompt, system, temperature=temperature)
    return parsed
