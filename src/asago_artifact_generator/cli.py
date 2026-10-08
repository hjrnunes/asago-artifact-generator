"""CLI: target-free artifact authoring (`generate`)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from .authoring.core import (
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    AUTHORING_THINKING_EXTRA_BODY,
    REVIEW_THINKING_EXTRA_BODY,
    AuthoringTransport,
)
from .authoring.orchestrator import AuthoringOrchestrator
from .authoring.policy import AuthoringPolicy, AuthoringResult
from .authoring.transport import PrivateModelAuthoringTransport
from .contract_kit import parse_document
from .input_adapter import InputSourceError, load_input
from .profiles import AuthoringProfile, ProfileLoadError, load_authoring_profile
from .target_inputs import TargetInputError, load_target_inputs

app = typer.Typer(
    help="Policy-driven agentic red-teaming artifact generator.",
    no_args_is_help=True,
)

TransportFactory = Callable[..., AuthoringTransport]


def run(args: Sequence[str], *, prog_name: str, transport_factory: TransportFactory) -> None:
    """Run the CLI with *transport_factory* building the provider transport."""

    app(args=list(args), prog_name=prog_name, obj=transport_factory)


@app.callback()
def _main() -> None:
    """Policy-driven agentic red-teaming artifact generator."""


@app.command()
def generate(
    ctx: typer.Context,
    source: Annotated[
        Path,
        typer.Argument(help="Producer scenario-handoff-v4 JSON/YAML file."),
    ],
    target_profile: Annotated[
        Path,
        typer.Option(
            "--target-profile",
            help="Producer execution-target-profile-v1 discovery output.",
        ),
    ],
    runtime_contract: Annotated[
        Path,
        typer.Option(
            "--runtime-contract", help="Supplied target-free runtime contract JSON/YAML."
        ),
    ],
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help=(
                "Named private authoring profile (required). Values are loaded in "
                "process and never persisted."
            ),
        ),
    ],
    target_observations: Annotated[
        Path | None,
        typer.Option(
            "--target-observations",
            help="Optional producer runtime-context JSON/YAML discovery output.",
        ),
    ] = None,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory receiving the immutable package."),
    ] = Path("runs/authoring"),
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
    profiles_file: Annotated[
        Path,
        typer.Option(
            "--profiles-file",
            help="YAML file containing the named private authoring profiles.",
        ),
    ] = Path("config/model-profiles.yaml"),
) -> None:
    """Author one immutable package from a producer scenario handoff and discovery output."""

    policy = _authoring_policy(
        plan_max_corrections=plan_max_corrections,
        artifact_max_corrections=artifact_max_corrections,
        review_plan=review_plan,
        review_artifact=review_artifact,
        review_model_profile=(
            review_model_profile if review_model_profile is not None else profile
        ),
    )
    try:
        view = load_input(source)
    except InputSourceError as exc:
        raise typer.BadParameter(str(exc), param_hint=str(source)) from None
    try:
        inventory_data, discovery_provenance = load_target_inputs(
            target_profile,
            target_observations,
        )
    except TargetInputError as exc:
        raise typer.BadParameter(str(exc), param_hint=str(target_profile)) from None
    runtime_data = _load_mapping(runtime_contract, "runtime contract")
    stable_task_id = task_id or view.scenario_id
    try:
        connection = load_authoring_profile(profiles_file, profile)
    except ProfileLoadError as exc:
        raise typer.BadParameter(str(exc), param_hint="--profile/--profiles-file") from None
    sampling_controls = connection.sampling_controls
    transport_factory = ctx.obj if ctx.obj is not None else PrivateModelAuthoringTransport
    transport = transport_factory(
        **_transport_options(connection),
        extra_body=(deepcopy(AUTHORING_THINKING_EXTRA_BODY) if sampling_controls else None),
        review_extra_body=(deepcopy(REVIEW_THINKING_EXTRA_BODY) if sampling_controls else None),
        review_fill_context=True,
    )
    package_dir = output_dir / stable_task_id
    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=package_dir,
        task_id=stable_task_id,
        policy=policy,
        discovery_provenance=discovery_provenance,
    ).run(view, inventory_data, runtime_data)
    _report_result(result)


def _authoring_policy(**options: Any) -> AuthoringPolicy:
    try:
        return AuthoringPolicy.from_cli(**options)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _transport_options(connection: AuthoringProfile) -> dict[str, object]:
    """Map a loaded profile to transport keywords, omitting unset optional controls."""

    transport_options: dict[str, object] = {
        "base_url": connection.base_url,
        "api_key": connection.api_key,
        "model": connection.model,
        "context_window_tokens": (
            connection.context_window
            if connection.context_window is not None
            else AUTHORING_CONTEXT_WINDOW_TOKENS
        ),
        "max_completion_tokens": (
            connection.max_completion_tokens
            if connection.max_completion_tokens is not None
            else AUTHORING_MAX_COMPLETION_TOKENS
        ),
        "profile_name": connection.name,
    }
    if connection.reasoning_effort is not None:
        transport_options["reasoning_effort"] = connection.reasoning_effort
    if connection.service_tier is not None:
        transport_options["service_tier"] = connection.service_tier
    if connection.service_tier_fallback is not None:
        transport_options["service_tier_fallback"] = connection.service_tier_fallback
    if connection.sampling_controls is False:
        transport_options["sampling_controls"] = False
    if connection.strict_json_schema is not None:
        transport_options["strict_json_schema"] = connection.strict_json_schema
    if connection.timeout is not None:
        transport_options["timeout"] = connection.timeout
    return transport_options


def _report_result(result: AuthoringResult) -> None:
    typer.echo(
        json.dumps(
            {
                "status": result.status,
                "package": str(result.package_path) if result.package_path else None,
                "review_status": result.review_status or None,
            }
        )
    )
    if result.status != "accepted":
        for finding in result.findings:
            typer.echo(f"{finding.code}: {finding.detail}", err=True)
        raise typer.Exit(1)


def _load_mapping(path: Path, label: str) -> dict:
    try:
        document = parse_document(path, path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise typer.BadParameter(f"cannot read {label}: {exc}", param_hint=str(path)) from exc
    if not isinstance(document, dict):
        raise typer.BadParameter(f"{label} must be an object", param_hint=str(path))
    return document


if __name__ == "__main__":
    app()
