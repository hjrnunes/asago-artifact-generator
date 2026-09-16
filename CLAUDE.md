# Asago Artifact Generator

Policy-driven agentic red-teaming: designs executable test artifacts from
verified producer scenario handoffs, binds explicit runtime facts, and
compiles traceable downstream artifacts. The producer execution bundle and
the taxonomy-era scenario YAMLs are historical/retired inputs: their
read-only readers remain, isolated behind `generate` and `generate-legacy`.

## Commands

```bash
uv sync --locked
./scripts/quality.sh
uv run pytest tests/ -q

# Primary workflow: design one artifact from a scenario handoff plus explicit environment
asago-artifact-generator design \
  --handoff <run>/scenario-handoff.json \
  --target-profile <profile>.yaml \
  --runtime-context <state>.json \
  --output-dir runs

# Deterministic design run with a prebound author result (no model contact).
# --author-result takes the FLAT prebound result (an object with top-level
# stimulus_text and requested_amount), not an object keyed by scenario id.
asago-artifact-generator design \
  --handoff <run>/scenario-handoff.json \
  --author-result author-result.json \
  --no-llm

# When the handoff names no single record and the environment exposes several
# candidates, name the record explicitly (validated against observed state)
asago-artifact-generator design ... --record-hint ORD-101

# Historical/retired: compile from a producer execution bundle
asago-artifact-generator generate --bundle <run>/execution-bundle.json --platform garak
```

Deterministic tests do not require an LLM endpoint. Live generation requires
a configured provider (Gemini, OpenAI, Ollama, Hugging Face, or OpenRouter)
via `.env` or environment variables.

## Architecture

- `src/asago_artifact_generator/` contains the scenario-handoff reader, the
  typed artifact-design path, strict historical bundle models, typed runtime
  binding/readiness, platform seams, atomic output, the LLM client, and the
  `typer` CLI.
- `bundle/loader.py` is the only bundle-loading seam; `planning/bind.py` is the
  pure typed readiness seam. Platform compilers accept only ready plans.
- `garak/` contains deterministic capabilities, ready-plan translation,
  target-facing conversation/oracle compilation, and isolated historical code.
- `examples/scenarios/` contains committed input scenario YAMLs.
- `examples/demo/` contains the interactive Jupyter walkthrough and runtime.
- `runs/` holds generated artifacts (gitignored).

Read `README.md` before changing the pipeline interface.

### Primary workflow: design over scenario handoffs

The `design` command is the primary artifact-design path. The verified
scenario handoff plus an explicit environment are the only producer inputs;
no execution bundle or projection is read.

- The reader verifies the vendored producer contract kit
  (`contracts/scenario-handoff/`, `UPSTREAM.lock`-pinned) before every load
  and fails closed on kit tampering. It rejects corrupted, unknown-version,
  or unresolvable handoffs, a cited-but-undeclared lineage id
  (`lineage_unresolved`), and a handoff missing the semantic failure
  criterion or safe alternative (`handoff_schema_invalid`) with typed
  reasons before any design or compilation.
- Environment resolution: `--target-profile` and `--runtime-context` select
  the explicit environment. Omitting either flag admits the request into the
  design run and persists a typed `needs-environment-binding` exclusion
  (design record plus exclusion record, nothing compiled) instead of failing
  with a CLI usage error.
- The consumer owns the test design: it selects the test record and
  establishes its prerequisites, authors the concrete stimulus wording (never
  producer text; designed history is user-only for exactly one
  continuation), derives the executable detector from the handoff's semantic
  failure criterion with the environment-observed limit, and records the
  fidelity assessment and honest observation limits (command-level, never
  money movement). Amount attribution binds the recorded `requested_amount`
  only when the actual stimulus text states it: an incidental numeric
  substring, a separate field contradicting the text, and negated or
  ambiguous requests stay typed-unresolved
  (`amount-attribution-unresolved`). Criterion-shape interpretation is scoped
  to the selected unsafe behavior: the criterion authoritatively selects the
  shape, auxiliary text (such as a safe-alternative sentence) cannot switch
  it, and compound criteria stay typed `ambiguous-criterion-shape`. That
  switch prohibition is exact only for criterion-selected shapes: when the
  criterion is silent about status, the precondition-record fallback
  (`_record_precondition`) computes its required-status set over the
  unscoped corroboration pool (`include_safe_alternative=True`), so
  safe-alternative wording can help supply (corroborate, not switch) the
  `precondition_record` shape. Before
  compilation the consumer freezes artifact-owned
  text and evidence behind a content digest; tampering fails verification.
