"""Isolated execution of the exact detector source in an artifact package.

The public seam in this module is :func:`execute_detector`.  It verifies the
immutable package, mounts the package and evidence read-only in the official
Python image, and returns either a validated rich detector result or a
separate runtime failure.  It deliberately does not interpret scenario
meaning or provide a detector fallback.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import textwrap
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .package_io import ArtifactPackage, PackageIntegrityError, load_package

DOCKER = "/usr/local/bin/docker"
PYTHON_IMAGE = "python:3.12-slim"
CLAIM_LEVELS = frozenset({"command_attempt", "reply", "returned_result", "state_effect"})
OUTCOMES = frozenset({"detected", "not_detected", "inconclusive"})
JUDGE_VERDICTS = frozenset({"supported", "contradicted", "unresolved"})
_MISSING = object()
RESULT_FIELDS = frozenset({"outcome", "reason", "evidence_refs", "claim_level"})
EVIDENCE_PACKET_ROOTS = frozenset(
    {
        "user_text",
        "history",
        "messages",
        "tool_calls",
        "bindings",
        "binding_provenance",
        "setup_outputs",
        "snapshots",
        "transport",
        "parse_errors",
        "correlation",
        "source",
        "availability",
        "completeness",
    }
)
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_MEMORY = "128m"
MAX_PIDS = "64"


class DetectorRuntimeError(ValueError):
    """Raised for a missing, invalid, or unverifiable generated detector."""


@dataclass(frozen=True)
class DetectorExecution:
    """The rich result plus independently recorded runtime status."""

    status: str
    result: dict[str, Any] | None
    failure: str | None
    package_digest: str | None
    package_digest_after: str | None
    detector_sha256: str | None
    detector_sha256_after: str | None
    docker_argv: tuple[str, ...]
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def runtime_failure(self) -> str | None:
        """Expose the failure using the receipt vocabulary."""

        return self.failure

    @property
    def runtime_status(self) -> str:
        """Expose execution status without conflating it with the result."""

        return self.status

    @property
    def package_digest_before(self) -> str | None:
        """Expose the pre-execution package digest."""

        return self.package_digest

    @property
    def detector_sha256_before(self) -> str | None:
        """Expose the pre-execution detector digest."""

        return self.detector_sha256

    @property
    def rich_result(self) -> dict[str, Any] | None:
        """Expose the validated detector result for receipt writers."""

        return self.result

    @property
    def garak_value(self) -> int | None:
        """Map only a completed rich result at the reporting edge."""

        from .reporting import garak_value

        return garak_value(self)


def resolve_docker_path() -> str:
    """Return the Docker CLI to run, preferring the documented ``DOCKER`` path.

    Hosts such as Linux CI runners install the CLI at ``/usr/bin/docker``, so
    the fixed path is used only when it is executable; otherwise the first
    ``docker`` on ``PATH`` is used.  When neither exists, ``DOCKER`` is
    returned so the runtime-unavailable failure names the documented location.
    """

    if os.access(DOCKER, os.X_OK):
        return DOCKER
    return shutil.which("docker") or DOCKER


def execute_detector(
    package: str | Path | ArtifactPackage,
    evidence: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    docker_path: str | None = None,
    image: str = PYTHON_IMAGE,
) -> DetectorExecution:
    """Execute the package's exact ``detector.py`` bytes in constrained Docker.

    ``status`` is ``completed``, ``failed`` or ``timeout``.  A non-completed
    execution never carries a detector result, even if generated code printed
    a plausible verdict before failing.  ``docker_path`` defaults to
    :func:`resolve_docker_path`.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if docker_path is None:
        docker_path = resolve_docker_path()
    argv: tuple[str, ...] = ()
    package_path: Path | None = None
    loaded: ArtifactPackage | None = None
    try:
        package_path, loaded = _load_package_for_execution(package)
        detector = loaded.members.get("detector.py")
        if detector is None:
            raise DetectorRuntimeError("package is missing detector.py")
        detector_sha = _sha256(detector)
        _validate_source_interface(detector)
        if not isinstance(evidence, dict):
            raise DetectorRuntimeError("evidence packet must be an object")
        evidence = normalize_evidence_packet(
            evidence,
            judge_enabled="judge.json" in loaded.members,
        )
        access_findings = validate_detector_evidence_access(
            detector,
            observations=_package_json_member(loaded, "observations.json", default={}),
            bindings=_package_json_member(loaded, "bindings.json", default=[]),
            judge_enabled="judge.json" in loaded.members,
        )
        if access_findings:
            raise DetectorRuntimeError(access_findings[0]["detail"])
        evidence_bytes = _json_bytes(evidence)
        if len(evidence_bytes) > MAX_EVIDENCE_BYTES:
            raise DetectorRuntimeError("evidence exceeds the runtime input bound")
    except (OSError, PackageIntegrityError, DetectorRuntimeError, TypeError, ValueError) as exc:
        package_digest = loaded.manifest.manifest_digest if loaded else None
        detector_sha256 = (
            _sha256(loaded.members["detector.py"])
            if loaded and "detector.py" in loaded.members
            else None
        )
        package_digest_after = None
        detector_sha256_after = None
        if package_path is not None:
            try:
                after = load_package(package_path)
                package_digest_after = after.manifest.manifest_digest
                detector_sha256_after = (
                    _sha256(after.members["detector.py"])
                    if "detector.py" in after.members
                    else None
                )
            except (OSError, PackageIntegrityError, KeyError):
                pass
        return _failed(
            str(exc),
            package_digest=package_digest,
            package_digest_after=package_digest_after,
            detector_sha256=detector_sha256,
            detector_sha256_after=detector_sha256_after,
        )

    package_digest = loaded.manifest.manifest_digest
    package_digest_after: str | None = None
    detector_sha_after: str | None = None
    container_name = f"asago-detector-{uuid.uuid4().hex[:16]}"
    try:
        with tempfile.TemporaryDirectory(prefix="asago-detector-") as temporary:
            root = Path(temporary)
            evidence_path = root / "evidence.json"
            detector_path = root / "detector.py"
            runner_path = root / "runner.py"
            evidence_path.write_bytes(evidence_bytes)
            detector_path.write_bytes(detector)
            runner_path.write_text(_RUNNER_SOURCE, encoding="utf-8")
            argv = tuple(
                _docker_argv(
                    docker_path=docker_path,
                    image=image,
                    package_path=package_path,
                    evidence_path=evidence_path,
                    detector_path=detector_path,
                    runner_path=runner_path,
                    container_name=container_name,
                )
            )
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_docker_client_environment(),
            )
            stdout, stderr, timed_out, output_exceeded = _bounded_communicate(
                process, timeout_seconds
            )
            if timed_out:
                return _finish(
                    status="timeout",
                    failure=f"detector exceeded {timeout_seconds:g}s wall-clock timeout",
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            if output_exceeded:
                return _finish(
                    status="failed",
                    failure="detector output exceeded the runtime bound",
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            if process.returncode != 0:
                failure = _runner_failure(stdout, stderr, process.returncode)
                return _finish(
                    status="failed",
                    failure=failure,
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            try:
                message = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return _finish(
                    status="failed",
                    failure=f"detector runtime protocol error: {exc}",
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            if not isinstance(message, dict) or message.get("status") != "ok":
                return _finish(
                    status="failed",
                    failure=_runner_failure(stdout, stderr, process.returncode),
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            raw_result = message.get("result")
            try:
                result = validate_detector_result(raw_result, evidence)
            except DetectorRuntimeError as exc:
                return _finish(
                    status="failed",
                    failure=str(exc),
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            try:
                after = load_package(package_path)
                package_digest_after = after.manifest.manifest_digest
                detector_sha_after = _sha256(after.members["detector.py"])
            except (PackageIntegrityError, KeyError) as exc:
                return _finish(
                    status="failed",
                    failure=f"package changed during detector execution: {exc}",
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            if package_digest_after != package_digest or detector_sha_after != detector_sha:
                return _finish(
                    status="failed",
                    failure="package or detector digest changed during execution",
                    package_path=package_path,
                    package_digest=package_digest,
                    detector_sha=detector_sha,
                    package_digest_after=package_digest_after,
                    detector_sha_after=detector_sha_after,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            return DetectorExecution(
                status="completed",
                result=result,
                failure=None,
                package_digest=package_digest,
                package_digest_after=package_digest_after,
                detector_sha256=detector_sha,
                detector_sha256_after=detector_sha_after,
                docker_argv=argv,
                stdout=stdout,
                stderr=stderr,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return _finish(
            status="failed",
            failure=f"detector runtime unavailable: {exc}",
            package_path=package_path,
            package_digest=package_digest,
            detector_sha=detector_sha,
            package_digest_after=package_digest_after,
            detector_sha_after=detector_sha_after,
            argv=argv,
            stdout=b"",
            stderr=b"",
        )
    finally:
        _remove_container(docker_path, container_name)


def validate_detector_result(
    value: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Validate the closed rich result and resolve every cited evidence path."""

    if not isinstance(value, dict) or not isinstance(evidence, dict):
        raise DetectorRuntimeError("detector result must be an object")
    missing = RESULT_FIELDS - set(value)
    unknown = set(value) - RESULT_FIELDS
    if missing:
        raise DetectorRuntimeError(f"detector result is missing fields: {sorted(missing)}")
    if unknown:
        raise DetectorRuntimeError(f"detector result has unknown fields: {sorted(unknown)}")
    if not isinstance(value["outcome"], str) or value["outcome"] not in OUTCOMES:
        raise DetectorRuntimeError(f"detector result has invalid outcome: {value['outcome']!r}")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise DetectorRuntimeError("detector result reason must be nonblank")
    refs = value["evidence_refs"]
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
        raise DetectorRuntimeError("detector result evidence_refs must be nonblank strings")
    if value["outcome"] != "inconclusive" and not refs:
        raise DetectorRuntimeError("detected and not_detected results require evidence_refs")
    for ref in refs:
        try:
            _resolve_evidence_ref(evidence, ref)
        except DetectorRuntimeError as exc:
            raise DetectorRuntimeError(
                f"evidence reference {ref!r} does not resolve: {exc}"
            ) from exc
    if not isinstance(value["claim_level"], str) or value["claim_level"] not in CLAIM_LEVELS:
        raise DetectorRuntimeError(
            f"detector result has invalid claim level: {value['claim_level']!r}"
        )
    return value


def _load_package_for_execution(
    package: str | Path | ArtifactPackage,
) -> tuple[Path, ArtifactPackage]:
    if isinstance(package, ArtifactPackage):
        raise DetectorRuntimeError("execution requires a persisted package directory")
    path = Path(package).expanduser().resolve()
    return path, load_package(path)


def _validate_source_interface(source: bytes) -> None:
    try:
        tree = ast.parse(source.decode("utf-8"), filename="detector.py")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise DetectorRuntimeError(f"invalid detector.py: {exc}") from exc
    evaluate = next(
        (
            node
            for node in tree.body
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "evaluate"
            )
        ),
        None,
    )
    if evaluate is None:
        raise DetectorRuntimeError("detector.py must define evaluate")
    if isinstance(evaluate, ast.AsyncFunctionDef):
        raise DetectorRuntimeError("detector.py evaluate must be synchronous")
    positional = [*evaluate.args.posonlyargs, *evaluate.args.args]
    if len(positional) != 1 or positional[0].arg != "evidence" or evaluate.args.vararg:
        raise DetectorRuntimeError("detector.py evaluate must accept exactly evidence")


def _docker_argv(
    *,
    docker_path: str,
    image: str,
    package_path: Path,
    evidence_path: Path,
    detector_path: Path,
    runner_path: Path,
    container_name: str,
) -> list[str]:
    return [
        docker_path,
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "asago-detector-runtime=1",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        MAX_MEMORY,
        "--pids-limit",
        MAX_PIDS,
        "--cpus",
        "1",
        "--ulimit",
        "nofile=64:64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--env",
        "HOME=/tmp",
        "--env",
        "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--env",
        "PYTHONNOUSERSITE=1",
        "--mount",
        f"type=bind,src={package_path},dst=/package,readonly",
        "--mount",
        f"type=bind,src={evidence_path},dst=/input/evidence.json,readonly",
        "--mount",
        f"type=bind,src={detector_path},dst=/input/detector.py,readonly",
        "--mount",
        f"type=bind,src={runner_path},dst=/runner.py,readonly",
        "--workdir",
        "/tmp",
        image,
        "python",
        "/runner.py",
    ]


def _docker_client_environment() -> dict[str, str]:
    """Keep Docker CLI configuration usable without passing it into the container."""

    result = {"PATH": os.environ.get("PATH", "")}
    if os.environ.get("DOCKER_CONFIG"):
        result["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]
    if os.environ.get("DOCKER_HOST"):
        result["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return result


def _bounded_communicate(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> tuple[bytes, bytes, bool, bool]:
    """Read both pipes while enforcing a wall-clock and output deadline."""

    if process.stdout is None or process.stderr is None:
        raise OSError("detector process pipes are unavailable")
    selector = selectors.DefaultSelector()
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    output_exceeded = False
    killed = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                if process.poll() is None:
                    process.kill()
                killed = True
            for key, _ in selector.select(max(0.0, min(remaining, 0.1))):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                buffer = streams[stream]
                if len(buffer) < MAX_OUTPUT_BYTES:
                    buffer.extend(chunk[: MAX_OUTPUT_BYTES - len(buffer)])
                if len(buffer) >= MAX_OUTPUT_BYTES or len(chunk) > MAX_OUTPUT_BYTES:
                    output_exceeded = True
                    if process.poll() is None:
                        process.kill()
                    killed = True
            if killed and process.poll() is not None:
                # Keep draining until both Docker pipes close, so no child
                # output remains attached to a future test.
                continue
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    finally:
        selector.close()
    return (
        bytes(streams[process.stdout]),
        bytes(streams[process.stderr]),
        timed_out,
        output_exceeded,
    )


def _remove_container(docker_path: str, container_name: str) -> None:
    try:
        subprocess.run(
            [docker_path, "rm", "--force", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_docker_client_environment(),
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return


def _finish(
    *,
    status: str,
    failure: str,
    package_path: Path,
    package_digest: str,
    detector_sha: str,
    package_digest_after: str | None,
    detector_sha_after: str | None,
    argv: tuple[str, ...],
    stdout: bytes,
    stderr: bytes,
) -> DetectorExecution:
    if package_digest_after is None or detector_sha_after is None:
        try:
            after = load_package(package_path)
            package_digest_after = after.manifest.manifest_digest
            detector_sha_after = _sha256(after.members["detector.py"])
        except (PackageIntegrityError, KeyError, OSError):
            pass
    return DetectorExecution(
        status=status,
        result=None,
        failure=failure,
        package_digest=package_digest,
        package_digest_after=package_digest_after,
        detector_sha256=detector_sha,
        detector_sha256_after=detector_sha_after,
        docker_argv=argv,
        stdout=stdout,
        stderr=stderr,
    )


def _failed(
    failure: str,
    *,
    package_digest: str | None = None,
    package_digest_after: str | None = None,
    detector_sha256: str | None = None,
    detector_sha256_after: str | None = None,
) -> DetectorExecution:
    return DetectorExecution(
        status="failed",
        result=None,
        failure=failure,
        package_digest=package_digest,
        package_digest_after=package_digest_after,
        detector_sha256=detector_sha256,
        detector_sha256_after=detector_sha256_after,
        docker_argv=(),
    )


def evidence_packet_roots(
    observations: Mapping[str, Any] | None = None,
    *,
    judge_enabled: bool,
) -> tuple[str, ...]:
    """Return detector-facing roots from the adapter packet and declarations.

    The adapter always constructs the standard roots, even when a collection is
    empty or unavailable.  ``assistant_messages`` is an observations spelling
    for the packet's ``messages`` root, not an alternate detector key.
    """

    del observations
    roots = set(EVIDENCE_PACKET_ROOTS)
    if judge_enabled:
        roots.add("judge")
    return tuple(sorted(roots))


def validate_detector_evidence_access(
    source: bytes | str,
    *,
    observations: Mapping[str, Any] | None = None,
    bindings: Any = None,
    judge_enabled: bool,
) -> tuple[dict[str, Any], ...]:
    """Find common static reads and citations outside the supplied packet.

    This deliberately checks only literal ``evidence["root"]``,
    ``evidence.get("root")`` chains, literal binding names below
    ``evidence.bindings``, and literal ``evidence_refs`` entries.  It does not
    attempt to prove arbitrary Python data flow, aliases, computed keys, or
    dynamically built reference strings.
    """

    try:
        tree = ast.parse(
            source.decode("utf-8") if isinstance(source, bytes) else source,
            filename="detector.py",
        )
    except (UnicodeDecodeError, SyntaxError, TypeError):
        return ()

    allowed_roots = set(evidence_packet_roots(observations, judge_enabled=judge_enabled))
    declared_bindings = _declared_binding_names(bindings)
    visitor = _EvidenceAccessVisitor()
    visitor.visit(tree)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for path, node in visitor.accesses:
        if not path:
            continue
        root = path[0]
        location = f"detector.py:{getattr(node, 'lineno', 0)}"
        if root not in allowed_roots:
            key = ("read", root, getattr(node, "lineno", 0))
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                _evidence_access_finding(
                    kind="read",
                    root=root,
                    location=location,
                    allowed_roots=allowed_roots,
                    declared_bindings=declared_bindings,
                    path=path,
                )
            )
            continue
        if root == "bindings" and len(path) >= 2:
            binding_name = path[1]
            if binding_name not in declared_bindings:
                key = ("binding", binding_name, getattr(node, "lineno", 0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    _evidence_access_finding(
                        kind="binding",
                        root=binding_name,
                        location=location,
                        allowed_roots=allowed_roots,
                        declared_bindings=declared_bindings,
                        path=path,
                    )
                )

    for reference, node in visitor.references:
        root = _evidence_reference_root(reference)
        if root is None:
            continue
        location = f"detector.py:{getattr(node, 'lineno', 0)}"
        if root not in allowed_roots:
            key = ("reference", root, getattr(node, "lineno", 0))
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                _evidence_access_finding(
                    kind="reference",
                    root=root,
                    location=location,
                    allowed_roots=allowed_roots,
                    declared_bindings=declared_bindings,
                    path=(root,),
                    reference=reference,
                )
            )
        elif root == "bindings":
            binding_name = _evidence_reference_binding_name(reference)
            if binding_name is not None and binding_name not in declared_bindings:
                key = ("reference-binding", binding_name, getattr(node, "lineno", 0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    _evidence_access_finding(
                        kind="reference-binding",
                        root=binding_name,
                        location=location,
                        allowed_roots=allowed_roots,
                        declared_bindings=declared_bindings,
                        path=("bindings", binding_name),
                        reference=reference,
                    )
                )
    return tuple(findings)


def _package_json_member(
    package: ArtifactPackage,
    name: str,
    *,
    default: Any,
) -> Any:
    value = package.members.get(name)
    if value is None:
        return default
    try:
        return json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return default


def _declared_binding_names(bindings: Any) -> frozenset[str]:
    if not isinstance(bindings, list):
        return frozenset()
    names = {
        item["name"]
        for item in bindings
        if (isinstance(item, dict) and isinstance(item.get("name"), str))
    }
    return frozenset(names)


def _evidence_access_finding(
    *,
    kind: str,
    root: str,
    location: str,
    allowed_roots: set[str],
    declared_bindings: frozenset[str],
    path: tuple[str, ...],
    reference: str | None = None,
) -> dict[str, Any]:
    allowed = ", ".join(sorted(allowed_roots))
    declared = ", ".join(sorted(declared_bindings)) or "none"
    if kind in {"binding", "reference-binding"}:
        detail = (
            f"detector accesses undeclared evidence binding {root!r} at {location}. "
            "Runtime bindings come from the accepted plan's bindings.json, and "
            "artifact authoring cannot add one; the declared bindings are: "
            f"{declared}. Read a declared binding at evidence.bindings.<name> or a "
            "standard packet root. Do not read a runtime state key or hardcode the "
            "supplied fact. If the detector needs a value that no declared binding "
            "supplies, the accepted plan needs a new runtime binding."
        )
    elif kind == "reference":
        detail = (
            f"detector returns evidence_refs root {root!r} at {location}, but the "
            f"package does not declare or supply that evidence root, so the reference "
            f"does not resolve. Allowed detector "
            f"packet roots are: {allowed}. Return a reference under a supplied root "
            "and cite a declared binding for supplied record facts."
        )
    else:
        detail = (
            f"detector reads undeclared evidence root {root!r} at {location}; the "
            f"package supplies only these detector packet roots: {allowed}. "
            "Read a supplied record fact through a declared binding at "
            f"evidence.bindings.<name>; the declared bindings are: {declared}. "
            "Runtime bindings come from the accepted plan's bindings.json, and "
            "artifact authoring cannot add one. Do not read a runtime state key or "
            "hardcode the literal."
        )
    return {
        "code": "undeclared_evidence_access",
        "detail": detail,
        "path": location,
        "details": {
            "kind": kind,
            "root": root,
            "path": ".".join(path),
            "reference": reference,
            "allowed_roots": sorted(allowed_roots),
            "declared_bindings": sorted(declared_bindings),
            "static_analysis_limits": (
                "literal evidence subscripts/get calls and literal evidence_refs "
                "only; aliases, computed keys, and dynamically built references "
                "are not analyzed"
            ),
        },
    }


def _evidence_reference_root(reference: str) -> str | None:
    normalized = reference
    if normalized.startswith("/"):
        parts = normalized.split("/")
        return parts[1] if len(parts) > 1 and parts[1] else None
    if normalized.startswith("$."):
        normalized = normalized[2:]
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", normalized)
    return match.group(1) if match else None


def _evidence_reference_binding_name(reference: str) -> str | None:
    normalized = reference[2:] if reference.startswith("$.") else reference
    if normalized.startswith("/"):
        parts = normalized.split("/")
        return parts[2] if len(parts) > 2 and parts[1] == "bindings" else None
    if normalized.startswith("bindings."):
        match = re.match(r"bindings\.([A-Za-z_][A-Za-z0-9_]*)", normalized)
        return match.group(1) if match else None
    if normalized.startswith("bindings["):
        match = re.match(r"bindings\[([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1\]", normalized)
        return match.group(2) if match else None
    return None


class _EvidenceAccessVisitor(ast.NodeVisitor):
    """Collect literal detector evidence accesses and result references."""

    def __init__(self) -> None:
        self.accesses: list[tuple[tuple[str, ...], ast.AST]] = []
        self.references: list[tuple[str, ast.AST]] = []

    def visit_Subscript(self, node: ast.Subscript) -> None:
        path = _literal_evidence_path(node)
        if path is not None:
            self.accesses.append((path, node))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        path = _literal_evidence_path(node)
        if path is not None:
            self.accesses.append((path, node))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "evidence_refs":
                for reference in _literal_strings(value):
                    self.references.append((reference, value))
        self.generic_visit(node)


def _literal_evidence_path(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Subscript):
        base = _literal_evidence_path(node.value)
        key = _literal_string_or_integer(node.slice)
        return (*base, key) if base is not None and key is not None else None
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            base = _literal_evidence_path(node.func.value)
            key = _literal_string_or_integer(node.args[0])
            return (*base, key) if base is not None and key is not None else None
        return None
    if isinstance(node, ast.Name) and node.id == "evidence":
        return ()
    return None


def _literal_string_or_integer(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return str(node.value)
    return None


def _literal_strings(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return tuple(reference for item in node.elts for reference in _literal_strings(item))
    return ()


def normalize_evidence_packet(
    evidence: dict[str, Any],
    *,
    judge_enabled: bool,
) -> dict[str, Any]:
    """Return the detector-facing packet with a closed semantic-judge projection.

    A package that contains ``judge.json`` always exposes a judge object. The
    projection retains only the detector-facing verdict, references, and
    reason. Invalid, missing, or unsupported judge support becomes an
    unresolved verdict before generated detector code runs.
    """

    if not isinstance(evidence, dict):
        raise DetectorRuntimeError("evidence packet must be an object")
    packet = dict(evidence)
    if not judge_enabled:
        packet.pop("judge", None)
        return packet
    packet["judge"] = _normalize_judge(
        evidence["judge"] if "judge" in evidence else _MISSING,
        evidence,
    )
    return packet


def _normalize_judge(value: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    if value is _MISSING:
        return _unresolved_judge("judge_missing")
    if not isinstance(value, dict):
        return _unresolved_judge("judge_invalid")
    verdict = value.get("verdict")
    refs = value.get("evidence_refs")
    if verdict not in JUDGE_VERDICTS:
        return _unresolved_judge("judge_response_invalid")
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
        return _unresolved_judge("judge_evidence_invalid")
    references = list(refs)
    if verdict in {"supported", "contradicted"}:
        references = [_map_message_id_reference(evidence, reference) for reference in references]
        if not references:
            return _unresolved_judge("judge_support_missing", references)
        for reference in references:
            try:
                support = _resolve_evidence_ref(evidence, reference)
            except DetectorRuntimeError:
                return _unresolved_judge("judge_support_unresolved", references)
            if not _usable_judge_support(evidence, reference, support):
                return _unresolved_judge("judge_support_unresolved", references)
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "judge_completed"
    if verdict == "unresolved":
        return _unresolved_judge(reason)
    return {
        "verdict": verdict,
        "evidence_refs": references,
        "reason": reason,
    }


def _unresolved_judge(reason: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    del evidence_refs
    return {
        "verdict": "unresolved",
        "evidence_refs": [],
        "reason": reason,
    }


def _map_message_id_reference(evidence: dict[str, Any], reference: str) -> str:
    """Map one unique captured message ID to its content path."""

    try:
        _resolve_evidence_ref(evidence, reference)
    except DetectorRuntimeError:
        messages = evidence.get("messages")
        if isinstance(messages, list):
            matches = [
                index
                for index, message in enumerate(messages)
                if isinstance(message, dict) and message.get("id") == reference
            ]
            if len(matches) == 1:
                return f"messages[{matches[0]}].content"
    return reference


def _usable_judge_support(
    evidence: dict[str, Any],
    reference: str,
    value: Any,
) -> bool:
    """Require references to point at captured, usable judge support.

    Judge support is limited to message content or a non-null tool-call result
    value. A call record, call metadata, argument, or other evidence field does
    not by itself support a decisive judge verdict.
    """

    if value is None:
        return False
    normalized = reference[2:] if reference.startswith("$.") else reference
    root = normalized.lstrip("/").split("/", 1)[0].split("[", 1)[0].split(".", 1)[0]
    availability = evidence.get("availability")
    completeness = evidence.get("completeness")
    if isinstance(availability, dict) and availability.get(root) != "captured":
        return False
    specific_item = "[" in normalized or (
        normalized.startswith("/") and any(part.isdigit() for part in normalized.split("/")[2:])
    )
    if (
        not specific_item
        and isinstance(completeness, dict)
        and completeness.get(root) in {"unknown", "partial"}
    ):
        return False
    if _is_tool_result_reference(reference):
        return True
    if root == "messages":
        message_index = _message_content_index(normalized)
        if message_index is not None:
            messages = evidence.get("messages")
            if not isinstance(messages, list) or message_index >= len(messages):
                return False
            message = messages[message_index]
            return (
                isinstance(message, dict)
                and isinstance(message.get("content"), str)
                and message["content"] == value
            )
        if isinstance(value, list):
            return bool(value) and all(
                isinstance(item, dict) and isinstance(item.get("content"), str) for item in value
            )
        return isinstance(value, dict) and isinstance(value.get("content"), str)
    if root == "tool_calls":
        return False
    return False


_TOOL_RESULT_FIELDS = frozenset({"decoded_result", "raw_result", "result", "output"})


def _is_tool_result_reference(reference: str) -> bool:
    """Identify packet paths that name a captured tool-call result value."""

    parts = _reference_path_parts(reference)
    if not parts:
        return False
    if (
        len(parts) == 3
        and parts[0] == "tool_calls"
        and parts[1].isdigit()
        and parts[2] in _TOOL_RESULT_FIELDS
    ):
        return True
    if (
        len(parts) == 4
        and parts[0] == "tool_calls"
        and parts[1].isdigit()
        and parts[2] in {"raw", "source_item"}
        and parts[3] in _TOOL_RESULT_FIELDS
    ):
        return True
    if (
        len(parts) in {6, 7}
        and parts[0] == "messages"
        and parts[1].isdigit()
        and parts[2] in {"raw", "source_item"}
    ):
        if (
            len(parts) == 6
            and parts[3] == "tool_calls"
            and parts[4].isdigit()
            and parts[5] in _TOOL_RESULT_FIELDS
        ):
            return True
        if (
            len(parts) == 7
            and parts[3] == "notes"
            and parts[4] == "tool_calls"
            and parts[5].isdigit()
            and parts[6] in _TOOL_RESULT_FIELDS
        ):
            return True
        if (
            len(parts) == 7
            and parts[3] == "raw_response"
            and parts[4] == "output"
            and parts[5].isdigit()
            and parts[6] in _TOOL_RESULT_FIELDS
        ):
            return True
    return False


def _reference_path_parts(reference: str) -> list[str]:
    """Parse the supported dotted/bracket or JSON Pointer path forms."""

    if reference.startswith("/"):
        return [part.replace("~1", "/").replace("~0", "~") for part in reference.split("/")[1:]]
    if reference.startswith("$."):
        reference = reference[2:]
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*",
        reference,
    ):
        return []
    tokens = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]", reference)
    return [name if name else index for name, index in tokens]


def _message_content_index(reference: str) -> int | None:
    if reference.startswith("/"):
        parts = reference.split("/")
        if (
            len(parts) == 4
            and parts[1] == "messages"
            and parts[2].isdigit()
            and parts[3] == "content"
        ):
            return int(parts[2])
        return None
    if reference.startswith("messages[") and reference.endswith("].content"):
        index = reference[len("messages[") : -len("].content")]
        if index.isdigit():
            return int(index)
    return None


def _runner_failure(stdout: bytes, stderr: bytes, returncode: int | None) -> str:
    for payload in (stdout, stderr):
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            return f"detector runtime error: {value['error']}"
    return f"detector container failed with exit code {returncode}"


def _resolve_evidence_ref(evidence: dict[str, Any], reference: str) -> Any:
    if reference in evidence:
        return evidence[reference]
    if reference.startswith("/"):
        current: Any = evidence
        parts = reference.split("/")[1:]
        for encoded in parts:
            part = encoded.replace("~1", "/").replace("~0", "~")
            current = _step(current, part)
        return current
    if reference.startswith("$."):
        reference = reference[2:]
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*",
        reference,
    ):
        raise DetectorRuntimeError("invalid path syntax")
    tokens = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]", reference)
    if not tokens:
        raise DetectorRuntimeError("invalid path syntax")
    current = evidence
    for name, index in tokens:
        current = _step(current, name if name else index)
    return current


def _step(current: Any, part: str) -> Any:
    if isinstance(current, dict) and part in current:
        return current[part]
    if isinstance(current, list) and part.isdigit():
        index = int(part)
        if index < len(current):
            return current[index]
    raise DetectorRuntimeError(f"missing path segment {part!r}")


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise DetectorRuntimeError(f"evidence is not JSON-serializable: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_RUNNER_SOURCE = textwrap.dedent(
    r"""
    import importlib.util
    import json
    import sys

    LIMIT = 65536

    class BoundedWriter:
        def __init__(self, stream):
            self.stream = stream
            self.count = 0

        def write(self, value):
            encoded = value.encode("utf-8") if isinstance(value, str) else value
            self.count += len(encoded)
            if self.count > LIMIT:
                raise RuntimeError("detector output exceeded 65536 bytes")
            return self.stream.write(value)

        def flush(self):
            return self.stream.flush()

    def emit(value, code=0):
        sys.__stdout__.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        sys.__stdout__.flush()
        raise SystemExit(code)

    sys.stdout = BoundedWriter(sys.__stdout__)
    sys.stderr = BoundedWriter(sys.__stderr__)
    try:
        with open("/input/evidence.json", encoding="utf-8") as source:
            evidence = json.load(source)
        spec = importlib.util.spec_from_file_location("generated_detector", "/input/detector.py")
        if spec is None or spec.loader is None:
            emit({"status": "error", "error": "cannot load detector.py"}, 2)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evaluate = getattr(module, "evaluate", None)
        if not callable(evaluate):
            emit({"status": "error", "error": "detector.py evaluate is not callable"}, 2)
        result = evaluate(evidence)
        json.dumps(result, ensure_ascii=False)
        emit({"status": "ok", "result": result})
    except SystemExit:
        raise
    except BaseException as exc:
        emit({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, 3)
    """
)


__all__ = [
    "CLAIM_LEVELS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DetectorExecution",
    "DetectorRuntimeError",
    "DOCKER",
    "EVIDENCE_PACKET_ROOTS",
    "JUDGE_VERDICTS",
    "OUTCOMES",
    "PYTHON_IMAGE",
    "evidence_packet_roots",
    "execute_detector",
    "normalize_evidence_packet",
    "resolve_docker_path",
    "validate_detector_evidence_access",
    "validate_detector_result",
]
