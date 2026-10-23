"""What the author report reads: scenarios, failure evidence, and package members.

The loader turns each ``<SCN>.failure-evidence.json`` into an ``Authoring``
record of ordered ``Step`` values. It keeps codes, question ids, locations,
digests, token counts, and the finding text it shows; the analysis compares
only the first four.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..authoring.core import _canonical_json

EVIDENCE_SUFFIX = ".failure-evidence.json"
STAGE_OF = {
    "call1": "plan",
    "plan_review": "plan",
    "call2": "artifact",
    "artifact_review": "artifact",
}
GROUPS = ("plan", "artifact")


@dataclass(frozen=True)
class Scenario:
    """One scenario the producer handed to authoring."""

    id: str
    kind: str
    criterion: str
    attack_shape: dict[str, Any] | None


@dataclass
class Compare:
    """A field a last review finding names, in the two reviewed candidates of its stage."""

    path: str
    before: Any
    after: Any
    before_step: int
    after_step: int
    earlier: list[Finding]

    @property
    def changed(self) -> bool:
        return _canonical_json(self.before) != _canonical_json(self.after)


@dataclass
class Finding:
    """A code-check finding, a review finding, or a review reply-format finding."""

    source: str  # check | review | format
    key: str  # check code or review question id
    location: str
    problem: str
    required_change: str = ""
    basis: str = ""
    compare: Compare | None = None


@dataclass
class Step:
    """One model request of a scenario's authoring."""

    index: int
    group: str  # plan | artifact
    kind: str  # draft | correct | revise | review
    outcome: str  # kit status
    title: str
    prompt_tokens: int
    completion_tokens: int
    candidate: str
    findings: list[Finding] = field(default_factory=list)
    unchanged: bool = False
    reviewed: Any = None  # the stored reviewed candidate, only when its digest matches
    retries: int = 0  # physical attempts after the first, from the dispatch's transport_retries

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def requests(self) -> int:
        """Physical model requests of this dispatch: the first attempt and each retry."""

        return 1 + self.retries


@dataclass
class Package:
    """The package members the report shows."""

    plan: dict[str, Any]
    stimulus: dict[str, Any]
    examples: dict[str, Any]
    explanation: str
    condition: dict[str, Any] | None
    judge: dict[str, Any] | None
    seed: dict[str, Any] | None


@dataclass
class Authoring:
    """One scenario's authoring as its failure evidence records it."""

    id: str
    scenario: Scenario
    status: str
    terminal_stage: str
    terminal_reason: str
    steps: list[Step]
    policy: dict[str, Any]
    model: str
    package: Package | None = None
    cause: str | None = None
    signals: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def tokens(self) -> int:
        return sum(step.tokens for step in self.steps)

    @property
    def requests(self) -> int:
        return sum(step.requests for step in self.steps)

    @property
    def last_findings(self) -> list[Finding]:
        return self.steps[-1].findings if self.steps else []


@dataclass
class AuthorRun:
    """Everything the author report shows for one author output."""

    output: Path
    scenarios_dir: Path
    scenarios: list[Scenario]
    items: list[Authoring]

    @property
    def missing(self) -> list[Scenario]:
        """Scenarios with no failure evidence: authoring did not run for them."""

        recorded = {item.id for item in self.items}
        return [scenario for scenario in self.scenarios if scenario.id not in recorded]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _optional_json(path: Path) -> Any:
    return _read_json(path) if path.is_file() else None


def load_scenario(path: Path) -> Scenario:
    """Read the fields the report shows from one scenario handoff YAML."""

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    shape = document.get("attack_shape")
    return Scenario(
        id=str(document.get("scenario_id") or path.stem),
        kind=str(document.get("kind") or "unknown"),
        criterion=str(document.get("semantic_failure_criterion") or ""),
        attack_shape=shape if isinstance(shape, dict) else None,
    )


def _tokens(attempt: dict[str, Any]) -> tuple[int, int]:
    value = (attempt.get("usage") or {}).get("value") or {}
    return int(value.get("prompt_tokens") or 0), int(value.get("completion_tokens") or 0)


def _check_findings(items: list[dict[str, Any]] | None, source: str = "check") -> list[Finding]:
    return [
        Finding(
            source, str(item.get("code", "")), str(item.get("path", "")), item.get("detail", "")
        )
        for item in items or []
    ]


def _review_findings(review: dict[str, Any]) -> list[Finding]:
    return [
        Finding(
            "review",
            str(item.get("question", "")),
            str(item.get("location", "")),
            item.get("problem", ""),
            item.get("required_change", ""),
            item.get("basis", ""),
        )
        for item in review.get("findings") or []
    ]


def stored_candidate(review: dict[str, Any]) -> Any:
    """Return the review's stored candidate when it hashes to the recorded digest, else None."""

    if "reviewed_candidate" not in review:
        return None
    candidate = review["reviewed_candidate"]
    digest = hashlib.sha256(_canonical_json(candidate).encode("utf-8")).hexdigest()
    return candidate if digest == review.get("reviewed_candidate_sha256") else None


_DECISIONS = {
    "accept": ("pass", "Reviewer accepts"),
    "revise": ("warn", "Reviewer asks for changes"),
    "blocked": ("fail", "Reviewer blocks"),
}


