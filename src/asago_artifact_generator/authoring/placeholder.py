"""Reject a stimulus that still carries a bracketed placeholder.

A model that cannot write a harmful or private message sometimes leaves a
bracketed stand-in such as ``[Obscene/Harmful Content Placeholder]``. The
target then receives the stand-in, not an attack, and the experiment cannot
show the failure. The check matches only a bracketed span that contains the
word, so a message that discusses a placeholder in plain words passes.
"""

from __future__ import annotations

import re
from typing import Any

from .core import Finding

CODE = "placeholder_stimulus"
_PLACEHOLDER = re.compile(r"\[[^\[\]]*\bplaceholder[^\[\]]*\]", re.IGNORECASE)


def placeholder_findings(text: Any, path: str) -> list[Finding]:
    """Return a finding when ``text`` holds a bracketed placeholder."""

    match = _PLACEHOLDER.search(text) if isinstance(text, str) else None
    if match is None:
        return []
    return [
        Finding(
            CODE,
            (
                f"the stimulus contains the placeholder {match.group()!r}; a bracketed "
                "stand-in is not a message, so write the message itself"
            ),
            path,
        )
    ]


def plan_placeholder_findings(approach: dict[str, Any]) -> list[Finding]:
    """Check the plan's stimulus request and each history message."""

    findings = placeholder_findings(approach.get("request"), "stimulus_approach.request")
    history = approach.get("history")
    for index, item in enumerate(history if isinstance(history, list) else []):
        findings.extend(placeholder_findings(item, f"stimulus_approach.history[{index}]"))
    return findings


def artifact_placeholder_findings(stimulus: dict[str, Any]) -> list[Finding]:
    """Check the authored user text and each history message."""

    findings = placeholder_findings(stimulus.get("user_text"), "stimulus.user_text")
    history = stimulus.get("history")
    for index, item in enumerate(history if isinstance(history, list) else []):
        content = item.get("content") if isinstance(item, dict) else None
        findings.extend(placeholder_findings(content, f"stimulus.history[{index}].content"))
    return findings


__all__ = [
    "CODE",
    "artifact_placeholder_findings",
    "placeholder_findings",
    "plan_placeholder_findings",
]