- Supported designs compile to the Garak-runner-consumable executable
  conversation (`asago-executable-conversation-v2`) plus an
  `artifact-design-plan-v1` execution plan carrying the frozen-content
  digest (`frozen_content_digest`) so execution receipts can cite and verify
  it directly. Live authoring is fully evidenced: every `LLMArtifactAuthor`
  attempt, including malformed or rejected responses, and the authoring call
  count are persisted in the design record and the compiled design's trace.
  Authoring also receives the selected operation's exact description and
  argument schema, plus a target-context contract for required non-attacked
  identities. Compiled artifacts preserve the selected operation and observed
  read-only lookup tools, while direct context delivery uses observed values
  and never substitutes `UNKNOWN`.
- Blocked designs are preserved with typed exclusion reasons
  (`needs-environment-binding`, `unsupported-observation`, `missing-setup`,
  `unresolved-prerequisite`, `unsupported-criterion-shape`, `invalid-design`,
  `effect-criterion-unsupported-by-command-observation`,
  `amount-attribution-unresolved`, `ambiguous-criterion-shape`, and others;
  the closed set is `DESIGN_EXCLUSION_CODES` in
  `src/asago_artifact_generator/design/records.py`) and are never compiled or
  dropped. A `--record-hint` is
  validated against the observed environment state (an unknown id fails
  closed with a typed `missing-setup` exclusion), disclosed in the design
  manifest as an explicit consumer choice, and recorded in the setup's
  establishment. No record is ever invented when none exists in the
  environment.
- Scenario kinds follow the recorded functional-feasibility decision
  (`docs/development/functional-feasibility-decision.md`): adversarial and
  functional handoffs both enter the criterion-shape interpretation, so the
  supported functional case class (a command-level criterion, e.g. the
  vendored functional refund-limit handoff) designs and compiles through the
  existing command-level observation capability with no invented attacker,
  while functional criteria no existing capability faithfully measures (the
  omission-shaped criteria the saved generations persist) stay typed-blocked
  (`unsupported-criterion-shape`; a persisted criterion that interprets to a
  shape only through corroboration-pool wording instead blocks
  `unsupported-observation`). Any other kind stays fail-closed
  blocked with `unsupported-scenario-kind`.
- Detector design and executable contracts are downstream-owned with no
  producer admission coupling: the handoff carries no admission record, every
  scenario is designed or typed-excluded by the consumer alone, and the
  design authority/trace chain references consumer design records plus the
  reused runtime/observer capability.
- Manifest summaries keep design outcomes separate from typed scenario
  exclusions and report exclusions explicitly. Exit-code semantics: a typed
  design exclusion exits 1 while still writing a valid
  `design-manifest.json` (`compiled: false`).

### Historical/retired bundle path

`generate` compiles from a canonical `stpa-execution-bundle-v1`/`-v2` JSON
index and is retired from the normal product path; its read-only readers
remain and must not grow execution or persistence dependencies. The
`generate` command never falls back to legacy YAML/narrative inference,
model-backed authoring runs only after deterministic readiness and receives
fixed text slots, never execution choices, and `--force` is rejected for
authoritative STPA inputs. The compiler dispatches exact version triples by
projection schema: historical bundle-v1/projection-v2 cases retain their
original v1 conversation, compiler, trace, and digest frames, while
projection-v3 uses the v2 triple (`asago-executable-conversation-v2`,
`garak-conversation-compiler-v2`, and
`asago-executable-conversation-trace-v2`); unknown or mixed generations are
rejected. A `conversation_context` stimulus carrying producer `turns`
compiles to consecutive user messages copied verbatim and in order, and a
projection-v3 direct prompt delivers the producer's `prepared_user_text`
verbatim with no author slot. The Garak adapter deterministically supplies
routine chat surfaces, stimulus placement, chat completion, and semantic
output observation; runtime bindings supply only unresolved deployment
locators, values, credentials, tools, clocks, and external observers.
Compilation must end before the target response. Probe selection and Garak
execution belong to a separate campaign orchestrator.

The taxonomy-era `generate-legacy` command is heuristic compatibility
behavior and is not an input path for the STPA consumer.

## Development

- Runtime adapter/observer choices must preserve the producer's action kind.
  Final-answer text is evidence only for model-output outcomes, not internal
  messages or state events. Unresolved comparison placeholders remain semantic
  binding requirements; schemas establish types, not business rules.

- Track durable work in GitHub Issues and PRs.
- Run `./scripts/quality.sh` before pushing; CI enforces `ruff` + `pytest`.
- Update `README.md` and this file when an interface or workflow changes.
- `AGENTS.md` is a symlink to this file.
