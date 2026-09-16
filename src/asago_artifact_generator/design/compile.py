"""Compile supported designs to the Garak-runner-consumable artifacts.

The compiled pair keeps the runner's executable-conversation shape (messages,
tools, structured oracle, conversation scope, semantic digest) while the plan
carries design-path provenance: handoff digest, source-scenario version,
environment binding, fidelity and the freeze record. Compilation closes the
case, trace, plan and freeze record to one another.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..garak.plan import load_execution_plan
from ..models._base import canonical_json_bytes, compute_framed_digest, sha256_bytes
from ..platforms.base import ArtifactValidationError, CompiledArtifact
from .records import (
    DESIGN_PLAN_SCHEMA_VERSION,
    DESIGN_TRACE_SCHEMA_VERSION,
    ArtifactDesignPlan,
)

CONVERSATION_SCHEMA_VERSION = "asago-executable-conversation-v2"
CONVERSATION_COMPILER_VERSION = "garak-conversation-compiler-v2"
_DESIGN_PLAN_FIELD_CHECKS = (
    "kind",
    "tool_name",
    "field_path",
    "comparison",
    "expected",
)


class FreezeVerificationError(ValueError):
    """Freeze verification failed with a typed reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _structured_oracle(plan: ArtifactDesignPlan) -> dict[str, Any]:
    lineage = plan.lineage
    return {
        "kind": "tool_argument",
        "condition_ref": "DESIGN-CRITERION-1",
        "condition_type": "parameter_limit",
        "control_action_id": lineage.get("control_action_id", ""),
        "semantic_property": plan.detector.field_path.removeprefix("arguments."),
        "comparison": plan.detector.comparison,
        "expected": plan.detector.expected,
        "semantic_proposition": None,
        "hazard_refs": list(lineage.get("hazard_ids", [])),
        "constraint_refs": list(lineage.get("constraint_ids", [])),
        "loss_refs": list(lineage.get("loss_ids", [])),
        "tool_name": plan.detector.tool_name,
        "field_path": plan.detector.field_path,
    }


def _conversation_body(plan: ArtifactDesignPlan) -> dict[str, Any]:
    messages = [{"role": "user", "content": turn.text} for turn in plan.stimulus.turns]
    roles = list(dict.fromkeys(message["role"] for message in messages))
    body: dict[str, Any] = {
        "schema_version": CONVERSATION_SCHEMA_VERSION,
        "case_id": plan.case_id,
        "profile": {
            "delivery_class": plan.stimulus.delivery_class,
            "interaction_mode": ("multi_turn" if len(messages) > 1 else "single_turn"),
            "message_roles": roles,
            "uses_tools": bool(plan.tool_declarations),
            "target_response_mode": "tool_call",
        },
        "messages": messages,
        "tools": [dict(declaration) for declaration in plan.tool_declarations],
        "tool_choice": "auto",
        "tool_choice_mode": "automatic",
        "tool_choice_reason": None,
        "judge_description": plan.judge_description,
        "structured_oracle": _structured_oracle(plan),
        "source": {
            "scenario_id": plan.scenario_id,
            "scenario_version": plan.scenario_version,
            "handoff_schema_version": plan.handoff_schema_version,
            "handoff_digest": plan.handoff_digest,
            "design_id": plan.design_id,
            "case_id": plan.case_id,
            "case_digest": plan.case_digest,
            "lineage": dict(plan.lineage),
            "semantic_failure_criterion": plan.semantic_failure_criterion,
            "safe_alternative": plan.safe_alternative,
            "criterion_shape": dict(plan.criterion_shape),
            "semantic_assessment": dict(plan.semantic_assessment),
            "environment": plan.environment.model_dump(mode="json"),
        },
        # Keep the target-facing context contract beside the frozen source
        # metadata.  The target sees only the selected operation and explicit
        # safe lookup routes; designer-only state remains labelled as such.
        "target_context": dict(plan.target_context),
        "binding": {
            "environment_id": plan.environment.profile_id,
            "environment_digest": plan.environment.profile_digest,
        },
        "author": dict(plan.stimulus.provenance),
        "compiler_version": CONVERSATION_COMPILER_VERSION,
        "frozen": dict(plan.freeze),
    }
    if plan.stimulus.delivery_class == "conversation_context":
        body["supplied_history"] = {
            "kind": "user_only",
            "user_turns": [
                {"turn_id": turn.turn_id, "text": turn.text} for turn in plan.stimulus.turns
            ],
        }
        body["turn_texts_verbatim"] = True
    return body


