# Asago Artifact Generator

Policy-driven agentic red-teaming: takes pre-built scenario YAMLs, classifies
their injection surface, and generates red-teaming artifacts for downstream
evaluation platforms.

## Commands

```bash
uv sync --locked
./scripts/quality.sh
uv run pytest tests/ -q
asago-artifact-generator generate -v
asago-artifact-generator check <package-dir> --evidence <evidence.json>
asago-artifact-generator author <source> --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> --task-dispatch-limit 2 \
  --aggregate-dispatch-limit <aggregate-limit> --aggregate-spent <already-spent> \
  --no-correction
```

Deterministic tests do not require an LLM endpoint. Live generation requires
a configured provider (Gemini, OpenAI, Ollama, Hugging Face, or OpenRouter)
via `.env` or environment variables.

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