def _group(attempt: dict[str, Any]) -> str:
    return STAGE_OF.get(attempt["stage"]) or STAGE_OF.get(
        attempt.get("failed_stage") or "", "plan"
    )


def _retries(attempt: dict[str, Any]) -> int:
    return len(attempt.get("transport_retries") or [])


def _review_step(attempt: dict[str, Any], group: str) -> Step:
    review = attempt.get("review") or {}
    prompt, completion = _tokens(attempt)
    digest = str(review.get("reviewed_candidate_sha256") or "")
    decision = review.get("decision")
    if decision not in _DECISIONS:
        findings = _check_findings(attempt.get("findings"), source="format")
        return Step(
            attempt["dispatch_index"],
            group,
            "review",
            "unknown",
            "Reviewer reply unusable",
            prompt,
            completion,
            digest,
            findings,
            retries=_retries(attempt),
        )
    outcome, title = _DECISIONS[decision]
    return Step(
        attempt["dispatch_index"],
        group,
        "review",
        outcome,
        title,
        prompt,
        completion,
        digest,
        _review_findings(review),
        reviewed=stored_candidate(review),
        retries=_retries(attempt),
    )


def _author_kind(attempt: dict[str, Any], group: str) -> tuple[str, str]:
    if attempt["stage"] in ("call1", "call2"):
        return "draft", "Author drafts the plan" if group == "plan" else "Author writes the test"
    if attempt.get("allowance") == "review_revision":
        return "revise", "Author revises after the review"
    return "correct", "Author fixes the check findings"


def to_step(attempt: dict[str, Any]) -> Step:
    """Return one evidence attempt as a timeline step."""

    group = _group(attempt)
    if attempt.get("role") == "reviewer":
        return _review_step(attempt, group)
    kind, title = _author_kind(attempt, group)
    findings = _check_findings(attempt.get("findings"))
    prompt, completion = _tokens(attempt)
    return Step(
        attempt["dispatch_index"],
        group,
        kind,
        "fail" if findings else "pass",
        title + (" · checks failed" if findings else " · checks pass"),
        prompt,
        completion,
        str(attempt.get("candidate_sha256") or ""),
        findings,
        retries=_retries(attempt),
    )


def mark_unchanged(steps: list[Step]) -> None:
    """Flag each correction or revision that returned its stage's previous candidate."""

    previous: dict[str, str] = {}
    for step in steps:
        if step.kind == "review":
            continue
        step.unchanged = (
            step.kind in ("correct", "revise")
            and bool(step.candidate)
            and previous.get(step.group) == step.candidate
        )
        previous[step.group] = step.candidate


def _model(attempts: list[dict[str, Any]]) -> str:
    for attempt in attempts:
        identity = attempt.get("model_identity") or {}
        if identity.get("requested_model"):
            return str(identity["requested_model"])
    return "unknown"


def _terminal_stage(terminal: dict[str, Any], steps: list[Step]) -> str:
    stage = terminal.get("stage")
    if stage in GROUPS:
        return str(stage)
    return steps[-1].group if steps else "plan"


def load_package(directory: Path) -> Package | None:
    """Read the shown members of a package, or None when the directory holds none."""

    if not (directory / "manifest.json").is_file():
        return None
    explanation = _optional_json(directory / "explanation.json") or {}
    return Package(
        plan=_optional_json(directory / "plan.json") or {},
        stimulus=_optional_json(directory / "stimulus.json") or {},
        examples=_optional_json(directory / "examples.json") or {},
        explanation=str(explanation.get("text") or ""),
        condition=_optional_json(directory / "tool_call_condition.json"),
        judge=_optional_json(directory / "judge.json"),
        seed=_optional_json(directory / "seed.json"),
    )


def load_authoring(path: Path, scenarios: dict[str, Scenario]) -> Authoring:
    """Read one failure-evidence file and, when packaged, its package."""

    evidence = _read_json(path)
    task_id = str(evidence["task_id"])
    attempts = list(evidence.get("attempts") or [])
    steps = [to_step(attempt) for attempt in attempts]
    mark_unchanged(steps)
    terminal = evidence.get("terminal") or {}
    scenario = scenarios.get(task_id) or Scenario(task_id, "unknown", "", None)
    status = str(evidence.get("status") or "unknown")
    package = load_package(path.parent / task_id) if status == "accepted" else None
    return Authoring(
        id=task_id,
        scenario=scenario,
        status=status,
        terminal_stage=_terminal_stage(terminal, steps),
        terminal_reason=str(terminal.get("reason") or status),
        steps=steps,
        policy=dict(evidence.get("policy") or {}),
        model=_model(attempts),
        package=package,
    )


def load_run(output: Path, scenarios_dir: Path) -> AuthorRun:
    """Read every scenario in *scenarios_dir* and every evidence file in *output*."""

    scenarios = [load_scenario(path) for path in sorted(scenarios_dir.glob("*.yaml"))]
    by_id = {scenario.id: scenario for scenario in scenarios}
    items = [load_authoring(path, by_id) for path in sorted(output.glob(f"*{EVIDENCE_SUFFIX}"))]
    return AuthorRun(output, scenarios_dir, scenarios, items)
