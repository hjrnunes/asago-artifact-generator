"""CLI for strict STPA bundles and isolated historical generation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from .authoring import PresentationRequest, PresentationResult
from .garak.plan import GarakPlan
from .models.execution_case import BoundExecutionCase, ExecutionCaseExclusion
from .models.execution_classification import ExecutionTargetProfile
from .models.readiness import ExecutionPlanResult
from .models.runtime_binding import RuntimeBindingSet
from .output import write_case_exclusion, write_entry_outputs, write_manifest
from .platforms.base import CompiledArtifact

app = typer.Typer(
    help="Policy-driven agentic red-teaming artifact generator.",
    no_args_is_help=True,
)

_READINESS_STATES = (
    "invalid",
    "needs_semantic_binding",
    "needs_runtime_binding",
    "unsupported",
    "ready",
)
_EXECUTION_CASE_CODES = (
    "analytical_only",
    "needs_target_binding",
    "ambiguous",
    "unsupported",
    "invalid_profile",
    "invalid_source_binding",
)
_SOURCE_CLASSIFICATION_STATES = ("concrete", "parameterized", "analytical_only")
_ENVIRONMENT_STATES = ("target_agnostic", "target_profile", "simulation_profile")


@app.callback()
def _main() -> None:
    """Policy-driven agentic red-teaming artifact generator."""


@app.command("generate-legacy")
def generate_legacy(
    scenarios: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Scenario YAML file(s). If omitted, processes all in examples/scenarios/",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Override runs/ output directory (default: runs/)"),
    ] = None,
    prompt: Annotated[
        Path | None,
        typer.Option(
            "--prompt",
            help="Override generation prompt (default: prompts/generate_artifact.md)",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Classify + LLM + validate only — do not write garak JSON"),
    ] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Refuse LLM (only useful for skip check)"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Write garak JSON even if artifact validation fails (result is still not ok)",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose", help="Verbose logging"),
    ] = False,
) -> None:
    """Generate historical Garak artifacts from taxonomy-era scenario YAMLs."""

    from .extract import load_scenario
    from .garak.gen import generate_artifact, list_scenario_files
    from .garak.spec_io import MANIFEST_FILE, runs_dir

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-5s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    prompt_path = prompt
    paths = list(scenarios) if scenarios else list_scenario_files()
    if not paths:
        typer.echo("No scenario files found.", err=True)
        raise typer.Exit(1)

    manifest: list[dict] = []
    coverage_counts = {"full": 0, "partial": 0, "skip": 0}
    validation_failed = 0
    process_errors = 0
    failed = False

    for path in paths:
        try:
            ctx = load_scenario(path)
            result = generate_artifact(
                ctx,
                prompt_path=prompt_path,
                output_dir=output_dir,
                use_llm=not no_llm,
                force=force,
                dry_run=dry_run,
            )
            entry = {
                "scenario_id": result.scenario_id,
                "ok": result.ok,
                "gate_result": result.gate,
                "gate_reason": result.gate_reason,
                "artifact_path": result.artifact_path,
            }
            if result.errors:
                entry["errors"] = result.errors
            manifest.append(entry)
            if result.gate in coverage_counts:
                coverage_counts[result.gate] += 1
            if not result.ok:
                validation_failed += 1
                failed = True
        except Exception as e:
            log.error("ERROR processing %s: %s", path.name, e)
            manifest.append(
                {
                    "scenario_id": path.stem,
                    "ok": False,
                    "error": str(e),
                }
            )
            process_errors += 1
            failed = True

    if not dry_run:
        out = runs_dir(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        manifest_path = out / MANIFEST_FILE
        manifest_path.write_text(json.dumps(manifest, indent=2))
        log.info("Manifest written to %s", manifest_path)

    print(f"\n{'=' * 50}")
    print("Garak artifact generation summary")
    print(f"{'=' * 50}")
    print(f"Total scenarios: {len(manifest)}")
    print("Coverage:")
    print(f"  Full:    {coverage_counts['full']}")
    print(f"  Partial: {coverage_counts['partial']}")
    print(f"  Skip:    {coverage_counts['skip']}")
    print(f"Validation failed: {validation_failed}")
    if process_errors:
        print(f"Process errors:    {process_errors}")

    print(f"\n{'Scenario':<25} {'Gate':<8} {'Ok':<6} {'Reason'}")
    print("-" * 80)
    for e in manifest:
        sid = e.get("scenario_id", "?")
        gr = e.get("gate_result", "-")
        ok = "yes" if e.get("ok") else "no"
        reason = e.get("gate_reason", e.get("error", ""))
        print(f"{sid:<25} {gr:<8} {ok:<6} {reason}")

    if failed:
        raise typer.Exit(1)


def _load_runtime_bindings(path: Path | None) -> RuntimeBindingSet | None:
    """Parse a closed, reviewed runtime-binding set after bundle loading."""

    if path is None:
        return None
    if not path.is_file():
        raise ValueError(f"bindings file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("bindings root must be an object")
    bindings = RuntimeBindingSet.model_validate(raw)
    bindings.verify_digest()
    return bindings


def _load_target_profile(path: Path | None) -> ExecutionTargetProfile | None:
    """Load one explicit, digest-attested target or simulation profile."""

    if path is None:
        return None
    if not path.is_file():
        raise ValueError(f"target profile file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("target profile root must be an object")
    if "semantic_digest" not in raw:
        raise ValueError("target profile must include semantic_digest")
    profile = ExecutionTargetProfile.model_validate(raw)
    profile.assert_integrity()
    return profile


class _LLMPresentationAuthor:
    """Late-bound provider adapter restricted to compiler-owned text slots."""

    def author(self, request: PresentationRequest) -> PresentationResult:
        from .llm import llm_json

        slot_spec = [
            {
                "slot_id": slot.slot_id,
                "purpose": slot.purpose,
                "allowed_role": slot.allowed_role,
                "max_chars": slot.max_chars,
            }
            for slot in request.slots
        ]
        prompt = json.dumps(
            {
                "slots": slot_spec,
                "scenario_narrative": request.scenario_narrative,
                "loss_context": request.loss_context,
                "allowed_tools": request.allowed_tools,
                "allowed_values": request.allowed_values,
                "constraints": request.constraints,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        data = llm_json(
            "Fill exactly the requested presentation slots with text only. "
            "Return a JSON object whose keys are exactly the slot IDs. "
            "Never add execution instructions, tools, values, surfaces, observers, "
            "or success criteria.\n\n" + prompt,
            "You author constrained presentation text for an already fixed execution plan. "
            "Respond with JSON only.",
        )
        if not isinstance(data, dict):
            raise ValueError("presentation author must return a JSON object")
        return PresentationResult.from_mapping(data, request)


def _diagnostic_documents(result: ExecutionPlanResult) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in result.diagnostics]


def _manifest_entry(
    scenario_id: str,
    result: ExecutionPlanResult,
    paths: dict[str, str],
    errors: list[str],
    execution_case: BoundExecutionCase,
) -> dict[str, Any]:
    classification = execution_case.execution_classification
    entry: dict[str, Any] = {
        "scenario_id": scenario_id,
        "overall": result.overall,
        "source_status": result.source_status,
        "semantic_binding_status": result.semantic_binding_status,
        "runtime_binding_status": result.runtime_binding_status,
        "platform_support_status": result.platform_support_status,
        "artifact_status": (
            "generated"
            if "artifact" in paths
            else "failed"
            if errors and result.ready
            else "not_attempted"
        ),
        "diagnostics": _diagnostic_documents(result),
        "paths": paths,
        "binding_completeness": execution_case.binding_completeness,
        "environment_basis": execution_case.environment_basis,
        "profile_fit": execution_case.profile_fit,
        "claim_scope": execution_case.claim_scope,
        "source_binding_completeness": classification.binding_completeness,
        "source_environment_basis": classification.environment_basis,
        "source_profile_fit": classification.profile_fit,
        "source_claim_scope": classification.claim_scope,
    }
    if errors:
        entry["errors"] = errors
    return entry


def _validate_stpa_options(bundle: Path | None, platform: str, force: bool) -> None:
    if bundle is None:
        raise typer.BadParameter("--bundle is required; use generate-legacy for historical YAML")
    if force:
        raise typer.BadParameter("--force cannot bypass STPA source, binding, or compiler errors")
    if platform != "garak":
        raise typer.BadParameter(f"unsupported platform: {platform}")


def _load_stpa_dependencies() -> tuple[Any, Any, Any, Any, Any, Any]:
    from .bundle.loader import load_execution_bundle
    from .garak.capabilities import garak_capabilities
    from .garak.compile import compile_execution_artifact
    from .garak.plan import build_garak_plan
    from .planning.bind import bind_and_plan
    from .planning.resolve_case import resolve_execution_case

    return (
        load_execution_bundle,
        garak_capabilities,
        compile_execution_artifact,
        build_garak_plan,
        bind_and_plan,
        resolve_execution_case,
    )


def _select_stpa_entries(verified: Any, entry: str | None) -> tuple[Any, ...]:
    selected = verified.entries
    if entry is None:
        return selected
    selected = tuple(item for item in selected if item.scenario_id == entry)
    if not selected:
        raise typer.BadParameter(f"bundle has no entry with scenario ID {entry!r}")
    return selected


def _compile_ready_entry(
    readiness: ExecutionPlanResult,
    readiness_only: bool,
    no_llm: bool,
    compile_execution_artifact: Any,
) -> CompiledArtifact | None:
    if readiness_only:
        return None
    if no_llm:
        return compile_execution_artifact(readiness.plan)
    return compile_execution_artifact(readiness.plan, _LLMPresentationAuthor())


def _pre_readiness_entry(
    scenario_id: str, paths: dict[str, str], errors: list[str]
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "overall": "invalid",
        "source_status": "invalid",
        "semantic_binding_status": "incomplete",
        "runtime_binding_status": "incomplete",
        "platform_support_status": "indeterminate",
        "artifact_status": "failed",
        "diagnostics": [],
        "paths": paths,
        "errors": errors or ["entry processing failed before readiness"],
    }


def _process_stpa_entry(
    bundle_entry: Any,
    run_dir: Path,
    runtime_bindings: RuntimeBindingSet | None,
    capabilities: Any,
    readiness_only: bool,
    no_llm: bool,
    bind_and_plan: Any,
    build_garak_plan: Any,
    compile_execution_artifact: Any,
    target_profile: ExecutionTargetProfile | None,
    resolve_execution_case: Any,
) -> tuple[dict[str, Any], bool]:
    try:
        execution_case = resolve_execution_case(bundle_entry.intent, target_profile)
    except Exception as exc:
        return (
            _pre_readiness_entry(bundle_entry.scenario_id, {}, [str(exc)]),
            True,
        )
    if isinstance(execution_case, ExecutionCaseExclusion):
        paths = write_case_exclusion(run_dir, bundle_entry.scenario_id, execution_case)
        return _case_exclusion_entry(bundle_entry.scenario_id, execution_case, paths)
    readiness, garak_plan, compiled, paths, errors, failed = _run_stpa_entry(
        bundle_entry,
        execution_case,
        run_dir,
        runtime_bindings,
        capabilities,
        readiness_only,
        no_llm,
        bind_and_plan,
        build_garak_plan,
        compile_execution_artifact,
    )
    if readiness is None:
        return _pre_readiness_entry(bundle_entry.scenario_id, paths, errors), failed
    return (
        _manifest_entry(
            bundle_entry.scenario_id,
            readiness,
            paths,
            errors,
            execution_case,
        ),
        failed,
    )


def _case_exclusion_entry(
    scenario_id: str,
    exclusion: ExecutionCaseExclusion,
    paths: dict[str, str],
) -> tuple[dict[str, Any], bool]:
    """Represent a semantic exclusion without treating it as a compiler error."""

    entry = {
        "scenario_id": scenario_id,
        "overall": "execution_case_excluded",
        "execution_case_code": exclusion.code,
        "source_status": "valid",
        "semantic_binding_status": "not_required",
        "runtime_binding_status": "incomplete",
        "platform_support_status": "indeterminate",
        "artifact_status": "not_attempted",
        "diagnostics": [item.model_dump(mode="json") for item in exclusion.diagnostics],
        "paths": paths,
        "binding_completeness": exclusion.execution_classification.binding_completeness,
        "environment_basis": exclusion.execution_classification.environment_basis,
        "profile_fit": exclusion.execution_classification.profile_fit,
        "claim_scope": exclusion.execution_classification.claim_scope,
        "source_binding_completeness": exclusion.execution_classification.binding_completeness,
        "source_environment_basis": exclusion.execution_classification.environment_basis,
        "source_profile_fit": exclusion.execution_classification.profile_fit,
        "source_claim_scope": exclusion.execution_classification.claim_scope,
    }
    return entry, exclusion.code in {"invalid_profile", "invalid_source_binding"}


def _run_stpa_entry(
    bundle_entry: Any,
    execution_case: Any,
    run_dir: Path,
    runtime_bindings: RuntimeBindingSet | None,
    capabilities: Any,
    readiness_only: bool,
    no_llm: bool,
    bind_and_plan: Any,
    build_garak_plan: Any,
    compile_execution_artifact: Any,
) -> tuple[
    ExecutionPlanResult | None,
    GarakPlan | None,
    CompiledArtifact | None,
    dict[str, str],
    list[str],
    bool,
]:
    errors: list[str] = []
    paths: dict[str, str] = {}
    readiness: ExecutionPlanResult | None = None
    garak_plan: GarakPlan | None = None
    compiled: CompiledArtifact | None = None
    try:
        readiness = bind_and_plan(execution_case, runtime_bindings, capabilities)
        if readiness.ready:
            garak_plan = build_garak_plan(readiness.plan)
            compiled = _compile_ready_entry(
                readiness,
                readiness_only,
                no_llm,
                compile_execution_artifact,
            )
        paths = write_entry_outputs(
            run_dir,
            bundle_entry.scenario_id,
            readiness,
            execution_case=execution_case,
            plan=garak_plan if readiness.ready else None,
            compiled=compiled,
        )
        failed = readiness.overall == "invalid"
        return readiness, garak_plan, compiled, paths, errors, failed
    except Exception as exc:
        errors.append(str(exc))
        failed = True
        paths = _write_partial_readiness(
            run_dir,
            bundle_entry.scenario_id,
            readiness,
            garak_plan,
            paths,
            execution_case,
        )
        return readiness, garak_plan, compiled, paths, errors, failed


def _write_partial_readiness(
    run_dir: Path,
    scenario_id: str,
    readiness: ExecutionPlanResult | None,
    garak_plan: GarakPlan | None,
    paths: dict[str, str],
    execution_case: BoundExecutionCase,
) -> dict[str, str]:
    if readiness is None or "readiness" in paths:
        return paths
    return write_entry_outputs(
        run_dir,
        scenario_id,
        readiness,
        execution_case=execution_case,
        plan=garak_plan if readiness.ready else None,
    )


def _process_stpa_entries(
    selected: tuple[Any, ...],
    run_dir: Path,
    runtime_bindings: RuntimeBindingSet | None,
    capabilities: Any,
    readiness_only: bool,
    no_llm: bool,
    bind_and_plan: Any,
    build_garak_plan: Any,
    compile_execution_artifact: Any,
    target_profile: ExecutionTargetProfile | None,
    resolve_execution_case: Any,
) -> tuple[list[dict[str, Any]], bool]:
    entries: list[dict[str, Any]] = []
    failed = False
    for bundle_entry in selected:
        manifest_entry, entry_failed = _process_stpa_entry(
            bundle_entry,
            run_dir,
            runtime_bindings,
            capabilities,
            readiness_only,
            no_llm,
            bind_and_plan,
            build_garak_plan,
            compile_execution_artifact,
            target_profile,
            resolve_execution_case,
        )
        entries.append(manifest_entry)
        failed = failed or entry_failed
    return entries, failed


def _manifest_field_counts(
    entries: list[dict[str, Any]], field: str, values: tuple[str, ...]
) -> dict[str, int]:
    return {value: sum(item.get(field) == value for item in entries) for value in values}


def _manifest_summary_counts(
    entries: list[dict[str, Any]],
    readiness_counts: dict[str, int],
    case_counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "readiness": readiness_counts,
        "execution_case_excluded": sum(
            item["overall"] == "execution_case_excluded" for item in entries
        ),
        "analytical_only": case_counts["analytical_only"],
    }


def _stpa_manifest(verified: Any, platform: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    readiness_counts = _manifest_field_counts(entries, "overall", _READINESS_STATES)
    case_counts = _manifest_field_counts(entries, "execution_case_code", _EXECUTION_CASE_CODES)
    summary_counts = _manifest_summary_counts(entries, readiness_counts, case_counts)
    source_counts = _manifest_field_counts(
        entries, "source_binding_completeness", _SOURCE_CLASSIFICATION_STATES
    )
    environment_counts = _manifest_field_counts(entries, "environment_basis", _ENVIRONMENT_STATES)
    source_environment_counts = _manifest_field_counts(
        entries, "source_environment_basis", (*_ENVIRONMENT_STATES, "none")
    )
    return {
        "schema_version": "artifact-manifest-v1",
        "run_id": verified.run_id,
        "bundle_digest": verified.bundle_digest,
        "platform": platform,
        "entry_count": len(entries),
        "counts": summary_counts,
        "source_classification_counts": source_counts,
        "environment_basis_counts": environment_counts,
        "source_environment_basis_counts": source_environment_counts,
        "execution_case_counts": case_counts,
        "entries": entries,
    }


@app.command("generate")
def generate_stpa(
    bundle: Annotated[
        Path | None,
        typer.Option("--bundle", help="Canonical stpa-execution-bundle-v1 JSON index."),
    ] = None,
    bindings: Annotated[
        Path | None,
        typer.Option("--bindings", help="Reviewed runtime-binding-set-v1 YAML/JSON."),
    ] = None,
    target_profile: Annotated[
        Path | None,
        typer.Option(
            "--target-profile",
            help="Explicit digest-attested execution-target-profile-v1 YAML/JSON.",
        ),
    ] = None,
    platform: Annotated[
        str,
        typer.Option("--platform", help="Target platform adapter (currently: garak)."),
    ] = "garak",
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Output root (default: runs/)."),
    ] = Path("runs"),
    readiness_only: Annotated[
        bool,
        typer.Option(
            "--readiness-only",
            help="Validate, bind and plan without authoring or compilation.",
        ),
    ] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Do not author text; compile only fully pre-bound plans."),
    ] = False,
    entry: Annotated[
        str | None,
        typer.Option("--entry", help="Process one exact scenario ID after bundle verification."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Rejected for authoritative STPA bundle inputs."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose", help="Verbose logging."),
    ] = False,
) -> None:
    """Generate Garak artifacts from a verified STPA execution bundle."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-5s %(name)s: %(message)s",
    )
    _validate_stpa_options(bundle, platform, force)

    # Deferred imports keep help/argument validation independent of bundle
    # content and prevent provider creation before deterministic readiness.
    (
        load_execution_bundle,
        garak_capabilities,
        compile_execution_artifact,
        build_garak_plan,
        bind_and_plan,
        resolve_execution_case,
    ) = _load_stpa_dependencies()
    verified = load_execution_bundle(bundle)
    runtime_bindings = _load_runtime_bindings(bindings)
    profile = _load_target_profile(target_profile)
    capabilities = garak_capabilities()
    selected = _select_stpa_entries(verified, entry)
    run_dir = output_dir / verified.run_id
    manifest_entries, invalid_or_failed = _process_stpa_entries(
        selected,
        run_dir,
        runtime_bindings,
        capabilities,
        readiness_only,
        no_llm,
        bind_and_plan,
        build_garak_plan,
        compile_execution_artifact,
        profile,
        resolve_execution_case,
    )
    manifest = _stpa_manifest(verified, platform, manifest_entries)
    manifest_path = write_manifest(run_dir, manifest)
    typer.echo(
        json.dumps(
            {"manifest": str(manifest_path), "counts": manifest["counts"]},
            indent=2,
        )
    )
    if invalid_or_failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
