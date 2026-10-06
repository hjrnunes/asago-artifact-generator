"""Tests for the helpers shared by the vendored-contract readers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from asago_artifact_generator.contract_kit import (
    canonical_json,
    framed_digest,
    sha256_hex,
    verify_contract_lock,
)


class _KitError(ValueError):
    pass


def test_canonical_json_sorts_compacts_and_keeps_unicode() -> None:
    assert canonical_json({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'


def test_canonical_json_normalizes_strings_and_keys_only_on_request() -> None:
    decomposed = "e\u0301"
    value = {decomposed: [decomposed]}

    assert canonical_json(value) == f'{{"{decomposed}":["{decomposed}"]}}'
    assert canonical_json(value, nfc=True) == '{"é":["é"]}'


def test_canonical_json_rejects_nan_only_when_asked() -> None:
    assert canonical_json(float("nan")) == "NaN"
    with pytest.raises(ValueError):
        canonical_json(float("nan"), allow_nan=False)


def test_framed_digest_frames_domain_with_a_nul_separator() -> None:
    expected = hashlib.sha256(b"domain\0" + b'{"a":"\xc3\xa9"}').hexdigest()

    assert framed_digest("domain", {"a": "e\u0301"}, nfc=True) == expected
    assert sha256_hex(b"x") == hashlib.sha256(b"x").hexdigest()


def _verify(root: Path) -> None:
    verify_contract_lock(
        root,
        _KitError,
        lock_label="kit lock",
        member_label="kit",
        metadata={"authority": "owner", "contract": "kit"},
        metadata_message="kit metadata mismatch",
    )


@pytest.mark.parametrize(
    ("lock", "message"),
    [
        (None, "^cannot read kit lock: "),
        ("{", "^cannot read kit lock: "),
        ({"authority": "owner"}, "^kit metadata mismatch$"),
        ({"authority": "other", "contract": "kit"}, "^kit metadata mismatch$"),
        (
            {"authority": "owner", "contract": "kit", "files": {"missing.json": "0" * 64}},
            "^kit digest mismatch: missing.json$",
        ),
        (
            {"authority": "owner", "contract": "kit", "files": {"member.json": "0" * 64}},
            "^kit digest mismatch: member.json$",
        ),
    ],
)
def test_verify_contract_lock_rejections(
    tmp_path: Path, lock: dict | str | None, message: str
) -> None:
    (tmp_path / "member.json").write_bytes(b"{}")
    if lock is not None:
        text = lock if isinstance(lock, str) else json.dumps(lock)
        (tmp_path / "CONTRACT.lock").write_text(text, encoding="utf-8")

    with pytest.raises(_KitError, match=message):
        _verify(tmp_path)


def test_verify_contract_lock_accepts_matching_metadata_and_digests(tmp_path: Path) -> None:
    (tmp_path / "member.json").write_bytes(b"{}")
    lock = {
        "authority": "owner",
        "contract": "kit",
        "files": {"member.json": sha256_hex(b"{}")},
    }
    (tmp_path / "CONTRACT.lock").write_text(json.dumps(lock), encoding="utf-8")

    _verify(tmp_path)
