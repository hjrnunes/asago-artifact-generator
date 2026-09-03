# Policy-Driven Agentic Red Teaming

Consumes verified STPA execution bundles, binds their explicit runtime facts,
and compiles ready plans into traceable Garak artifacts. Historical taxonomy-era
scenario YAML generation is retained only behind the explicit
`generate-legacy` command.

```
execution-bundle.json + runtime-binding-set.yaml
  → strict load and identity/digest verification
  → typed binding and platform readiness
  → reviewed stimulus placement and constrained conversation authoring
  → deterministic prompt history plus target-response oracle
  → runs/<run-id>/<scenario-id>/
```

## Setup

Asago Artifact Generator requires Python 3.11 or newer. The lock file is the
authoritative development environment.

```bash
uv sync --locked
cp .env.example .env   # set GEMINI_API_KEY or configure Ollama
```

The installed command is `asago-artifact-generator` and the Python package is
`asago_artifact_generator`.

Supported LLM backends: **Gemini** (default when `GEMINI_API_KEY` is set), **OpenAI**, **Ollama**, Hugging Face, or OpenRouter.

## How it works

1. **Verify** the canonical bundle, paired scenario/projection bytes, closed
   schema, identities, and semantic digests.
2. **Resolve** each producer-owned execution contract against an explicit,
   reviewed target profile. This produces either a `BoundExecutionCase` or a
   typed exclusion; the consumer never invents a tool or changes the attack.
   An explicit complete simulation profile may be inferred, but its selected
   simulation behavior is retained and the resulting claim stays
   simulation-scoped.
3. **Bind** reviewed semantic values, concrete surfaces, stimulus delivery,
   control-action tools, and observers through `RuntimeBindingSet`.
4. **Assess readiness** independently for source integrity, semantic/runtime
   binding, and Garak support. Only `overall: ready` reaches authoring.
5. **Plan and compile** fixed prompt-side messages, bound tool declarations,
   and an oracle derived from the producer unsafe condition. The target response
   is never authored or inserted into the compiled conversation.
6. **Publish** the bound case or exclusion alongside readiness, execution-plan,
   artifact, validation, trace, and a
   batch manifest atomically. Runtime observations are separate receipts.

## Supported platforms

