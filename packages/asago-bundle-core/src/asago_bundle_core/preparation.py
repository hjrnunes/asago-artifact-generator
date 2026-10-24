"""Prepare a package for one run: setup, bindings, prerequisites, stimulus and judge facts.

``prepare_package`` is what an adapter's public ``instantiate`` runs (the Garak
adapter's ``entry_points.instantiate``) before it hands the rendered stimulus and
the judge's facts to its lower-level template filler, which takes them already
resolved. It runs the package's setup operations through a target call (the
caller's own, or the adapter's MCP call to the target), validates and resolves the
package's bindings, checks its prerequisites, fills the binding values into the
stimulus, and resolves the facts the judge reads.

Setup is preparation, not graded: each operation calls the target directly and
makes a real write, which the returned setup ledger records. The result is plain
data; recording it in a receipt is the caller's job.

A package that cannot be prepared returns ``PreparationIncomplete`` with one of
``INCOMPLETE_REASONS``; nothing after the failed step runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from asago_artifact_generator.binding_rules import (
    BindingError,
    RuntimeBinding,
    stimulus_history,
    validate_binding_declarations,
)
from asago_artifact_generator.package_io import ArtifactPackage

from .bindings import resolve_bindings, select_value, substitute_slots
from .errors import BundleError

# The call a setup operation makes: (operation, arguments) -> the target's result object.
TargetCall = Callable[[str, dict[str, Any]], dict[str, Any]]


class ToolResult(dict[str, Any]):
    """A target call's decoded result object that keeps the response it was decoded from.

    ``native_result`` is the MCP ``tools/call`` result as received (content blocks,
    ``isError``, ``structuredContent``). A target call may return one in place of a plain
    dict; the setup ledger then records ``native_result`` beside the decoded ``result``.
    """

    def __init__(self, value: dict[str, Any], native_result: Any) -> None:
        super().__init__(value)
        self.native_result = native_result


class TargetCallFailed(RuntimeError):
    """A target call that received an answer and failed on it, such as an MCP tool error.

    ``native_result`` is the answer as received, which the setup ledger records.
    """

    def __init__(self, message: str, *, native_result: Any) -> None:
        super().__init__(message)
        self.native_result = native_result


# The category of every setup ledger record, which a setup binding's provenance names.
SETUP_LEDGER_CATEGORY = "setup_capture"
# A checks.json with this interface makes every prerequisite take the canonical shape.
CANONICAL_CHECKS_INTERFACE = "artifact-authoring-v2"
BINDING_REASONS = frozenset({"binding_invalid", "binding_missing", "binding_mistyped"})
PREREQUISITE_REASONS = frozenset(
    {
        "prerequisite_failed",
        "prerequisite_unavailable",
        "prerequisite_noncanonical",
        "prerequisite_expected_missing",
        "prerequisite_binding_invalid",
        "prerequisite_unknown_binding",
        "prerequisite_value_mismatch",
    }
)
INCOMPLETE_REASONS = frozenset({"setup_failed"}) | BINDING_REASONS | PREREQUISITE_REASONS


class PreparationError(BundleError):
    """A package member cannot be read, so no step of preparation runs."""


class SetupError(ValueError):
    """Raised when a package setup declaration cannot be run safely.

    ``code`` is ``setup_permission_undeclared``, ``setup_permissions_invalid``,
    or ``setup_invalid`` for a declaration that is refused before any call,
    and ``setup_failed`` for a failed call. Preparation reports each as
    ``setup_failed``.
    """

    def __init__(self, message: str, *, code: str = "setup_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedPackage:
    """A package ready to instantiate: its setup ran and its values resolved.

    ``setup_ledger`` holds one record per setup call, in call order, with the
    result the target returned (``result``, the decoded object) and the MCP result
    it was decoded from (``native_result``, None when the call returned a plain dict
    or received no answer). ``prerequisites`` holds one result per declared
    prerequisite. ``stimulus`` is the package stimulus with every slot of its
    ``user_text`` and ``history`` filled. ``judge_facts`` are the facts a reply
    package's judge reads (the resolved bindings); a command-attempt package
    has none.
    """

    setup_outputs: dict[str, Any]
    setup_ledger: list[dict[str, Any]]
    bindings: dict[str, Any]
    binding_provenance: dict[str, Any]
    prerequisites: list[dict[str, Any]]
    stimulus: dict[str, Any]
    judge_facts: dict[str, Any]


@dataclass(frozen=True)
class PreparationIncomplete:
    """A package that cannot run: ``reason`` is one of ``INCOMPLETE_REASONS``.

    ``error`` describes a setup failure. The other fields hold what preparation
    reached before it stopped: a field is None when its step did not finish,
    and ``setup_outputs`` stays empty unless every setup call succeeded.
    """

    reason: str
    error: str | None
    setup_outputs: dict[str, Any]
    setup_ledger: list[dict[str, Any]]
    bindings: dict[str, Any] | None = None
    binding_provenance: dict[str, Any] | None = None
    prerequisites: list[dict[str, Any]] | None = None


def prepare_package(
    package: ArtifactPackage, *, call_target: TargetCall | None
) -> PreparedPackage | PreparationIncomplete:
    """Run the package's setup through ``call_target`` and resolve what the run needs.

    ``call_target`` is called once per setup operation, in declared order,
    with the operation name and its arguments, and returns the target's result
    object. Without it, a package that declares setup is incomplete
    (``setup_failed``). Raises ``PreparationError`` when a member is not JSON.
    """

    members = _Members.read(package)
    ledger: list[dict[str, Any]] = []
    setup_outputs: dict[str, Any] = {}
    values: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    prerequisites: list[dict[str, Any]] | None = None

    def incomplete(reason: str, error: str | None = None) -> PreparationIncomplete:
        return PreparationIncomplete(
            reason, error, setup_outputs, ledger, values, provenance, prerequisites
        )

    try:
        _validate_setup(members.setup, members.inventory, members.runtime_contract)
        setup_outputs = _run_setup(members.setup, call_target, ledger)
        declarations = validate_binding_declarations(
            members.bindings,
            inventory=members.inventory,
            runtime_contract=members.runtime_contract,
        )
        resolved, resolved_provenance = resolve_bindings(
            declarations,
            setup_outputs=setup_outputs,
            supplied_inputs=members.supplied_facts,
            allow_null_bindings=_null_prerequisite_bindings(
                members.prerequisites if members.canonical_prerequisites else []
            ),
        )
        _annotate_binding_provenance(resolved_provenance, declarations, ledger)
        values, provenance = resolved, resolved_provenance
        prerequisites = check_prerequisites(
            members.prerequisites,
            values=values,
            setup_outputs=setup_outputs,
            strict=members.canonical_prerequisites,
        )
        reason = _prerequisite_failure_reason(prerequisites)
        if reason is not None:
            return incomplete(reason)
        rendered = _render_stimulus(members.stimulus, values, declarations)
    except BindingError as exc:
        return incomplete(_binding_reason(exc))
    except SetupError as exc:
        return incomplete("setup_failed", str(exc))
    return PreparedPackage(
        setup_outputs=setup_outputs,
        setup_ledger=ledger,
        bindings=values,
        binding_provenance=provenance,
        prerequisites=prerequisites,
        stimulus=rendered,
        judge_facts=dict(values) if members.claim_level == "reply" else {},
    )


@dataclass(frozen=True)
class _Members:
    """The decoded members preparation reads, with the facts derived from them."""

    plan: Any
    stimulus: Any
    setup: Any
    bindings: Any
    prerequisites: Any
    checks: Any
    inputs: Any
    runtime_capabilities: dict[str, Any]

    @classmethod
    def read(cls, package: ArtifactPackage) -> _Members:
        """Decode every member preparation reads, plan first."""

        return cls(
            plan=_json_member(package, "plan.json", {}),
            stimulus=_json_member(package, "stimulus.json", {}),
            setup=_json_member(package, "setup.json", []),
            bindings=_json_member(package, "bindings.json", []),
            prerequisites=_json_member(package, "prerequisites.json", []),
            checks=_json_member(package, "checks.json", {}),
            inputs=_json_member(package, "inputs.json", {}),
            runtime_capabilities=package.manifest.runtime_capabilities,
        )

    @property
    def claim_level(self) -> str | None:
        claim = self.plan.get("observation_claim") if isinstance(self.plan, dict) else None
        level = claim.get("claim_level") if isinstance(claim, dict) else None
        return level if isinstance(level, str) else None

    @property
    def runtime_contract(self) -> dict[str, Any]:
        """Return the runtime contract of ``inputs.json``, else the plan's, else the manifest's."""

        for candidate in (
            self.inputs.get("runtime_contract") if isinstance(self.inputs, dict) else None,
            self.plan.get("runtime_contract") if isinstance(self.plan, dict) else None,
        ):
            if isinstance(candidate, dict):
                return candidate
        return self.runtime_capabilities

    @property
    def inventory(self) -> Any:
        return self.inputs.get("inventory", {}) if isinstance(self.inputs, dict) else {}

    @property
    def canonical_prerequisites(self) -> bool:
        """Return whether prerequisites use the canonical check shape."""

        return (
            isinstance(self.checks, dict)
            and self.checks.get("interface") == CANONICAL_CHECKS_INTERFACE
        ) or has_canonical_prerequisites(self.prerequisites)

    @property
    def supplied_facts(self) -> dict[str, Any]:
        return supplied_fact_values(self.inputs)


def _json_member(package: ArtifactPackage, name: str, default: Any) -> Any:
    content = package.members.get(name)
    if content is None:
        return default
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"invalid JSON package member: {name}") from exc


def supplied_fact_values(inputs: Any) -> dict[str, Any]:
    """Return the supplied fact values of ``inputs.json``, by fact reference."""

    if not isinstance(inputs, dict):
        return {}
    direct = inputs.get("facts")
    if isinstance(direct, dict):
        return dict(direct)
    inventory = inputs.get("inventory")
    facts = inventory.get("facts") if isinstance(inventory, dict) else None
    if not isinstance(facts, list):
        return {}
    return {
        item["ref"]: item["value"]
        for item in facts
        if isinstance(item, dict) and isinstance(item.get("ref"), str) and "value" in item
    }


def has_canonical_prerequisites(declarations: Any) -> bool:
    """Return whether any prerequisite has the canonical ``check``/``binding`` shape."""

    return isinstance(declarations, list) and any(
        isinstance(item, dict)
        and {"check", "evidence_refs", "binding"}.issubset(item)
        and not ({"source", "expected"} & set(item))
        for item in declarations
    )


def _setup_permissions(runtime_contract: dict[str, Any]) -> list[Any]:
    allowed = runtime_contract.get("setup_permissions", [])
    if not isinstance(allowed, list):
        raise SetupError("setup_permissions must be a list", code="setup_permissions_invalid")
    return allowed


def _validate_setup(
    setup: Any, inventory: dict[str, Any], runtime_contract: dict[str, Any]
) -> None:
    if not isinstance(setup, list):
        raise SetupError("setup must be a list")
    allowed = _setup_permissions(runtime_contract)
    operations = {
        item.get("name"): item
        for item in inventory.get("operations", [])
        if isinstance(item, dict)
    }
    for index, step in enumerate(setup):
        if not isinstance(step, dict) or not isinstance(step.get("operation"), str):
            raise SetupError(f"setup[{index}] must name an operation")
        operation = step["operation"]
        if operation not in operations:
            raise SetupError(f"setup operation undocumented: {operation}")
        if operation not in allowed:
            raise SetupError(
                f"setup operation not permitted: {operation}",
                code="setup_permission_undeclared",
            )
        if not isinstance(step.get("arguments", {}), dict):
            raise SetupError(f"setup[{index}].arguments must be an object")


def _run_setup(
    setup: list[dict[str, Any]],
    call_target: TargetCall | None,
    ledger: list[dict[str, Any]],
) -> dict[str, Any]:
    """Call the target once per setup step; the ledger gets each record before its call."""

    outputs: dict[str, Any] = {}
    for step in setup:
        operation = step["operation"]
        arguments = step.get("arguments", {})
        record = {
            "category": SETUP_LEDGER_CATEGORY,
            "sequence": len(ledger) + 1,
            "started_monotonic": monotonic(),
            "operation": operation,
            "arguments": arguments,
            "retries": 0,
            "native_result": None,
        }
        ledger.append(record)
        if call_target is None:
            record.update(status="failed", error="setup dispatch unavailable")
            raise SetupError("setup dispatch unavailable", code="setup_failed")
        try:
            output = call_target(operation, arguments)
        except Exception as exc:  # the caller's target call is the boundary
            record.update(
                status="failed",
                error=str(exc),
                native_result=getattr(exc, "native_result", None),
            )
            raise SetupError(f"setup dispatch failed: {operation}", code="setup_failed") from exc
        record["native_result"] = getattr(output, "native_result", None)
        if not isinstance(output, dict):
            record.update(status="failed", error="setup result is not an object")
            raise SetupError(f"setup result is not an object: {operation}", code="setup_failed")
        output = dict(output)
        record.update(status="completed", result=output)
        outputs[operation] = output
    return outputs


def check_prerequisites(
    declarations: Any,
    *,
    values: dict[str, Any],
    setup_outputs: dict[str, Any],
    strict: bool | None = None,
) -> list[dict[str, Any]]:
    """Evaluate exact declared equality checks without interpreting prose.

    ``strict`` selects the canonical ``binding``/``equals`` shape; None infers
    it from the declarations.
    """

    if declarations is None:
        return []
    if not isinstance(declarations, list):
        raise BindingError("prerequisites must be a list")
    if strict is None:
        strict = has_canonical_prerequisites(declarations)
    context = {"bindings": values, "setup": setup_outputs}
    results = []
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise BindingError(f"prerequisite[{index}] must be an object")
        if strict:
            results.append(_check_canonical_prerequisite(declaration, values=values))
            continue
        results.append(_check_legacy_prerequisite(index, declaration, context))
    return results


def _check_legacy_prerequisite(
    index: int, declaration: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate one ``source``/``binding`` equality prerequisite."""

    name = declaration.get("name", f"prerequisite-{index + 1}")
    expected = declaration.get("equals", declaration.get("expected"))
    reference_fields = [field for field in ("source", "binding") if field in declaration]
    if not reference_fields:
        return {
            "name": name,
            "status": "unavailable",
            "reason": "source_missing",
            "required": False,
        }
    if any(
        not isinstance(declaration[field], str) or not declaration[field].strip()
        for field in reference_fields
    ):
        return {"name": name, "status": "failed", "reason": "source_invalid", "required": True}
    source = declaration[reference_fields[0]]
    try:
        actual = select_value(context, source)
    except BindingError:
        return {
            "name": name,
            "status": "unavailable",
            "reason": "source_unavailable",
            "required": True,
        }
    return {
        "name": name,
        "status": "passed" if actual == expected else "failed",
        "actual": actual,
        "expected": expected,
        "source": source,
        "required": True,
    }


