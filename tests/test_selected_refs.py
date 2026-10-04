from __future__ import annotations

from asago_artifact_generator.authoring.inventory import _selected_refs


def _inventory() -> dict:
    return {
        "operations": [{"name": "lookup"}, {"name": "refund"}, {"name": ""}, "not an operation"],
        "facts": [{"ref": "order:1"}, {"ref": "order:2"}, {}],
    }


def test_selected_evidence_refs_are_classified_by_inventory_kind() -> None:
    plan = {
        "selected_evidence": [
            {"ref": "lookup"},
            {"ref": "operation:refund"},
            {"ref": "operation:missing"},
            {"ref": "order:1"},
            {"ref": "source:case"},
            "not an item",
        ]
    }

    assert _selected_refs(plan, _inventory()) == {
        "operations": {"lookup", "refund"},
        "facts": {"order:1"},
        "sources": {"operation:missing", "source:case", ""},
    }


def test_runtime_binding_sources_add_setup_operations_and_facts() -> None:
    plan = {
        "runtime_bindings": [
            {"source_ref": "setup:lookup"},
            {"source_ref": "facts:order:2"},
            {"source_ref": "other:value"},
            {},
            "not a binding",
        ]
    }

    assert _selected_refs(plan, _inventory()) == {
        "operations": {"lookup"},
        "facts": {"order:2"},
        "sources": set(),
    }
