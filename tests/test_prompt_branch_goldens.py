"""Prompt branches that no recorded replay dispatches, pinned as golden text.

The replay gate proves that a prompt stays byte-identical only for the branches its
recordings reach, and the stage goldens in ``tests/golden/prompts`` render one world. Each
case here renders a model-facing branch that neither reaches: the prompt section exactly as
``_render_sections`` writes it. ``tests/golden/prompt_branches/<case>.txt`` holds that text.

Rewrite the files only after a deliberate prompt change, with the stage's version bumped:

    uv run python -m tests.test_prompt_branch_goldens
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from asago_artifact_generator.authoring.binding_repair import (
    CorrectionRepairInputs,
    _reference_repair_options_for_correction,
    _repair_consumer_mismatch_option,
    _repair_unknown_binding_option,
    _selector_notes,
)
from asago_artifact_generator.authoring.planted_item import (
    indirect_block,
    planted_review_sections,
)
from asago_artifact_generator.authoring.prompt_context import (
    _neutral_status_binding_example,
    _scenario_design,
)
from asago_artifact_generator.authoring.sequential_turns import multi_turn_block
from asago_artifact_generator.input_adapter import InputView, _framed_digest, load_input

from .support import HANDOFF, world
from .turn_support import indirect_runtime, indirect_shape, v4_view

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "prompt_branches"
_REFRESH = "run `uv run python -m tests.test_prompt_branch_goldens` after a deliberate change"


def _section(value: Any) -> str:
    """Return ``value`` as ``_render_sections`` writes a non-compact section."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _indirect_view(
    directory: Path, controller: str, record_ref: str | None = "ORD-101"
) -> InputView:
    """Return a four-turn indirect handoff whose benign turns use every purpose but one."""

    payload = json.loads(HANDOFF.read_text(encoding="utf-8"))
    payload.pop("content_digest")
    payload["schema_version"] = "scenario-handoff-v4"
    shape = indirect_shape(4, record_ref)
    purposes = ("establish_context", "assert_authority", "ask_to_read_item", "apply_pressure")
    shape["turn_plan"] = [
        {"position": position, "speaker": "benign_user", "purpose": purpose}
        for position, purpose in enumerate(purposes, start=1)
    ]
    shape["indirect"]["party_relation"]["controller"] = controller
    payload["attack_shape"] = shape
    payload["content_digest"] = _framed_digest("scenario-handoff-v4", payload)
    path = directory / f"indirect-{controller}-{record_ref}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_input(path)


def _indirect_case(controller: str) -> Callable[[Path], str]:
    def render(directory: Path) -> str:
        runtime = indirect_runtime(world("refund")["runtime_contract"])
        return _section(indirect_block(_indirect_view(directory, controller), runtime))

    return render


def _plan_chooses_record(directory: Path) -> str:
    runtime = indirect_runtime(world("refund")["runtime_contract"])
    view = _indirect_view(directory, "counterparty", record_ref=None)
    return _section(indirect_block(view, runtime))


def _record_fixed_by_plan(directory: Path) -> str:
    runtime = indirect_runtime(world("refund")["runtime_contract"])
    view = _indirect_view(directory, "counterparty", record_ref=None)
    plan = {"stimulus_approach": {"planted_record_ref": "ORD-102"}}
    return _section(indirect_block(view, runtime, plan))


def _planted_review_record_fixed_by_plan(directory: Path) -> str:
    runtime = indirect_runtime(world("refund")["runtime_contract"])
    view = _indirect_view(directory, "counterparty", record_ref=None)
    plan = {"stimulus_approach": {"planted_record_ref": "ORD-102"}}
    ((_, section),) = planted_review_sections(view, {"planted_text": "planted"}, runtime, plan)
    return _section(section)


def _planted_review(directory: Path) -> str:
    view = _indirect_view(directory, "counterparty")
    runtime = indirect_runtime(world("refund")["runtime_contract"])
    ((_, section),) = planted_review_sections(view, {"planted_text": "planted"}, runtime, {})
    return _section(section)