def validate_design_case(
    case: Mapping[str, Any],
    plan: ArtifactDesignPlan,
    trace: Mapping[str, Any] | None = None,
) -> list[str]:
    """Close a compiled design case to its plan authority."""

    errors: list[str] = []
    if case.get("schema_version") != CONVERSATION_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CONVERSATION_SCHEMA_VERSION}")
    if case.get("case_id") != plan.case_id:
        errors.append("case_id differs from the design plan authority")
    messages = case.get("messages")
    expected_messages = [{"role": "user", "content": turn.text} for turn in plan.stimulus.turns]
    if messages != expected_messages:
        errors.append(
            "messages differ from the designed stimulus; compiled content must be "
            "the frozen consumer design, not regenerated text"
        )
    if case.get("tools") != [dict(item) for item in plan.tool_declarations]:
        errors.append("tools differ from the design plan authority")
    if case.get("target_context") != dict(plan.target_context):
        errors.append("target_context differs from the design plan authority")
    if case.get("tool_choice") != "auto":
        errors.append("tool_choice must be auto for the designed conversation")
    oracle = case.get("structured_oracle")
    expected_oracle = _structured_oracle(plan)
    if not isinstance(oracle, Mapping):
        errors.append("structured_oracle must be an object")
    else:
        for field_name in _DESIGN_PLAN_FIELD_CHECKS:
            if oracle.get(field_name) != expected_oracle[field_name]:
                errors.append(
                    f"structured_oracle {field_name} differs from the design plan authority"
                )
        for refs in ("hazard_refs", "constraint_refs", "loss_refs"):
            if list(oracle.get(refs, [])) != list(expected_oracle[refs]):
                errors.append(f"structured_oracle {refs} differ from the design plan lineage")
    if case.get("frozen") != dict(plan.freeze):
        errors.append("frozen block differs from the design plan freeze authority")
    source = case.get("source")
    if not isinstance(source, Mapping):
        errors.append("source must be an object")
    else:
        if source.get("scenario_id") != plan.scenario_id:
            errors.append("source scenario identity differs from the design plan")
        if source.get("handoff_digest") != plan.handoff_digest:
            errors.append("source handoff digest differs from the design plan")
        if source.get("handoff_schema_version") != plan.handoff_schema_version:
            errors.append("source handoff schema version differs from the design plan")
        if source.get("criterion_shape") != dict(plan.criterion_shape):
            errors.append("source criterion shape differs from the design plan")
        if source.get("semantic_assessment") != dict(plan.semantic_assessment):
            errors.append("source semantic assessment differs from the design plan")
    scope = plan.conversation_scope
    if scope.get("history_kind") != "user_only" or scope.get("continuation_count") != 1:
        errors.append("the design plan must record user-only history with one continuation")
    if (
        not isinstance(messages, list)
        or not messages
        or any(
            not isinstance(message, Mapping) or message.get("role") != "user"
            for message in messages
        )
    ):
        errors.append("designed history must be non-empty and user-only")
    if plan.stimulus.delivery_class == "conversation_context":
        history = case.get("supplied_history")
        expected_history = {
            "kind": "user_only",
            "user_turns": [
                {"turn_id": turn.turn_id, "text": turn.text} for turn in plan.stimulus.turns
            ],
        }
        if history != expected_history or case.get("turn_texts_verbatim") is not True:
            errors.append("supplied_history must record the designed user-only turns")
    else:
        if "supplied_history" in case:
            errors.append("a direct-prompt design must not record supplied_history")
    if case.get("judge_description") != plan.judge_description:
        errors.append("judge_description differs from the design plan authority")
    if trace is not None:
        errors.extend(_design_trace_errors(trace, plan, case))
    return errors


