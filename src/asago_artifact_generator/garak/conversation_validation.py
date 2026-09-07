"""Validate text-only prompt history without interpreting its content."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .schema import validate_instance, validate_schema

Nonempty = Annotated[str, Field(min_length=1)]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


class _Function(_Record):
    name: Nonempty
    arguments: str

    @field_validator("arguments")
    @classmethod
    def object_arguments(cls, value: str) -> str:
        if not isinstance(json.loads(value, parse_constant=_reject_constant), dict):
            raise ValueError("function arguments must encode a JSON object")
        return value


class _Call(_Record):
    id: Nonempty
    type: Literal["function"]
    function: _Function


class _DeclaredFunction(_Record):
    name: Nonempty
    parameters: dict[str, Any]
    description: str | None = None
    strict: bool | None = None


class _Declaration(_Record):
    type: Literal["function"]
    function: _DeclaredFunction


class _Text(_Record):
    role: Literal["system", "developer", "user"]
    content: str
    name: Nonempty | None = None


class _Assistant(_Record):
    role: Literal["assistant"]
    content: str | None = None
    name: Nonempty | None = None
    tool_calls: Annotated[list[_Call], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def text_or_calls(self) -> _Assistant:
        if self.content is None and self.tool_calls is None:
            raise ValueError("assistant history requires content or tool calls")
        return self


class _Tool(_Record):
    role: Literal["tool"]
    content: str
    tool_call_id: Nonempty
    name: Nonempty | None = None


_Message = Annotated[_Text | _Assistant | _Tool, Field(discriminator="role")]
_HISTORY = TypeAdapter(list[_Message])
_TOOLS = TypeAdapter(list[_Declaration])


def validate_prompt_history(messages: list[object], tools: object) -> list[str]:
    """Return shape/pairing errors; never author or repair a conversation."""
    try:
        history = _HISTORY.validate_python(messages)
    except ValidationError as exc:
        return [
            f"messages {error['loc']}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
    registry, errors = _tool_registry(tools)
    errors.extend(_history_argument_errors(history, registry))
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for index, message in enumerate(history):
        if isinstance(message, _Tool):
            errors.extend(_consume_result(message, pending, index))
            continue
        if pending:
            errors.append(f"messages {index}: outstanding tool results must precede this turn")
        if isinstance(message, _Assistant):
            errors.extend(_register_calls(message.tool_calls or [], pending, seen, index))
    if pending:
        errors.append("messages: missing tool results for " + ", ".join(sorted(pending)))
    if history and isinstance(history[-1], _Assistant):
        errors.append("conversation must end before the target assistant response")
    return errors


def _tool_registry(tools: object) -> tuple[dict[str, dict[str, Any]], list[str]]:
    try:
        declarations = _TOOLS.validate_python(tools)
    except ValidationError as exc:
        return {}, [
            f"tools {error['loc']}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
    registry: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for declaration in declarations:
        function = declaration.function
        if function.name in registry:
            errors.append(f"tools: duplicate declaration for {function.name}")
        registry[function.name] = function.parameters
        errors.extend(
            f"tools {function.name}: {error}" for error in validate_schema(function.parameters)
        )
    return registry, errors


def _history_argument_errors(
    history: list[_Message], registry: dict[str, dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    for index, message in enumerate(history):
        if not isinstance(message, _Assistant):
            continue
        for call in message.tool_calls or []:
            errors.extend(_call_argument_errors(call, registry, index))
    return errors


def _call_argument_errors(
    call: _Call, registry: dict[str, dict[str, Any]], index: int
) -> list[str]:
    schema = registry.get(call.function.name)
    if schema is None:
        return [f"messages {index}: undeclared tool {call.function.name}"]
    return [
        f"messages {index}: arguments for {call.function.name}: {error}"
        for error in validate_instance(json.loads(call.function.arguments), schema)
    ]


def _register_calls(
    calls: list[_Call], pending: dict[str, str], seen: set[str], index: int
) -> list[str]:
    errors = []
    for call in calls:
        if call.id in seen:
            errors.append(f"messages {index}: duplicate tool call ID {call.id}")
        else:
            seen.add(call.id)
            pending[call.id] = call.function.name
    return errors


def _consume_result(message: _Tool, pending: dict[str, str], index: int) -> list[str]:
    expected_name = pending.pop(message.tool_call_id, None)
    if expected_name is None:
        return [f"messages {index}: tool result has no outstanding matching call"]
    if message.name is not None and message.name != expected_name:
        return [f"messages {index}: tool result name differs from matching call"]
    return []
