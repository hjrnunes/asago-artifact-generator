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
import subprocess
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .package_io import ArtifactPackage, PackageIntegrityError, load_package

DOCKER = "/usr/local/bin/docker"
PYTHON_IMAGE = "python:3.12-slim"
CLAIM_LEVELS = frozenset({"command_attempt", "reply", "returned_result", "state_effect"})
OUTCOMES = frozenset({"detected", "not_detected", "inconclusive"})
RESULT_FIELDS = frozenset({"outcome", "reason", "evidence_refs", "claim_level"})
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


def execute_detector(
    package: str | Path | ArtifactPackage,
    evidence: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    docker_path: str = DOCKER,
    image: str = PYTHON_IMAGE,
) -> DetectorExecution:
    """Execute the package's exact ``detector.py`` bytes in constrained Docker.

    ``status`` is ``completed``, ``failed`` or ``timeout``.  A non-completed
    execution never carries a detector result, even if generated code printed
    a plausible verdict before failing.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
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


run_detector = execute_detector
execute_generated_detector = execute_detector
validate_result = validate_detector_result


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
    "OUTCOMES",
    "PYTHON_IMAGE",
    "execute_detector",
    "execute_generated_detector",
    "run_detector",
    "validate_result",
    "validate_detector_result",
]
