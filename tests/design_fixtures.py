"""Shared builders for handoff/design-path tests.

These fixtures are self-contained: the handoff comes from the vendored kit,
the target profile is an observed-MCP profile built over a fixed MiniKlarna
inventory, and the runtime context mirrors the caller-captured shape.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

from asago_artifact_generator.models._base import compute_framed_digest, sha256_bytes
from asago_artifact_generator.models.execution_classification import (
    McpInventoryObservation,
    McpToolObservation,
    TargetProfileOperation,
    TargetProfileResource,
    TargetSemanticInterpretation,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_DIR = REPO_ROOT / "contracts" / "scenario-handoff"
REFUND_HANDOFF_PATH = KIT_DIR / "handoff-v1" / "valid" / "adversarial-refund.json"
#: The vendored functional handoff (finding F): the concrete functional
#: acceptance case whose command-level criterion the existing observation
#: capability faithfully measures.
FUNCTIONAL_HANDOFF_PATH = KIT_DIR / "handoff-v1" / "valid" / "functional-no-attacker.json"

#: The real observed MiniKlarna ``process_refund`` schema (attempt-9 run
#: evidence, ``execution-target-profile.json``): two string arguments plus the
#: numeric amount.
PROCESS_REFUND_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"title": "Amount", "type": "number"},
        "order_id": {"title": "Order Id", "type": "string"},
        "reason": {"title": "Reason", "type": "string"},
    },
    "required": ["order_id", "amount", "reason"],
    "title": "process_refundArguments",
}
PROCESS_REFUND_DESCRIPTION = "Process a refund for an order."

KLARNA_INVENTORY: list[dict[str, Any]] = [
    {
        "name": "process_refund",
        "description": PROCESS_REFUND_DESCRIPTION,
        "input_schema": PROCESS_REFUND_SCHEMA,
    },
    {
        "name": "get_klarna_state_summary",
        "description": "Read-only summary of the current order and refund state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "retrieve_policy",
        "description": "Retrieve approved policy snippets that match the query.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"title": "Query", "type": "string"}},
            "required": ["query"],
        },
    },
]


def _observation(name: str, entry: dict[str, Any]) -> McpToolObservation:
    return McpToolObservation(
        name=name,
        source_observation_sha256=sha256_bytes(json.dumps(entry, sort_keys=True).encode()),
        description=entry["description"],
        input_schema=entry["input_schema"],
    )


def _observed_target_profile(target_id: str, inventory_entries: list[dict[str, Any]]) -> Any:
    """An observed MCP target profile closing exactly over the given inventory."""

    from asago_artifact_generator.models.execution_classification import ExecutionTargetProfile

    observations = tuple(_observation(entry["name"], entry) for entry in inventory_entries)
    inventory = McpInventoryObservation(
        target_id=target_id,
        authorization_scope_id="local-test",
        source_protocol="mcp",
        tools=observations,
    )
    resources = []
    interpretations = []
    for observation in observations:
        resource_id = f"mcp:{target_id}:{observation.name}"
        resources.append(
            TargetProfileResource(
                resource_id=resource_id,
                resource_kind="tool",
                target_id=target_id,
                tool_name=observation.name,
                description=observation.description,
                input_schema=observation.input_schema,
                attacker_influence="unknown",
                surfaces=["tool_call", "tool_result"],
                operations=[
                    TargetProfileOperation(
                        operation_id=observation.name,
                        semantic_operation=observation.name,
                        argument_names=observation.argument_names,
                    )
                ],
                evidence_refs=[f"inventory:tool:{observation.name}"],
            )
        )
        interpretations.append(
            TargetSemanticInterpretation(
                resource_id=resource_id,
                tool_name=observation.name,
                disposition="supported",
                evidence_refs=[f"inventory:tool:{observation.name}:input_schema"],
                rationale=(
                    "Observed MCP tool; interpretation records the observed schema only "
                    "and establishes no permission."
                ),
            )
        )
    return ExecutionTargetProfile(
        target_id=target_id,
        authorization_scope_id="local-test",
        basis="target",
        inventory_authority="observed",
        semantic_authority="inferred",
        inventory_completeness="observed_complete",
        source_protocol="mcp",
        source_inventory_digest=inventory.semantic_digest,
        discovery_provenance={
            "scanner_id": "test-fixture",
            "interpreter_id": "test-fixture",
            "verifier_id": "test-fixture",
        },
        inventory=inventory,
        resources=resources,
        interpretations=interpretations,
    )


def klarna_target_profile(refund_schema: dict[str, Any] | None = None) -> Any:
    """An observed MCP target profile closing exactly over the fixed inventory.

    ``refund_schema`` replaces the ``process_refund`` observed schema for tests
    that exercise schema-shape variants; the default is the real observed
    two-string-arg schema.
    """

    inventory_entries = copy.deepcopy(KLARNA_INVENTORY)
    if refund_schema is not None:
        for entry in inventory_entries:
            if entry["name"] == "process_refund":
                entry["input_schema"] = refund_schema
    return _observed_target_profile("mini-klarna", inventory_entries)


def klarna_runtime_context(
    *,
    record_id: str = "ORD-101",
    balance: float = 40.0,
    eligible: bool = True,
    customer_id: str = "CUST001",
    authenticated_customer_id: str | None = "CUST001",
    refund_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A caller-captured runtime context in the normalized shape.

    ``refund_schema`` must match the schema used to build the paired target
    profile so the runtime context's profile digest stays consistent.
    """

    order: dict[str, Any] = {
        "customer_id": customer_id,
        "item": "Desk lamp",
        "merchant": "Nordic Home",
        "original_price": balance,
        "refund_eligible": eligible,
        "remaining_to_pay": balance,
        "status": "open",
    }
    state: dict[str, Any] = {
        "audit_log": [],
        "orders": {record_id: order},
        "refunds": [],
    }
    if authenticated_customer_id is not None:
        state["authenticated_customer_id"] = authenticated_customer_id
    return {
        "target_profile_digest": klarna_target_profile(refund_schema).semantic_digest,
        "read_observation_diagnostics": [],
        "read_observation_input": {
            "authorization_scope_id": "local-test",
            "profile_digest": klarna_target_profile(refund_schema).semantic_digest,
        },
        "read_observations": [],
        "state": state,
    }


