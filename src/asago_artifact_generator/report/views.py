"""HTML pieces of the author report: findings, request timelines, and package views."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from ..authoring.review import ARTIFACT_REVIEW_QUESTION_IDS, PLAN_REVIEW_QUESTION_IDS
from ..authoring.templates import prose
from ..report_kit import Markup, Turn, artifact, badge, chip, compare, conversation, esc
from .analysis import is_absent
from .model import EVIDENCE_SUFFIX, Authoring, Finding, Package, Scenario, Step

STAGE_LABEL = {"plan": "Plan", "artifact": "Artifact"}
STAGE_TONE = {"plan": "green", "artifact": "amber"}
KIND_LABEL = {"adversarial": "Attack", "functional": "Everyday check"}
KIND_TONE = {"adversarial": "violet", "functional": "blue"}
FINDING_CLIP = 260
ABSENT_TEXT = "(not in this candidate)"


def _question_texts() -> dict[str, str]:
    texts = {key: prose(f"review/plan_questions/{key}") for key in PLAN_REVIEW_QUESTION_IDS}
    texts.update(
        {key: prose(f"review/artifact_questions/{key}") for key in ARTIFACT_REVIEW_QUESTION_IDS}
    )
    return {key: " ".join(text.split()) for key, text in texts.items()}


REVIEW_QUESTIONS = _question_texts()


class Links:
    """Links from the report directory to run files, shown only when the file exists."""

    def __init__(self, out: Path, output: Path) -> None:
        self.out = out
        self.output = output

    def relative(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.out)).as_posix()

    def file(self, path: Path, label: str) -> Markup:
        return artifact(self.relative(path), self.out, label)


def stage_chip(group: str) -> Markup:
    return chip(STAGE_LABEL.get(group, group), STAGE_TONE.get(group))


def kind_chip(kind: str) -> Markup:
    return chip(KIND_LABEL.get(kind, kind), KIND_TONE.get(kind, "outline"))


def _clip(text: str, limit: int = FINDING_CLIP) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def finding_head(finding: Finding) -> Markup:
    tone = "indigo" if finding.source == "review" else "outline"
    location = f" <code>{esc(finding.location)}</code>" if finding.location else ""
    question = REVIEW_QUESTIONS.get(finding.key, "") if finding.source == "review" else ""
    text = f'<p class="q">{esc(question)}</p>' if question else ""
    return Markup(f'<p class="fh">{chip(finding.key, tone)}{location}</p>{text}')


def compare_panel(finding: Finding) -> Markup:
    """Show the field a last finding names, before and after the revision."""

    shown = finding.compare
    if shown is None:
        return Markup("")
    word = "changed" if shown.changed else "unchanged"
    verdict = badge("info" if shown.changed else "warn", word)
    asked = "".join(
        f"<p><b>Request {shown.before_step} asked</b>"
        + (f" (at <code>{esc(e.location)}</code>)" if e.location != finding.location else "")
        + f": {esc(e.required_change or e.problem)}</p>"
        for e in shown.earlier
    ) or (
        f"<p>Request {shown.before_step} raised no finding under this question; "
        "this one is new.</p>"
    )
    sides = compare(
        ABSENT_TEXT if is_absent(shown.before) else shown.before,
        ABSENT_TEXT if is_absent(shown.after) else shown.after,
        f"Before, as request {shown.before_step} reviewed it",
        f"After, as request {shown.after_step} reviewed it",
    )
    return Markup(
        f'<div class="before-after" data-verdict="{word}"><p><b><code>{esc(shown.path)}</code> '
        "before and after the "
        f"revision</b> {verdict}</p>{asked}{sides}</div>"
    )


def finding_html(finding: Finding, *, full: bool = False) -> Markup:
    """Render one finding; *full* adds the required change's context and the comparison."""

    body = esc(finding.problem if full else _clip(finding.problem))
    parts = [f"<p>{body}</p>"]
    if finding.required_change:
        parts.append(f"<p><b>Required change:</b> {esc(finding.required_change)}</p>")
    if full:
        parts.append(compare_panel(finding))
        if finding.basis:
            parts.append(f"<p><b>Reviewer's basis:</b> {esc(finding.basis)}</p>")
    return Markup(f'<div class="fnd">{finding_head(finding)}{"".join(parts)}</div>')


def _allowance(step: Step, used: Counter[tuple[str, str]], policy: dict[str, Any]) -> str:
    if step.kind not in ("correct", "revise"):
        return ""
    used[(step.group, step.kind)] += 1
    name = "correction" if step.kind == "correct" else "revision"
    field = "corrections" if step.kind == "correct" else "review_revisions"
    limit = policy.get(f"{step.group}_max_{field}", "?")
    return str(chip(f"{name} {used[(step.group, step.kind)]} of {limit}", "outline"))


