from __future__ import annotations

import copy
import dataclasses
import json

from asago_artifact_generator.authoring.prompt_context import build_plan_author_context
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2

from .support import json_section, world_builders

_inventory, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "runtime_contract", "view"
)


def _view_with(**changes):
    view = _view()
    payload = copy.deepcopy(view.payload)
    for key, value in changes.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return dataclasses.replace(view, payload=payload)


def test_call1_projects_kind_attack_path_and_unavailable_classification() -> None:
    packet = build_call1_packet_v2(_view(), _inventory(), _runtime_contract())

    user = packet.user
    assert user.startswith("TASK\n")
    assert user.index("\nSCENARIO DESIGN\n") < user.index("\nSOURCE CONTEXT\n")
    design = json_section(user, "SCENARIO DESIGN")
    assert design["kind"] == "adversarial"
    assert "The message is the attack itself" in design["stimulus_rule"]
    task = json_section(user, "TASK")
    assert task["scenario"]["classification"]["status"] == "unavailable"
    assert "Do not infer" in task["scenario"]["classification"]["reason"]
    path = design["attack_path"]
    assert "proposed hypothesis" in path["note"]
    assert (
        "  - AP-ASM-1 (attacker_assumption): The session has one open patient chart."
        in (path["outline"])
    )
    context = build_plan_author_context(_view(), _inventory(), _runtime_contract())
    assert context["scenario_design"]["attack_path"]["outline"] == path["outline"]


def test_functional_and_missing_kinds_get_their_own_stimulus_rules() -> None:
    functional = json_section(
        build_call1_packet_v2(
            _view_with(kind="functional"), _inventory(), _runtime_contract()
        ).user,
        "SCENARIO DESIGN",
    )
    missing = json_section(
        build_call1_packet_v2(_view_with(kind=None), _inventory(), _runtime_contract()).user,
        "SCENARIO DESIGN",
    )

    assert functional["kind"] == "functional"
    assert "legitimate user" in functional["stimulus_rule"]
    assert "no deception" in functional["stimulus_rule"]
    assert missing["kind"] == "unavailable"
    assert "unresolved_requirements" in missing["stimulus_rule"]


def test_structured_classification_is_copied_when_supplied() -> None:
    classification = {"family": "refund", "test_class": "timing", "adversary": "customer"}

    assert (
        json_section(
            build_call1_packet_v2(
                _view_with(classification=classification), _inventory(), _runtime_contract()
            ).user,
            "TASK",
        )["scenario"]["classification"]
        == classification
    )


def test_current_call1_lists_operations_once() -> None:
    packet = build_call1_packet_v2(_view(), _inventory(), _runtime_contract())

    capabilities = json_section(packet.user, "EXECUTION CAPABILITIES")
    assert "SOURCE CONTEXT operations" in capabilities["available_operations"]
    assert "Return a draft and its status." not in json.dumps(capabilities)
