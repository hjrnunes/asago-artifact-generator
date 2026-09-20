# Asago Artifact Generator

Policy-driven agentic red-teaming: takes pre-built scenario YAMLs, classifies
their injection surface, and generates red-teaming artifacts for downstream
evaluation platforms.

## Commands

```bash
uv sync --locked
./scripts/quality.sh
uv run pytest tests/ -q
uv run asago-artifact-generator author <scenario-handoff-or-input.json> \
  --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id>
uv run asago-artifact-generator check <package-dir> --evidence <evidence.json>
```

`author` is the target-free primary workflow. It defaults to one plan
correction, one artifact correction, and both semantic reviews enabled; the
stages are configured independently through `--plan-max-corrections`,
`--artifact-max-corrections`, `--review-plan/--no-review-plan`,
`--review-artifact/--no-review-artifact`, and `--review-model-profile`.
Deterministic checks and artifact Docker detector controls run before each
semantic review; a reviewer `revise` consumes its own stage's allowance, and a
reviewer `blocked` at the artifact stage stops as `needs_plan_revision`.
`check` executes only supplied evidence through the immutable package.
Deterministic tests do not require an LLM endpoint. Live authoring requires
a configured provider (Gemini, OpenAI, Ollama, Hugging Face, or OpenRouter)
via `.env` or environment variables.

For the approved private endpoint, invoke `author` with
`--profile gemma4-oc --profiles-file /absolute/path/to/asago-scenario-generator/config/model-profiles.yaml`.
The consumer resolves the named profile in process and passes its base URL,
API key, and model directly to `PrivateModelAuthoringTransport`. Credentials
and endpoint values stay in memory and never enter shell output, prompts,
ledgers, packages, or failure evidence. Without `--profile`, environment-only
configuration still works when it supplies a real API key; missing credentials
fail before dispatch.

Prompt roles are versioned independently from the response wire:
`authoring-call1-v3`, `authoring-plan-review-v1`,
`authoring-call2-v3`, `authoring-artifact-review-v1`, and
`authoring-correction-v3`. Each rendered packet exposes a SHA-256 hash and
dispatch evidence records the role, version, hash, raw response, controls,
findings, and review status. The v3 author prompts keep the v2 11-field plan
and two-block artifact contracts unchanged.

Build reviewer contexts from the original scenario and supplied facts,
operations, result schemas, provenance, and runtime capabilities. Do not pass
an author transcript or unrelated plumbing to a reviewer. Artifact authoring
receives the accepted plan as read-only and cannot rewrite plan-owned setup,
bindings, prerequisites, observations, or judge decisions. Correction prompts
show only the active plan or artifact format and all current findings.
Overflow, endpoint/credential values, and bounded duplicate candidate forms
fail before dispatch. The neutral plan example uses a case-permitted status
binding when supplied operations provide one; otherwise it is a labeled
generic illustration with no operation, binding, or prerequisite.

The `generate` command remains a read-only compatibility path for historical
scenario YAMLs. New work uses producer `run`, consumer `author`, consumer
`check`, and frozen downstream execution.

## Architecture

- `src/asago_artifact_generator/` contains shared domain models, the LLM
  client, the exact-source Docker detector harness, and the `typer` CLI.
- `src/asago_artifact_generator/garak/` contains the Garak platform generator:
  classification, gating, artifact specification, artifact I/O, prompt templates,
  and Garak plugin sources (probe + detector).
- `examples/scenarios/` contains committed input scenario YAMLs.
- `examples/demo/` contains the interactive Jupyter walkthrough and runtime.
- `runs/` holds generated artifacts (gitignored).

The `check` command runs only supplied or synthetic evidence. It executes the
packaged `detector.py` bytes in constrained `/usr/local/bin/docker` using
`python:3.12-slim`; target, model, setup, and discovery transports stay outside
the consumer runtime.

Read `README.md` before changing the pipeline interface. Each platform
generator lives in its own subpackage (`garak/`, future `agentdojo/`, `pyrit/`)
and shares the parent package modules (`extract`, `llm`).

## Development

- Track durable work in GitHub Issues and PRs.
- Run `./scripts/quality.sh` before pushing; CI enforces `ruff` + `pytest`.
- Update `README.md` and this file when an interface or workflow changes.
- `AGENTS.md` is a symlink to this file.