| Platform | Status | Details |
|----------|--------|---------|
| [Garak](https://github.com/NVIDIA/garak) | Supported | See `src/asago_artifact_generator/garak/` |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | Planned | — |
| [PyRIT](https://github.com/Azure/PyRIT) | Planned | — |

Each platform generator lives in its own subpackage under
`src/asago_artifact_generator/` and writes artifacts under `runs/`.

## Generate STPA artifacts

```bash
# Verify, bind and compile all bundle entries for Garak
asago-artifact-generator generate \
  --bundle <run>/execution-bundle.json \
  --bindings <runtime-binding-set.yaml> \
  --platform garak \
  --output-dir runs

# Readiness and plan only; no authoring, model client, or artifact compilation
asago-artifact-generator generate \
  --bundle <run>/execution-bundle.json \
  --bindings <runtime-binding-set.yaml> \
  --readiness-only
```

| Flag | Effect |
|------|--------|
| `--bundle PATH` | Required canonical `stpa-execution-bundle-v1` JSON index |
| `--bindings PATH` | Optional reviewed `runtime-binding-set-v1` YAML/JSON |
| `--platform garak` | Select the deterministic Garak adapter |
| `--readiness-only` | Validate, bind, and plan without authoring or compilation |
| `--no-llm` | Compile only when all presentation slots are already supplied |
| `--entry SCENARIO_ID` | Process one exact entry after bundle verification |
| `--output-dir DIR` | Override default `runs/` output directory |
| `--force` | Rejected for authoritative STPA inputs; it cannot bypass readiness |
| `-v` | Verbose logging |

### Pipeline

The primary command never autodetects YAML or falls back to narrative parsing.
The model-backed author receives only fixed conversation slots after readiness;
it cannot choose steps, surfaces, tools, values, observers, or detector logic.
The artifact generator does not choose or run a Garak probe; a separate
campaign orchestrator routes the compiled case using its typed delivery
profile. A target profile is explicit input when the producer contract needs
target resources; without one, the entry is retained as a typed exclusion.
Every profile match includes the requirement's exact surfaces, operation,
owner, role, properties, and attacker-influence facts; no fuzzy or prose
fallback is used.

### Historical generation

The retired taxonomy-era workflow remains available only when explicitly named:

```bash
asago-artifact-generator generate-legacy \
  examples/scenarios/AP-T2-01-28712e.yaml \
  --output-dir runs
```

It is heuristic compatibility behavior and is not an input path for the STPA
consumer.

## Output layout

Each bundle run gets its own directory under `runs/`:

```
runs/
  <run-id>/
    artifact-manifest.json
    SCN-001/
      bound-execution-case.json  # or execution-case-exclusion.json
      readiness.json
      execution-plan.json
      executable-conversation.json
      artifact-trace.json
      validation.json
      observations/              # optional append-only receipts
```

For a ready STPA entry, `executable-conversation.json` contains OpenAI-style
prompt-side `messages`, reviewed tools, delivery profile, structured oracle,
source/binding identity, and a semantic digest. Direct prompts, indirect tool
results, and ordinary multi-turn context use the same compiler. The messages
end before the target response; the oracle describes the unsafe behavior the
runner must observe. `artifact-trace.json` closes the stimulus and oracle back
to the verified projection and binding set. No artifact is written for invalid,
unbound, or unsupported entries.

The historical `generate-legacy` command retains its former artifact shape:

- `scenario_id`, `injection_surface`, `platform_coverage` (`full` | `partial` | `null` for skips)
- `narrative.summary`
- `disclosure` — `"This artifact contains AI generated content"`
- `model` — LLM used for generation (`null` on skip)
- `timestamp` — UTC ISO time when the artifact was written
- `turns[]` with adversarial attack turn
- `detector_rubric` (judge prompt + success/blocked rubrics)

**`validation.json`** — sidecar from the STPA compiler (or historical gate):

```json
{
  "ok": true,
  "checks": "Artifact structural gate after LLM generation: ...",
  "errors": []
}
```

Skipped scenarios (supply chain / unwritable surfaces) get a pre-plan `checks` string and no LLM call.

**`artifact-manifest.json`** — run-level summary retaining exact readiness
states, diagnostics, output paths, and per-entry errors. Its `counts.readiness`
map is independent of `counts.execution_case_excluded` and
`counts.analytical_only`, so a bundle containing only analytical exclusions is
reported explicitly instead of appearing to contain zero results.

## Interactive demo

End-to-end Jupyter walkthrough (API key → scenario YAML → artifact → Garak `toolchat.ToolChat` attack).

From the **repository root**:

```bash
uv sync --locked
uv pip install ipywidgets jupyter ipykernel
uv run python -m ipykernel install --user --name asago-artifact-generator --display-name "asago-artifact-generator"
cp .env.example .env   # set GEMINI_API_KEY or GOOGLE_API_KEY
uv run jupyter notebook examples/demo/garak-artifact-demo.ipynb
```

In Cursor / VS Code, pick this repo’s `.venv` as the notebook kernel. Gemini is a first-class provider (`GEMINI_API_KEY` or `GOOGLE_API_KEY`).

## Development

```bash
./scripts/quality.sh
uv run pytest tests/ -q
```

The unit test suite is deterministic and does not require an LLM endpoint.

The strict bundle, immutable intent, runtime-binding, and readiness seams are
documented in [docs/stpa-execution-consumer.md](docs/stpa-execution-consumer.md).

## Project structure

```
├── src/asago_artifact_generator/    # strict consumer models, planning, CLI
│   ├── bundle/                       # verified bundle loader
│   ├── models/                       # immutable intent/binding/readiness values
│   ├── planning/                     # pure binding and readiness
│   ├── platforms/                    # typed adapter seams
│   └── garak/                        # capabilities, plan, compiler, legacy code
├── tests/                            # unit tests
├── examples/
│   ├── scenarios/                    # input scenario YAMLs
│   └── demo/                         # Jupyter walkthrough
└── runs/                             # generated artifacts (gitignored)
```

## Modules

| Module | Role |
|--------|------|
| `cli.py` | Explicit STPA `generate` CLI and isolated `generate-legacy` command |
| `bundle/loader.py` | Strict bundle, pair, path, schema, and digest verification |
| `models/` | Immutable `ExecutionIntent`, `RuntimeBindingSet`, and readiness models |
| `planning/bind.py` | Pure typed runtime binding and platform readiness |
| `platforms/base.py` | Generic platform plan/compiler and compiled-artifact seams |
| `authoring.py` | Constrained presentation-only author interface |
| `garak/capabilities.py` | Deterministic Garak capability facts |
| `garak/plan.py` | Ready-plan to immutable Garak plan translation |
| `garak/conversation.py` | Target-facing prompt-history and oracle compiler |
| `garak/compile.py` | Public compiler seam plus isolated historical compiler |
| `trace.py` | Immutable artifact trace and observation-receipt contracts |
| `output.py` | Atomic readiness, plan, artifact, trace, and manifest output |
| `garak/gen.py` | Core generation logic for Garak artifacts |
| `garak/artifact_spec.py` | `ScenarioArtifact` schema, LLM call, `gate_artifact_errors`, artifact dicts |
| `garak/spec_io.py` | Paths and I/O for `runs/{id}/{id}-garak.json` and `validation.json` |
| `garak/classify.py` | Injection-surface table, pre-plan skip, `platform_coverage` gates |
| `extract.py` | Load scenario YAML into `ScenarioContext` |
| `garak/gate.py` | `gate_from_context` — full vs partial vs skip coverage |
| `llm.py` | Provider-agnostic completion (Gemini / Ollama / OpenAI / HF / OpenRouter) |
| `garak/prompts/generate_artifact.md` | One-shot artifact generation prompt |

## License

Apache 2.0 — see [LICENSE](LICENSE).
