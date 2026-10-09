"""The one text form of every JSON document an adapter writes."""

from __future__ import annotations

import json
from typing import Any


def canonical_text(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