_MISSING = object()
_CANONICAL_PREREQUISITE_FIELDS = frozenset({"name", "check", "evidence_refs", "binding", "equals"})


def _check_canonical_prerequisite(
    declaration: dict[str, Any], *, values: dict[str, Any]
) -> dict[str, Any]:
    name = declaration.get("name", "prerequisite")
    if set(declaration) - _CANONICAL_PREREQUISITE_FIELDS:
        return _failed_prerequisite(name, "prerequisite_noncanonical")
    if "equals" not in declaration:
        return {
            "name": name,
            "status": "failed",
            "reason": "prerequisite_expected_missing",
            "binding": declaration.get("binding"),
            "required": True,
        }
    if _CANONICAL_PREREQUISITE_FIELDS - set(declaration):
        return _failed_prerequisite(name, "prerequisite_noncanonical")
    binding = declaration["binding"]
    if not isinstance(binding, str) or not binding.strip():
        return _failed_prerequisite(name, "prerequisite_binding_invalid")
    if binding not in values:
        return {
            "name": name,
            "status": "failed",
            "reason": "prerequisite_unknown_binding",
            "binding": binding,
            "required": True,
        }
    expected = declaration["equals"]
    actual = values[binding]
    result = {
        "name": name,
        "status": "passed" if actual == expected else "failed",
        "actual": actual,
        "expected": expected,
        "source": f"bindings.{binding}",
        "binding": binding,
        "required": True,
    }
    if actual != expected:
        result["reason"] = "prerequisite_value_mismatch"
    return result


