from __future__ import annotations

import json

from asago_artifact_generator.authoring.context_budget import _context_budget_estimate
from asago_artifact_generator.authoring.prompt_packets import build_call2_packet_v2
from tests.test_versioned_prompt_roles import _inventory, _plan, _runtime_contract, _view

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

    body = _section(packet.user, _SOURCE_TITLE)

    assert "\n" not in body
    assert json.loads(body) == {
        "scenario": packet.payload["original_scenario"],
        "authoritative_context": packet.payload["authoritative_context"],
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
    source = {
        "scenario": packet.payload["original_scenario"],
        "authoritative_context": packet.payload["authoritative_context"],
    }
    indented = json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True)

    assert indented not in packet.user
    compact_bytes = len(_section(packet.user, _SOURCE_TITLE).encode("utf-8"))
    assert compact_bytes < len(indented.encode("utf-8"))
    assert _context_budget_estimate(packet)["estimated_prompt_tokens"] > 0
