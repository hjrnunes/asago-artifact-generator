"""Runtime-binding and keyed-order builders shared by the binding-repair tests."""

from __future__ import annotations

from .support import world_builders

(_inventory,) = world_builders("ehr", "inventory")


def runtime_binding(source_ref: str, selector: str = "value", **changes: object) -> dict:
    return {
        "name": "record_value",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": source_ref,
        "selector": selector,
        "consumers": ["stimulus.user_text"],
        "on_missing": "stop",
        **changes,
    }


ORDER_FIELDS = ("customer_id", "item", "status")


def keyed_orders_inventory() -> dict:
    """Return keyed order facts whose full selector list exceeds the repair cap.

    ORD-201 sorts after fourteen other orders, so a collection-wide
    enumeration capped at 40 selectors never reaches it.
    """

    keys = [f"ORD-1{index:02d}" for index in range(1, 15)] + ["ORD-201"]
    inventory = _inventory()
    inventory["facts"].extend(
        [
            {
                "ref": "state:orders",
                "value": {
                    key: {field: f"{key}-{field}" for field in ORDER_FIELDS} for key in keys
                },
                "schema": {
                    "type": "object",
                    "properties": {
                        key: {
                            "type": "object",
                            "properties": {field: {"type": "string"} for field in ORDER_FIELDS},
                        }
                        for key in keys
                    },
                },
            },
            {
                "ref": "state:orders:records",
                "value": {key: {"record_key": key} for key in keys},
                "schema": {
                    "type": "object",
                    "properties": {
                        key: {"type": "object", "properties": {"record_key": {"type": "string"}}}
                        for key in keys
                    },
                },
            },
        ]
    )
    return inventory


def named_record_binding(source_ref: str, selector: str) -> dict:
    return runtime_binding(
        source_ref,
        selector,
        name="target_order_id",
        consumers=["setup.arguments.target_order_id"],
    )


NAMED_ORDER_SOURCES = [
    {
        "source_kind": "supplied_input",
        "source_ref": "facts:state:orders",
        "source_schema_type": "object",
        "documented_selectors": {
            "value.ORD-201": "object",
            "value.ORD-201.customer_id": "string",
            "value.ORD-201.item": "string",
            "value.ORD-201.status": "string",
        },
        "matching_expected_type": [
            "value.ORD-201.customer_id",
            "value.ORD-201.item",
            "value.ORD-201.status",
        ],
        "truncated": False,
    },
    {
        "source_kind": "supplied_input",
        "source_ref": "facts:state:orders:records",
        "source_schema_type": "object",
        "documented_selectors": {
            "value.ORD-201": "object",
            "value.ORD-201.record_key": "string",
        },
        "matching_expected_type": ["value.ORD-201.record_key"],
        "truncated": False,
    },
]
