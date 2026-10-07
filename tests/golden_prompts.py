"""Golden prompt files: the text each authoring stage sends, kept readable under version control.

``tests/golden/prompts/<case>/<stage>.txt`` holds the system and user text a stage renders from a
test world. ``tests/golden/prompts/manifest.json`` maps each stage to its prompt version.
``tests/test_prompt_goldens.py`` compares a fresh render with these files and requires a new
version whenever a stage's text changes.

Rewrite the files after a deliberate prompt change, with the stage's version already bumped:

    uv run python -m tests.golden_prompts

The command refuses to rewrite a stage whose text changed under an unchanged version. When a
test world changed and no template did, add ``--test-world-changed`` to accept that change.
"""

from __future__ import annotations

import difflib
import json
import sys
from functools import cache
from pathlib import Path

from asago_artifact_generator.authoring.core import PromptPacket
from asago_artifact_generator.input_adapter import load_input

from .prompt_support import STAGES, render_stage_packets
from .support import OBSERVED_HANDOFF

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "prompts"
MANIFEST_PATH = GOLDEN_DIR / "manifest.json"

# The ehr case renders every stage. The second case renders the plan review for a handoff that
# carries a tool-call condition, so a first-round review keeps its condition wording.
CASE_STAGES: dict[str, tuple[str, ...]] = {
    "ehr": STAGES,
    "ehr-observed-condition": ("plan_review",),
}


@cache
def rendered_packets(case: str) -> dict[str, PromptPacket]:
    """Return the packets of ``case`` keyed by stage."""

    if case == "ehr":
        packets = render_stage_packets("ehr")
    elif case == "ehr-observed-condition":
        packets = render_stage_packets("ehr", view=load_input(OBSERVED_HANDOFF))
    else:
        raise KeyError(case)
    return {stage: packets[stage] for stage in CASE_STAGES[case]}


def golden_text(packet: PromptPacket) -> str:
    """Return the text a golden file holds for ``packet``."""

    return f"=== system ===\n{packet.system}\n=== user ===\n{packet.user}\n"


def golden_path(case: str, stage: str) -> Path:
    return GOLDEN_DIR / case / f"{stage}.txt"


def read_golden(case: str, stage: str) -> str | None:
    path = golden_path(case, stage)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def read_manifest() -> dict[str, str]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def stage_versions() -> dict[str, str]:
    """Return the prompt version each stage renders, requiring all cases to agree."""

    versions: dict[str, str] = {}
    for case, stages in CASE_STAGES.items():
        for stage in stages:
            version = rendered_packets(case)[stage].version
            if versions.setdefault(stage, version) != version:
                raise ValueError(f"stage {stage} renders two versions")
    return dict(sorted(versions.items()))


def _changed_span(old: str, new: str, context: int = 60, width: int = 240) -> tuple[str, str]:
    """Return ``old`` and ``new`` cut to the span between their common prefix and suffix."""

    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    start = max(prefix - context, 0)

    def cut(text: str) -> str:
        changed_end = len(text) - suffix
        tail = text[changed_end : changed_end + min(context, suffix)]
        middle = text[start:changed_end]
        if len(middle) > width:
            middle = middle[:width] + "..."
        more = "..." if suffix > context else ""
        return f"{'...' if start else ''}{middle}{tail}{more}"

    return cut(old), cut(new)


def text_diff(case: str, stage: str, limit: int = 40) -> str:
    """Describe where a fresh render differs from the golden file; return "" when it matches.

    Rendered JSON sits on single long lines, so a changed line shows only the span between
    its common prefix and suffix.
    """

    expected = read_golden(case, stage)
    if expected is None:
        return f"{golden_path(case, stage)} does not exist"
    actual = golden_text(rendered_packets(case)[stage])
    if expected == actual:
        return ""
    old_lines, new_lines = expected.split("\n"), actual.split("\n")
    report: list[str] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        report.append(f"@@ golden line {i1 + 1}-{i2} / rendered line {j1 + 1}-{j2} @@")
        if tag == "replace" and i2 - i1 == j2 - j1:
            for old, new in zip(old_lines[i1:i2], new_lines[j1:j2], strict=True):
                old_span, new_span = _changed_span(old, new)
                report += [f"- {old_span}", f"+ {new_span}"]
        else:
            report += [f"- {line[:240]}" for line in old_lines[i1:i2]]
            report += [f"+ {line[:240]}" for line in new_lines[j1:j2]]
    shown = report[:limit]
    if len(report) > limit:
        shown.append(f"... {len(report) - limit} more diff lines")
    return "\n".join(shown)


def changed_stages() -> list[str]:
    """Return the stages whose rendered text differs from a golden file in any case."""

    return sorted(
        {
            stage
            for case, stages in CASE_STAGES.items()
            for stage in stages
            if read_golden(case, stage) != golden_text(rendered_packets(case)[stage])
        }
    )


def refresh(allow_unversioned: bool = False) -> int:
    """Rewrite the golden files and the manifest from a fresh render.

    ``allow_unversioned`` accepts a text change under an unchanged version. Use it only when
    a test world changed, not a prompt template.
    """

    versions = stage_versions()
    recorded = read_manifest() if MANIFEST_PATH.is_file() else {}
    unversioned = [
        stage
        for stage in changed_stages()
        if stage in recorded and recorded[stage] == versions[stage]
    ]
    if unversioned and not allow_unversioned:
        print(
            "refusing to rewrite: the text of "
            + ", ".join(unversioned)
            + " changed under an unchanged prompt version; bump the version first",
            file=sys.stderr,
        )
        return 1
    for case, stages in CASE_STAGES.items():
        for stage in stages:
            path = golden_path(case, stage)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(golden_text(rendered_packets(case)[stage]), encoding="utf-8")
    MANIFEST_PATH.write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(refresh(allow_unversioned="--test-world-changed" in sys.argv[1:]))
