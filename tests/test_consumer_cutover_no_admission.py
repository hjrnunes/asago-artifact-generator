"""M4 consumer cutover pins (VAL-CONS-015): detector design is downstream-owned
and the authority/trace chain references no producer admission record.

The retired producer path admitted oracle kinds before authoring and suppressed
candidates with no compilable kind. Under the cutover ownership the handoff
carries no admission record at all: every scenario is designed or
typed-excluded by the consumer alone, and the design's authority/trace chain
references the consumer design records plus the reused runtime/observer
capability (captured runtime context, deterministic predicates), never a
producer admission decision.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from asago_artifact_generator.cli import _design_manifest
from asago_artifact_generator.design.authoring import (
    DesignBrief,
    PreboundAuthor,
    design_artifact,
)
from asago_artifact_generator.design.compile import (
    compile_design,
    verify_frozen_artifact,
    write_design_outputs,
)
from asago_artifact_generator.garak.capabilities import garak_capabilities
from asago_artifact_generator.handoff.reader import load_scenario_handoff
from tests.design_fixtures import (
    REFUND_HANDOFF_PATH,
    klarna_runtime_context,
    klarna_target_profile,
    load_refund_payload,
    prebound_result,
)
from tests.test_artifact_design import (
    STIMULUS,
    _design_payload,
    _wrong_timing_payload,
)

#: Producer-admission reference markers. The retired producer seam admitted
#: oracle kinds (`admit_oracle_kinds`) before authoring; no consumer design
#: output may reference such a decision.
_ADMISSION_MARKERS = re.compile(
    r"admit_oracle|admitted_oracle|oracle_admission|producer_admission|admission_record",
    re.IGNORECASE,
)

_SRC = Path(__file__).resolve().parent.parent / "src" / "asago_artifact_generator"


def test_no_admission_scenario_receives_typed_exclusion(tmp_path: Path) -> None:
    """A scenario the retired producer path would have suppressed (no
    compilable oracle kind) still receives a consumer design outcome: a typed
    exclusion with the scenario kept visible, never an absent scenario."""
    outcome = _design_payload(tmp_path, _wrong_timing_payload())
    assert outcome.plan is None
    assert outcome.exclusion is not None
    assert outcome.exclusion.code == "unsupported-criterion-shape"
    paths = write_design_outputs(tmp_path, outcome)
    assert "artifact" not in paths
    record = json.loads(Path(paths["design_record"]).read_text(encoding="utf-8"))
    assert record["scenario_id"] == "SCN-034"
    assert record["compiled"] is False
    exclusion = json.loads(Path(paths["exclusion"]).read_text(encoding="utf-8"))
    assert exclusion["code"] == "unsupported-criterion-shape"
    assert exclusion["detail"]


def test_control_scenario_still_compiles(tmp_path: Path) -> None:
    """Control: the supported observation design still compiles to the
    runner-consumable executable artifact and freeze verification passes."""
    verified = load_scenario_handoff(REFUND_HANDOFF_PATH)
    outcome = design_artifact(
        verified,
        profile=klarna_target_profile(),
        runtime_context=klarna_runtime_context(),
        capabilities=garak_capabilities(),
        brief=DesignBrief(),
        author=PreboundAuthor(
            {
                "SCN-007": prebound_result(
                    STIMULUS,
                    100.0,
                    argument_values={"reason": "The lamp arrived broken"},
                )
            }
        ),
    )
    assert outcome.exclusion is None
    compiled = compile_design(outcome.plan)
    paths = write_design_outputs(tmp_path, outcome, compiled=compiled)
    assert paths["artifact"].endswith("executable-conversation.json")
    assert verify_frozen_artifact(tmp_path)["ok"] is True


def _manifest_entry(outcome: Any, paths: dict[str, str]) -> dict[str, Any]:
    return _design_manifest(outcome, paths, freeze_verification=None)


def _all_output_json(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.json"))
    }


def test_design_outputs_reference_no_producer_admission(tmp_path: Path) -> None:
    """The authority/trace chain of a compiled design and of a typed exclusion
    references consumer design records and the reused runtime/observer
    capability, never a producer admission record."""
    compiled_verified = load_scenario_handoff(REFUND_HANDOFF_PATH)
    compiled_outcome = design_artifact(
        compiled_verified,
        profile=klarna_target_profile(),
        runtime_context=klarna_runtime_context(),
        capabilities=garak_capabilities(),
        brief=DesignBrief(),
        author=PreboundAuthor(
            {
                "SCN-007": prebound_result(
                    STIMULUS,
                    100.0,
                    argument_values={"reason": "The lamp arrived broken"},
                )
            }
        ),
    )
    compiled = compile_design(compiled_outcome.plan)
    compiled_paths = write_design_outputs(tmp_path, compiled_outcome, compiled=compiled)

    excluded_outcome = _design_payload(tmp_path, _wrong_timing_payload())
    excluded_paths = write_design_outputs(tmp_path, excluded_outcome)

    outputs: dict[str, Any] = {}
    for root in (tmp_path,):
        outputs.update(_all_output_json(root))
    outputs["design-manifest.json"] = json.dumps(
        _manifest_entry(compiled_outcome, compiled_paths)
    ) + json.dumps(_manifest_entry(excluded_outcome, excluded_paths))

    hits = {
        name: sorted(set(_ADMISSION_MARKERS.findall(text)))
        for name, text in outputs.items()
        if _ADMISSION_MARKERS.search(text)
    }
    assert hits == {}

    # The compiled chain references the consumer design record and the reused
    # observer capability.
    record = json.loads(Path(compiled_paths["design_record"]).read_text(encoding="utf-8"))
    assert record["design_id"] == compiled_outcome.design_id
    assert record["handoff"]["content_digest"] == compiled_outcome.plan.handoff_digest
    assert record["stimulus"]["provenance"]["authored_by"] == "consumer-design"
    environment = record["environment"]
    assert environment["runtime_context_digest"]
    assert environment["state_digest"]
    assert record["detector"]["observation_level"] == "command"

    trace = json.loads(Path(compiled_paths["trace"]).read_text(encoding="utf-8"))
    source = trace["source"]
    assert source["design_id"] == compiled_outcome.design_id
    assert source["case_digest"] == compiled_outcome.plan.case_digest
    assert source["handoff_digest"] == compiled_outcome.plan.handoff_digest
    assert source["semantic_failure_criterion"]
    assert source["environment"]["runtime_context_digest"] == environment["runtime_context_digest"]
    oracle = json.loads(Path(compiled_paths["artifact"]).read_text(encoding="utf-8"))
    assert oracle["structured_oracle"]["kind"] == "tool_argument"

    # The excluded scenario's chain likewise references only consumer records.
    exclusion = json.loads(Path(excluded_paths["exclusion"]).read_text(encoding="utf-8"))
    assert exclusion["design_id"] == excluded_outcome.design_id
    assert exclusion["handoff_digest"]


def test_design_and_handoff_modules_carry_no_admission_seam() -> None:
    """Source-level regression pin (advisory per VAL-CUT-003): the design and
    handoff modules reference no retired producer admission symbol."""
    offenders: dict[str, str] = {}
    for directory in ("design", "handoff"):
        for path in sorted((_SRC / directory).glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if _ADMISSION_MARKERS.search(text):
                offenders[str(path.relative_to(_SRC))] = path.name
    assert offenders == {}


def test_handoff_schema_declares_no_admission_field() -> None:
    """The vendored handoff kit defines no producer-admission field: the
    consumer never receives an admission record to depend on."""
    kit_payload = load_refund_payload()
    assert _ADMISSION_MARKERS.search(json.dumps(kit_payload)) is None
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "contracts"
        / "scenario-handoff"
        / "handoff-v1"
        / "schema.json"
    )
    if schema_path.exists():
        assert _ADMISSION_MARKERS.search(schema_path.read_text(encoding="utf-8")) is None
