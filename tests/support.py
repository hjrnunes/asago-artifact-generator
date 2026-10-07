"""Test doubles and neutral-example helpers shared by the offline tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.contracts import (
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
)
from asago_artifact_generator.authoring.core import (
    AUTHORING_INTERFACE_VERSION_V2,
    Finding,
    PromptPacket,
    TransportResponse,
    _json_bytes,
    _sha256,
)
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport
from asago_artifact_generator.failure_evidence import FAILURE_EVIDENCE_SCHEMA_VERSION
from asago_artifact_generator.package_io import (
    build_package,
    tool_call_condition_bytes,
    write_package,
)

# A neutral producer tool-call condition: the record command exceeds its bound.
NEUTRAL_TOOL_CALL_CONDITION = {
    "comparisons": [
        {
            "kind": "value",
            "left": {"argument": "value", "operation": "inspect_record", "source": "argument"},
            "op": "gt",
            "right": {"source": "literal", "value": 3},
        }
    ]
}

# The operations the neutral example's captures call.
NEUTRAL_INVENTORY = {
    "operations": [
        {
            "name": "inspect_record",
            "arguments": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "value": {"type": "integer"}},
            },
        }
    ],
    "facts": [],
    "source_handles": [],
}


class ScriptedAuthoringTransport:
    """Deterministic transport used by tests and offline rehearsals."""

    max_retries = 0

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def complete(self, packet: PromptPacket) -> TransportResponse | str | bytes:
        self.requests.append(
            {
                "stage": packet.stage,
                "version": packet.version,
                "system": packet.system,
                "user": packet.user,
                "payload": packet.payload,
                "extra_body": deepcopy(getattr(self, "extra_body", None)),
            }
        )
        if not self.responses:
            raise RuntimeError("scripted transport exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeCompletions:
    """Stand in for ``client.chat.completions``: replay ``responses``, keep every request.

    A response that is an exception is raised instead of returned.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.clients: list[FakeOpenAI] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeOpenAI:
    """Stand in for an ``openai.OpenAI`` client that answers from ``completions``."""

    def __init__(self, completions: FakeCompletions, **init_kwargs: Any) -> None:
        self.init_kwargs = init_kwargs
        self.chat = SimpleNamespace(completions=completions)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.chat.completions.requests


def scripted_client(responses: list[Any]) -> FakeOpenAI:
    """Return a client for ``PrivateModelAuthoringTransport(client=...)``."""

    return FakeOpenAI(FakeCompletions(responses))


def fake_openai(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> FakeCompletions:
    """Replace ``openai.OpenAI`` so every client answers from one scripted ``FakeCompletions``.

    The returned object keeps the requests and, in ``clients``, each client's constructor kwargs.
    """

    completions = FakeCompletions(responses)

    def factory(**kwargs: Any) -> FakeOpenAI:
        completions.clients.append(FakeOpenAI(completions, **kwargs))
        return completions.clients[-1]

    monkeypatch.setattr("openai.OpenAI", factory)
    return completions


def chat_completion(
    content: Any = "{}",
    *,
    finish_reason: str | None = "stop",
    usage: Any = None,
    model: str | None = "returned-model",
    message: Any = None,
    **message_fields: Any,
) -> SimpleNamespace:
    """Build one chat completion; ``model=None`` omits the field, ``message`` replaces it."""

    if message is None:
        message = SimpleNamespace(content=content, **message_fields)
    fields: dict[str, Any] = {
        "choices": [SimpleNamespace(message=message, finish_reason=finish_reason)],
        "usage": usage,
    }
    if model is not None:
        fields["model"] = model
    return SimpleNamespace(**fields)


def prompt_packet(stage: str = "call1", *, user: str = "user") -> PromptPacket:
    return PromptPacket(stage=stage, version="test", system="system", user=user, payload={})


def private_transport(**options: Any) -> PrivateModelAuthoringTransport:
    """Build a transport over a private endpoint; ``options`` override the defaults."""

    return PrivateModelAuthoringTransport(
        **{
            "base_url": "https://private.invalid/v1",
            "api_key": "secret-value",
            "model": "luna",
            **options,
        }
    )


_PROVIDER_REQUEST = httpx2.Request("POST", "https://private.invalid/v1")


def connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(message="connection reset", request=_PROVIDER_REQUEST)


def status_error(status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=_PROVIDER_REQUEST)
    if status == 429:
        return openai.RateLimitError("rate limited", response=response, body=None)
    if status >= 500:
        return openai.InternalServerError("upstream failed", response=response, body=None)
    return openai.BadRequestError("bad request", response=response, body=None)


def scripted_orchestrator(
    tmp_path: Path, responses: list[Any], *, task_id: str, **kwargs: Any
) -> tuple[AuthoringOrchestrator, ScriptedAuthoringTransport]:
    """Build an orchestrator that replays ``responses`` and packages under ``tmp_path``."""

    transport = ScriptedAuthoringTransport(responses)
    orchestrator = AuthoringOrchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id=task_id, **kwargs
    )
    return orchestrator, transport


