"""Value predicates the package values share with the consumer's readers."""

from __future__ import annotations

from typing import Any

SHA256_HEX_LENGTH = 64


def is_sha256_hex(value: Any) -> bool:
    """Return whether ``value`` is a lowercase hexadecimal SHA-256 digest."""

    return (
        isinstance(value, str)
        and len(value) == SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )
