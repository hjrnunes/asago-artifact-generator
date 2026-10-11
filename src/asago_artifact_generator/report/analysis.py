"""Causes, signals, and before-and-after comparisons of failed scenarios.

Every rule compares codes, question ids, locations, statuses, and digests from
the failure evidence, never finding text.
"""

from __future__ import annotations

import re
from typing import Any

from .model import Authoring, Compare, Step

# Cause id -> (label, note). The order is the report's order.
CAUSES: dict[str, tuple[str, str]] = {
    "review_not_satisfied": (
        "Reviewer still asked for changes",
        "after the revisions the stage allows",
    ),
    "review_unusable": (
        "Review response unusable",
        "the reviewer's reply could not be read, so no decision exists",
    ),
    "checks_not_satisfied": (
        "Code checks still failed",
        "after the corrections the stage allows",
    ),
    "blocked": (
        "Declared untestable",
        "the author or a reviewer found the scenario cannot be tested as planned",
    ),
    "stopped": (
        "Stopped by a request limit or error",
        "a transport error, a budget, a prompt size limit, or a crash ended the run",
    ),
}
CAUSE_ORDER = tuple(CAUSES)
_BLOCKED = frozenset({"blocked", "needs_plan_revision"})
_STOPPED = frozenset({"transport_failure", "budget_exhausted", "prompt_overflow"})
_STOPPED_REASONS = frozenset({"authoring_crashed", "package_write_failed"})

SAME_FINDING = "same finding twice"
SAME_QUESTION = "same question twice"
UNCHANGED = "revision changed nothing"
SAME_CHECK = "same check failed twice"
SIGNALS = (SAME_FINDING, SAME_QUESTION, UNCHANGED, SAME_CHECK)

_PREFIX = {"plan": "candidate_plan", "artifact": "candidate_artifact"}
_ABSENT = object()


def cause_of(item: Authoring) -> str | None:
    """Return the cause id of a failed scenario, or None when it was packaged."""

    if item.accepted:
        return None
    if item.status == "review_unavailable":
        return "review_unusable"
    if item.status in _BLOCKED:
        return "blocked"
    if item.status in _STOPPED or item.terminal_reason in _STOPPED_REASONS:
        return "stopped"
    last = item.steps[-1] if item.steps else None
    if last is not None and last.kind == "review" and last.outcome == "warn":
        return "review_not_satisfied"
    return "checks_not_satisfied"


def _review_signal(reviews: list[Step]) -> str | None:
    if len(reviews) < 2:
        return None
    before = {(f.key, f.location) for f in reviews[-2].findings}
    after = {(f.key, f.location) for f in reviews[-1].findings}
    if before & after:
        return SAME_FINDING
    if {key for key, _ in before} & {key for key, _ in after}:
        return SAME_QUESTION
    return None


def _check_signal(checks: list[Step]) -> str | None:
    if len(checks) < 2:
        return None
    if {f.key for f in checks[-2].findings} & {f.key for f in checks[-1].findings}:
        return SAME_CHECK
    return None


def signals_of(item: Authoring) -> list[str]:
    """Return the signals of a failed scenario in ``SIGNALS`` order."""

    if item.accepted:
        return []
    stage = [step for step in item.steps if step.group == item.terminal_stage]
    reviews = [step for step in stage if step.kind == "review" and step.findings]
    authored = [step for step in stage if step.kind != "review"]
    found = {
        _review_signal(reviews),
        UNCHANGED if any(step.unchanged for step in item.steps) else None,
        _check_signal(authored),
    }
    return [signal for signal in SIGNALS if signal in found]


_PART = re.compile(r"\.(\w+)|\[(\d+)\]")


def field_path(location: str, group: str) -> list[str | int] | None:
    """Split ``candidate_plan.a.b[0]`` into keys; None unless it names this stage's candidate."""

    prefix = _PREFIX[group]
    text = location.strip()
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :]
    parts = list(_PART.finditer(rest))
    if "".join(match.group(0) for match in parts) != rest:
        return None
    return [int(index) if index else str(key) for key, index in (m.groups() for m in parts)]


def lookup(value: Any, path: list[str | int]) -> Any:
    """Return the value at *path*, or ``_ABSENT`` when a step is missing."""

    for key in path:
        container = list if isinstance(key, int) else dict
        if not isinstance(value, container):
            return _ABSENT
        try:
            value = value[key]  # type: ignore[index]
        except (IndexError, KeyError):
            return _ABSENT
    return value


def is_absent(value: Any) -> bool:
    return value is _ABSENT


def _comparison(last: Step, previous: Step, finding: Any) -> Compare | None:
    path = field_path(finding.location, last.group) if finding.source == "review" else None
    if path is None:
        return None
    before, after = lookup(previous.reviewed, path), lookup(last.reviewed, path)
    if before is _ABSENT and after is _ABSENT:
        return None
    shown = finding.location.strip()[len(_PREFIX[last.group]) :].lstrip(".")
    earlier = [f for f in previous.findings if f.key == finding.key]
    return Compare(shown, before, after, previous.index, last.index, earlier)


def attach_comparisons(item: Authoring) -> None:
    """Give each last review finding the field it names, before and after the revision.

    Both values come from reviewed candidates whose digests match; the verdict
    is equality of their canonical JSON only.
    """

    if item.accepted or not item.steps:
        return
    last = item.steps[-1]
    if last.kind != "review" or last.reviewed is None:
        return
    earlier = [
        step
        for step in item.steps[:-1]
        if step.kind == "review" and step.group == last.group and step.reviewed is not None
    ]
    if not earlier:
        return
    for finding in last.findings:
        finding.compare = _comparison(last, earlier[-1], finding)


def analyse(items: list[Authoring]) -> None:
    """Set each item's cause, signals, and before-and-after comparisons."""

    for item in items:
        item.cause = cause_of(item)
        item.signals = signals_of(item)
        attach_comparisons(item)


def asked_again(item: Authoring) -> bool:
    """Whether a last finding repeats a previous question although its field changed."""

    return any(
        finding.compare is not None and finding.compare.changed and finding.compare.earlier
        for finding in item.last_findings
    )