def _scenario_design_unavailable(_: Path) -> str:
    return _section(_scenario_design(SimpleNamespace(payload={})))


def _generic_binding_example(_: Path) -> str:
    refund = world("refund")
    inventory = {**refund["inventory"], "facts": []}
    runtime = {**refund["runtime_contract"], "setup_permissions": []}
    return _section(_neutral_status_binding_example(inventory, runtime))


def _four_turns(directory: Path) -> str:
    return _section(multi_turn_block(v4_view(directory, 4)))


def _reference_repairs(_: Path) -> str:
    references = {
        "provenance_ids": {"ids": {"LIN-1": "scenario lineage (loss IDs)"}},
        "field_rules": {
            "selected_evidence[].ref": "Each entry is exactly one citable_references value.",
            "assumptions[].ref": "Each entry is one supplied fact or provenance ID.",
        },
    }
    findings = [
        {
            "code": "unknown_reference",
            "detail": f"unknown_reference: {value}",
            "path": path,
        }
        for value, path in (
            ("LIN-1", "selected_evidence[0].ref"),
            ("tool_calls", "selected_evidence[1].ref"),
            ("state:missing", "assumptions[0].ref"),
        )
    ]
    inputs = CorrectionRepairInputs(
        inventory={}, runtime_contract={}, evidence_references=references, observation={}
    )
    return _section(_reference_repair_options_for_correction(findings, inputs))


def _binding_repair_texts(_: Path) -> str:
    finding = {"code": "unknown_binding", "path": "prerequisites[0].binding"}
    declared = {"runtime_bindings": [{"name": f"binding_{index:02d}"} for index in range(41)]}
    return _section(
        {
            "consumer_mismatch": _repair_consumer_mismatch_option(
                {"code": "consumer_mismatch", "path": "prerequisites[0].binding"},
                {"binding": "loan_id"},
            ),
            "selector_notes": _selector_notes(True, [], "facts:state:loans", "integer"),
            "unknown_binding": _repair_unknown_binding_option(
                finding, {"binding": "loan_id", "evidence_refs": []}, declared, {"facts": []}
            ),
        }
    )


CASES: dict[str, Callable[[Path], str]] = {
    "binding_repair_texts": _binding_repair_texts,
    "four_sequential_turns": _four_turns,
    "generic_binding_example": _generic_binding_example,
    "indirect_counterparty": _indirect_case("counterparty"),
    "indirect_operator_insider": _indirect_case("operator_insider"),
    "indirect_plan_chooses_record": _plan_chooses_record,
    "indirect_record_fixed_by_plan": _record_fixed_by_plan,
    "indirect_unknown_controller": _indirect_case("unknown"),
    "planted_review_counterparty": _planted_review,
    "planted_review_record_fixed_by_plan": _planted_review_record_fixed_by_plan,
    "reference_repair_kinds": _reference_repairs,
    "scenario_design_unavailable": _scenario_design_unavailable,
}


def golden_path(case: str) -> Path:
    return GOLDEN_DIR / f"{case}.txt"


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_branch_renders_its_golden_text(case: str, tmp_path: Path) -> None:
    expected = golden_path(case).read_text(encoding="utf-8")

    assert CASES[case](tmp_path) == expected, f"{case} differs from its golden file; {_REFRESH}"


def test_the_golden_directory_holds_only_the_files_the_cases_name() -> None:
    assert set(GOLDEN_DIR.glob("*")) == {golden_path(case) for case in CASES}


def refresh(directory: Path) -> int:
    """Rewrite every golden file from a fresh render; ``directory`` holds scratch handoffs."""

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for case, render in CASES.items():
        golden_path(case).write_text(render(directory), encoding="utf-8")
    return 0


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        sys.exit(refresh(Path(scratch)))
