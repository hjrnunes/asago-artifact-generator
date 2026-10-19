"""The suite environment: the domain's ledger from the MiDojo base, with the seed planted.

mini-agents exports a base file (``mini-agents-midojo-base-v1``) holding, per
domain, the ledger MiDojo serves as the suite ``environment``, the seed slots
(the ledger fields a seed may write) and the tool access. A package's seed
becomes MiDojo probes: each seed item is one probe, and the ledger field the
item targets holds the probe's placeholder, which MiDojo replaces with the
probe's payload when it loads the evaluation.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from asago_bundle_core.errors import BundleError

BASE_SCHEMA_VERSION = "mini-agents-midojo-base-v1"
SEED_SCHEMA_VERSIONS = frozenset({"artifact-seed-v1", "mini-agents-seed-v1"})
INJECTION_TASK_ID = "asago_injection_task"
PROBE_PREFIX = "seed_"
REPLACE = "replace"
APPEND = "append"


class EnvironmentFault(BundleError):
    """The base or the seed cannot make a suite environment."""


def placeholder(index: int) -> str:
    """Return the text MiDojo replaces with probe ``seed_<index>``'s payload."""

    return f"{{{INJECTION_TASK_ID}:{PROBE_PREFIX}{index}}}"


def base_target(base: Any, domain: str) -> Mapping[str, Any]:
    """Return the domain's entry of a base document, or raise."""

    if not isinstance(base, dict) or base.get("schema_version") != BASE_SCHEMA_VERSION:
        raise EnvironmentFault(f"the MiDojo base is not {BASE_SCHEMA_VERSION}")
    targets = base.get("targets")
    target = targets.get(domain) if isinstance(targets, dict) else None
    if not isinstance(target, dict):
        raise EnvironmentFault(f"the MiDojo base has no domain {domain!r}")
    return target


def suite_environment(
    target: Mapping[str, Any], seed: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Return the environment (a copy, placeholders written) and the probes of the seed."""

    environment = copy.deepcopy(target["environment"])
    probes: dict[str, dict[str, str]] = {}
    if seed is None:
        return environment, probes
    if not isinstance(seed, Mapping) or seed.get("schema_version") not in SEED_SCHEMA_VERSIONS:
        raise EnvironmentFault(f"the seed is not one of {sorted(SEED_SCHEMA_VERSIONS)}")
    items = seed.get("items")
    if not isinstance(items, list) or not items:
        raise EnvironmentFault("the seed has no items")
    slots = {slot["id"]: slot for slot in target.get("seed_slots", [])}
    for index, item in enumerate(items):
        _plant(environment, slots, item, index)
        probes[f"{PROBE_PREFIX}{index}"] = {
            "payload": item["content"],
            "attack_type": "verbatim",
        }
    return environment, probes


def _plant(
    environment: dict[str, Any], slots: Mapping[str, Mapping[str, Any]], item: Any, index: int
) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("content"), str):
        raise EnvironmentFault(f"seed item {index} has no content text")
    slot = slots.get(item.get("slot"))  # type: ignore[arg-type]
    if slot is None:
        raise EnvironmentFault(f"seed item {index}: unknown slot {item.get('slot')!r}")
    record_ref = item.get("record_ref")
    if record_ref not in slot["records"]:
        raise EnvironmentFault(
            f"seed item {index}: record {record_ref!r} is not a record of slot {slot['id']}"
        )
    operation = item.get("mode", item.get("operation"))
    if operation not in (REPLACE, APPEND):
        raise EnvironmentFault(f"seed item {index}: unknown operation {operation!r}")
    try:
        record = environment[slot["container"]][record_ref]
        current = record[slot["attribute"]]
    except (KeyError, TypeError) as exc:
        raise EnvironmentFault(
            f"seed item {index}: the base environment has no {slot['container']}"
            f"[{record_ref!r}][{slot['attribute']!r}]"
        ) from exc
    text = placeholder(index)
    record[slot["attribute"]] = text if operation == REPLACE else f"{current} {text}"
