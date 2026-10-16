"""The author report's sections, each answering one question."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..report_kit import (
    Column,
    Markup,
    Row,
    badge,
    disclosure,
    esc,
    expand_controls,
    fields,
    join,
    metric,
    metrics,
    section,
    table,
)
from .analysis import CAUSE_ORDER, CAUSES, asked_again
from .model import EVIDENCE_SUFFIX, Authoring, AuthorRun, Finding
from .reasons import reason_meaning
from .views import (
    REVIEW_QUESTIONS,
    STAGE_LABEL,
    Links,
    claim_of,
    delivery_of,
    finding_html,
    kind_chip,
    oracle_of,
    oracle_sentence,
    package_conversation,
    shape_sentence,
    stage_chip,
    timeline,
    turns_of,
)

GROUP_ORDER = ("plan", "artifact")
STEP_ORDER = ("draft", "correct", "review", "revise")
STEP_NAMES = {
    "draft": "draft",
    "correct": "fix check findings",
    "review": "review",
    "revise": "revise after review",
}
SOURCE_LABEL = {"review": "Review", "check": "Code check", "format": "Review reply format"}


def counted(key: str, value: int) -> Markup:
    """Render a count in running text under its ``data-metric`` key."""

    return Markup(f'<b data-metric="{esc(key)}" data-value="{value}">{value:,}</b>')


@dataclass(frozen=True)
class Counts:
    """The headline numbers, computed once for the page and the stage summary."""

    sent: int
    packaged: int
    attack: int
    attack_packaged: int
    everyday: int
    everyday_packaged: int
    review_left: int
    asked_again: int
    tokens: int
    failed_tokens: int
    requests: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def failed(self) -> int:
        return self.sent - self.packaged

    @property
    def failed_share(self) -> str:
        return f"{round(100 * self.failed_tokens / self.tokens)}%" if self.tokens else "0%"


def _of_kind(items: Sequence[Authoring], kind: str) -> list[Authoring]:
    return [item for item in items if item.scenario.kind == kind]


def _packaged(items: Sequence[Authoring]) -> int:
    return sum(item.accepted for item in items)


def _failed_tokens(items: Sequence[Authoring]) -> int:
    return sum(item.tokens for item in items if not item.accepted)


def _usage(items: Sequence[Authoring]) -> tuple[int, int, int]:
    """Return the physical requests, prompt tokens, and completion tokens of every step."""

    steps = [step for item in items for step in item.steps]
    prompt = sum(step.prompt_tokens for step in steps)
    return sum(item.requests for item in items), prompt, sum(s.completion_tokens for s in steps)


def counts(items: Sequence[Authoring]) -> Counts:
    attack = _of_kind(items, "adversarial")
    everyday = _of_kind(items, "functional")
    review_left = [i for i in items if i.cause == "review_not_satisfied"]
    requests, prompt_tokens, completion_tokens = _usage(items)
    return Counts(
        sent=len(items),
        packaged=_packaged(items),
        attack=len(attack),
        attack_packaged=_packaged(attack),
        everyday=len(everyday),
        everyday_packaged=_packaged(everyday),
        review_left=len(review_left),
        asked_again=sum(asked_again(i) for i in review_left),
        tokens=sum(i.tokens for i in items),
        failed_tokens=_failed_tokens(items),
        requests=requests,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def answer_section(n: Counts) -> Markup:
    lead = (
        f"<p>{counted('answer.packaged', n.packaged)} of {counted('answer.sent', n.sent)} "
        "scenarios became runnable tests.</p>"
    )
    review = (
        f"<p>{counted('answer.review_left', n.review_left)} of the "
        f"{counted('answer.failed', n.failed)} failures ended with the reviewer still asking "
        f"for changes. In {counted('answer.asked_again', n.asked_again)} of those the reviewer "
        "asked the same question again although the revision had changed the field it names; "
        "open a failure to compare that field before and after.</p>"
    )
    tiles = metrics(
        [
            metric("packaged", "scenarios packaged", n.packaged, of=n.sent, unit="scenarios"),
            metric(
                "attack_packaged",
                "attack scenarios packaged",
                n.attack_packaged,
                of=n.attack,
                unit="scenarios",
                status="fail" if n.attack_packaged * 2 < n.attack else None,
            ),
            metric(
                "everyday_packaged",
                "everyday checks packaged",
                n.everyday_packaged,
                of=n.everyday,
                unit="scenarios",
            ),
            metric(
                "failed_token_share",
                f"of tokens spent on failed scenarios ({n.failed_tokens:,} of {n.tokens:,})",
                n.failed_share,
                unit="of tokens",
            ),
        ]
    )
    return section(
        "answer",
        "Did authoring turn scenarios into runnable tests?",
        "How many scenarios became packages, by kind, and what the failures cost.",
        Markup(lead + review + tiles),
    )


def _allowances(items: Sequence[Authoring]) -> str:
    policy = next((item.policy for item in items if item.policy), {})
    if not policy:
        return "The evidence records no allowances."
    return "; ".join(
        f"{group}: {policy.get(f'{group}_max_corrections', '?')} correction(s) after failed "
        f"code checks, {policy.get(f'{group}_max_review_revisions', '?')} revision(s) after a "
        "review"
        for group in GROUP_ORDER
    )


def how_section(items: Sequence[Authoring]) -> Markup:
    body = fields(
        [
            ("Plan", "The author drafts a plan; code checks run; a reviewer reviews it."),
            (
                "Artifact",
                "The author writes the test from the accepted plan; code checks run; a "
                "reviewer reviews it. An accepted artifact becomes a package.",
            ),
            ("Allowances", _allowances(items) + ". The next failure ends the scenario."),
        ]
    )
    return section(
        "how",
        "How authoring works",
        "What does each scenario go through, and how many tries does it get?",
        body,
    )


def _failure_key(item: Authoring) -> tuple[int, bool, bool, str]:
    cause = CAUSE_ORDER.index(item.cause) if item.cause in CAUSE_ORDER else len(CAUSE_ORDER)
    return (cause, item.terminal_stage != "plan", item.scenario.kind != "adversarial", item.id)


def failures_of(items: Sequence[Authoring]) -> list[Authoring]:
    """Return failed scenarios ordered by cause, stage, kind, and id."""

    return sorted((item for item in items if not item.accepted), key=_failure_key)


def _ids(members: Sequence[Authoring]) -> Markup:
    return join(
        (
            Markup(f'<a href="#{esc(m.id)}">{esc(m.id)}</a>')
            for m in sorted(members, key=lambda m: m.id)
        ),
        " ",
    )


def causes_section(failed: Sequence[Authoring]) -> Markup:
    groups: dict[tuple[str, str], list[Authoring]] = defaultdict(list)
    for item in failed:
        groups[(str(item.cause), item.terminal_stage)].append(item)
    rows = []
    ordered = sorted(
        groups.items(), key=lambda kv: (CAUSE_ORDER.index(kv[0][0]), GROUP_ORDER.index(kv[0][1]))
    )
    for (cause, stage), members in ordered:
        title, note = CAUSES[cause]
        kinds = Counter(m.scenario.kind for m in members)
        reasons = Counter(m.terminal_reason for m in members)
        reason_text = join(
            (Markup(f"<code>{esc(r)}</code> {c}") for r, c in sorted(reasons.items())), ", "
        )
        rows.append(
            Row(
                [
                    stage_chip(stage),
                    Markup(f"<b>{esc(title)}</b><br>{esc(note)}"),
                    counted(f"cause.{cause}.{stage}", len(members)),
                    kinds["adversarial"],
                    kinds["functional"],
                    reason_text,
                    _ids(members),
                ],
                facets={"stage": stage},
            )
        )
    columns = [
        Column("Stage"),
        Column("Cause"),
        Column("Scenarios", sortable=False),
        Column("Attack", numeric=True),
        Column("Everyday", numeric=True),
        Column("Terminal reasons", sortable=False),
        Column("Scenario ids", sortable=False),
    ]
    body = table(columns, rows, "cause-table") if rows else Markup("<p>No scenario failed.</p>")
    return section(
        "causes",
        "Why did authoring fail?",
        "Each failed scenario once, by how it ended. Open a scenario below for its history.",
        body,
    )


def _recurring_rows(items: Sequence[Authoring]) -> list[Row]:
    raised: dict[tuple[str, str, str], list[tuple[str, Finding]]] = defaultdict(list)
    ended: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for item in items:
        for step in item.steps:
            for finding in step.findings:
                raised[(finding.source, step.group, finding.key)].append((item.id, finding))
        for finding in [] if item.accepted else item.last_findings:
            ended[(finding.source, item.terminal_stage, finding.key)].add(item.id)
    packaged = {item.id for item in items if item.accepted}
    order = sorted(
        raised.items(),
        key=lambda kv: (-len(ended[kv[0]]), -len({i for i, _ in kv[1]}), kv[0]),
    )
    return [
        _recurring_row(index, key, hits, ended[key], packaged)
        for index, (key, hits) in enumerate(order, start=1)
    ]


def _recurring_row(
    index: int,
    key: tuple[str, str, str],
    hits: list[tuple[str, Finding]],
    ended: set[str],
    packaged: set[str],
) -> Row:
    source, stage, code = key
    scenarios = sorted({scenario for scenario, _ in hits})
    if source == "review":
        what = f"<code>{esc(code)}</code><br>{esc(REVIEW_QUESTIONS.get(code, ''))}"
    else:
        locations = ", ".join(sorted({f.location for _, f in hits if f.location}))
        what = f"<code>{esc(code)}</code><br>at {esc(locations or 'no location')}"
    examples = "".join(
        f'<li><a href="#{esc(s)}">{esc(s)}</a> {esc(f.problem[:170])}</li>' for s, f in hits[:6]
    )
    shown = disclosure(
        f"{len(hits)} findings", Markup(f"<ul>{examples}</ul>"), f"rec-{index}", mini=True
    )
    return Row(
        [
            SOURCE_LABEL.get(source, source),
            stage_chip(stage),
            Markup(what + shown),
            len(scenarios),
            len([s for s in scenarios if s in packaged]),
            len(ended),
        ],
        facets={"source": source},
    )


def recurring_section(items: Sequence[Authoring]) -> Markup:
    columns = [
        Column("Source"),
        Column("Stage"),
        Column("Problem", sortable=False),
        Column("Scenarios", numeric=True),
        Column("Packaged anyway", numeric=True),
        Column("Ended authoring", numeric=True),
    ]
    rows = _recurring_rows(items)
    body = table(columns, rows, "recurring-table") if rows else Markup("<p>No findings.</p>")
    return section(
        "recurring",
        "Which problems recur?",
        f"Every code-check and review finding across all {len(items)} scenarios sent, packaged "
        "or not, by check code or review question.",
        body,
    )


@dataclass(frozen=True)
class Places:
    """Where the report links outside its own directory."""

    links: Links
    scenarios_dir: Path
    generate_report: Path


def _scenario_links(item: Authoring, places: Places) -> Markup:
    links = places.links
    parts = [
        links.file(links.output / f"{item.id}{EVIDENCE_SUFFIX}", "failure evidence"),
        links.file(places.scenarios_dir / f"{item.id}.yaml", "handoff YAML"),
    ]
    if places.generate_report.is_file():
        href = f"{links.relative(places.generate_report)}#{item.id}-narrative"
        parts.append(Markup(f'<a href="{esc(href)}">scenario as written (generate report)</a>'))
    return Markup(f'<p class="links">{join(parts, " · ")}</p>')


def _failure_summary(item: Authoring) -> Markup:
    title, _ = CAUSES[str(item.cause)]
    last = item.last_findings[0] if item.last_findings else None
    problem = last.problem if last else item.terminal_reason
    signals = join((badge("warn", s) for s in item.signals), " ")
    return Markup(
        f"<b>{esc(item.id)}</b> {kind_chip(item.scenario.kind)} {stage_chip(item.terminal_stage)} "
        f"{esc(title)} · {esc(problem[:160])} {signals} · {item.tokens:,} tokens"
    )


def failure_row(item: Authoring, places: Places) -> Markup:
    stage = STAGE_LABEL.get(item.terminal_stage, item.terminal_stage)
    asked = fields(
        [
            ("What the producer asked for", item.scenario.criterion or "not recorded"),
            ("Attack shape", shape_sentence(item.scenario)),
            (
                "Where it stopped",
                f"{stage} stage, after {item.requests} model requests.",
            ),
            (
                "Reason",
                Markup(
                    f"<code>{esc(item.terminal_reason)}</code>: "
                    f"{esc(reason_meaning(item.terminal_reason))}"
                ),
            ),
        ]
    )
    last = join(finding_html(finding, full=True) for finding in item.last_findings)
    body = Markup(
        f"{asked}<h3>Last finding</h3>{last or 'None recorded.'}"
        f"<h3>Every request, in order</h3>{timeline(item, places.links)}"
        f"{_scenario_links(item, places)}"
    )
    return disclosure(_failure_summary(item), body, item.id, level="failure")


def failures_section(failed: Sequence[Authoring], places: Places) -> Markup:
    rows = join(failure_row(item, places) for item in failed)
    body = Markup(
        f'{expand_controls("failure-list", "failure")}<div id="failure-list">{rows}</div>'
        if failed
        else "<p>No scenario failed.</p>"
    )
    return section(
        "failures",
        "Failed scenarios",
        "What happened to each failure, request by request?",
        body,
    )


def _examples(item: Authoring) -> Markup:
    examples = item.package.examples if item.package else {}
    rows = [
        (f"{label.capitalize()} example", str((examples.get(label) or {}).get("description", "")))
        for label in ("unsafe", "safe")
        if examples.get(label)
    ]
    return fields(rows) if rows else Markup("")


def package_row(item: Authoring, places: Places) -> Markup:
    package = item.package
    assert package is not None
    fixes = sum(step.kind in ("correct", "revise") for step in item.steps)
    summary = Markup(
        f"<b>{esc(item.id)}</b> {kind_chip(item.scenario.kind)} {esc(claim_of(package))} · "
        f"{esc(oracle_of(package))} · {esc(delivery_of(package))} · {turns_of(package)} turn(s) · "
        f"{item.requests} requests, {fixes} fixes · {item.tokens:,} tokens"
    )
    links = places.links
    files = join(
        [
            links.file(links.output / item.id, "package"),
            links.file(links.output / item.id / "authoring" / "reviews.json", "reviews"),
            links.file(links.output / item.id / "authoring" / "ledger.json", "ledger"),
        ],
        " · ",
    )
    explanation = (
        f"<p><b>Author's explanation:</b> {esc(package.explanation)}</p>"
        if package.explanation
        else ""
    )
    body = Markup(
        f"<h3>What the test sends</h3>{package_conversation(package)}"
        f"<h3>How it decides</h3><p>{esc(oracle_sentence(package))}</p>{_examples(item)}"
        f"{explanation}<h3>Every request, in order</h3>{timeline(item, links)}"
        f'<p class="links">{files}</p>'
    )
    return disclosure(summary, body, item.id, level="package")


def packages_section(items: Sequence[Authoring], places: Places) -> Markup:
    accepted = sorted(
        (item for item in items if item.accepted and item.package is not None),
        key=lambda item: (item.scenario.kind != "adversarial", item.id),
    )
    rows = join(package_row(item, places) for item in accepted)
    body = Markup(
        f'{expand_controls("package-list", "package")}<div id="package-list">{rows}</div>'
        if accepted
        else "<p>No scenario was packaged.</p>"
    )
    return section(
        "packages",
        "Packages",
        "What does each runnable test send, and how does it decide?",
        body,
    )


def missing_section(run: AuthorRun) -> Markup:
    missing = run.missing
    rows = [Row([scenario.id, kind_chip(scenario.kind)], id=scenario.id) for scenario in missing]
    intro = Markup(
        f"<p>{counted('scenarios.no_record', len(missing))} of {len(run.scenarios)} scenarios "
        "have no failure evidence in this output: the author stage did not run authoring for "
        "them, for example because orch skipped a duplicate.</p>"
    )
    body = intro + (
        table([Column("Scenario"), Column("Kind")], rows, "missing-table") if rows else ""
    )
    return section(
        "not-authored",
        "Scenarios without authoring",
        "Which scenarios did the author stage not author?",
        Markup(body),
    )


def _effort_cells(items: Sequence[Authoring]) -> dict[tuple[str, str], list[int]]:
    cells: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for item in items:
        column = 0 if item.accepted else 2
        for step in item.steps:
            row = cells[(step.group, step.kind)]
            row[column] += 1
            row[column + 1] += step.tokens
    return cells


def effort_section(items: Sequence[Authoring]) -> Markup:
    cells = _effort_cells(items)
    rows = [
        Row([Markup(f"{stage_chip(group)} {esc(STEP_NAMES[kind])}"), *cells[(group, kind)]])
        for group in GROUP_ORDER
        for kind in STEP_ORDER
        if (group, kind) in cells
    ]
    totals = [sum(row[i] for row in cells.values()) for i in range(4)]
    rows.append(Row(["Total", *totals]))
    columns = [
        Column("Step", sortable=False),
        Column("Packaged: requests", numeric=True, sortable=False),
        Column("Packaged: tokens", numeric=True, sortable=False),
        Column("Failed: requests", numeric=True, sortable=False),
        Column("Failed: tokens", numeric=True, sortable=False),
    ]
    intro = Markup(
        f"<p>{counted('effort.requests', totals[0] + totals[2])} model requests and "
        f"{counted('effort.tokens', totals[1] + totals[3])} tokens; failed scenarios used "
        f"{counted('effort.failed_tokens', totals[3])} tokens over "
        f"{counted('effort.failed_requests', totals[2])} requests.</p>"
    )
    return section(
        "effort",
        "Where did the effort go?",
        "Model requests and tokens by step, for packaged and failed scenarios.",
        Markup(intro + table(columns, rows, "effort-table")),
    )
