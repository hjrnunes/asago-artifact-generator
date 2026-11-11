"""The injection-spec compiler: a planted_item package becomes one MiDojo injection spec.

This library owns the document the adapter compiles and the reader that parses its sealed
result, so the adapter declares it rather than carrying a copy. It reads the target's
declarations (``declared``), places each seed item in a tool result (``placement``), checks
that the conversation can reach its record (``reachable``), writes the consumer envelope
around MiDojo's document (``document``, ``injection_spec``), and reads the sealed snapshot,
verdict and delivery documents back into an execution receipt (``sealed``).
:mod:`asago_injection_spec.judge_turns` owns the vocabulary compile and the judge service
share.
"""

from __future__ import annotations

from .judge_turns import (
    ALL_TURNS,
    FINAL_TURN,
    JUDGE_TURNS,
    TIMEOUT_SECONDS,
    judged_count,
)

__all__ = [
    "ALL_TURNS",
    "FINAL_TURN",
    "JUDGE_TURNS",
    "TIMEOUT_SECONDS",
    "judged_count",
]
