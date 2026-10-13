"""The consumer's profile readers run the producer's ``model-profile`` contract.

``contracts/model-profile/model-profile-v1`` is a byte-for-byte copy of the
producer's field table, load cases and request cases. The producer and orch run
the same files against their own readers, so a field handled differently in one
reader fails there.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from asago_artifact_generator.authoring.core import (
    AUTHORING_THINKING_EXTRA_BODY,
    REVIEW_THINKING_EXTRA_BODY,
    PromptPacket,
)
from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport
from asago_artifact_generator.cli import _request_body, _transport_options
from asago_artifact_generator.contract_kit import verify_contract_lock
from asago_artifact_generator.profiles import (
    OPTIONAL_PROFILE_FIELDS,
    REQUIRED_PROFILE_FIELDS,
    AuthoringProfile,
    ProfileFieldError,
    ProfileNotFoundError,
    load_authoring_profile,
)

from .support import chat_completion, scripted_client, status_error

READER = "consumer"
ROOT = Path(__file__).resolve().parents[1] / "contracts/model-profile"
KIT = ROOT / "model-profile-v1"


def _read(name: str) -> Any:
    return json.loads((KIT / name).read_text(encoding="utf-8"))


FIELDS: list[dict[str, Any]] = _read("fields.json")["fields"]
LOAD_CASES: list[dict[str, Any]] = _read("load-cases.json")["cases"]
_REQUESTS = _read("request-cases.json")
CALL_DEFAULTS: dict[str, Any] = _REQUESTS["call_defaults"]
REQUEST_CASES: list[dict[str, Any]] = _REQUESTS["cases"]
BASE = {"base_url": "https://models.example.test/v1", "model": "m-1", "api_key": "key-1"}


def _default(row: dict[str, Any]) -> Any:
    return row["default"].get(READER, row["default"]["*"])


def expected_loaded(profile: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for row in FIELDS:
        if READER in row["used_by"]:
            value = profile.get(row["name"])
            out[row["name"]] = _default(row) if value is None else value
    return out


def load(tmp_path: Path, profile: dict[str, Any], *, shape: str = "flat", name: str = "p"):
    """Return ('loaded', profile), ('rejected', error) or ('not_found', None)."""
    document = {"profiles": {"p": profile}} if shape == "nested" else {"p": profile}
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    try:
        return "loaded", load_authoring_profile(path, name)
    except ProfileNotFoundError:
        return "not_found", None
    except ProfileFieldError as error:
        return "rejected", error


def loaded_view(connection: AuthoringProfile) -> dict[str, Any]:
    return {
        row["name"]: getattr(connection, row["name"]) for row in FIELDS if READER in row["used_by"]
    }


def _expect(case: dict[str, Any]) -> dict[str, Any]:
    return case["expect"].get(READER, case["expect"].get("*"))


def test_lock_pins_every_file_of_the_mirror() -> None:
    verify_contract_lock(
        ROOT,
        ValueError,
        lock_label="model-profile lock",
        member_label="model-profile file",
        metadata={
            "authority": "asago-scenario-generator",
            "contract": "model-profile",
            "version": "model-profile-v1",
        },
        metadata_message="model-profile lock metadata mismatch",
    )
    on_disk = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in ROOT.rglob("*")
        if path.is_file() and path.name != "CONTRACT.lock"
    }
    assert json.loads((ROOT / "CONTRACT.lock").read_text(encoding="utf-8"))["files"] == on_disk


def test_field_table_names_the_consumer_allow_list() -> None:
    assert {row["name"] for row in FIELDS} == {
        *REQUIRED_PROFILE_FIELDS,
        *OPTIONAL_PROFILE_FIELDS,
    }


@pytest.mark.parametrize("case", LOAD_CASES, ids=lambda case: case["id"])
def test_load_case(case: dict[str, Any], tmp_path: Path) -> None:
    outcome, result = load(
        tmp_path,
        case["profile"],
        shape=case.get("file_shape", "flat"),
        name=case.get("lookup_name", "p"),
    )
    expect = _expect(case)
    if "rejected" in expect:
        assert outcome == "rejected"
        assert result.field == expect["rejected"]
        for value in case["profile"].values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in str(result)
    elif "not_found" in expect:
        assert outcome == "not_found"
    else:
        assert outcome == "loaded"
        assert loaded_view(result) == expected_loaded(case["profile"])


_VALID = [(row["name"], value) for row in FIELDS for value in row["valid"]]
_INVALID = [(row["name"], value) for row in FIELDS for value in row["invalid"]]
_REQUIRED = [row["name"] for row in FIELDS if READER in row["required_by"]]


@pytest.mark.parametrize("name,value", _VALID, ids=lambda item: str(item)[:30])
def test_valid_field_value_loads(name: str, value: Any, tmp_path: Path) -> None:
    profile = {**BASE, name: value}
    outcome, result = load(tmp_path, profile)
    assert outcome == "loaded"
    assert loaded_view(result) == expected_loaded(profile)


@pytest.mark.parametrize("name,value", _INVALID, ids=lambda item: str(item)[:30])
def test_invalid_field_value_is_rejected_by_name(name: str, value: Any, tmp_path: Path) -> None:
    outcome, result = load(tmp_path, {**BASE, name: value})
    assert outcome == "rejected"
    assert result.field == name
    if isinstance(value, str) and len(value) > 3:
        assert value not in str(result)


@pytest.mark.parametrize("name", _REQUIRED)
def test_absent_required_field_is_rejected_by_name(name: str, tmp_path: Path) -> None:
    profile = {key: value for key, value in BASE.items() if key != name}
    outcome, result = load(tmp_path, profile)
    assert outcome == "rejected"
    assert result.field == name


def test_repr_redacts_the_connection_values(tmp_path: Path) -> None:
    _, connection = load(tmp_path, BASE)
    shown = repr(connection)
    assert not any(value in shown for value in BASE.values())


@pytest.mark.parametrize(
    "name,value",
    [
        ("timeout", float("inf")),
        ("temperature", float("nan")),
        ("repetition_penalty", float("inf")),
    ],
    ids=lambda item: str(item),
)
def test_non_finite_number_is_rejected_by_name(name: str, value: float, tmp_path: Path) -> None:
    """JSON cannot carry infinity or NaN, so the contract's value lists omit them."""
    outcome, result = load(tmp_path, {**BASE, name: value})
    assert outcome == "rejected"
    assert result.field == name


