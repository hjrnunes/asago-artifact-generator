"""Every terminal reason the authoring code can emit has a one-line meaning."""

from __future__ import annotations

from asago_artifact_generator.report.reasons import REASON_MEANINGS, reason_meaning

from .reason_codes import collect_reasons


def test_every_emittable_reason_has_a_meaning() -> None:
    collected = collect_reasons()

    assert collected.unresolved == [], (
        "these sites build a finding code or status the coverage scan cannot resolve; "
        "pass a literal or a module constant, or extend tests/reason_codes.py"
    )
    missing = {
        reason: sorted(sites)
        for reason, sites in collected.reasons.items()
        if reason not in REASON_MEANINGS
    }
    assert missing == {}, "add a meaning to report/reasons.py for each reason"


def test_the_scan_finds_the_known_kinds_of_reason() -> None:
    reasons = collect_reasons().reasons

    assert {"correction_limit_exhausted", "review_schema", "trailing_content"} <= set(reasons)
    assert {"unresolved", "review_unavailable", "needs_plan_revision"} <= set(reasons)
    assert {"oracle_quiet_on_unsafe_example", "shape_channel_unsupported"} <= set(reasons)
    assert {"undocumented_selector", "plan_binding_validation"} <= set(reasons)


def test_each_meaning_is_one_sentence_line() -> None:
    for reason, meaning in REASON_MEANINGS.items():
        assert "\n" not in meaning, reason
        assert meaning.endswith("."), reason
        assert meaning[0].isupper(), reason


def test_an_unknown_reason_reads_as_unlisted() -> None:
    assert reason_meaning("no_such_reason") == "No meaning is recorded for this reason."
    assert reason_meaning("review_schema") == REASON_MEANINGS["review_schema"]
