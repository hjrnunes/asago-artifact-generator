"""The author report, checked against a fixture trimmed from a recorded run.

The fixture (``tests/fixtures/author_report``) holds 10 scenarios: 6 failed,
3 packaged, and 1 that authoring never ran. Its failure evidence carries the
reviewed candidate in each review record, as the consumer now stores it.
"""

from __future__ import annotations

import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.report.author_report import SCHEMA, build_report, write_report
from asago_artifact_generator.report.model import load_run
from asago_artifact_generator.report_kit import KitError, check_headline, metric, read_metrics

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "author_report"


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A writable copy of the fixture run."""

    target = tmp_path / "run"
    shutil.copytree(FIXTURE, target)
    return target


def _output(run_dir: Path) -> Path:
    return run_dir / "author" / "output"


def _scenarios(run_dir: Path) -> Path:
    return run_dir / "generate" / "output" / "scenarios"


def _render(run_dir: Path) -> tuple[str, dict]:
    output = _output(run_dir)
    written = write_report(output, _scenarios(run_dir), output / "report")
    summary = json.loads(written.summary.read_text(encoding="utf-8"))
    return written.index.read_text(encoding="utf-8"), summary


class _Elements(HTMLParser):
    """Collect each element's attributes, keyed by tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append((tag, {k: v or "" for k, v in attrs}))


