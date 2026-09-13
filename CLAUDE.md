# Asago Artifact Generator

Policy-driven agentic red-teaming: consumes verified STPA execution bundles,
binds explicit runtime facts, and generates traceable downstream artifacts.
Historical taxonomy-era scenario YAMLs are isolated behind `generate-legacy`.

## Commands

```bash
uv sync --locked
./scripts/quality.sh
uv run pytest tests/ -q
asago-artifact-generator generate --bundle <run>/execution-bundle.json --platform garak
```

Deterministic tests do not require an LLM endpoint. Live generation requires
a configured provider (Gemini, OpenAI, Ollama, Hugging Face, or OpenRouter)
via `.env` or environment variables.

## Architecture

- `src/asago_artifact_generator/` contains strict bundle models, typed runtime
  binding/readiness, platform seams, atomic output, the LLM client, and the
  `typer` CLI.
- `bundle/loader.py` is the only bundle-loading seam; `planning/bind.py` is the
  pure typed readiness seam. Platform compilers accept only ready plans.
- `garak/` contains deterministic capabilities, ready-plan translation,
  target-facing conversation/oracle compilation, and isolated historical code.
- `examples/scenarios/` contains committed input scenario YAMLs.
- `examples/demo/` contains the interactive Jupyter walkthrough and runtime.
- `runs/` holds generated artifacts (gitignored).

Read `README.md` before changing the pipeline interface. The primary `generate`
command requires an explicit canonical STPA bundle and never falls back to
legacy YAML/narrative inference. Model-backed presentation authoring runs only
after deterministic readiness and receives fixed text slots, never execution
choices. `--force` is rejected for authoritative STPA inputs.
The current pre-alpha execution contracts change in place. The producer fixes
the route, causal factor, operation, action, and oracle semantics. The producer
also owns the control-action-to-operation mapping: a `target_action`
requirement names the control action in `owner_ref` and the semantic operation
in `operation`, never the action id. The consumer validates ownership
(`owner_ref` equals the unsafe outcome's control action), resource
availability, operation support on the selected resource, and binding
consistency; it does not independently verify the producer's mapping, so a
resource that exposes several operations is bound to whichever the projection
names. An unbound route compiles nothing. The consumer
resolves target-agnostic model conversations without a profile. Resource-backed
actions with no selected environment remain pending until an explicit target or
simulation profile is supplied; an explicit complete simulation profile may
carry inferred authority and is always simulation-scoped. It does not
reinterpret prose or invent resources.
The Garak adapter deterministically supplies routine chat surfaces, stimulus
placement, chat completion, and semantic output observation. Runtime bindings
then supply only unresolved deployment locators, values, credentials, tools,
clocks, and external observers. Provenance-only structural factors do not
require writable surfaces or prompt messages. A `conversation_context`
stimulus that carries producer `turns` compiles to consecutive user messages
copied verbatim and in order; the author receives no slot for them, and the
case records `supplied_history` (`user_only`) with `turn_texts_verbatim`.
A projection-v3 direct prompt delivers the producer's `prepared_user_text`
verbatim with no author slot, and the compiler verifies each structured
omission-carrier stimulus quotation against the delivered prepared text or the
referenced published turn, failing closed on mismatch. When the ready plan
carries the closed `stpa-omission-evidence-v1` carrier, the compiled
`structured_oracle` copies it verbatim as `omission_evidence`, the
action-absence judge description appends one deterministic labeled canonical
evidence block derived from it (a pure function of the carrier bytes, never
model-authored or truncated, with the inconclusive rule unchanged), and the
conversation trace records the recomputed `omission_evidence_digest` beside
the proposition digest; tampering with the carrier, its digest, or the judge
block fails validation. The compiled conversation, compiler, and trace schema
versions are `asago-executable-conversation-v2`,
`garak-conversation-compiler-v2`, and
`asago-executable-conversation-trace-v2`; legacy compiled cases keep identical
behavior with schema-bumped bytes.
An `ordering` condition with `reference_tool` and `reference_argument`
compiles to an `event_order` oracle; legacy ordering stays unbound.
Compilation must end before the
target response. Probe selection and Garak execution belong to a separate
campaign orchestrator.
Manifest summaries keep readiness outcomes separate from semantic case
exclusions and report analytical-only exclusions explicitly.

## Development

- Runtime adapter/observer choices must preserve the producer's action kind.
  Final-answer text is evidence only for model-output outcomes, not internal
  messages or state events. Unresolved comparison placeholders remain semantic
  binding requirements; schemas establish types, not business rules.

- Track durable work in GitHub Issues and PRs.
- Run `./scripts/quality.sh` before pushing; CI enforces `ruff` + `pytest`.
- Update `README.md` and this file when an interface or workflow changes.
- `AGENTS.md` is a symlink to this file.
