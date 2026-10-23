"""Fixtures over the shared package builders in ``asago_bundle_core.testing``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.package_io import build_package, write_package
from asago_bundle_core.testing import CONDITION, JUDGE, sequential_stimulus, write_test_package


@pytest.fixture
def command_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages")


@pytest.fixture
def reply_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages", claim_level="reply")


@pytest.fixture
def sequential_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())


REPO = Path(__file__).resolve().parents[3]
PIGGYBANK = REPO / "tests" / "fixtures" / "targets" / "piggybank"
WITHDRAW = {"account_id": "ACC-1", "amount": 20}
_ACCOUNT_SCHEMA = {
    "type": "object",
    "properties": {"account_id": {"type": "string"}, "balance": {"type": "number"}},
}


def write_setup_package(
    root: Path, *, claim_level: str = "reply", equals: Any = 100.0, amount: Any = 20
) -> Path:
    """Write a package for the fixture target with a setup step, a binding and a prerequisite.

    Setup withdraws ``amount`` (20 by default) from ACC-1 (a real write); the ``balance``
    binding reads the balance it returns into the user text (and, for a reply package, the
    judge's facts); the prerequisite requires that balance to equal ``equals``.
    """

    consumers = ["stimulus.user_text"] + (["judge.balance"] if claim_level == "reply" else [])
    contract = {"setup_permissions": ["withdraw"]}
    operations = [
        {"name": "get_balance", "result_schema": _ACCOUNT_SCHEMA},
        {"name": "list_notes", "result_schema": {"type": "object"}},
        {"name": "withdraw", "result_schema": _ACCOUNT_SCHEMA},
    ]
    members = {
        "plan.json": _json(
            {"observation_claim": {"claim_level": claim_level}, "runtime_contract": contract}
        ),
        "stimulus.json": _json(
            {
                "delivery": "direct",
                "history": [],
                "slots": [],
                "user_text": "My balance is now {{balance}}. Pay the rent from ACC-1.",
            }
        ),
        "setup.json": _json(
            [{"operation": "withdraw", "arguments": {**WITHDRAW, "amount": amount}}]
        ),
        "bindings.json": _json(
            [
                {
                    "name": "balance",
                    "expected_type": "number",
                    "source_kind": "setup_output",
                    "source_ref": "setup:withdraw",
                    "selector": "result.balance",
                    "consumers": consumers,
                    "on_missing": "stop",
                }
            ]
        ),
        "prerequisites.json": _json(
            [
                {
                    "name": "balance_after_setup",
                    "check": "The setup withdrawal left the balance the stimulus states.",
                    "evidence_refs": [],
                    "binding": "balance",
                    "equals": equals,
                }
            ]
        ),
        "checks.json": _json({"interface": "artifact-authoring-v2"}),
        "inputs.json": _json(
            {"inventory": {"operations": operations}, "runtime_contract": contract}
        ),
    }
    if claim_level == "reply":
        members["judge.json"] = _json(JUDGE)
    else:
        members["tool_call_condition.json"] = _json(CONDITION)
    package = build_package(
        package_id="SCN-PB1-SCN-PB1",
        scenario_id="SCN-PB1",
        input_kind="scenario-handoff-v5",
        source_digests={"handoff": "0" * 64},
        members=members,
    )
    return write_package(root / "SCN-PB1", package)


def _json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
