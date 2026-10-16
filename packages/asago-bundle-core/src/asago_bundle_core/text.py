"""The one text form of every JSON document an adapter writes, and the split of JSON lines."""

from __future__ import annotations

import json
from typing import Any


def canonical_text(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def jsonl_lines(text: str) -> list[str]:
    """Split JSONL *text* into its non-blank record lines.

    Splits on ``"\\n"`` only: ``json.dumps(..., ensure_ascii=False)`` leaves
    U+0085, U+2028 and U+2029 raw, and ``str.splitlines()`` would cut a record
    at each of them.
    """

    return [line for line in text.split("\n") if line.strip()]
