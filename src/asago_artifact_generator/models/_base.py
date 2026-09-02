"""Shared immutable model and canonical JSON primitives for the consumer.

This module intentionally has no provider, CLI, or platform imports.  The
producer and consumer use the same small canonicalisation rule: NFC-normalise
strings, sort object keys, emit compact UTF-8 JSON, and frame SHA-256 digests
with ``<domain> + NUL``.  Keeping the recipe here gives the inward models and
the bundle loader one implementation to rely on.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StrictStr, StringConstraints, model_validator

SHA256Digest = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenDict(dict[str, Any]):
    """A JSON-friendly mapping that rejects every mutating operation."""

    __slots__ = ()

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("mapping is immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation

    def __ior__(self, other: object) -> FrozenDict:
        del other
        raise TypeError("mapping is immutable")

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        del memo
        return self


class FrozenList(list[Any]):
    """A JSON-friendly sequence that rejects every mutating operation."""

    __slots__ = ()

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("list is immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation

    def __copy__(self) -> FrozenList:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenList:
        del memo
        return self


def freeze_value(value: Any) -> Any:
    """Recursively close arbitrary values retained as context/evidence."""

    if isinstance(value, FrozenDict | FrozenList | BaseModel):
        return value
    return _freeze_collection(value)


def _freeze_collection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return FrozenList(_freeze_items(value))
    if isinstance(value, tuple):
        return tuple(_freeze_items(value))
    return value


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenDict:
    return FrozenDict({str(key): freeze_value(item) for key, item in value.items()})


def _freeze_items(value: list[Any] | tuple[Any, ...]) -> list[Any]:
    return [freeze_value(item) for item in value]


def normalize_unicode(value: Any) -> Any:
    """Return a recursively NFC-normalised JSON-compatible value."""

    if isinstance(value, BaseModel):
        return normalize_unicode(value.model_dump(mode="json"))
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return _normalize_mapping(value)
    if isinstance(value, (list, tuple)):
        return _normalize_items(value)
    if isinstance(value, float):
        return _normalize_float(value)
    return value


def _normalize_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        canonical_key = _normalize_key(key)
        if canonical_key in output:
            raise ValueError("canonical JSON object keys collide after NFC normalization")
        output[canonical_key] = normalize_unicode(item)
    return output


def _normalize_key(key: Any) -> str:
    if not isinstance(key, str):
        raise TypeError("canonical JSON object keys must be strings")
    return unicodedata.normalize("NFC", key)


def _normalize_items(value: list[Any] | tuple[Any, ...]) -> list[Any]:
    return [normalize_unicode(item) for item in value]


def _normalize_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("canonical JSON does not permit NaN or infinity")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize value under the producer's compact canonical JSON contract."""

    return json.dumps(
        normalize_unicode(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_framed_digest(domain: str, value: Any) -> str:
    """Compute SHA-256 over ``UTF-8(domain) + NUL + canonical_json(value)``."""

    return hashlib.sha256(domain.encode("utf-8") + b"\0" + canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    """Hash exact persisted bytes (as opposed to semantic canonical content)."""

    return hashlib.sha256(value).hexdigest()


class ImmutableModel(BaseModel):
    """Closed Pydantic model with recursively immutable nested values."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> ImmutableModel:
        for name, value in self.__dict__.items():
            object.__setattr__(self, name, freeze_value(value))
        return self


__all__ = [
    "FrozenDict",
    "FrozenList",
    "ImmutableModel",
    "SHA256Digest",
    "canonical_json_bytes",
    "compute_framed_digest",
    "freeze_value",
    "normalize_unicode",
    "sha256_bytes",
]
