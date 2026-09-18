# Policy-Driven Agentic Red Teaming

Takes pre-built **scenario** YAMLs, classifies their injection surface, and generates red-teaming artifacts that can be run on downstream evaluation platforms.

```
examples/scenarios/*.yaml
  → classify (injection surface + platform coverage)
  → generate a platform-specific artifact
  → runs/{scenario_id}/
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

1. **Classify** the injection surface from `narrative.entry_point` (`input` → `user_turn`, `tool_execution` → `tool_return`). Supply chain threats (`threat_name`) skip with no coverage.
2. **Skip** surfaces the target platform cannot express (including supply chain).
3. **Generate** a red-teaming artifact for that platform (transcript + detector rubric).
4. **Validate** deos the artifact pass all checks (`ok` / `errors`). 
5. **Gate** platform coverage: `full`, `partial`, or `skip`.

## Supported platforms

| Platform | Status | Details |
|----------|--------|---------|
| [Garak](https://github.com/NVIDIA/garak) | Supported | See `src/asago_artifact_generator/garak/` |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | Planned | — |
| [PyRIT](https://github.com/Azure/PyRIT) | Planned | — |

Each platform generator lives in its own subpackage under
`src/asago_artifact_generator/` and writes artifacts under `runs/`.

## Generate artifacts

```bash
# One scenario
asago-artifact-generator generate examples/scenarios/AP-T2-01-28712e.yaml --force -v

# All scenarios in examples/scenarios/
asago-artifact-generator generate -v
```

| Flag | Effect |
|------|--------|
| `--force` | Write garak JSON even when structural validation fails |
| `--dry-run` | Classify + LLM + validate only — no files written |
| `--no-llm` | Skip LLM (useful to test pre-plan surface skips) |
| `--output-dir DIR` | Override default `runs/` output directory |
| `-v` | Verbose logging |

### Pipeline

1. **Classify** injection surface from `narrative.entry_point` (`input` → `user_turn`, `tool_execution` → `tool_return`). `threat_name` containing “supply chain” is `none` (no coverage).
2. **Skip** unwritable surfaces (supply chain / `none`) — writes a minimal artifact without calling the LLM.
3. **Generate** artifact via one-shot LLM (`prompts/generate_artifact.md`).
4. **Validate** structural gates (rubric completeness, surface/turn alignment, schema). 
5. **Gate** platform coverage: `full`, `partial`, or `skip` .

## Source-pinned artifact foundation

The new authoring path accepts three target-free inputs:

- Producer-owned `scenario-handoff-v1` JSON or YAML.
- Native semantic scenario YAML, including dictionary-shaped
  `behavior_spec.gherkin_text`.
- A labeled development reference task.

Use `asago_artifact_generator.input_adapter.load_input` to validate the
vendored handoff kit, preserve authoritative narrative and Gherkin bytes, and
record SHA-256 source pins. Use `snapshot_input` or `snapshot_inputs` before
authoring when a supplied reference source must be copied into a run-local,
hash-addressed snapshot. These functions only read the source.

The consumer-owned `artifact-package-v1` contract lives in
`contracts/artifact-package/`. `package_io.write_package` writes a complete
directory atomically, and `load_package` verifies its manifest, member paths,
lengths, and digests before returning content. Runtime receipts remain outside
the immutable package. The existing `generate` command remains the legacy
compatibility path.

### Target-free authoring

Use `author` to run the bounded Call 1 plan and Call 2 package sequence. Supply
the complete inventory and runtime contract as JSON or YAML:

```bash
asago-artifact-generator author scenario.json \
  --inventory inventory.json \
  --runtime-contract runtime-contract.json \
  --output-dir runs/authoring
```

Authoring uses only the configured private model client. It sets provider
retries to zero, records prompts, raw and decoded responses, usage, controls,
and one shared correction allowance, and never contacts a target, setup,
discovery, or runtime-judge transport. An essential unresolved requirement
produces a retained `*.blocked.json` plan and no package. The package contains
the model-authored detector source, user-only stimulus, exact runtime-binding
declarations, observations, explanation, examples, and digest-bound evidence.

## Output layout

Each scenario gets its own directory under `runs/`:

```
runs/
  manifest.json
  AP-T2-01-28712e/
    AP-T2-01-28712e-garak.json    # Garak artifact
    validation.json               # structural gate result
```

**`{scenario_id}-garak.json`** — Garak artifact (transcript + detector predicates):

- `scenario_id`, `injection_surface`, `platform_coverage` (`full` | `partial` | `null` for skips)
- `narrative.summary`
- `disclosure` — `"This artifact contains AI generated content"`
- `model` — LLM used for generation (`null` on skip)
- `timestamp` — UTC ISO time when the artifact was written
- `turns[]` with adversarial attack turn
- `detector_rubric` (judge prompt + success/blocked rubrics)

**`validation.json`** — sidecar from the structural gate:

```json
{
  "ok": true,
  "checks": "Artifact structural gate after LLM generation: ...",
  "errors": []
}
```

Skipped scenarios (supply chain / unwritable surfaces) get a pre-plan `checks` string and no LLM call.

**`manifest.json`** — batch summary (`ok`, `gate_result`, `gate_reason`, `artifact_path`, optional `errors` per scenario). `gate_result` is coverage only.

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

## Project structure

```
├── src/asago_artifact_generator/    # shared models, LLM client, CLI
│   └── garak/                        # Garak platform generator
│       ├── plugins/                  # probe + detector sources
│       └── prompts/                  # generation prompt
├── tests/                            # unit tests
├── examples/
│   ├── scenarios/                    # input scenario YAMLs
│   └── demo/                         # Jupyter walkthrough
└── runs/                             # generated artifacts (gitignored)
```

## Modules

| Module | Role |
|--------|------|
| `cli.py` | `typer` CLI — orchestrates classify → generate → validate → save |
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
