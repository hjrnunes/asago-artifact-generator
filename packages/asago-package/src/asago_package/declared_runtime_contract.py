"""Build a declared target's runtime contract from a shared base contract.

A declared target states its seed slots, and optionally its user-message
deliveries and limit overrides, instead of exposing a live endpoint. The
contract a consumer authors against is the shared base with those statements
applied, and the result must satisfy the runtime contract this library carries
and reads through its installed spec set.

The orchestrator runs this builder in the consumer's interpreter on a target's
declaration, so the module imports only the contract kit and the runtime-contract
spec.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .contract_kit import contract_dir, first_schema_error
from .runtime_contract_spec import VERSION, write_runtime_contract

PLANTED_ITEM = "planted_item"
RUNTIME_CONTRACT_VERSION = VERSION
RUNTIME_CONTRACT_WRITER = "asago_package.declared_runtime_contract"
RUNTIME_CONTRACT_WRITER_VERSION = "0.1.0"


class RuntimeContractViolation(ValueError):
    """A built contract fails the runtime-contract schema."""


@functools.cache
def _schema() -> dict[str, Any]:
    """Return the schema for the contract this builder emits."""

    path = contract_dir("runtime-contract") / RUNTIME_CONTRACT_VERSION / "schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _refuse_violation(contract: Mapping[str, Any]) -> None:
    """Refuse a built payload the schema rejects, naming where and why."""

    violation = first_schema_error(_schema(), contract)
    if violation is None:
        return
    raise RuntimeContractViolation(
        f"the runtime contract violates {RUNTIME_CONTRACT_VERSION}: {violation}"
    )


def declared_runtime_contract(
    base: Mapping[str, Any],
    *,
    seed_slots: Sequence[Mapping[str, Any]],
    delivery: Sequence[str] | None = None,
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return a declared target's runtime contract, built on a copy of ``base``.

    ``delivery`` replaces the base's deliveries when given. ``planted_item`` is
    never taken from either source: it is offered, last, exactly when at least
    one seed slot is declared. ``limits`` overrides single keys of the base's
    limits and keeps the rest. Each seed slot is already in the contract's form
    and is copied into a plain dict. Every other base key is carried over, and
    ``base`` is left unchanged.

    The contract is checked against the runtime-contract schema before it is
    returned: a target declaration that would build a contract no consumer can
    load raises :class:`RuntimeContractViolation` instead. The returned contract
    states ``artifact-runtime-contract-v3`` and carries the self-identifying
    envelope.
    """

    contract = {key: value for key, value in base.items()}
    contract["schema_version"] = RUNTIME_CONTRACT_VERSION
    chosen = list(delivery) if delivery is not None else base.get("delivery", [])
    offered = [item for item in chosen if item != PLANTED_ITEM]
    if seed_slots:
        offered.append(PLANTED_ITEM)
    contract["delivery"] = offered
    contract["limits"] = {**base.get("limits", {}), **(limits or {})}
    contract["seed_slots"] = [dict(slot) for slot in seed_slots]
    _refuse_violation(contract)
    return write_runtime_contract(
        contract,
        writer=RUNTIME_CONTRACT_WRITER,
        writer_version=RUNTIME_CONTRACT_WRITER_VERSION,
    )
