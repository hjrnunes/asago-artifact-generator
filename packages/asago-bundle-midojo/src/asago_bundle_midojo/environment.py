"""The episode environment: the domain's ledger from the MiDojo base, with the seed planted.

mini-agents exports a base file (``mini-agents-midojo-base-v1``) holding, per
domain, the ledger MiDojo serves as the episode's ``environment``, the seed slots
(the ledger fields a seed may write, by the runtime contract's slot id) and the tool
access. Each item of a package's seed becomes one injection instruction
(``episode.template_instruction``) at the path of the ledger field its slot names, and
that field holds the item's text: ``replace`` swaps the field's text for it, ``append``
adds it after the field's text and one space. Items apply in order.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from asago_bundle_core.errors import BundleError

from .episode import probe_key, template_instruction

BASE_SCHEMA_VERSION = "mini-agents-midojo-base-v1"
SEED_SCHEMA_VERSIONS = frozenset({"artifact-seed-v1", "mini-agents-seed-v1"})
REPLACE = "replace"
APPEND = "append"


class EnvironmentFault(BundleError):
    """The base or the seed cannot make an episode environment."""


def base_target(base: Any, domain: str) -> Mapping[str, Any]:
    """Return the domain's entry of a base document, or raise."""

    if not isinstance(base, dict) or base.get("schema_version") != BASE_SCHEMA_VERSION:
        raise EnvironmentFault(f"the MiDojo base is not {BASE_SCHEMA_VERSION}")
    targets = base.get("targets")
    target = targets.get(domain) if isinstance(targets, dict) else None
    if not isinstance(target, dict):
        raise EnvironmentFault(f"the MiDojo base has no domain {domain!r}")
    return target


def planted_environment(
    target: Mapping[str, Any], seed: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the environment (a copy, the seed planted) and one instruction per seed item."""

    environment = copy.deepcopy(target["environment"])
    if not isinstance(seed, Mapping) or seed.get("schema_version") not in SEED_SCHEMA_VERSIONS:
        raise EnvironmentFault(f"the seed is not one of {sorted(SEED_SCHEMA_VERSIONS)}")
    items = seed.get("items")
    if not isinstance(items, list) or not items:
        raise EnvironmentFault("the seed has no items")
    slots = {slot["id"]: slot for slot in target.get("seed_slots", [])}
    instructions = []
    for index, item in enumerate(items):
        path = _plant(environment, slots, item, index)
        instructions.append(template_instruction(path, probe_key(index), item["content"]))
    return environment, instructions


def _plant(
    environment: dict[str, Any], slots: Mapping[str, Mapping[str, Any]], item: Any, index: int
) -> list[str]:
    """Write the item's text into its field and return the field's path."""

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
    operation = item.get("mode")
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
    if not isinstance(current, str):
        raise EnvironmentFault(
            f"seed item {index}: {slot['container']}[{record_ref!r}][{slot['attribute']!r}] "
            "is not text"
        )
    text = item["content"]
    record[slot["attribute"]] = text if operation == REPLACE else f"{current} {text}"
    return [slot["container"], record_ref, slot["attribute"]]
