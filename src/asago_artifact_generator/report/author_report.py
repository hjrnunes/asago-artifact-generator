"""The author report: ``index.html`` and ``stage-summary.json`` for one author output.

Both files come from one read of the output, and the stage summary is checked
against its schema and the page before either is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..report_kit import STAGE_SUMMARY_VERSION, Markup, check_headline, page
from .analysis import CAUSES, analyse
from .model import AuthorRun, load_run
from .sections import (
    Counts,
    Places,
    answer_section,
    causes_section,
    counts,
    effort_section,
    failures_of,
    failures_section,
    how_section,
    missing_section,
    packages_section,
    recurring_section,
)
from .views import STAGE_LABEL, Links

INDEX = "index.html"
SUMMARY = "stage-summary.json"
SCHEMA = Path(__file__).resolve().parent.parent / "report_kit" / "stage-summary-v1.schema.json"
BANNER = "Confidential: contains test attacks and system responses"
EXTRA_CSS = (
    ".tl{list-style:none;margin:6px 0;padding:0 0 0 14px;border-left:2px solid var(--line)}"
    ".tl>li{padding:4px 0 8px}.tl-h{display:flex;flex-wrap:wrap;gap:6px;align-items:center}"
    ".tl-n{font-family:var(--mono);color:var(--muted)}"
    ".tl-t{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}"
    ".tl>li.st-fail{border-left:3px solid var(--fail);padding-left:8px}"
    ".tl>li.st-warn{border-left:3px solid var(--warn);padding-left:8px}"
    ".fnd{border-left:3px solid var(--line-strong);padding:2px 10px;margin:6px 0}"
    ".fnd p{margin:2px 0}.before-after{margin:8px 0}"
)


@dataclass(frozen=True)
class Written:
    """Where a report was written."""

    index: Path
    summary: Path


def _run_id(output: Path) -> str | None:
    """Return the run id when *output* is ``runs/<id>/stages/author/output``."""

    parts = output.resolve().parts
    if len(parts) >= 4 and parts[-3] == "stages" and parts[-1] == "output":
        return parts[-4]
    return None


def _identity(run: AuthorRun, n: Counts) -> list[tuple[str, Any]]:
    models = sorted({item.model for item in run.items})
    identity: list[tuple[str, Any]] = [
        ("Stage", "author"),
        ("Owner", "consumer (asago-artifact-generator)"),
    ]
    run_id = _run_id(run.output)
    if run_id:
        identity.append(("Run", run_id))
    identity += [
        ("Model", ", ".join(models) or "none"),
        ("Scenarios", f"{len(run.scenarios)} handed over, {n.sent} authored"),
        ("Model requests", f"{n.requests:,}"),
    ]
    return identity


def _status(n: Counts) -> str:
    if n.sent == 0:
        return "unknown"
    if n.packaged == n.sent:
        return "pass"
    return "fail" if n.packaged * 2 < n.sent else "warn"


def _alerts(run: AuthorRun) -> list[dict[str, Any]]:
    failed = [item for item in run.items if not item.accepted]
    by_cause: dict[str, int] = {}
    for item in failed:
        by_cause[str(item.cause)] = by_cause.get(str(item.cause), 0) + 1
    return [
        {
            "status": "fail",
            "text": f"{count} scenario(s) failed: {CAUSES[cause][0].lower()}",
            "href": f"{INDEX}#causes",
        }
        for cause, count in sorted(by_cause.items(), key=lambda kv: list(CAUSES).index(kv[0]))
    ]


def _items(run: AuthorRun) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for item in run.items:
        if item.accepted:
            entry = {"status": "pass", "label": "Packaged"}
        else:
            stage = STAGE_LABEL.get(item.terminal_stage, item.terminal_stage)
            entry = {
                "status": "fail",
                "label": f"{stage}: {CAUSES[str(item.cause)][0].lower()}",
                "code": f"{item.terminal_stage}:{item.terminal_reason}",
            }
        entries[item.id] = {"scenario_id": item.id, **entry, "href": f"{INDEX}#{item.id}"}
    for scenario in run.missing:
        entries[scenario.id] = {
            "scenario_id": scenario.id,
            "status": "skip",
            "label": "No authoring record",
            "href": f"{INDEX}#{scenario.id}",
        }
    return [entries[key] for key in sorted(entries)]


def stage_summary(run: AuthorRun, n: Counts) -> dict[str, Any]:
    """Return the ``stage-summary-v1`` document of the author stage."""

    return {
        "schema_version": STAGE_SUMMARY_VERSION,
        "stage": "author",
        "owner": "consumer",
        "status": _status(n),
        "report": INDEX,
        "headline": [
            {
                "key": "packaged",
                "label": "Runnable tests",
                "value": n.packaged,
                "of": n.sent,
                "unit": "scenarios",
            },
            {
                "key": "attack_packaged",
                "label": "Attack scenarios packaged",
                "value": n.attack_packaged,
                "of": n.attack,
                "unit": "scenarios",
            },
            {
                "key": "everyday_packaged",
                "label": "Everyday checks packaged",
                "value": n.everyday_packaged,
                "of": n.everyday,
                "unit": "scenarios",
            },
            {
                "key": "failed_token_share",
                "label": "Tokens spent on failed scenarios",
                "value": n.failed_share,
                "unit": "of tokens",
            },
        ],
        "alerts": _alerts(run),
        "items": _items(run),
        "usage": {
            "model_requests": n.requests,
            "prompt_tokens": n.prompt_tokens,
            "completion_tokens": n.completion_tokens,
        },
    }


def build_report(run: AuthorRun, out: Path) -> tuple[Markup, dict[str, Any]]:
    """Return the page and its stage summary for *run*, linking files relative to *out*."""

    analyse(run.items)
    n = counts(run.items)
    failed = failures_of(run.items)
    places = Places(
        Links(out, run.output),
        run.scenarios_dir,
        run.scenarios_dir.parent / "report" / INDEX,
    )
    sections = [
        answer_section(n),
        how_section(run.items),
        causes_section(failed),
        recurring_section(run.items),
        failures_section(failed, places),
        packages_section(run.items, places),
        missing_section(run),
        effort_section(run.items),
    ]
    html = page(
        "Author report",
        "developer",
        _identity(run, n),
        sections,
        banner=BANNER,
        footer="Reads the scenarios handed to authoring, the failure evidence, and the packages.",
        extra_css=EXTRA_CSS,
    )
    return html, stage_summary(run, n)


def write_report(output: Path, scenarios: Path, out: Path) -> Written:
    """Write ``index.html`` and ``stage-summary.json`` for the author output under *out*."""

    out.mkdir(parents=True, exist_ok=True)
    html, summary = build_report(load_run(output, scenarios), out)
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(summary)
    check_headline(summary, html)
    index = out / INDEX
    index.write_text(html, encoding="utf-8")
    path = out / SUMMARY
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return Written(index, path)
