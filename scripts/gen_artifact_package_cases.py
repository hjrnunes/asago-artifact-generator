"""Write the artifact-package contract cases and their lock entries.

Each case is one JSON file holding a package as data: ``manifest`` is the
manifest.json value and ``files`` maps each file in the package directory to
its UTF-8 text. A reader materializes the directory and loads it. Every
reader runs the same cases: ``valid/`` cases must load, and each
``invalid/`` case must fail with the stable code that
``expected-violations.json`` names. ``metadata-policy.json`` lists manifest
metadata the closed policy accepts, and rejected metadata with the exact
violation paths.

``artifact-package-v5/`` holds the cases of the package version the writer writes, and
``artifact-package-v4/`` those of the version before it, which readers still load.

``artifact-seed-v1/`` holds the cases of the seed member's schema, each one
seed document: ``valid/`` cases pass its ``schema.json``, and each
``invalid/`` case fails first at the path and keyword that its
``expected-violations.json`` names. Every package case carries a valid seed.

``binding-cases/`` holds hand-written binding conformance cases: binding
declarations, the inventory and stimulus they apply to, and the expected outcome
of each binding check. Authoring and execute run them through their own
functions. This script copies them unchanged and locks them.

Run ``uv run python scripts/gen_artifact_package_cases.py`` after changing a
case; ``tests/test_artifact_package_cases.py`` checks that the committed files
equal this script's output.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "artifact-package"
V4 = "artifact-package-v4"
V5 = "artifact-package-v5"
BINDING_CASES = "binding-cases"
_HANDOFF_V5 = "scenario-handoff-v5"
SEED = "artifact-seed-v1"
_SEED_ITEM = {
    "slot": "records.note",
    "record_ref": "REC-1",
    "mode": "replace",
    "content": "Planted text.",
}
_SEED_DOCUMENT = {
    "schema_version": SEED,
    "carrier_operation": "read_record",
    "items": [_SEED_ITEM],
}
_CONDITION = '{\n  "comparisons": []\n}\n'
_FILES = {
    "plan.json": '{"runtime_contract":{"setup_permissions":[]}}\n',
    "stimulus.json": '{"user_text":"hello","history":[],"mode":"single","turn_count":1}\n',
    "setup.json": "[]\n",
    "bindings.json": "[]\n",
    "prerequisites.json": "[]\n",
    "inputs.json": '{"runtime_contract":{},"inventory":{}}\n',
    "tool_call_condition.json": _CONDITION,
    "seed.json": json.dumps(_SEED_DOCUMENT, separators=(",", ":")) + "\n",
}
_V4_AUTHORING = {"interface": "artifact-authoring-v1", "max_retries": 0}
_V4_CREATION_MODEL = {"model": "configured-private-authoring"}
# A v5 manifest records only the authoring outcome and an empty creation model.
_V5_AUTHORING = {
    "status": "accepted",
    "terminal_status": "accepted",
    "review_status": {"artifact": "accepted"},
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _media_type(path: str) -> str:
    return "application/json" if path.endswith(".json") else "application/octet-stream"


def _record(path: str, text: str) -> dict[str, Any]:
    data = text.encode("utf-8")
    return {
        "path": path,
        "media_type": _media_type(path),
        "length": len(data),
        "sha256": _sha256(data),
    }


def _sign(manifest: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    return {**unsigned, "manifest_digest": _sha256(_canonical(unsigned))}


def _package(files: dict[str, str], *, description: str, version: str) -> dict[str, Any]:
    v4 = version == V4
    manifest = _sign(
        {
            "schema_version": version,
            "package_id": "pkg-1",
            "scenario_id": "SCN-1",
            "input_kind": _HANDOFF_V5,
            "source_digests": {"input": "a" * 64},
            "members": [_record(path, files[path]) for path in sorted(files)],
            "authoring": copy.deepcopy(_V4_AUTHORING if v4 else _V5_AUTHORING),
            "runtime_capabilities": {},
            "creation_model": dict(_V4_CREATION_MODEL) if v4 else {},
        }
    )
    return {"description": description, "manifest": manifest, "files": dict(sorted(files.items()))}


def _changed(
    base: dict[str, Any], description: str, *, resign: bool = True, **changes: Any
) -> dict[str, Any]:
    manifest = {**copy.deepcopy(base["manifest"]), **changes}
    return {
        "description": description,
        "manifest": _sign(manifest) if resign else manifest,
        "files": dict(base["files"]),
    }


def _with_member(base: dict[str, Any], description: str, index: int, **change: Any):
    members = copy.deepcopy(base["manifest"]["members"])
    members[index] = {**members[index], **change}
    return _changed(base, description, members=members)


def _with_files(base: dict[str, Any], description: str, files: dict[str, str]):
    return {"description": description, "manifest": base["manifest"], "files": files}


def _invalid_cases(base: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Return the invalid cases derived from ``base``, each with its code, by case name."""

    first = base["manifest"]["members"][0]["path"]
    tampered = {**base["files"], first: "changed"}
    return {
        "manifest-not-object": (
            "manifest_not_object",
            {"description": "The manifest is a JSON array.", "manifest": [], "files": {}},
        ),
        "unknown-manifest-field": (
            "manifest_fields_invalid",
            _changed(base, "The manifest carries a field the schema does not define.", extra=1),
        ),
        "detector-interface-field": (
            "manifest_fields_invalid",
            _changed(
                base,
                "The manifest carries the retired v2 detector_interface field.",
                detector_interface="evaluate(evidence: dict) -> dict",
            ),
        ),
        "blank-package-id": (
            "identity_blank",
            _changed(base, "package_id is the empty string.", package_id=""),
        ),
        "uppercase-source-digest": (
            "source_digests_invalid",
            _changed(
                base, "A source digest uses uppercase hex.", source_digests={"input": "A" * 64}
            ),
        ),
        "empty-source-digests": (
            "source_digests_invalid",
            _changed(base, "source_digests is empty.", source_digests={}),
        ),
        "stale-manifest-digest": (
            "manifest_digest_mismatch",
            _changed(
                base,
                "package_id changed after the manifest digest was computed.",
                resign=False,
                package_id="pkg-2",
            ),
        ),
        "empty-members": (
            "members_empty",
            _changed(base, "The manifest lists no members.", members=[]),
        ),
        "creation-model-not-object": (
            "metadata_not_object",
            _changed(base, "creation_model is a string.", creation_model="model"),
        ),
        "secret-runtime-capability": (
            "metadata_secret",
            _changed(
                base,
                "runtime_capabilities carries a secret-bearing key.",
                runtime_capabilities={"api_key": "x"},
            ),
        ),
        "handoff-v3-input-kind": (
            "input_kind_unsupported",
            _changed(
                base, "input_kind names a retired handoff.", input_kind="scenario-handoff-v3"
            ),
        ),
        "reference-task-input-kind": (
            "input_kind_unsupported",
            _changed(
                base,
                "input_kind names an input the consumer no longer emits.",
                input_kind="reference-task",
            ),
        ),
        "native-semantic-yaml-input-kind": (
            "input_kind_unsupported",
            _changed(
                base,
                "input_kind names the retired native YAML input.",
                input_kind="native-semantic-yaml",
            ),
        ),
        "schema-version-v1": (
            "schema_version_unknown",
            _changed(
                base,
                "schema_version names the first package version.",
                schema_version="artifact-package-v1",
            ),
        ),
        "schema-version-v2": (
            "schema_version_unknown",
            _changed(
                base,
                "schema_version names a retired package version.",
                schema_version="artifact-package-v2",
            ),
        ),
        "member-length-mismatch": (
            "member_length_mismatch",
            _with_member(base, "The first member record's length is wrong.", 0, length=1),
        ),
        "member-media-type-mismatch": (
            "member_media_type_mismatch",
            _with_member(
                base, "The first member record's media type is wrong.", 0, media_type="text/plain"
            ),
        ),
        "member-path-backslash": (
            "member_path_invalid",
            _with_member(base, "A member path contains a backslash.", 0, path="a\\b"),
        ),
        "member-path-escapes": (
            "member_path_escapes",
            _with_member(base, "A member path climbs out of the package.", 0, path="../plan.json"),
        ),
        "member-path-non-canonical": (
            "member_path_non_canonical",
            _with_member(
                base, "A member path has a doubled separator.", 0, path="authoring//plan.json"
            ),
        ),
        "member-path-unexpected": (
            "member_path_unexpected",
            _with_member(base, "A member path is not a package member name.", 0, path="notes.txt"),
        ),
        "detector-member": (
            "member_path_unexpected",
            _with_files_listed(
                base,
                "The package carries detector code.",
                {"detector.py": "def evaluate(evidence):\n    return {}\n"},
            ),
        ),
        "duplicate-member": (
            "member_duplicate",
            _changed(
                base,
                "The manifest lists one member twice.",
                members=[*base["manifest"]["members"], base["manifest"]["members"][0]],
            ),
        ),
        "unlisted-file": (
            "member_set_mismatch",
            _with_files(
                base,
                "The directory holds a file the manifest does not list.",
                {**base["files"], "notes.txt": "x"},
            ),
        ),
        "tampered-member": (
            "member_digest_mismatch",
            _with_files(base, "A member's bytes changed after the manifest was signed.", tampered),
        ),
    }


