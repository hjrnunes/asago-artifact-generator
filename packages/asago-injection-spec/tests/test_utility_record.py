"""The utility record: what the evaluation block states and which grades the readers refuse.

MiDojo grades a check its injection spec does not author ``False`` with the validity
``not_applicable``, so the grade's value alone says nothing about whether a check ran. The
receipt states no utility check for every evaluation, and a grade whose validity says one ran is
refused under a reason of its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_injection_spec.sealed import (
    NO_UTILITY_CHECK,
    NOT_APPLICABLE,
    VERDICT_UTILITY_GRADED,
    evaluation_block,
    read_document,
    utility_block,
    verdict_shape,
    verdict_utility_reason,
    verdict_version_reason,
)

SNAPSHOT_ID = "sha256:" + "a" * 64


def verdict_of(*, utility: Any = False, status: str = NOT_APPLICABLE) -> dict[str, Any]:
    """A ``midojo-verdict-v1`` document whose grade states ``utility`` and its validity."""

    return {
        "schema_version": "midojo-verdict-v1",
        "evaluation_id": "evaluation-1",
        "snapshot_id": SNAPSHOT_ID,
        "grade": {
            "security": True,
            "security_reason": "the condition holds on the captured calls",
            "utility": utility,
            "utility_reason": None,
            "grade_validity": {
                "security": {"status": "valid", "reasons": []},
                "utility": {"status": status, "reasons": []},
            },
            "evidence_status": {},
        },
    }


def written(tmp_path: Path, verdict: dict[str, Any]) -> Path:
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(verdict), encoding="utf-8")
    return path


def read(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    return read_document(
        path, "verdict", verdict_shape, verdict_version_reason, verdict_utility_reason
    )


@pytest.mark.parametrize("utility", [False, None, True])
def test_a_grade_not_applicable_for_utility_reads_whatever_value_it_carries(
    utility: Any,
) -> None:
    verdict = verdict_of(utility=utility)

    assert verdict_shape(verdict)
    assert verdict_utility_reason(verdict) is None


@pytest.mark.parametrize("status", ["valid", "invalid"])
@pytest.mark.parametrize("utility", [False, None, True])
def test_a_grade_whose_utility_check_ran_is_refused_whatever_value_it_carries(
    status: str, utility: Any
) -> None:
    verdict = verdict_of(utility=utility, status=status)

    assert verdict_shape(verdict), "the verdict is well formed; the refusal is its own"
    assert verdict_utility_reason(verdict) == VERDICT_UTILITY_GRADED


def test_the_refusal_is_a_reason_no_other_failure_uses() -> None:
    assert VERDICT_UTILITY_GRADED == "verdict_utility_graded"
    assert VERDICT_UTILITY_GRADED not in {"verdict_missing", "verdict_malformed"}
    assert not VERDICT_UTILITY_GRADED.startswith("verdict_version_unknown")


def test_a_document_read_names_the_refusal_not_a_malformed_verdict(tmp_path: Path) -> None:
    document, reason = read(written(tmp_path, verdict_of(utility=True, status="valid")))

    assert (document, reason) == (None, "verdict_utility_graded")


def test_a_document_with_false_beside_not_applicable_is_read(tmp_path: Path) -> None:
    verdict = verdict_of(utility=False, status=NOT_APPLICABLE)

    assert read(written(tmp_path, verdict)) == (verdict, None)


def test_a_malformed_grade_is_malformed_before_it_is_a_utility_grade(tmp_path: Path) -> None:
    verdict = verdict_of(status="valid")
    verdict["grade"]["grade_validity"]["utility"]["status"] = "unclear"

    assert read(written(tmp_path, verdict)) == (None, "verdict_malformed")


def test_an_unknown_version_is_named_before_the_utility_grade(tmp_path: Path) -> None:
    verdict = verdict_of(status="valid")
    verdict["schema_version"] = "midojo-verdict-v9"

    assert read(written(tmp_path, verdict)) == (None, "verdict_version_unknown:midojo-verdict-v9")


def test_the_utility_block_states_that_no_check_applied() -> None:
    assert utility_block({"snapshot_id": SNAPSHOT_ID}) == {
        "value": None,
        "applicability": NOT_APPLICABLE,
        "reason": NO_UTILITY_CHECK,
        "snapshot_id": SNAPSHOT_ID,
        "pairing_id": None,
    }


def test_the_utility_block_of_a_run_with_no_snapshot_names_none() -> None:
    assert utility_block(None)["snapshot_id"] is None


@pytest.mark.parametrize("utility", [False, None, True])
def test_the_evaluation_block_never_copies_the_grade_s_utility(utility: Any) -> None:
    block = evaluation_block("midojo", None, verdict_of(utility=utility))

    assert block["utility"] == {
        "value": None,
        "applicability": "not_applicable",
        "reason": "no_utility_check",
        "snapshot_id": None,
        "pairing_id": None,
    }
    assert "utility" not in block["verdict"]


def test_the_evaluation_block_without_a_verdict_still_states_the_record() -> None:
    assert evaluation_block("midojo", None, None)["utility"] == utility_block(None)