def prebound_result(
    stimulus_text: str,
    amount: float,
    *,
    history_turns: list[str] | None = None,
    rationale: str = "Prebound test stimulus.",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stimulus_text": stimulus_text,
        "requested_amount": amount,
        "rationale": rationale,
    }
    if history_turns is not None:
        result["history_turns"] = history_turns
    return result


def _rewrite_digest(payload: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(payload)
    payload.pop("content_digest", None)
    payload["content_digest"] = compute_framed_digest("scenario-handoff-v1", payload)
    return payload


def load_refund_payload() -> dict[str, Any]:
    return json.loads(REFUND_HANDOFF_PATH.read_text(encoding="utf-8"))


def mutated_handoff_json(tmp_path: Path, mutate: Any) -> Path:
    """Write a digest-consistent mutated copy of the kit refund handoff."""
    payload = _rewrite_digest(mutate(load_refund_payload()))
    path = tmp_path / "mutated-handoff.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_yaml_handoff(tmp_path: Path, payload: dict[str, Any]) -> Path:
    """Write one digest-consistent YAML handoff (producer run envelope shape)."""
    prepared = _rewrite_digest(payload)
    path = tmp_path / "SCN-007.yaml"
    path.write_text(yaml.safe_dump(prepared, sort_keys=True), encoding="utf-8")
    return path


#: The real observed MiniOcciAI ``commit_to_ehr`` schema (m3-occiai-attempt1
#: discovery, ``occiai-discovery-r2/execution-target-profile.json``): two
#: string arguments, no numeric argument.
COMMIT_TO_EHR_SCHEMA = {
    "type": "object",
    "properties": {
        "draft_id": {"title": "Draft Id", "type": "string"},
        "patient_id": {"title": "Patient Id", "type": "string"},
    },
    "required": ["patient_id", "draft_id"],
    "title": "commit_to_ehrArguments",
}
COMMIT_TO_EHR_DESCRIPTION = (
    "Commit a reviewed draft to the EHR. Unreviewed drafts are rejected in safe mode."
)

OCCIAI_INVENTORY: list[dict[str, Any]] = [
    {
        "name": "commit_to_ehr",
        "description": COMMIT_TO_EHR_DESCRIPTION,
        "input_schema": COMMIT_TO_EHR_SCHEMA,
    },
    {
        "name": "get_occiai_state_summary",
        "description": "JSON ledger: drafts, EHR records, commits, questionnaires, escalations.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "summarize_for_ehr",
        "description": "Store a draft EHR summary. This does not write the record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"title": "Patient Id", "type": "string"},
                "text": {"title": "Text", "type": "string"},
            },
            "required": ["patient_id", "text"],
            "title": "summarize_for_ehrArguments",
        },
    },
]


def occiai_target_profile() -> Any:
    """An observed MCP profile over the real MiniOcciAI tool observations."""

    return _observed_target_profile("mini-occiai", copy.deepcopy(OCCIAI_INVENTORY))


def occiai_runtime_context(
    *,
    drafts: list[dict[str, Any]] | None = None,
    authenticated_patient_id: str | None = "PAT-104",
) -> dict[str, Any]:
    """A caller-captured MiniOcciAI runtime context in the normalized shape.

    The state mirrors the real ``get_occiai_state_summary`` ledger; ``drafts``
    supplies the observed ``ehr_drafts`` records (empty in the seeded target
    until a summary draft is stored).
    """

    state: dict[str, Any] = {
        "session_id": "A53E21D0",
        "ehr_drafts": list(drafts) if drafts is not None else [],
        "ehr_records": {"PAT-101": [], "PAT-104": [], "PAT-201": []},
        "commits": [],
        "questionnaires": [],
        "escalations": [],
        "audit_log": [],
    }
    if authenticated_patient_id is not None:
        state["authenticated_patient_id"] = authenticated_patient_id
    return {
        "target_profile_digest": occiai_target_profile().semantic_digest,
        "state": state,
    }
