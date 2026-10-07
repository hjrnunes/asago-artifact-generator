"""Write the artifact-package contract cases and their lock entries.

Each case is one JSON file holding a package as data: ``manifest`` is the
manifest.json value and ``files`` maps each file in the package directory to
its UTF-8 text. A reader materializes the directory and loads it. Every
reader runs the same cases: ``valid/`` cases must load, and each
``invalid/`` case must fail with the stable code that
``expected-violations.json`` names. ``metadata-policy.json`` lists manifest
metadata the closed policy accepts, and rejected metadata with the exact
violation paths.

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
V3 = "artifact-package-v3"
V4 = "artifact-package-v4"
_HANDOFF_V3 = "scenario-handoff-v3"
_HANDOFF_V4 = "scenario-handoff-v4"
_CONDITION = '{\n  "comparisons": []\n}\n'
_V3_FILES = {
    "plan.json": '{"runtime_contract":{"setup_permissions":[]}}\n',
    "stimulus.json": '{"user_text":"hello"}\n',
    "setup.json": "[]\n",
    "bindings.json": "[]\n",
    "prerequisites.json": "[]\n",
    "inputs.json": '{"runtime_contract":{},"inventory":{}}\n',
    "tool_call_condition.json": _CONDITION,
}
_V4_FILES = {
    **_V3_FILES,
    "stimulus.json": '{"user_text":"hello","history":[],"mode":"single","turn_count":1}\n',
    "seed.json": '{"schema_version":"mini-agents-seed-v1","items":[]}\n',
}
_AUTHORING = {"interface": "artifact-authoring-v1", "max_retries": 0}
_CREATION_MODEL = {"model": "configured-private-authoring"}


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


def _package(
    version: str,
    files: dict[str, str],
    *,
    input_kind: str = _HANDOFF_V3,
    description: str,
) -> dict[str, Any]:
    manifest = _sign(
        {
            "schema_version": version,
            "package_id": "pkg-1",
            "scenario_id": "SCN-1",
            "input_kind": input_kind,
            "source_digests": {"input": "a" * 64},
            "members": [_record(path, files[path]) for path in sorted(files)],
            "authoring": dict(_AUTHORING),
            "runtime_capabilities": {},
            "creation_model": dict(_CREATION_MODEL),
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


def _shared_invalid(base: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Return the rejections every package version shares, by case name."""

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
        "handoff-v1-input-kind": (
            "input_kind_unsupported",
            _changed(
                base, "input_kind names a retired handoff.", input_kind="scenario-handoff-v1"
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
    files = {**base["files"], **extra}
    version = base["manifest"]["schema_version"]
    kind = base["manifest"]["input_kind"]
    return _package(version, files, input_kind=kind, description=description)


def _v3_cases() -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, dict[str, Any]]]]:
    base = _package(V3, _V3_FILES, description="A v3 package with a tool-call condition.")
    invalid = _shared_invalid(base)
    invalid["v4-input-kind"] = (
        "input_kind_unsupported",
        _changed(base, "A v3 package names a v4 handoff.", input_kind=_HANDOFF_V4),
    )
    return {"minimal": base}, invalid


def _v4_cases() -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, dict[str, Any]]]]:
    base = _package(
        V4, _V4_FILES, description="A v4 package with a seed member, from a v3 handoff."
    )
    valid = {
        "minimal": base,
        "from-handoff-v4": _package(
            V4,
            _V4_FILES,
            input_kind=_HANDOFF_V4,
            description="A v4 package with a seed member, from a v4 handoff.",
        ),
    }
    invalid = _shared_invalid(base)
    return valid, invalid


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


def expected_files() -> dict[str, str]:
    """Return every generated case file's text by path relative to the contract root."""

    files: dict[str, str] = {"metadata-policy.json": _json_text(_metadata_policy())}
    for version, (valid, invalid) in ((V3, _v3_cases()), (V4, _v4_cases())):
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