def _failed_prerequisite(name: Any, reason: str) -> dict[str, Any]:
    return {"name": name, "status": "failed", "reason": reason, "required": True}


def _prerequisite_failure_reason(results: list[dict[str, Any]]) -> str | None:
    """Return the incomplete reason for the first failed required prerequisite."""

    failed = next(
        (item for item in results if item["status"] != "passed" and item.get("required", True)),
        None,
    )
    if failed is None:
        return None
    if str(failed.get("reason", "")).startswith("prerequisite_"):
        return failed.get("reason")
    if failed["status"] == "failed":
        return "prerequisite_failed"
    return "prerequisite_unavailable"


def _null_prerequisite_bindings(declarations: Any) -> frozenset[str]:
    """Return the bindings a canonical prerequisite expects to be null."""

    if not isinstance(declarations, list):
        return frozenset()
    return frozenset(
        item["binding"]
        for item in declarations
        if isinstance(item, dict)
        and item.get("equals", _MISSING) is None
        and isinstance(item.get("binding"), str)
    )


def _annotate_binding_provenance(
    provenance: dict[str, Any],
    declarations: tuple[RuntimeBinding, ...],
    ledger: list[dict[str, Any]],
) -> None:
    """Join each setup binding to the setup ledger record of its operation."""

    for binding in declarations:
        if binding.source_kind != "setup_output":
            provenance[binding.name]["ledger_category"] = "supplied_input"
            continue
        operation = binding.source_ref.partition(":")[2]
        record = next((item for item in ledger if item.get("operation") == operation), None)
        if record is not None:
            provenance[binding.name]["ledger_category"] = record["category"]
            provenance[binding.name]["ledger_sequence"] = record["sequence"]


