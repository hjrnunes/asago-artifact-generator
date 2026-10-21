"""The ``openshell`` agent runtime: the MiDojo base's sandbox block, with its slots.

mini-agents' base (``mini-agents-midojo-base-v1``) holds a top-level ``runtime`` block:
the sandbox image (``<image_id>``), the agent command (``<domain>`` stands for the domain
it serves) and the OpenShell policy, default deny with two port slots,
``<control_plane_port>`` and ``<relay_port>``. A slot is a JSON string exactly equal to
its name, as mini-agents ``kernel/sandbox_policy.fill_policy`` reads it.

``compile`` fills the domain and turns each other slot into a value slot: the image
into ``image_id``, the control plane's port into ``service_port`` (the port ``serve``
listens on, which MiDojo hands the sandbox as ``host.openshell.internal:<port>``) and
the relay's into ``relay_port``; ``instantiate`` fills the ports as integers. The agent
command reads its inputs from the sandbox environment (mini-agents README, "Sandbox
agent command"): the model, the relay as the sandbox reaches it, the system message and
the package's exposed tools (the Garak path's ``allowed_tools`` list, comma-separated in
the sandbox and sent as the MCP tool's ``allowed_tools``) are values, and the relay's
key is ``OPENAI_API_KEY``, the placeholder the orchestrator sets, which MiDojo expands
when it loads the suite. MiDojo sets the turn variables, ``MIDOJO_URL`` and
``MIDOJO_SESSION_TOKEN`` itself.
"""

from __future__ import annotations

from typing import Any

from asago_bundle_core.errors import BundleError
from asago_bundle_core.slots import contains_marker, value_slot

OPENSHELL = "openshell"
UNMANAGED = "unmanaged"
RUNTIMES = (UNMANAGED, OPENSHELL)
IMAGE_SLOT = "<image_id>"
DOMAIN_SLOT = "<domain>"
# Each port slot of the base's policy and the value that fills it.
PORT_VALUES = {"<control_plane_port>": "service_port", "<relay_port>": "relay_port"}
# MiDojo expands ``${env.VAR}`` in the runtime settings from midojo-run's environment.
API_KEY_REFERENCE = "${env.OPENAI_API_KEY}"


class RuntimeFault(BundleError):
    """The base's runtime block cannot make an ``openshell`` agent runtime."""


def agent_runtime(base: Any, domain: str) -> dict[str, Any]:
    """Return the base's runtime block as the suite's ``agent_runtime`` for ``domain``."""

    runtime = base.get("runtime") if isinstance(base, dict) else None
    if not isinstance(runtime, dict):
        raise RuntimeFault("the MiDojo base has no runtime block")
    if contains_marker(runtime):
        raise RuntimeFault("the MiDojo base's runtime block contains a slot marker")
    image, command, policy = (runtime.get(key) for key in ("image", "agent_command", "policy"))
    if image != IMAGE_SLOT:
        raise RuntimeFault(f"the runtime image is not the {IMAGE_SLOT} slot: {image!r}")
    if not (
        isinstance(command, list)
        and all(isinstance(argument, str) for argument in command)
        and DOMAIN_SLOT in command
    ):
        raise RuntimeFault(f"the runtime agent_command is not a command with {DOMAIN_SLOT}")
    if not isinstance(policy, dict):
        raise RuntimeFault("the runtime has no policy")
    missing = [slot for slot in PORT_VALUES if slot not in _strings(policy)]
    if missing:
        raise RuntimeFault(f"the runtime policy lacks port slots: {missing}")
    return {
        "type": OPENSHELL,
        "image": value_slot("image_id"),
        "agent_command": [domain if argument == DOMAIN_SLOT else argument for argument in command],
        "policy": _fill_ports(policy),
        "files": {},
        "env_vars": {
            "AGENT_MODEL": value_slot("model"),
            "AGENT_MODEL_BASE_URL": value_slot("relay_url"),
            "AGENT_MODEL_API_KEY": API_KEY_REFERENCE,
            "AGENT_INSTRUCTIONS": value_slot("instructions"),
            "AGENT_ALLOWED_TOOLS": value_slot("allowed_tools"),
        },
    }


def runtime_type(suite: Any) -> Any:
    """Return the ``agent_runtime`` type of a suite document (MiDojo's default is openshell)."""

    runtime = suite.get("agent_runtime") if isinstance(suite, dict) else None
    return runtime.get("type", OPENSHELL) if isinstance(runtime, dict) else None


def _fill_ports(node: Any) -> Any:
    if isinstance(node, str) and node in PORT_VALUES:
        return value_slot(PORT_VALUES[node])
    if isinstance(node, dict):
        return {key: _fill_ports(child) for key, child in node.items()}
    if isinstance(node, list):
        return [_fill_ports(child) for child in node]
    return node


def _strings(node: Any) -> set[str]:
    if isinstance(node, str):
        return {node}
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    return set().union(*(_strings(child) for child in children))
