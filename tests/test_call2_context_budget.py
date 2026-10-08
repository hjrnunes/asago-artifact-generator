from __future__ import annotations

import json

from asago_artifact_generator.authoring.context_budget import _context_budget_estimate
from asago_artifact_generator.authoring.prompt_context import build_artifact_author_context
from asago_artifact_generator.authoring.prompt_packets import build_call2_packet_v2

from .support import world_builders

_inventory, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "plan", "runtime_contract", "view"
)

_SOURCE_TITLE = "ORIGINAL SCENARIO AND SOURCE CONTEXT"


def _section(user: str, title: str) -> str:
    lines = user.split("\n")
    start = lines.index(title) + 1
    body: list[str] = []
    for line in lines[start:]:
        if line == "":
            break
        body.append(line)
    return "\n".join(body)


def test_call2_renders_source_context_as_compact_json_with_every_value() -> None:
    packet = build_call2_packet_v2(_view(), _plan(), _inventory(), _runtime_contract())
    context = build_artifact_author_context(_view(), _plan(), _inventory(), _runtime_contract())

    body = _section(packet.user, _SOURCE_TITLE)

    assert "\n" not in body
    assert json.loads(body) == {
        "scenario": context["original_scenario"],
        "authoritative_context": context["authoritative_context"],
    }
    assert body == json.dumps(
        json.loads(body), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def test_call2_source_context_costs_no_indentation_tokens() -> None:
    inventory = _inventory()
    inventory["facts"].extend(
        {
            "ref": f"state:record_{index}",
            "value": {"owner": f"OWN{index:03d}", "status": "open", "notes": ["a", "b"]},
            "schema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
            "provenance": "seeded state",
        }
        for index in range(60)
    )
    plan = _plan()
    packet = build_call2_packet_v2(_view(), plan, inventory, _runtime_contract())
    context = build_artifact_author_context(_view(), plan, inventory, _runtime_contract())
    source = {
        "scenario": context["original_scenario"],
        "authoritative_context": context["authoritative_context"],
    }
    indented = json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True)

    assert indented not in packet.user
    compact_bytes = len(_section(packet.user, _SOURCE_TITLE).encode("utf-8"))
    assert compact_bytes < len(indented.encode("utf-8"))
    assert _context_budget_estimate(packet)["estimated_prompt_tokens"] > 0