def _render_stimulus(
    stimulus: Any,
    values: dict[str, Any],
    declarations: tuple[RuntimeBinding, ...],
) -> dict[str, Any]:
    if not isinstance(stimulus, dict) or not isinstance(stimulus.get("user_text"), str):
        raise BindingError("stimulus user_text is unavailable", code="stimulus_invalid")
    rendered = dict(stimulus)
    rendered["user_text"] = substitute_slots(stimulus["user_text"], values, declarations)
    history = stimulus_history(stimulus)
    rendered["history"] = [
        {**turn, "content": substitute_slots(turn["content"], values, declarations)}
        for turn in history
        if isinstance(turn, dict) and isinstance(turn.get("content"), str)
    ]
    return rendered


def _binding_reason(error: BindingError) -> str:
    """Return the incomplete reason of a binding error; any other code is binding_invalid."""

    return error.code if error.code in BINDING_REASONS else "binding_invalid"


__all__ = [
    "INCOMPLETE_REASONS",
    "PreparationError",
    "PreparationIncomplete",
    "PreparedPackage",
    "SetupError",
    "TargetCall",
    "TargetCallFailed",
    "ToolResult",
    "check_prerequisites",
    "has_canonical_prerequisites",
    "prepare_package",
    "supplied_fact_values",
]