def unreviewed_policy(**changes: Any) -> AuthoringPolicy:
    """Return a stage-local policy whose runs dispatch no semantic reviews."""

    return AuthoringPolicy(**{"review_plan": False, "review_artifact": False, **changes})


def stage_local_orchestrator(
    *,
    policy: AuthoringPolicy | None = None,
    **kwargs: Any,
) -> AuthoringOrchestrator:
    """Build an orchestrator on the stage-local path; reviews are off by default."""

    return AuthoringOrchestrator(
        policy=unreviewed_policy() if policy is None else policy,
        **kwargs,
    )


def neutral_call2_response_v2() -> bytes:
    """Return the neutral Call 2 response as one fenced JSON object."""

    return b"```json\n" + _json_bytes(neutral_artifact_response_without_source()) + b"```\n"


def validate_neutral_example() -> list[Finding]:
    """Validate the neutral example through the v2 response seams."""

    plan = neutral_artifact_plan_v2()
    metadata = neutral_artifact_response_without_source()
    inventory = deepcopy(NEUTRAL_INVENTORY)
    runtime_contract = {"delivery": ["direct_user_message"], "setup_permissions": []}
    return [
        *collect_plan_findings_v2(plan, inventory, runtime_contract),
        *collect_artifact_findings_v2(metadata, plan, inventory, runtime_contract),
    ]


def build_neutral_artifact_package(destination: str | Path) -> Path:
    """Persist the neutral v2 example through the real package writer."""

    metadata = neutral_artifact_response_without_source()
    plan = neutral_artifact_plan_v2()
    findings = validate_neutral_example()
    if findings:
        raise ValueError(
            "neutral v2 example is invalid: " + "; ".join(finding.detail for finding in findings)
        )
    members = {
        "plan.json": _json_bytes(plan),
        "stimulus.json": _json_bytes(metadata["stimulus"]),
        "setup.json": _json_bytes(plan["setup_recipe"]),
        "bindings.json": _json_bytes(plan["runtime_bindings"]),
        "prerequisites.json": _json_bytes(plan["prerequisites"]),
        "tool_call_condition.json": tool_call_condition_bytes(NEUTRAL_TOOL_CALL_CONDITION),
        "checks.json": _json_bytes({"interface": AUTHORING_INTERFACE_VERSION_V2}),
        "inputs.json": _json_bytes({"neutral": True, "operation": "inspect_record"}),
        "source-hashes.json": _json_bytes({"neutral": _sha256(b"neutral-example-v2")}),
        "observations.json": _json_bytes(plan["required_observations"]),
        "explanation.json": _json_bytes({"text": metadata["explanation"]}),
        "examples.json": _json_bytes(metadata["examples"]),
    }
    package = build_package(
        package_id="offline-neutral-example-v2",
        scenario_id="neutral-example",
        input_kind="scenario-handoff-v3",
        source_digests={"neutral": _sha256(b"neutral-example-v2")},
        members=members,
        authoring={
            "status": "scripted-offline-example",
            "interface": AUTHORING_INTERFACE_VERSION_V2,
        },
        runtime_capabilities={"delivery": ["direct_user_message"]},
        creation_model={"model": "maintained-neutral-example"},
    )
    return write_package(destination, package)


def load_failure_evidence(path: str | Path) -> dict[str, Any]:
    """Read a failure-evidence sidecar and check its schema version and attempts."""

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    assert document["schema_version"] == FAILURE_EVIDENCE_SCHEMA_VERSION
    assert isinstance(document["attempts"], list)
    return document