def _elements(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = _Elements()
    parser.feed(html)
    return parser.found


def _rows(html: str, level: str) -> list[str]:
    return [
        a["id"] for tag, a in _elements(html) if tag == "details" and a.get("data-level") == level
    ]


def _table_rows(html: str, table: str) -> int:
    section = html.split(f'<table id="{table}"', 1)[1].split("</table>", 1)[0]
    return section.count("<tr") - 1


# Rule: every rendered number has a stable data-metric key.
def test_the_numbers_carry_their_data_metric_keys(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    shown = {key: (v["value"], v["of"]) for key, v in read_metrics(html).items()}

    assert shown == {
        "answer.packaged": ("3", None),
        "answer.sent": ("9", None),
        "answer.review_left": ("3", None),
        "answer.failed": ("6", None),
        "answer.asked_again": ("2", None),
        "packaged": ("3", "9"),
        "attack_packaged": ("1", "6"),
        "everyday_packaged": ("2", "3"),
        "failed_token_share": ("61%", None),
        "cause.review_not_satisfied.plan": ("2", None),
        "cause.review_not_satisfied.artifact": ("1", None),
        "cause.review_unusable.plan": ("2", None),
        "cause.checks_not_satisfied.plan": ("1", None),
        "scenarios.no_record": ("1", None),
        "effort.requests": ("43", None),
        "effort.tokens": ("810340", None),
        "effort.failed_tokens": ("491684", None),
        "effort.failed_requests": ("25", None),
    }


# Rule: stage-summary.json agrees with its HTML.
def test_the_stage_summary_matches_the_page_and_its_schema(run_dir: Path) -> None:
    html, summary = _render(run_dir)

    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(summary)
    check_headline(summary, html)
    assert [entry["key"] for entry in summary["headline"]] == [
        "packaged",
        "attack_packaged",
        "everyday_packaged",
        "failed_token_share",
    ]
    assert summary["usage"] == {
        "model_requests": 43,
        "prompt_tokens": 786299,
        "completion_tokens": 24041,
    }
    tampered = json.loads(json.dumps(summary))
    tampered["headline"][0]["value"] = 4
    with pytest.raises(KitError, match="headline packaged"):
        check_headline(tampered, html)


def test_the_stage_summary_labels_every_scenario(run_dir: Path) -> None:
    _, summary = _render(run_dir)

    items = {item["scenario_id"]: item for item in summary["items"]}

    assert sorted(items) == [f"SCN-{n:03d}" for n in (1, 2, 4, 5, 11, 12, 15, 21, 26, 30)]
    assert items["SCN-001"] == {
        "scenario_id": "SCN-001",
        "status": "fail",
        "label": "Artifact: reviewer still asked for changes",
        "code": "artifact:correction_limit_exhausted",
        "href": "index.html#SCN-001",
    }
    assert items["SCN-030"]["label"] == "Plan: review response unusable"
    assert items["SCN-026"]["code"] == "plan:established_trigger_not_observation"
    assert items["SCN-004"]["label"] == "Packaged"
    assert items["SCN-002"]["status"] == "skip"
    assert summary["status"] == "fail"


def _retry(run_dir: Path, scenario: str, retries: int) -> None:
    """Record *retries* transport retries on the first dispatch of *scenario*."""

    path = _output(run_dir) / f"{scenario}.failure-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["attempts"][0]["transport_retries"] = [
        {"attempt": n + 2, "retry_of": {"status": 503}} for n in range(retries)
    ]
    path.write_text(json.dumps(evidence), encoding="utf-8")


# Rule: the report counts physical requests: each dispatch plus its transport retries.
def test_the_summary_counts_transport_retries_as_requests(run_dir: Path) -> None:
    _retry(run_dir, "SCN-001", 2)
    _retry(run_dir, "SCN-004", 1)

    _, summary = _render(run_dir)

    assert summary["usage"]["model_requests"] == 46
    assert summary["usage"]["prompt_tokens"] == 786299


def test_each_scenario_text_counts_transport_retries_as_requests(run_dir: Path) -> None:
    _retry(run_dir, "SCN-001", 2)
    _retry(run_dir, "SCN-004", 1)

    html, _ = _render(run_dir)

    scn001 = html.split('id="SCN-001"', 1)[1].split("</details>", 1)[0]
    scn004 = html.split('id="SCN-004"', 1)[1].split("</details>", 1)[0]
    assert "after 9 model requests" in scn001
    assert "5 requests," in scn004


def _total_row(html: str, table: str) -> list[int]:
    section = html.split(f'<table id="{table}"', 1)[1].split("</table>", 1)[0]
    row = section.rsplit("<tr", 1)[1]
    cells = re.findall(r"<td[^>]*>\s*([\d,]+)\s*</td>", row)
    return [int(cell.replace(",", "")) for cell in cells]


def test_the_effort_table_counts_transport_retries_as_requests(run_dir: Path) -> None:
    _retry(run_dir, "SCN-001", 2)
    _retry(run_dir, "SCN-004", 1)

    html, _ = _render(run_dir)

    metrics = read_metrics(html)
    assert metrics["effort.requests"]["value"] == "46"
    assert metrics["effort.failed_requests"]["value"] == "27"
    assert _total_row(html, "effort-table") == [19, 810340 - 491684, 27, 491684]


# Rule: every input item appears.
def test_every_scenario_has_a_row(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    assert _rows(html, "failure") == [
        "SCN-011",
        "SCN-015",
        "SCN-001",
        "SCN-030",
        "SCN-005",
        "SCN-026",
    ]
    assert _rows(html, "package") == ["SCN-021", "SCN-004", "SCN-012"]
    assert _table_rows(html, "missing-table") == 1


def test_removing_a_failed_scenario_drops_its_row(run_dir: Path) -> None:
    (_output(run_dir) / "SCN-026.failure-evidence.json").unlink()
    (_scenarios(run_dir) / "SCN-026.yaml").unlink()

    html, summary = _render(run_dir)

    assert "SCN-026" not in _rows(html, "failure")
    assert len(_rows(html, "failure")) == 5
    assert len(summary["items"]) == 9


def test_removing_a_package_drops_its_row(run_dir: Path) -> None:
    (_output(run_dir) / "SCN-012.failure-evidence.json").unlink()
    shutil.rmtree(_output(run_dir) / "SCN-012")

    html, summary = _render(run_dir)

    assert _rows(html, "package") == ["SCN-021", "SCN-004"]
    assert _table_rows(html, "missing-table") == 2
    assert {"scenario_id": "SCN-012", "status": "skip"}.items() <= next(
        i for i in summary["items"] if i["scenario_id"] == "SCN-012"
    ).items()


def test_removing_an_unauthored_scenario_drops_its_row(run_dir: Path) -> None:
    (_scenarios(run_dir) / "SCN-002.yaml").unlink()

    html, _ = _render(run_dir)

    assert "missing-table" not in html
    assert read_metrics(html)["scenarios.no_record"]["value"] == "0"


# Rule: labels state units.
def test_every_metric_tile_states_its_unit(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    tiles = re.findall(r'<div class="metric[^"]*" data-metric="([^"]+)"[^>]*>(.*?)</div>', html)
    assert [key for key, _ in tiles] == [
        "packaged",
        "attack_packaged",
        "everyday_packaged",
        "failed_token_share",
    ]
    for key, body in tiles:
        assert re.search(r'<span class="u"> \S', body), key
    with pytest.raises(KitError, match="needs a unit"):
        metric("packaged", "scenarios packaged", 3, unit="")


# Rule: rendering is deterministic.
def test_rendering_twice_gives_identical_bytes(run_dir: Path) -> None:
    first = _render(run_dir)
    second = _render(run_dir)

    assert first == second
    report = _output(run_dir) / "report"
    assert sorted(p.name for p in report.iterdir()) == ["index.html", "stage-summary.json"]


# Rule: reports are offline-complete.
def test_the_page_loads_nothing_from_outside(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    for tag, attrs in _elements(html):
        assert not re.match(r"\s*(https?:)?//", attrs.get("src", "")), tag
        if tag == "link":
            assert not re.match(r"\s*(https?:)?//", attrs.get("href", ""))
        if tag == "a":
            assert not re.match(r"\s*(https?:|/)", attrs.get("href", "")), attrs["href"]


def test_the_page_escapes_no_markup_by_mistake(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    assert "&lt;b data-metric" not in html
    assert "&lt;span class=" not in html
    assert "&lt;code&gt;" not in html


# Rule: before-and-after panels show reviewed candidates only.
def test_a_revision_that_changed_the_field_shows_changed(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    scn011 = html.split('id="SCN-011"', 1)[1].split("</details>", 1)[0]
    assert scn011.count('data-verdict="changed"') == 1
    assert 'data-verdict="unchanged"' not in scn011
    assert "before and after the revision" in scn011


def test_a_revision_that_left_the_field_alone_shows_unchanged(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    scn001 = html.split('id="SCN-001"', 1)[1].split("<h3>Every request", 1)[0]
    assert scn001.count('data-verdict="unchanged"') == 1
    assert 'data-verdict="changed"' not in scn001


def test_a_candidate_that_does_not_match_its_digest_gets_no_panel(run_dir: Path) -> None:
    path = _output(run_dir) / "SCN-011.failure-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    last = evidence["attempts"][-1]["review"]
    last["reviewed_candidate"]["observation_claim"] = {"claim_level": "edited"}
    path.write_text(json.dumps(evidence), encoding="utf-8")

    html, _ = _render(run_dir)

    scn011 = html.split('id="SCN-011"', 1)[1].split("</details>", 1)[0]
    assert "data-verdict=" not in scn011
    assert "before and after the revision" not in scn011


def test_evidence_without_stored_candidates_gets_no_panel(run_dir: Path) -> None:
    for path in _output(run_dir).glob("*.failure-evidence.json"):
        evidence = json.loads(path.read_text(encoding="utf-8"))
        for attempt in evidence["attempts"]:
            (attempt.get("review") or {}).pop("reviewed_candidate", None)
        path.write_text(json.dumps(evidence), encoding="utf-8")

    html, _ = _render(run_dir)

    assert "data-verdict=" not in html


# Other content the design requires.
def test_a_failure_states_its_reason_and_its_meaning(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    scn030 = html.split('id="SCN-030"', 1)[1].split("</details>", 1)[0]
    assert "<code>trailing_content</code>: The response had text after its JSON block" in scn030
    assert "Review reply format" in html


def test_request_links_point_at_existing_files_only(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    hrefs = [a["href"] for tag, a in _elements(html) if tag == "a" and "href" in a]
    files = [h for h in hrefs if not h.startswith("#")]
    report = _output(run_dir) / "report"
    assert "../SCN-001.failure-evidence.json" in files
    assert "../SCN-004.failure-evidence.json" in files
    assert all((report / h.split("#", 1)[0]).exists() for h in files)
    assert ".authoring" not in html
    assert "/authoring/" not in html


def _request_labels(html: str, scenario: str) -> list[str]:
    section = html.split(f'id="{scenario}"', 1)[1].split("</details>", 1)[0]
    return re.findall(
        rf'<a class="artifact" href="\.\./{scenario}\.failure-evidence\.json">'
        r"(attempts\[\d+\])</a>",
        section,
    )


def test_each_request_links_its_record_in_the_failure_evidence(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    for scenario in ("SCN-001", "SCN-004"):
        path = _output(run_dir) / f"{scenario}.failure-evidence.json"
        attempts = json.loads(path.read_text(encoding="utf-8"))["attempts"]
        assert _request_labels(html, scenario) == [
            f"attempts[{index}]" for index in range(len(attempts))
        ]
    assert "authoring evidence" in html
    assert ">reviews<" not in html
    assert ">ledger<" not in html


def test_the_generate_report_link_appears_only_when_that_report_exists(run_dir: Path) -> None:
    html, _ = _render(run_dir)
    assert "generate report" not in html

    generate = run_dir / "generate" / "output" / "report"
    generate.mkdir()
    (generate / "index.html").write_text("<!DOCTYPE html>", encoding="utf-8")
    html, _ = _render(run_dir)

    assert 'href="../../../generate/output/report/index.html#SCN-001-narrative"' in html


def test_the_page_is_marked_confidential(run_dir: Path) -> None:
    html, _ = _render(run_dir)

    assert '<p class="banner" role="note">Confidential: contains test attacks' in html


def test_build_report_writes_nothing(run_dir: Path) -> None:
    output = _output(run_dir)

    build_report(load_run(output, _scenarios(run_dir)), output / "report")

    assert not (output / "report").exists()


def test_the_cli_writes_the_report_where_asked(run_dir: Path) -> None:
    out = run_dir / "elsewhere"
    result = CliRunner().invoke(
        cli.app,
        [
            "report",
            str(_output(run_dir)),
            "--scenarios",
            str(_scenarios(run_dir)),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "index": str(out / "index.html"),
        "summary": str(out / "stage-summary.json"),
    }
    assert "../author/output/SCN-001.failure-evidence.json" in (out / "index.html").read_text()


def test_the_cli_defaults_to_the_report_directory_of_the_output(run_dir: Path) -> None:
    result = CliRunner().invoke(
        cli.app, ["report", str(_output(run_dir)), "--scenarios", str(_scenarios(run_dir))]
    )

    assert result.exit_code == 0, result.output
    assert (_output(run_dir) / "report" / "stage-summary.json").is_file()


def test_the_cli_rejects_a_missing_directory(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app, ["report", str(tmp_path / "absent"), "--scenarios", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "not a directory" in result.output