def _design_trace_errors(
    trace: Mapping[str, Any],
    plan: ArtifactDesignPlan,
    case: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    if trace.get("schema_version") != DESIGN_TRACE_SCHEMA_VERSION:
        errors.append(f"trace schema_version must be {DESIGN_TRACE_SCHEMA_VERSION}")
    trace_digest = trace.get("trace_digest")
    trace_body = {key: value for key, value in trace.items() if key != "trace_digest"}
    if trace_digest != compute_framed_digest(DESIGN_TRACE_SCHEMA_VERSION, trace_body):
        errors.append("trace digest does not match trace content")
    if trace.get("source") != case.get("source"):
        errors.append("trace source differs from artifact authority")
    if trace.get("frozen_content_digest") != plan.freeze.get("frozen_content_digest"):
        errors.append("trace frozen digest differs from the design plan freeze authority")
    return errors


def compile_design(
    plan: ArtifactDesignPlan,
    *,
    authoring: Mapping[str, Any] | None = None,
) -> CompiledArtifact:
    """Compile one supported design to the runner-consumable pair.

    ``authoring`` is the live authoring attempt evidence (every attempt with
    its raw response plus the authoring call count); when supplied it is
    persisted in the design trace beside the digest chain.
    """

    if not isinstance(plan, ArtifactDesignPlan):
        raise TypeError("design compilation requires an ArtifactDesignPlan")
    body = _conversation_body(plan)
    semantic_digest = compute_framed_digest(CONVERSATION_SCHEMA_VERSION, body)
    artifact = {**body, "semantic_digest": semantic_digest}
    errors = validate_design_case(artifact, plan)
    if errors:
        raise ArtifactValidationError("; ".join(errors))
    trace_body: dict[str, Any] = {
        "schema_version": DESIGN_TRACE_SCHEMA_VERSION,
        "source": body["source"],
        "binding": body["binding"],
        "stimulus_ids": [turn.turn_id for turn in plan.stimulus.turns],
        "condition_ref": body["structured_oracle"]["condition_ref"],
        "hazard_refs": body["structured_oracle"]["hazard_refs"],
        "constraint_refs": body["structured_oracle"]["constraint_refs"],
        "loss_refs": body["structured_oracle"]["loss_refs"],
        "author_result_digest": plan.stimulus.provenance.get("result_digest"),
        "frozen_content_digest": plan.freeze.get("frozen_content_digest"),
        "conversation_scope": dict(plan.conversation_scope),
    }
    if authoring:
        trace_body["authoring"] = dict(authoring)
    trace = {
        **trace_body,
        "trace_digest": compute_framed_digest(DESIGN_TRACE_SCHEMA_VERSION, trace_body),
    }
    validation = {
        "schema_version": "artifact-design-validation-v1",
        "ok": True,
        "errors": [],
        "checks": {
            "handoff_digest_verified": True,
            "design_plan_digest_verified": True,
            "freeze_verified": True,
            "conversation_scope": "user_only_single_continuation",
        },
    }
    return CompiledArtifact(
        platform=plan.platform,
        artifact=artifact,
        artifact_digest=sha256_bytes(canonical_json_bytes(artifact)),
        trace=trace,
        validation=validation,
    )


def write_design_outputs(
    root: Path,
    outcome: Any,
    *,
    compiled: CompiledArtifact | None = None,
) -> dict[str, str]:
    """Persist one design outcome's sidecars atomically, manifest aside.

    Blocked designs keep their design record and typed exclusion; compiled
    designs additionally publish the freeze record, plan, executable
    conversation, trace and validation sidecars.
    """

    from ..output import atomic_write_json
    from .records import DesignOutcome

    if not isinstance(outcome, DesignOutcome):
        raise TypeError("design outputs require a DesignOutcome")
    entry_dir = Path(root) / outcome.design_id
    paths: dict[str, str] = {
        "design_record": str(
            atomic_write_json(entry_dir / "design-record.json", outcome.design_record)
        )
    }
    if outcome.exclusion is not None:
        paths["exclusion"] = str(
            atomic_write_json(
                entry_dir / "design-exclusion.json",
                outcome.exclusion.model_dump(mode="json"),
            )
        )
        return paths
    assert outcome.plan is not None and outcome.freeze is not None
    paths["freeze"] = str(
        atomic_write_json(
            entry_dir / "frozen-artifact.json", outcome.freeze.model_dump(mode="json")
        )
    )
    paths["plan"] = str(
        atomic_write_json(entry_dir / "execution-plan.json", outcome.plan.model_dump(mode="json"))
    )
    if compiled is None:
        return paths
    paths["artifact"] = str(
        atomic_write_json(entry_dir / "executable-conversation.json", dict(compiled.artifact))
    )
    paths["trace"] = str(
        atomic_write_json(entry_dir / "artifact-trace.json", dict(compiled.trace))
    )
    paths["validation"] = str(
        atomic_write_json(entry_dir / "validation.json", dict(compiled.validation))
    )
    return paths


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeVerificationError("frozen_record_missing", str(exc)) from exc
    if not isinstance(value, dict):
        raise FreezeVerificationError("frozen_record_missing", f"{path.name} is not an object")
    return value


def verify_frozen_artifact(root: Path) -> dict[str, Any]:
    """Verify every frozen record under one design run root.

    Recomputes the frozen-content digest, then closes the compiled artifact
    and the plan to the frozen record: the compiled stimulus must be the
    frozen bytes, and the receipts must reference the frozen digest. Tampering
    with the frozen record's text fails with ``frozen_content_mismatch``.
    """

    root = Path(root)
    frozen_paths = sorted(root.glob("*/frozen-artifact.json"))
    if not frozen_paths:
        raise FreezeVerificationError(
            "frozen_record_missing", f"no frozen-artifact.json under {root}"
        )
    for frozen_path in frozen_paths:
        entry_dir = frozen_path.parent
        frozen = _read_json(frozen_path)
        content = frozen.get("frozen_content")
        digest = frozen.get("frozen_content_digest")
        if not isinstance(content, Mapping) or not isinstance(digest, str):
            raise FreezeVerificationError(
                "frozen_content_mismatch", f"{frozen_path} has no verifiable frozen content"
            )
        recomputed = compute_framed_digest("artifact-freeze-v1", content)
        if digest != recomputed:
            raise FreezeVerificationError(
                "frozen_content_mismatch",
                f"{frozen_path}: recorded digest {digest!r} does not match recomputed "
                f"{recomputed!r}",
            )
        artifact = _read_json(entry_dir / "executable-conversation.json")
        frozen_block = artifact.get("frozen")
        if (
            not isinstance(frozen_block, Mapping)
            or frozen_block.get("frozen_content_digest") != digest
        ):
            raise FreezeVerificationError(
                "frozen_content_mismatch",
                f"{entry_dir}: the compiled artifact does not reference the frozen digest",
            )
        expected_messages = [
            {"role": "user", "content": turn["text"]} for turn in content["stimulus"]["turns"]
        ]
        if artifact.get("messages") != expected_messages:
            raise FreezeVerificationError(
                "frozen_content_mismatch",
                f"{entry_dir}: compiled messages differ from the frozen stimulus text",
            )
        plan_path = entry_dir / "execution-plan.json"
        plan = load_execution_plan(plan_path)
        if plan.schema_version != DESIGN_PLAN_SCHEMA_VERSION:
            raise FreezeVerificationError(
                "frozen_content_mismatch",
                f"{plan_path}: unexpected plan schema {plan.schema_version!r}",
            )
        if dict(plan.freeze) != {
            "frozen_content_digest": digest,
            "source_scenario_id": frozen["source_scenario_id"],
            "source_scenario_version": frozen["source_scenario_version"],
        }:
            raise FreezeVerificationError(
                "frozen_content_mismatch",
                f"{plan_path}: plan freeze authority differs from the frozen record",
            )
        if plan.frozen_content_digest and plan.frozen_content_digest != digest:
            raise FreezeVerificationError(
                "frozen_content_mismatch",
                f"{plan_path}: plan frozen_content_digest {plan.frozen_content_digest!r} "
                f"differs from the frozen record digest {digest!r}",
            )
    return {"ok": True}


__all__ = [
    "CONVERSATION_COMPILER_VERSION",
    "CONVERSATION_SCHEMA_VERSION",
    "FreezeVerificationError",
    "compile_design",
    "validate_design_case",
    "verify_frozen_artifact",
    "write_design_outputs",
]
