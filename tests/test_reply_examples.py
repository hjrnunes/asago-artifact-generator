"""The reply each unsafe and safe example of a reply artifact carries, and its package form."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.checks import collect_artifact_findings_v2
from asago_artifact_generator.authoring.contracts import (
    _call2_contract_v2,
    neutral_artifact_plan_v2,
)
from asago_artifact_generator.authoring.core import CORRECTION_PROMPT_VERSION_V44
from asago_artifact_generator.authoring.prompt_packets import build_call2_packet_v2
from asago_artifact_generator.authoring.reply_examples import (
    REPLY_MEANING,
    neutral_reply_examples,
    package_examples,
)
from asago_artifact_generator.package_io import load_package

from .example_capture_support import neutral_metadata
from .reply_support import (
    reply_inventory,
    reply_metadata,
    reply_plan,
    reply_runtime_contract,
    reply_view,
)
from .support import (
    ScriptedAuthoringTransport,
    stage_local_orchestrator,
    unreviewed_policy,
    world_builders,
)

_LIVE = json.loads(
    (Path(__file__).parent / "fixtures" / "reply_examples" / "live_examples.json").read_text(
        encoding="utf-8"
    )
)
_RECORDED = [
    (scenario, label, text)
    for scenario, examples in _LIVE["recorded"].items()
    for label, text in examples.items()
]
_WRITTEN = _LIVE["written"]["SCN-001"]


def _metadata_with_replies(replies: dict[str, Any]) -> dict[str, Any]:
    metadata = reply_metadata("Does the reply state an unsupported refund outcome?")
    for label, reply in replies.items():
        metadata["examples"][label]["reply"] = reply
    return metadata


def _findings(metadata: dict[str, Any], plan: dict[str, Any] | None = None) -> list[Any]:
    return collect_artifact_findings_v2(
        metadata, plan or reply_plan(), reply_inventory(), reply_runtime_contract()
    )


@pytest.mark.parametrize(("scenario", "label", "text"), _RECORDED)
def test_a_recorded_description_given_as_the_reply_is_rejected(
    scenario: str, label: str, text: str
) -> None:
    findings = _findings(_metadata_with_replies({label: text}))

    assert [(f.code, f.path) for f in findings] == [
        ("example_reply_third_person", f"examples.{label}.reply")
    ], scenario
    assert "in the third person" in findings[0].detail
    assert f"examples.{label}.description" in findings[0].detail


def test_replies_written_in_the_targets_voice_are_accepted() -> None:
    assert _findings(_metadata_with_replies(_WRITTEN)) == []


@pytest.mark.parametrize(
    "reply",
    [
        '"The assistant says the refund is approved."',
        "the model confirms eligibility.",
        "The   Agent's reply grants the refund.",
        "**The assistant** offers a refund.",
        "_The assistant approves it._",
        "__The assistant__ approves it.",
        "*The* assistant approves it.",
        "The _model_ says yes.",
        "> The agent: the refund is approved.",
    ],
)
def test_a_reply_opening_by_naming_the_assistant_is_rejected(reply: str) -> None:
    findings = _findings(_metadata_with_replies({"unsafe": reply}))

    assert [(f.code, f.path) for f in findings] == [
        ("example_reply_third_person", "examples.unsafe.reply")
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "The order ORD-104 qualifies for a refund.",
        "Thanks for waiting. The assistant team has approved it.",
        "These models are not covered by the refund policy.",
        "Theagent field is empty.",
        "The modeling of refunds takes a day.",
        "_The agency_ handles that request.",
    ],
)
def test_a_reply_that_does_not_open_by_naming_the_assistant_is_accepted(reply: str) -> None:
    assert _findings(_metadata_with_replies({"unsafe": reply})) == []


@pytest.mark.parametrize("label", ["unsafe", "safe"])
def test_a_reply_example_without_a_reply_is_a_missing_field(label: str) -> None:
    metadata = reply_metadata("Does the reply assert it?")
    metadata["examples"][label].pop("reply")

    findings = _findings(metadata)

    assert [(f.code, f.path) for f in findings] == [("missing_field", f"examples.{label}.reply")]
    assert "in the assistant's own voice" in findings[0].detail


def test_an_empty_reply_is_rejected() -> None:
    findings = _findings(_metadata_with_replies({"safe": "  \n"}))

    assert [(f.code, f.path) for f in findings] == [("example_reply_empty", "examples.safe.reply")]


def test_a_reply_that_is_not_a_string_is_a_type_error() -> None:
    findings = _findings(_metadata_with_replies({"unsafe": ["Yes."]}))

    assert [(f.code, f.path) for f in findings] == [("type_error", "examples.unsafe.reply")]


def test_the_inconclusive_example_cannot_carry_a_reply() -> None:
    findings = _findings(_metadata_with_replies({"inconclusive": "I can't tell."}))

    assert [(f.code, f.path) for f in findings] == [
        ("unexpected_field", "examples.inconclusive.reply")
    ]


@pytest.mark.parametrize("label", ["unsafe", "safe"])
def test_a_command_attempt_example_cannot_carry_a_reply(label: str) -> None:
    metadata = neutral_metadata()
    metadata["examples"][label]["reply"] = "Done."

    findings = collect_artifact_findings_v2(
        metadata,
        neutral_artifact_plan_v2(),
        {
            "facts": [],
            "operations": [{"name": "inspect_record"}],
            "source_handles": [],
        },
        {"delivery": ["direct_user_message"], "setup_permissions": []},
    )

    assert [(f.code, f.path) for f in findings] == [
        ("unexpected_field", f"examples.{label}.reply")
    ]


def _example_schema(plan: dict[str, Any], label: str) -> dict[str, Any]:
    return _call2_contract_v2(plan)["schema"]["properties"]["examples"]["properties"][label]


def test_the_reply_contract_requires_and_explains_a_reply_on_unsafe_and_safe() -> None:
    contract = _call2_contract_v2(reply_plan())

    for label in ("unsafe", "safe"):
        schema = _example_schema(reply_plan(), label)
        assert schema["required"] == ["label", "description", "reply"]
        assert "capture" not in schema["properties"]
        assert "verbatim" in schema["properties"]["reply"]["description"]
        assert "summary" in schema["properties"]["description"]["description"]
    inconclusive = _example_schema(reply_plan(), "inconclusive")
    assert inconclusive["required"] == ["label", "description"]
    assert "reply" not in inconclusive["properties"]
    assert contract["example_reply_meaning"] == REPLY_MEANING
    assert contract["example_reply_neutral_example"] == neutral_reply_examples()
    assert "example_capture_meaning" not in contract


def test_the_command_attempt_contract_offers_no_reply() -> None:
    contract = _call2_contract_v2(neutral_artifact_plan_v2())

    for label in ("unsafe", "safe", "inconclusive"):
        assert "reply" not in _example_schema(neutral_artifact_plan_v2(), label)["properties"]
    assert "example_reply_meaning" not in contract
    assert "example_reply_neutral_example" not in contract


def test_the_neutral_reply_example_passes_the_checks_it_illustrates() -> None:
    examples = neutral_reply_examples()
    metadata = reply_metadata("Does the reply assert it?")
    for label in ("unsafe", "safe"):
        metadata["examples"][label] = examples[label]

    assert _findings(metadata) == []


def test_the_call2_prompt_of_a_reply_plan_carries_the_reply_meaning() -> None:
    packet = build_call2_packet_v2(
        reply_view(), reply_plan(), reply_inventory(), reply_runtime_contract()
    )

    assert json.dumps(REPLY_MEANING)[1:-1] in packet.user
    assert '"example_reply_neutral_example"' in packet.user
    assert "see example_reply_meaning" in packet.system


def _run(tmp_path: Path, responses: list[bytes], **policy: Any):
    transport = ScriptedAuthoringTransport(responses)
    orchestrator = stage_local_orchestrator(
        policy=unreviewed_policy(**policy),
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="task",
    )
    result = orchestrator.run(reply_view(), reply_inventory(), reply_runtime_contract())
    return result, transport


def _recorded_metadata() -> dict[str, Any]:
    return _metadata_with_replies(_LIVE["recorded"]["SCN-001"])


def test_a_third_person_reply_is_corrected_and_the_package_holds_the_reply(
    tmp_path: Path,
) -> None:
    described = json.dumps(_recorded_metadata()).encode()
    corrected = _metadata_with_replies(_WRITTEN)
    responses = [json.dumps(reply_plan()).encode(), described, json.dumps(corrected).encode()]

    result, transport = _run(tmp_path, responses)

    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "call2",
        "correction",
    ]
    correction = transport.requests[-1]
    assert correction["version"] == CORRECTION_PROMPT_VERSION_V44
    payload = correction["payload"]
    assert payload["current_output"] == described.decode()
    assert [(f["code"], f["path"]) for f in payload["findings"]] == [
        ("example_reply_third_person", "examples.unsafe.reply"),
        ("example_reply_third_person", "examples.safe.reply"),
    ]
    assert payload["response_contract"]["example_reply_meaning"] == REPLY_MEANING
    assert result.status == "accepted", result.findings
    stored = json.loads(result.package.members["examples.json"])
    for label in ("unsafe", "safe"):
        assert stored[label] == {
            "label": "author-proposed",
            "description": _WRITTEN[label],
            "summary": label,
        }
    assert stored["inconclusive"] == {"label": "author-proposed", "description": "inconclusive"}
    assert "judge.json" in result.package.members
    loaded = load_package(tmp_path / "package")
    assert loaded.members["examples.json"] == result.package.members["examples.json"]


def test_a_third_person_reply_spends_the_existing_correction_allowance(tmp_path: Path) -> None:
    described = json.dumps(_recorded_metadata()).encode()
    responses = [json.dumps(reply_plan()).encode(), described]

    result, transport = _run(tmp_path, responses, artifact_max_corrections=0)

    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    assert result.status != "accepted"
    assert result.package_path is None
    assert "example_reply_third_person" in {finding.code for finding in result.findings}


def test_command_attempt_examples_are_stored_as_written() -> None:
    examples = neutral_metadata()["examples"]

    assert package_examples(examples, reply_claim=False) == examples


def test_reply_examples_store_the_reply_as_the_description() -> None:
    examples = reply_metadata("Does the reply assert it?")["examples"]

    stored = package_examples(examples, reply_claim=True)

    assert stored["unsafe"]["description"] == examples["unsafe"]["reply"]
    assert stored["unsafe"]["summary"] == examples["unsafe"]["description"]
    assert "reply" not in stored["unsafe"]
    assert stored["inconclusive"] == examples["inconclusive"]
    assert examples["unsafe"]["description"] == "unsafe"


_SHAPE_ERROR = ("type_error", "explanation")


@pytest.mark.parametrize(
    ("change", "reply_finding"),
    [
        (
            lambda examples: examples["unsafe"].update(reply="The assistant approves it."),
            ("example_reply_third_person", "examples.unsafe.reply"),
        ),
        (
            lambda examples: examples["unsafe"].pop("reply"),
            ("missing_field", "examples.unsafe.reply"),
        ),
        (
            lambda examples: examples["safe"].update(reply="  "),
            ("example_reply_empty", "examples.safe.reply"),
        ),
    ],
    ids=["third_person", "missing", "blank"],
)
def test_a_reply_finding_is_reported_beside_a_shape_error(
    change: Any, reply_finding: tuple[str, str]
) -> None:
    metadata = _metadata_with_replies(_WRITTEN) | {"explanation": 17}
    change(metadata["examples"])

    findings = _findings(metadata)

    assert [(f.code, f.path) for f in findings] == [_SHAPE_ERROR, reply_finding]
    assert {f.stage for f in findings} == {"artifact"}


def test_a_reply_that_is_not_a_string_is_reported_once_beside_other_shape_errors() -> None:
    metadata = _metadata_with_replies({"unsafe": ["Yes."]}) | {"explanation": 17}
    metadata["examples"]["unsafe"]["note"] = "extra"

    findings = _findings(metadata)

    assert [(f.code, f.path) for f in findings] == [
        ("unexpected_field", "examples.unsafe.note"),
        ("type_error", "examples.unsafe.reply"),
        _SHAPE_ERROR,
    ]


def test_a_command_attempt_reply_is_reported_beside_a_shape_error() -> None:
    metadata = neutral_metadata() | {"explanation": 17}
    metadata["examples"]["safe"]["reply"] = "Done."

    findings = collect_artifact_findings_v2(
        metadata,
        neutral_artifact_plan_v2(),
        {"facts": [], "operations": [{"name": "inspect_record"}], "source_handles": []},
        {"delivery": ["direct_user_message"], "setup_permissions": []},
    )

    assert [(f.code, f.path) for f in findings] == [
        _SHAPE_ERROR,
        ("unexpected_field", "examples.safe.reply"),
    ]


@pytest.mark.parametrize(
    "examples",
    [None, "text", [], {"unsafe": "text", "safe": None}, {"unsafe": ["x"], "safe": 3}],
)
def test_malformed_examples_leave_the_reply_checks_to_the_shape_checks(examples: Any) -> None:
    metadata = reply_metadata("Does the reply assert it?") | {"examples": examples}

    findings = _findings(metadata)

    assert findings
    assert not any(f.code.startswith("example_reply") for f in findings)


def test_a_shape_error_and_a_third_person_reply_go_to_one_correction(tmp_path: Path) -> None:
    first = _recorded_metadata() | {"explanation": 17}
    responses = [
        json.dumps(reply_plan()).encode(),
        json.dumps(first).encode(),
        json.dumps(_metadata_with_replies(_WRITTEN)).encode(),
    ]

    result, transport = _run(tmp_path, responses)

    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "call2",
        "correction",
    ]
    payload = transport.requests[-1]["payload"]
    assert [(f["code"], f["path"]) for f in payload["findings"]] == [
        _SHAPE_ERROR,
        ("example_reply_third_person", "examples.unsafe.reply"),
        ("example_reply_third_person", "examples.safe.reply"),
    ]
    assert result.status == "accepted", result.findings


@pytest.mark.parametrize(
    ("change", "reply_finding"),
    [
        (
            lambda examples: examples["unsafe"].pop("reply"),
            ("missing_field", "examples.unsafe.reply"),
        ),
        (
            lambda examples: examples["safe"].update(reply=""),
            ("example_reply_empty", "examples.safe.reply"),
        ),
    ],
    ids=["missing", "blank"],
)
def test_a_shape_error_and_a_missing_or_blank_reply_go_to_one_correction(
    tmp_path: Path, change: Any, reply_finding: tuple[str, str]
) -> None:
    first = _metadata_with_replies(_WRITTEN) | {"explanation": 17}
    change(first["examples"])
    responses = [
        json.dumps(reply_plan()).encode(),
        json.dumps(first).encode(),
        json.dumps(_metadata_with_replies(_WRITTEN)).encode(),
    ]

    result, transport = _run(tmp_path, responses)

    payload = transport.requests[-1]["payload"]
    assert transport.requests[-1]["stage"] == "correction"
    assert [(f["code"], f["path"]) for f in payload["findings"]] == [_SHAPE_ERROR, reply_finding]
    assert result.status == "accepted", result.findings


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda examples: examples["unsafe"].pop("reply"), "missing_field"),
        (lambda examples: examples["safe"].update(reply=" "), "example_reply_empty"),
    ],
    ids=["missing", "blank"],
)
def test_a_missing_or_blank_reply_without_allowance_stops_unresolved(
    tmp_path: Path, change: Any, code: str
) -> None:
    first = _metadata_with_replies(_WRITTEN)
    change(first["examples"])
    responses = [json.dumps(reply_plan()).encode(), json.dumps(first).encode()]

    result, transport = _run(tmp_path, responses, artifact_max_corrections=0)

    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    assert result.status == "unresolved"
    assert result.package_path is None
    assert code in {finding.code for finding in result.findings}


_NON_STRINGS = [None, [], ["The assistant approves it."], 3]
_NEUTRAL_INVENTORY = {
    "facts": [],
    "operations": [{"name": "inspect_record"}],
    "source_handles": [],
}
_NEUTRAL_RUNTIME = {"delivery": ["direct_user_message"], "setup_permissions": []}


def _command_attempt_findings(metadata: dict[str, Any]) -> list[Any]:
    return collect_artifact_findings_v2(
        metadata, neutral_artifact_plan_v2(), _NEUTRAL_INVENTORY, _NEUTRAL_RUNTIME
    )


@pytest.mark.parametrize("value", _NON_STRINGS)
@pytest.mark.parametrize("label", ["unsafe", "safe"])
def test_a_non_string_command_attempt_reply_is_only_an_unexpected_field(
    label: str, value: Any
) -> None:
    metadata = neutral_metadata()
    metadata["examples"][label]["reply"] = value

    findings = _command_attempt_findings(metadata)

    assert [(f.code, f.path) for f in findings] == [
        ("unexpected_field", f"examples.{label}.reply")
    ]


@pytest.mark.parametrize("value", _NON_STRINGS)
def test_a_non_string_command_attempt_reply_beside_a_shape_error_is_only_unexpected(
    value: Any,
) -> None:
    metadata = neutral_metadata() | {"explanation": 17}
    for label in ("unsafe", "safe"):
        metadata["examples"][label]["reply"] = value

    findings = _command_attempt_findings(metadata)

    assert [(f.code, f.path) for f in findings] == [
        _SHAPE_ERROR,
        ("unexpected_field", "examples.unsafe.reply"),
        ("unexpected_field", "examples.safe.reply"),
    ]


@pytest.mark.parametrize("value", [*_NON_STRINGS, "I can't tell."])
@pytest.mark.parametrize("plan", [reply_plan, neutral_artifact_plan_v2], ids=["reply", "command"])
def test_an_inconclusive_reply_is_only_an_unexpected_field(value: Any, plan: Any) -> None:
    metadata = _metadata_with_replies(_WRITTEN) if plan is reply_plan else neutral_metadata()
    metadata["examples"]["inconclusive"]["reply"] = value

    findings = collect_artifact_findings_v2(
        metadata,
        plan(),
        reply_inventory() if plan is reply_plan else _NEUTRAL_INVENTORY,
        reply_runtime_contract() if plan is reply_plan else _NEUTRAL_RUNTIME,
    )

    assert [(f.code, f.path) for f in findings] == [
        ("unexpected_field", "examples.inconclusive.reply")
    ]


def test_a_required_non_string_reply_stays_one_type_error() -> None:
    metadata = _metadata_with_replies({"unsafe": None, "safe": 3}) | {"explanation": 17}

    findings = _findings(metadata)

    assert [(f.code, f.path) for f in findings] == [
        ("type_error", "examples.unsafe.reply"),
        ("type_error", "examples.safe.reply"),
        _SHAPE_ERROR,
    ]


def test_one_correction_removes_a_non_string_command_attempt_reply(tmp_path: Path) -> None:
    view, inventory, runtime, plan, metadata, framed = world_builders(
        "refund", "view", "inventory", "runtime_contract", "plan", "metadata", "framed"
    )
    first = metadata()
    first["examples"]["unsafe"]["reply"] = None
    first["examples"]["safe"]["reply"] = []
    transport = ScriptedAuthoringTransport([json.dumps(plan()), framed(first), framed(metadata())])

    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(view(), inventory(), runtime())

    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "call2",
        "correction",
    ]
    payload = transport.requests[-1]["payload"]
    assert [(f["code"], f["path"]) for f in payload["findings"]] == [
        ("unexpected_field", "examples.unsafe.reply"),
        ("unexpected_field", "examples.safe.reply"),
    ]
    assert all("Remove examples." in f["detail"] for f in payload["findings"])
    assert result.status == "accepted", result.findings
    stored = json.loads(result.package.members["examples.json"])
    assert stored == metadata()["examples"]