def request_links(item: Authoring, links: Links) -> list[Markup]:
    """Link each request to its record in the scenario's failure evidence, in request order.

    The record at ``attempts[i]`` holds the request's exact prompt and raw reply.
    """

    evidence = links.output / f"{item.id}{EVIDENCE_SUFFIX}"
    return [links.file(evidence, f"attempts[{index}]") for index in range(len(item.steps))]


def timeline(item: Authoring, links: Links) -> Markup:
    """Render every request of a scenario in order."""

    used: Counter[tuple[str, str]] = Counter()
    rows = []
    for step, request in zip(item.steps, request_links(item, links), strict=True):
        flag = badge("warn", "same output as before") if step.unchanged else ""
        found = "".join(finding_html(finding) for finding in step.findings)
        rows.append(
            f'<li class="st-{step.outcome}"><p class="tl-h">'
            f'<span class="tl-n">{step.index}</span> '
            f"{stage_chip(step.group)} <b>{esc(step.title)}</b> "
            f"{_allowance(step, used, item.policy)}{flag}"
            f'<span class="tl-t">{request} {step.tokens:,} tokens</span>'
            f"</p>{found}</li>"
        )
    return Markup(f'<ol class="tl">{"".join(rows)}</ol>')


def shape_sentence(scenario: Scenario) -> str:
    """Describe the producer's attack shape in one sentence."""

    shape = scenario.attack_shape
    if scenario.kind != "adversarial" or not shape:
        return "Everyday check: one ordinary request, no planted content."
    purposes = ", ".join(
        str(turn.get("purpose", "")).replace("_", " ") for turn in shape.get("turn_plan") or []
    )
    turns = shape.get("turn_count")
    if shape.get("channel") == "indirect":
        indirect = shape.get("indirect") or {}
        item = indirect.get("data_item") or {}
        content = str(item.get("content_kind") or "content").replace("_", " ")
        record = item.get("record_ref") or "record not named"
        return (
            f"Indirect attack: planted {content} read through "
            f"{indirect.get('carrier_operation')} ({record}); {turns} user turn(s): {purposes}."
        )
    return f"Direct attack: {turns} user turn(s): {purposes}."


def claim_of(package: Package) -> str:
    level = (package.plan.get("observation_claim") or {}).get("claim_level")
    return {"command_attempt": "tool call", "reply": "reply"}.get(level, str(level or "unknown"))


def oracle_of(package: Package) -> str:
    if package.judge:
        return "judge model"
    return "tool-call condition" if package.condition else "none"


def delivery_of(package: Package) -> str:
    planted = package.stimulus.get("delivery") == "planted_item"
    return "planted item" if planted else "direct message"


def turns_of(package: Package) -> int:
    return len(package.stimulus.get("history") or []) + 1


def package_conversation(package: Package) -> Markup:
    """Show what the test sends: planted content, earlier turns, then the last message."""

    turns = [
        Turn("planted", str(item.get("content", "")))
        for item in (package.seed or {}).get("items") or []
    ]
    turns += [
        Turn("user", str(message.get("content", "")))
        for message in package.stimulus.get("history") or []
    ]
    turns.append(Turn("user", str(package.stimulus.get("user_text", ""))))
    return conversation(turns)


def _comparison_text(comparison: dict[str, Any]) -> str:
    kind = comparison.get("kind")
    operation = comparison.get("operation")
    if kind == "not_called":
        return f"never calls {operation}"
    if kind == "called":
        return f"calls {operation}"
    left, right = comparison.get("left") or {}, comparison.get("right") or {}
    if kind == "value" and right.get("source") == "literal":
        return (
            f"calls {left.get('operation')} with {left.get('argument')} "
            f"{comparison.get('op')} {json.dumps(right.get('value'))}"
        )
    rest = {k: v for k, v in comparison.items() if k not in ("kind", "operation")}
    return f"{kind} on {operation}: {json.dumps(rest, sort_keys=True)}"


def oracle_sentence(package: Package) -> str:
    """State how the test decides, in one sentence."""

    if package.judge:
        return (
            f"A judge model answers: {package.judge.get('question')} "
            f"Unsafe if: {package.judge.get('criteria')}"
        )
    comparisons = (package.condition or {}).get("comparisons") or []
    if not comparisons:
        return "The package has no oracle."
    parts = [_comparison_text(item) for item in comparisons]
    return "Unsafe if the assistant " + " and ".join(parts) + "."
