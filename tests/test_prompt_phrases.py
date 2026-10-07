"""Each template's fixed phrases, listed in ``tests/phrases/<template>.yaml``, are in its prompt.

A failure names the template, the rendered case, the prompt part and the phrase. Edit the YAML
file when you reword a template on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .phrase_tables import (
    CASES,
    KINDS,
    PARTS,
    PHRASES_DIR,
    entries,
    load_table,
    rendered_part,
    violations,
)
from .prompt_support import STAGES


@pytest.fixture(scope="session")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("phrase-cases")


@pytest.mark.parametrize(("template", "case", "part", "kind"), entries())
def test_the_rendered_prompt_obeys_the_phrase_table(
    template: str, case: str, part: str, kind: str, scratch: Path
) -> None:
    phrases = load_table(template)[case][part][kind]
    text = rendered_part(template, case, part, scratch)

    problems = violations(kind, phrases, text)

    assert problems == [], f"{template} / {case} / {part} / {kind}: {problems}"


def test_there_is_one_table_per_template() -> None:
    assert sorted(path.stem for path in PHRASES_DIR.glob("*.yaml")) == sorted(STAGES)


@pytest.mark.parametrize("template", STAGES)
def test_a_table_names_only_known_cases_parts_and_kinds(template: str) -> None:
    table = load_table(template)

    assert set(table) <= set(CASES)
    for parts in table.values():
        assert set(parts) <= set(PARTS)
        for kinds in parts.values():
            assert set(kinds) <= set(KINDS)


@pytest.mark.parametrize("template", STAGES)
def test_a_table_lists_each_phrase_once_per_list(template: str) -> None:
    for case, parts in load_table(template).items():
        for part, kinds in parts.items():
            for kind, phrases in kinds.items():
                flat = [p for item in phrases for p in (item if kind == "ordered" else [item])]
                assert all(isinstance(p, str) and p for p in flat), (case, part, kind)
                if kind != "ordered":
                    assert len(set(phrases)) == len(phrases), (template, case, part, kind)


def test_the_violation_report_names_each_failing_phrase() -> None:
    text = "alpha beta beta"

    assert violations("required", ["alpha", "gamma"], text) == ["missing: 'gamma'"]
    assert violations("once", ["alpha", "beta", "gamma"], text) == [
        "appears 2 times, expected once: 'beta'",
        "appears 0 times, expected once: 'gamma'",
    ]
    assert violations("forbidden", ["beta", "gamma"], text) == ["present but forbidden: 'beta'"]
    assert violations("ordered", [["alpha", "beta"]], text) == []
    assert violations("ordered", [["beta", "alpha"]], text) == ["out of order: ['beta', 'alpha']"]
    assert violations("ordered", [["alpha", "gamma"]], text) == [
        "missing from the ordered list: 'gamma'"
    ]
