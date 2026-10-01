"""Garak artifact generation: scenario YAML → runs/{scenario_id}/{scenario_id}-garak.json."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..extract import ScenarioContext
from .artifact_spec import (
    ARTIFACT_VALIDATION_CHECKS,
    GENERATE_ARTIFACT_PROMPT,
    SKIP_VALIDATION_CHECKS,
    artifact_to_dict,
    generate_scenario_artifact,
    skip_to_artifact_dict,
)
from .classify import lookup_surface, surface_skip_reason
from .gate import gate_from_context
from .spec_io import (
    runs_dir,
    save_garak_artifact,
    save_validation,
)

log = logging.getLogger(__name__)

EXAMPLES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "examples" / "scenarios"


@dataclass
class GenResult:
    scenario_id: str
    ok: bool
    gate: str
    gate_reason: str = ""
    artifact_path: str | None = None
    errors: list[str] | None = None


def _trigger_tool(ctx: ScenarioContext) -> str:
    return ctx.quoted_tools[0] if ctx.quoted_tools else ""


def _coverage(ctx: ScenarioContext, surface: str) -> tuple[str, str]:
    trigger = _trigger_tool(ctx)
    return gate_from_context(
        ctx,
        surface=surface,
        trigger_tool=trigger,
        oracle_kind="forbidden_call" if trigger else "output_string",
        tools_grounded=bool(ctx.quoted_tools),
    )


def generate_artifact(
    ctx: ScenarioContext,
    *,
    prompt_path: Path | None = None,
    output_dir: Path | None = None,
    use_llm: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> GenResult:
    """Scenario YAML → runs/{scenario_id}/{scenario_id}-garak.json.
    ``gate`` is Garak coverage (full / partial / skip)
    ``ok`` / ``errors`` are structural validation.
    ``force`` only controls whether an invalid artifact JSON is written.
    """
    prompt = prompt_path or GENERATE_ARTIFACT_PROMPT
    out_base = runs_dir(output_dir)

    skip_reason = surface_skip_reason(ctx)
    if skip_reason:
        log.info("SKIP %s — %s", ctx.scenario_id, skip_reason)
        artifact_path: str | None = None
        if not dry_run:
            save_validation(
                ctx.scenario_id,
                True,
                [],
                out_base,
                checks=SKIP_VALIDATION_CHECKS,
            )
            path = save_garak_artifact(ctx.scenario_id, skip_to_artifact_dict(ctx), out_base)
            artifact_path = str(path)
            log.info("DONE %s → %s (no coverage)", ctx.scenario_id, path.name)
        return GenResult(
            scenario_id=ctx.scenario_id,
            ok=True,
            gate="skip",
            gate_reason=skip_reason,
            artifact_path=artifact_path,
        )

    injection_surface = lookup_surface(ctx)
    gate_result, gate_reason = _coverage(ctx, injection_surface)
    log.info(
        "Classified %s injection_surface=%s coverage=%s",
        ctx.scenario_id,
        injection_surface,
        gate_result,
    )

    if not use_llm:
        return GenResult(
            scenario_id=ctx.scenario_id,
            ok=False,
            gate=gate_result,
            gate_reason=gate_reason,
            errors=["Artifact generation requires LLM (omit --no-llm)"],
        )

    build = generate_scenario_artifact(ctx, prompt, injection_surface=injection_surface)
    save_validation(
        ctx.scenario_id,
        build.ok,
        build.errors,
        out_base,
        checks=ARTIFACT_VALIDATION_CHECKS,
    )

    if not build.ok:
        log.warning("Artifact validation failed for %s: %s", ctx.scenario_id, build.errors)
        if not force:
            return GenResult(
                scenario_id=ctx.scenario_id,
                ok=False,
                gate=gate_result,
                gate_reason=gate_reason,
                errors=build.errors,
            )

    if dry_run:
        log.info("DRY-RUN %s — gate=%s (%s)", ctx.scenario_id, gate_result, gate_reason)
        return GenResult(
            scenario_id=ctx.scenario_id,
            ok=build.ok,
            gate=gate_result,
            gate_reason=gate_reason,
            errors=build.errors if build.errors else None,
        )

    artifact_path = save_garak_artifact(
        ctx.scenario_id,
        artifact_to_dict(ctx, build.artifact, platform_coverage=gate_result),
        out_base,
    )
    log.info("DONE %s → %s", ctx.scenario_id, artifact_path.name)
    return GenResult(
        scenario_id=ctx.scenario_id,
        ok=build.ok,
        gate=gate_result,
        gate_reason=gate_reason,
        artifact_path=str(artifact_path),
        errors=build.errors if build.errors else None,
    )


def list_scenario_files() -> list[Path]:
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(EXAMPLES_DIR.glob("AP-*.yaml"))