def _with_files_listed(base: dict[str, Any], description: str, extra: dict[str, str]):
    return _package(
        {**base["files"], **extra},
        description=description,
        version=base["manifest"]["schema_version"],
    )


_Cases = tuple[dict[str, dict[str, Any]], dict[str, tuple[str, dict[str, Any]]]]


def _v4_cases() -> _Cases:
    base = _package(
        _FILES, description="A v4 package with a seed member, from a v5 handoff.", version=V4
    )
    return {"minimal": base}, _invalid_cases(base)


def _v5_cases() -> _Cases:
    """Return the v5 cases: v4's, and the v4 members and metadata a v5 package refuses."""

    base = _package(
        _FILES, description="A v5 package with a seed member, from a v5 handoff.", version=V5
    )
    invalid = _invalid_cases(base)
    for name, member, text in (
        ("retired-source-hashes-member", "source-hashes.json", '{"input":"' + "a" * 64 + '"}\n'),
        ("retired-request-copy-member", "authoring/01-call1.json", '{"stage":"call1"}\n'),
        ("retired-transformations-member", "authoring/transformations.json", "[]\n"),
    ):
        invalid[name] = (
            "member_path_unexpected",
            _with_files_listed(
                base,
                f"The package carries {member}, which only a v4 package held.",
                {member: text},
            ),
        )
    invalid["authoring-ledger-field"] = (
        "authoring_field_unexpected",
        _changed(
            base,
            "authoring carries the ledger a v4 manifest held.",
            authoring={**_V5_AUTHORING, "ledger": [{"stage": "call1"}]},
        ),
    )
    invalid["authoring-plan-review-status"] = (
        "authoring_field_unexpected",
        _changed(
            base,
            "authoring.review_status carries the plan review a v4 manifest held.",
            authoring={
                **_V5_AUTHORING,
                "review_status": {"artifact": "accepted", "plan": "accepted"},
            },
        ),
    )
    invalid["creation-model-not-empty"] = (
        "creation_model_not_empty",
        _changed(
            base,
            "creation_model names the authoring model, as a v4 manifest could.",
            creation_model=dict(_V4_CREATION_MODEL),
        ),
    )
    invalid["schema-version-v6"] = (
        "schema_version_unknown",
        _changed(
            base, "schema_version names a version after v5.", schema_version="artifact-package-v6"
        ),
    )
    return {"minimal": base}, invalid


