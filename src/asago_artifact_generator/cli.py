"""CLI: scenario YAML → runs/{scenario_id}/{scenario_id}-garak.json (one-shot LLM)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
import yaml

from .authoring import (
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    PrivateModelAuthoringTransport,
)
from .detector_runtime import execute_detector
from .extract import load_scenario
from .garak.gen import generate_artifact, list_scenario_files
from .garak.spec_io import MANIFEST_FILE, runs_dir
from .input_adapter import InputKind, load_input
from .llm import BASE_URL, MODEL
from .profiles import (
    ProfileLoadError,
    authoring_profile_from_environment,
    load_authoring_profile,
)
from .reporting import garak_value

app = typer.Typer(
    help="Policy-driven agentic red-teaming artifact generator.",
    no_args_is_help=True,
)


@app.callback()
def _main() -> None:
    """Policy-driven agentic red-teaming artifact generator."""


@app.command()
def generate(
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
    """Generate Garak artifacts from scenario YAMLs."""

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


@app.command()
def author(
    source: Annotated[Path, typer.Argument(help="Scenario handoff or semantic input file.")],
    inventory: Annotated[
        Path,
        typer.Option("--inventory", help="Complete supplied operation/fact inventory JSON/YAML."),
    ],
    runtime_contract: Annotated[
        Path,
        typer.Option(
            "--runtime-contract", help="Supplied target-free runtime contract JSON/YAML."
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory receiving the immutable package."),
    ] = Path("runs/authoring"),
    input_kind: Annotated[
        InputKind | None,
        typer.Option("--input-kind", help="Explicit input kind; otherwise infer it."),
    ] = None,
    reference_label: Annotated[
        str | None,
        typer.Option("--reference-label", help="Optional development-input label."),
    ] = None,
    reference_id: Annotated[
        str | None,
        typer.Option("--reference-id", help="Optional reference-task identity."),
    ] = None,
    task_id: Annotated[
        str | None,
        typer.Option("--task-id", help="Stable task identity used in package metadata."),
    ] = None,
    plan_max_corrections: Annotated[
        int | None,
        typer.Option(
            "--plan-max-corrections",
            help="Plan-stage correction allowance (default: 1). Zero disables plan corrections.",
        ),
    ] = None,
    artifact_max_corrections: Annotated[
        int | None,
        typer.Option(
            "--artifact-max-corrections",
            help=(
                "Artifact-stage correction allowance (default: 1). Zero disables "
                "artifact corrections."
            ),
        ),
    ] = None,
    review_plan: Annotated[
        bool,
        typer.Option(
            "--review-plan/--no-review-plan",
            help="Review the plan semantically before artifact authoring (default: enabled).",
        ),
    ] = True,
    review_artifact: Annotated[
        bool,
        typer.Option(
            "--review-artifact/--no-review-artifact",
            help="Review the artifact semantically before packaging (default: enabled).",
        ),
    ] = True,
    review_model_profile: Annotated[
        str | None,
        typer.Option(
            "--review-model-profile",
            help=(
                "Recorded name of an already-authorized reviewer profile; the default "
                "inherits the configured private authoring profile."
            ),
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help=(
                "Named private authoring profile. Values are loaded in process and "
                "never persisted."
            ),
        ),
    ] = None,
    profiles_file: Annotated[
        Path,
        typer.Option(
            "--profiles-file",
            help="YAML file containing the named private authoring profiles.",
        ),
    ] = Path("config/model-profiles.yaml"),
    no_correction: Annotated[
        bool,
        typer.Option(
            "--no-correction",
            help=(
                "Legacy no-correction mode: both stage allowances become zero. "
                "Conflicts with an explicit nonzero stage allowance."
            ),
        ),
    ] = False,
    prior_author_correction_spend: Annotated[
        int,
        typer.Option(
            "--prior-author-correction-spend",
            "--prior-author-spend",
            help=(
                "Caller-supplied prior author/correction requests for this case "
                "(default: 0). Pass explicitly when resuming."
            ),
        ),
    ] = 0,
    prior_review_spend: Annotated[
        int,
        typer.Option(
            "--prior-review-spend",
            help=(
                "Caller-supplied prior design-review requests for this case "
                "(default: 0). Pass explicitly when resuming."
            ),
        ),
    ] = 0,
) -> None:
    """Author one target-free immutable detector package with per-stage corrections and reviews."""

    effective_review_profile = (
        review_model_profile if review_model_profile is not None else profile
    )
    try:
        policy = AuthoringPolicy.from_cli(
            plan_max_corrections=plan_max_corrections,
            artifact_max_corrections=artifact_max_corrections,
            review_plan=review_plan,
            review_artifact=review_artifact,
            no_correction=no_correction,
            review_model_profile=effective_review_profile,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    view = load_input(
        source,
        kind=input_kind,
        reference_label=reference_label,
        reference_id=reference_id,
    )
    inventory_data = _load_mapping(inventory, "inventory")
    runtime_data = _load_mapping(runtime_contract, "runtime contract")
    stable_task_id = task_id or view.scenario_id
    try:
        AuthoringBudget.from_prior_spend(
            task_id=stable_task_id,
            prior_author_correction_spend=prior_author_correction_spend,
            prior_review_spend=prior_review_spend,
        )
    except ValueError as exc:
        raise typer.BadParameter(
            str(exc),
            param_hint="--prior-author-correction-spend/--prior-review-spend",
        ) from None
    try:
        connection = (
            load_authoring_profile(profiles_file, profile)
            if profile is not None
            else authoring_profile_from_environment(base_url=BASE_URL, model=MODEL)
        )
    except ProfileLoadError as exc:
        raise typer.BadParameter(
            str(exc),
            param_hint="--profile/--profiles-file" if profile is not None else "--profile",
        ) from None
    transport_options: dict[str, object] = {
        "base_url": connection.base_url,
        "api_key": connection.api_key,
        "model": connection.model,
    }
    if profile is not None:
        transport_options["profile_name"] = connection.name
    transport = PrivateModelAuthoringTransport(
        **transport_options,
    )
    package_dir = output_dir / stable_task_id
    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=package_dir,
        task_id=stable_task_id,
        wire_version="v2",
        policy=policy,
        prior_author_correction_spend=prior_author_correction_spend,
        prior_review_spend=prior_review_spend,
    ).run(view, inventory_data, runtime_data)
    typer.echo(
        json.dumps(
            {
                "status": result.status,
                "package": str(result.package_path) if result.package_path else None,
                "review_status": result.review_status or None,
            }
        )
    )
    if result.status not in {"packaged", "accepted"}:
        for finding in result.findings:
            typer.echo(f"{finding.code}: {finding.detail}", err=True)
        raise typer.Exit(1)


@app.command()
def check(
    package: Annotated[Path, typer.Argument(help="Immutable artifact package directory.")],
    evidence_file: Annotated[
        Path | None,
        typer.Argument(help="JSON/YAML evidence packet (or use --evidence)."),
    ] = None,
    evidence_option: Annotated[
        Path | None,
        typer.Option("--evidence", "-e", help="JSON/YAML evidence packet."),
    ] = None,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0.1, help="Detector wall-clock deadline."),
    ] = 10.0,
) -> None:
    """Run exact packaged detector bytes against offline evidence."""

    evidence_path = evidence_option or evidence_file
    if evidence_path is None:
        raise typer.BadParameter("provide an evidence path as an argument or with --evidence")
    evidence = _load_mapping(evidence_path, "evidence")
    execution = execute_detector(package, evidence, timeout_seconds=timeout_seconds)
    typer.echo(
        json.dumps(
            {
                "status": execution.status,
                "failure": execution.failure,
                "package_digest": execution.package_digest,
                "package_digest_before": execution.package_digest_before,
                "package_digest_after": execution.package_digest_after,
                "detector_sha256": execution.detector_sha256,
                "detector_sha256_before": execution.detector_sha256_before,
                "detector_sha256_after": execution.detector_sha256_after,
                "docker_argv": list(execution.docker_argv),
                "result": execution.result,
                "garak_value": garak_value(execution),
            },
            sort_keys=True,
        )
    )
    if execution.status != "completed":
        raise typer.Exit(1)


def _load_mapping(path: Path, label: str) -> dict:
    try:
        document = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.suffix.lower() == ".json"
            else yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise typer.BadParameter(f"cannot read {label}: {exc}", param_hint=str(path)) from exc
    if not isinstance(document, dict):
        raise typer.BadParameter(f"{label} must be an object", param_hint=str(path))
    return document


if __name__ == "__main__":
    app()
