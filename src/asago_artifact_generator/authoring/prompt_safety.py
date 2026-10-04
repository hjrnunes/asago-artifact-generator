"""Pre-dispatch prompt checks for secrets, data URLs, and duplicated chunks."""

from __future__ import annotations

import base64
import json
from collections.abc import Collection
from typing import Any
from urllib.parse import urlsplit

from ..input_adapter import InputView
from ..metadata_policy import prompt_secret_metadata_paths, secret_metadata_paths
from .core import (
    _PROMPT_TOKEN_RE,
    _PROMPT_URL_RE,
    AuthoringError,
    Finding,
    ParsedCall2Response,
    PromptPacket,
    PromptPreflightError,
    _canonical_json,
)


def scan_for_secrets(value: Any, path: str = "") -> list[str]:
    """Return secret-bearing metadata paths without inspecting secret values."""

    return secret_metadata_paths(value, path)


def scan_for_prompt_secrets(
    value: Any,
    path: str = "",
    *,
    allowed_urls: Collection[str] = (),
) -> list[str]:
    """Return secret-bearing paths from a model-facing prompt view.

    ``allowed_urls`` lists URLs with scenario or candidate provenance (see
    :func:`prompt_data_urls`); every other URL in the rendered text is flagged.
    """

    if isinstance(value, PromptPacket):
        paths = prompt_secret_metadata_paths(value.payload, "payload")
        paths.extend(_prompt_secret_text_paths(value, allowed_urls=allowed_urls))
        return paths
    return prompt_secret_metadata_paths(value, path)


def prompt_data_urls(*values: Any) -> frozenset[str]:
    """Return the URLs that supplied scenario data or model-authored candidates contain.

    A scenario can carry a URL as attack content, and an author can write one
    into a stimulus; neither is an endpoint or credential. Prompts render these
    strings inside JSON, so each string contributes its raw and JSON-escaped
    matches.
    """

    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, InputView):
            visit(item.payload)
            visit(item.owner_scope)
            visit(item.gherkin_text)
        elif isinstance(item, ParsedCall2Response):
            visit(item.metadata)
            visit(item.python_bytes)
        elif isinstance(item, Finding):
            visit(item.to_dict())
        elif isinstance(item, bytes):
            visit(item.decode("utf-8", errors="replace"))
        elif isinstance(item, str):
            found.update(_PROMPT_URL_RE.findall(item))
            found.update(_PROMPT_URL_RE.findall(json.dumps(item, ensure_ascii=False)[1:-1]))
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    for value in values:
        visit(value)
    return frozenset(found)


def assert_no_secrets(value: Any) -> None:
    paths = scan_for_secrets(value)
    if paths:
        raise AuthoringError(f"secret-bearing authoring evidence: {', '.join(paths)}")


def assert_no_prompt_secrets(value: Any, *, allowed_urls: Collection[str] = ()) -> None:
    paths = scan_for_prompt_secrets(value, allowed_urls=allowed_urls)
    if paths:
        raise PromptPreflightError(f"secret-bearing authoring evidence: {', '.join(paths)}")


def _duplicate_search_forms(value: str) -> list[tuple[str, str]]:
    forms = [("raw", value)]
    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    if escaped != value:
        forms.append(("json-escaped", escaped))
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    if encoded != value:
        forms.append(("base64", encoded))
    return forms


def scan_prompt_duplicates(packet: PromptPacket) -> list[str]:
    """Find repeated copies of bounded candidate chunks in one rendered prompt.

    This intentionally scans only candidate-bearing fields.  Repeated ordinary
    words in an instruction or schema are not duplicate candidate forms.
    """

    if not isinstance(packet, PromptPacket):
        raise TypeError("duplicate scans require a PromptPacket")
    findings: list[str] = []
    for label, value in _duplicate_prompt_chunks(packet.payload):
        if not isinstance(value, str) or len(value.strip()) < 8:
            continue
        for form_name, form in _duplicate_search_forms(value):
            if len(form.strip()) < 8:
                continue
            occurrences = packet.user.count(form)
            if occurrences > 1:
                findings.append(
                    f"{label} {form_name} form appears {occurrences} times "
                    "in rendered user context"
                )
    return findings


def assert_no_prompt_duplicates(packet: PromptPacket) -> None:
    """Fail closed when a bounded candidate chunk is rendered more than once."""

    findings = scan_prompt_duplicates(packet)
    if findings:
        raise PromptPreflightError("duplicate prompt candidate forms: " + "; ".join(findings))


def _duplicate_prompt_chunks(payload: dict[str, Any]) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    candidate_keys = {
        "candidate",
        "candidate_plan",
        "candidate_metadata",
        "candidate_python_source",
        "current_output",
        "failed_response",
        "accepted_plan",
    }
    for key, value in payload.items():
        if key not in candidate_keys and "candidate" not in key and "current_output" not in key:
            continue
        if isinstance(value, str):
            chunks.append((key, value))
            continue
        if isinstance(value, (dict, list)):
            chunks.append((key, _canonical_json(value)))
            chunks.append(
                (
                    f"{key}.pretty",
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                )
            )
    return chunks


def _prompt_secret_text_paths(
    packet: PromptPacket,
    *,
    allowed_urls: Collection[str] = (),
) -> list[str]:
    """Detect unprovenanced URLs and credential values without returning their contents."""

    allowed = frozenset(allowed_urls)
    paths: list[str] = []
    for name, text in (("system", packet.system), ("user", packet.user)):
        if any(url not in allowed for url in _PROMPT_URL_RE.findall(text)):
            paths.append(f"prompt.{name}.url")
        if _PROMPT_TOKEN_RE.search(text):
            paths.append(f"prompt.{name}.credential")
    return paths


def _endpoint_identity(base_url: str) -> tuple[str, str]:
    """Return the lowercase network location and hostname of a configured endpoint."""

    try:
        parts = urlsplit(base_url)
        hostname = parts.hostname or ""
    except ValueError:
        return "", ""
    netloc = parts.netloc.rpartition("@")[2].lower()
    return netloc, hostname.lower()


def _endpoint_prompt_paths(packet: PromptPacket, netloc: str, hostname: str) -> list[str]:
    """Flag prompt text that names the configured endpoint's host."""

    paths: list[str] = []
    for name, text in (("system", packet.system), ("user", packet.user)):
        lowered = text.lower()
        named = bool(netloc) and netloc in lowered
        if not named and hostname:
            for url in _PROMPT_URL_RE.findall(text):
                try:
                    url_host = urlsplit(url).hostname
                except ValueError:
                    continue
                if url_host is not None and url_host.lower() == hostname:
                    named = True
                    break
        if named:
            paths.append(f"prompt.{name}.endpoint")
    return paths