_USAGE_TRIPLE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _available(value: Any) -> dict[str, Any]:
    return {"usage": [{"availability": "available", "value": value}]}


def _metadata_policy() -> dict[str, Any]:
    allowed = [
        (
            "captured-provider-usage",
            {
                "interface": "artifact-authoring-v1",
                "usage": [
                    {
                        "availability": "available",
                        "value": {
                            "prompt_tokens": 4,
                            "completion_tokens": 3,
                            "total_tokens": 7,
                            "prompt_tokens_details": {
                                "cached_tokens": 1,
                                "nested": {"audio_tokens": 0},
                            },
                            "completion_tokens_details": {"reasoning_tokens": 2},
                        },
                    }
                ],
            },
        ),
        (
            "direct-usage-with-empty-details",
            {
                "usage": {
                    "availability": "available",
                    "value": {
                        **{"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                        "prompt_tokens_details": None,
                        "completion_tokens_details": {},
                    },
                }
            },
        ),
        ("raw-usage-counters", {"usage": {"prompt_tokens": 1, "prompt_tokens_details": None}}),
        (
            "unavailable-usage-list",
            {
                "usage": [
                    {"availability": "unavailable", "reason": "provider did not report usage"}
                ]
            },
        ),
        (
            "unavailable-usage-direct",
            {"usage": {"availability": "unavailable", "reason": "not reported"}},
        ),
        ("plain-authoring-controls", {"interface": "artifact-authoring-v1", "max_retries": 0}),
        (
            "model-controls",
            {
                "interface": "artifact-authoring-v2",
                "context_window_tokens": 32768,
                "max_completion_tokens": 8192,
            },
        ),
        (
            "recorded-model-controls",
            {
                "ledger": [
                    {
                        "controls": {
                            "context_window_tokens": 32768,
                            "max_completion_tokens": 8192,
                        },
                        "review": {
                            "effective_controls": {
                                "context_window_tokens": 32768,
                                "max_completion_tokens": 8192,
                            }
                        },
                    }
                ]
            },
        ),
    ]
    rejected: list[tuple[str, dict[str, Any], list[str]]] = [
        ("string-counter", _available({"prompt_tokens": "4"}), ["usage[0].value.prompt_tokens"]),
        ("negative-counter", _available({"prompt_tokens": -1}), ["usage[0].value.prompt_tokens"]),
        ("boolean-counter", _available({"prompt_tokens": True}), ["usage[0].value.prompt_tokens"]),
        (
            "unknown-counter",
            _available({**_USAGE_TRIPLE, "total_tokens_extra": 2}),
            ["usage[0].value.total_tokens_extra"],
        ),
        (
            "unknown-usage-key",
            _available({"prompt_tokens": 1, "unexpected": 2}),
            ["usage[0].value.unexpected"],
        ),
        (
            "secret-in-detail-map",
            _available({**_USAGE_TRIPLE, "prompt_tokens_details": {"api_key": 1}}),
            ["usage[0].value.prompt_tokens_details.api_key"],
        ),
        (
            "api-token-in-detail-map",
            _available({**_USAGE_TRIPLE, "prompt_tokens_details": {"api_tokens": 1}}),
            ["usage[0].value.prompt_tokens_details.api_tokens"],
        ),
        (
            "non-object-detail-map",
            {"usage": {"prompt_tokens": 1, "prompt_tokens_details": 5}},
            ["usage.prompt_tokens_details"],
        ),
        (
            "string-detail-value",
            {"usage": {"prompt_tokens": 1, "prompt_tokens_details": {"cached_tokens": "1"}}},
            ["usage.prompt_tokens_details.cached_tokens"],
        ),
        ("no-counter", {"usage": {"zeta": 1, "alpha": 2}}, ["usage.alpha", "usage.zeta", "usage"]),
        ("secret-in-list", {"notes": [{"api_key": "redacted"}]}, ["notes[0].api_key"]),
        ("empty-usage", {"usage": {}}, ["usage"]),
        ("string-usage", {"usage": "not-a-list"}, ["usage"]),
        ("scalar-usage-value", _available(7), ["usage[0].value"]),
        ("empty-usage-value", _available({}), ["usage[0].value"]),
        (
            "blank-unavailable-reason",
            {"usage": [{"availability": "unavailable", "reason": ""}]},
            ["usage[0]"],
        ),
        (
            "extra-completion-control",
            {"max_completion_tokens_extra": 8192},
            ["max_completion_tokens_extra"],
        ),
        (
            "extra-context-control",
            {"context_window_tokens_extra": 32768},
            ["context_window_tokens_extra"],
        ),
    ]
    for key in (
        "auth_token",
        "auth",
        "session_token",
        "session",
        "access_token",
        "access",
        "bearer_token",
        "bearer",
        "api_key",
        "apikey",
        "credential",
        "password",
        "authorization_header",
        "endpoint",
        "base_url",
        "base-url",
        "baseurl",
    ):
        rejected.append((f"secret-key-{key}", {key: "redacted"}, [key]))
    return {
        "description": (
            "Manifest metadata (authoring, runtime_capabilities, creation_model) under the "
            "closed secret policy. Each rejected entry lists the violation paths in the "
            "order the policy reports them."
        ),
        "allowed": [{"name": name, "metadata": value} for name, value in allowed],
        "rejected": [
            {"name": name, "metadata": value, "paths": paths} for name, value, paths in rejected
        ],
    }


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _seed(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(_SEED_DOCUMENT), **changes}


def _seed_with_item(**changes: Any) -> dict[str, Any]:
    return _seed(items=[{**_SEED_ITEM, **changes}])


def _seed_cases() -> tuple[dict[str, Any], dict[str, tuple[tuple[str, str], Any]]]:
    """Return the valid seeds, and each invalid seed with the path and keyword it fails at."""

    no_record = {key: value for key, value in _SEED_ITEM.items() if key != "record_ref"}
    no_carrier = {key: value for key, value in _seed().items() if key != "carrier_operation"}
    valid = {"minimal": _seed(), "append": _seed_with_item(mode="append")}
    invalid = {
        "unknown-schema-version": (
            ("/schema_version", "const"),
            _seed(schema_version="artifact-seed-v2"),
        ),
        "missing-carrier-operation": (("/", "required"), no_carrier),
        "unknown-top-level-key": (("/", "additionalProperties"), _seed(note="x")),
        "no-items": (("/items", "minItems"), _seed(items=[])),
        "item-missing-record": (("/items/0", "required"), _seed(items=[no_record])),
        "item-unknown-key": (
            ("/items/0", "additionalProperties"),
            _seed_with_item(max_chars=2000),
        ),
        "item-mode-unknown": (("/items/0/mode", "enum"), _seed_with_item(mode="prepend")),
        "item-blank-content": (("/items/0/content", "minLength"), _seed_with_item(content="")),
    }
    return valid, invalid


def _seed_files() -> dict[str, str]:
    """Return each seed case file's text by path relative to the contract root."""

    valid, invalid = _seed_cases()
    files = {f"{SEED}/valid/{name}.json": _json_text(seed) for name, seed in valid.items()}
    files.update(
        {f"{SEED}/invalid/{name}.json": _json_text(seed) for name, (_, seed) in invalid.items()}
    )
    expected = {
        f"invalid/{name}.json": {"path": path, "keyword": keyword}
        for name, ((path, keyword), _) in sorted(invalid.items())
    }
    files[f"{SEED}/expected-violations.json"] = _json_text(expected)
    return files


def _binding_cases() -> dict[str, str]:
    """Return each hand-written binding case's text by path relative to the contract root."""

    return {
        f"{BINDING_CASES}/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((CONTRACT_ROOT / BINDING_CASES).glob("*.json"))
    }


def expected_files() -> dict[str, str]:
    """Return every case file's text by path relative to the contract root."""

    files: dict[str, str] = {"metadata-policy.json": _json_text(_metadata_policy())}
    files.update(_binding_cases())
    files.update(_seed_files())
    for version, (valid, invalid) in ((V4, _v4_cases()), (V5, _v5_cases())):
        for name, case in valid.items():
            files[f"{version}/valid/{name}.json"] = _json_text(case)
        for name, (_, case) in invalid.items():
            files[f"{version}/invalid/{name}.json"] = _json_text(case)
        expected = {f"invalid/{name}.json": code for name, (code, _) in sorted(invalid.items())}
        files[f"{version}/expected-violations.json"] = _json_text(expected)
    return dict(sorted(files.items()))


def expected_lock(lock: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
    """Return ``lock`` with every generated file's digest added to its files."""

    locked = {key: digest for key, digest in lock["files"].items() if key.endswith("/schema.json")}
    locked.update({path: _sha256(text.encode("utf-8")) for path, text in files.items()})
    return {**lock, "files": dict(sorted(locked.items()))}


def main() -> int:
    files = expected_files()
    lock_path = CONTRACT_ROOT / "CONTRACT.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for relative, text in files.items():
        target = CONTRACT_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    lock_path.write_text(json.dumps(expected_lock(lock, files), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