def normalized(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key != "messages"}


def send(connection: AuthoringProfile, call: dict[str, Any]):
    first = [status_error(call["first_error"])] if call["first_error"] else []
    client = scripted_client([*first, chat_completion(), chat_completion()])
    transport = PrivateModelAuthoringTransport(
        **_transport_options(connection),
        extra_body=_request_body(AUTHORING_THINKING_EXTRA_BODY, connection),
        review_extra_body=_request_body(REVIEW_THINKING_EXTRA_BODY, connection),
        review_fill_context=True,
        client=client,
    )
    stage = "plan_review" if call["role"] == "review" else "plan_author"
    packet = PromptPacket(
        stage=stage, version="test", system=call["system"], user=call["user"], payload={}
    )
    error = None
    try:
        transport.complete(packet)
    except Exception as exc:
        error = type(exc).__name__
    return [normalized(request) for request in client.requests], error


@pytest.mark.parametrize("case", REQUEST_CASES, ids=lambda case: case["id"])
def test_request_case(case: dict[str, Any], tmp_path: Path) -> None:
    outcome, connection = load(tmp_path, case["profile"])
    assert outcome == "loaded"
    requests, error = send(connection, {**CALL_DEFAULTS, **case["call"]})
    expect = _expect(case)
    assert requests == expect["requests"]
    assert error == expect.get("error")
