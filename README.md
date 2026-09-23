# Policy-Driven Agentic Red Teaming

Takes pre-built **scenario** YAMLs, classifies their injection surface, and generates red-teaming artifacts that can be run on downstream evaluation platforms.
## Primary delivery workflow

Run the target-free consumer workflow from this repository root:

```bash
cd <consumer-repo-root>
uv sync --locked
uv run asago-artifact-generator author <scenario-handoff-or-input.json> \
  --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id>
uv run asago-artifact-generator check runs/authoring/<case-id>/<case-id> \
  --evidence <evidence.json>
```

`author` uses the versioned v2 authoring wire. Call 1 returns one closed plan
root as either one bare JSON object or exactly one lowercase `json` fenced
object, with optional surrounding whitespace. Untagged, uppercase, or other
fences, multiple objects or blocks, prose, trailing content, and malformed JSON
are rejected. The raw Call 1 bytes remain preserved, and a removed outer fence
is recorded before plan validation. Call 2 remains exactly one fenced JSON
metadata block followed by one fenced Python block; one or more
whitespace-only lines may separate the blocks. The Python block becomes
`detector.py` byte-for-byte.
Each v2 prompt carries the selected case meaning once under `case_meaning`;
the input projection retains scenario/reference identities and narrative/Gherkin
SHA-256 digests without repeating those texts.
The accepted Call 1 plan owns setup, bindings, prerequisites, evidence,
assumptions, observation requirements, and judge decisions; Call 2 cannot
resubmit those fields. Historical v1 readers remain explicit for preserved
responses and packages.

### Private authoring profiles

For live private authoring, pass the approved named profile and the producer
profile file directly to `author`:

```bash
uv run asago-artifact-generator author <scenario-handoff-or-input.json> \
  --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id> \
  --profile gemma4-oc \
  --profiles-file /absolute/path/to/asago-scenario-generator/config/model-profiles.yaml
```

The consumer loads `base_url`, `api_key`, and `model` in process and passes
them directly to the private transport. It does not extract profile values
through a shell or print them. Credentials and endpoint values stay out of
prompts, ledgers, packages, and failure evidence. A missing profile or required
field stops before dispatch.
Every `author` request (author, correction, and review) sends
`chat_template_kwargs.enable_thinking=false` through the transport's additive
`extra_body`, records the non-secret controls, and keeps `max_retries=0`.
Configured live requests reserve a 32,768-token context window and an 8,192-token
completion limit, plus the existing 256-token framing reserve. The guard
estimates prompt tokens as the ceiling of all model-facing system and user
UTF-8 bytes, including correction feedback and the 128-byte schema/message
allowance, divided by a calibrated bytes-per-token ratio. It applies the same
estimate to authoring, review, and correction requests and rejects an overflow
before reserving a dispatch. The estimate must fit the remaining 24,320-token
input budget.

The calibration uses the minimum ratio from three saved
`authoring-plan-review-v2` requests on the `gemma4-oc` profile and
`gemma-4-26b-a4b-it` model: 3.964777680907 bytes per provider-reported prompt
token. A 12% margin lowers the ratio to 3.489004359198 bytes per estimated
token. The saved record paths, dispatch IDs, byte totals, and provider-reported
prompt-token values live in
`src/asago_artifact_generator/authoring.py` as `CONTEXT_GUARD_CALIBRATION`.
Every guard result labels the value `estimated_prompt_tokens`; provider usage
remains separate. A rejected prompt returns `prompt_overflow`, spends no
provider request or author/reviewer dispatch, and stays case-local in the
five-case caller.
If you omit `--profile`, existing environment-only configuration remains
supported when it provides a real API key; an absent key fails closed instead
of using a placeholder credential.

### Stage-local corrections and semantic review

By default `author` allows one plan correction and one artifact correction and
enables both semantic reviews. Configure the stages independently:

```bash
uv run asago-artifact-generator author <input.json> \
  --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> \
  --plan-max-corrections 1 \
  --artifact-max-corrections 1 \
  --review-plan/--no-review-plan \
  --review-artifact/--no-review-artifact \
  --review-model-profile <already-authorized-profile>
```

Correction allowances are nonnegative integers; booleans, negatives, and
non-integers are rejected before dispatch and values are never clamped. The
legacy `--no-correction` flag sets both stage allowances to zero and conflicts
with an explicit nonzero stage allowance instead of choosing a precedence.
Stage allowances are independent: a plan correction never consumes artifact
allowance, and zero disables only its own stage.

With reviews enabled, each stage runs deterministic checks (and, for the
artifact stage, the isolated Docker detector controls) before its semantic
review of a mechanically valid candidate. A reviewer returns one JSON object
with `decision` (`accept`, `revise`, or `blocked`), a nonblank `summary`, and
findings with exactly `location`, `problem`, `basis`, and `required_change`;
the framing rules match Call 1. `revise` feeds a stage correction that repeats
all checks, controls, and review; `blocked` at the artifact stage stops as
`needs_plan_revision` without recursing into plan authoring. Malformed or
contradictory reviewer responses and reviewer transport failures produce
`review_unavailable`, never a silent pass, and transport failures stop the run
with no automatic retry. An explicit caller, per-task, or aggregate budget cap
stops the run before the next dispatch.

### Supplied detector-control cases

Callers can add extra detector controls to the artifact stage with the
`supplied_control_cases` option on `AuthoringOrchestrator`. Pass either a
sequence of `ControlCase` objects or a callable that receives the current
candidate plan and metadata and returns the extra cases for that candidate,
so you can mechanically remap candidate-local binding names and dynamic record
IDs into the case evidence. The orchestrator runs the supplied cases together
with the mechanically derived controls in one isolated control execution and
labels every control result with `origin: normal` or `origin: supplied`. A
failing supplied control is an ordinary artifact-stage finding: it consumes
the existing single artifact correction, the correction packet carries the
actual failing control evidence, and the run terminates when the correction
allowance is exhausted. Without the option, orchestration and rendered prompt
bytes stay unchanged.

### Owner-supplied scope

Prepared inputs can optionally set `InputView.owner_scope` to keep owner-provided
scenario premises and evaluation instructions separate from verified inventory
facts and policy data:

```python
from dataclasses import replace

view = replace(
    view,
    owner_scope={
        "scenario_premises": [
            {"text": "A caller-supplied premise.", "source": "scope-spec.md"}
        ],
        "evaluation_instructions": [
            {"text": "Evaluate only the captured result.", "source": "scope-spec.md"}
        ],
    },
)
```

Each category contains `{text, source}` items. A non-empty block renders in its
own `SOURCE CONTEXT — OWNER-SUPPLIED SCOPE` section in plan authoring, plan
review, artifact authoring, artifact review, and corrections. The section labels
the material as owner-supplied, gives each item's category and source, and says
that the material is not an observed target fact or runtime evidence. The generic
plan field guide explains this boundary only when the block is present. An absent
or empty block leaves rendered request bytes unchanged.

### Fresh five-case authoring trial

The reusable trial caller reads the frozen input index and validates every
source, control file, and prepared input before it constructs a transport.
Render and inspect all five exact Call 1 requests without provider contact:

```bash
uv run python -m scripts.fresh_trial.run_fresh_authoring_trial \
  --run-dir /absolute/path/to/fresh-consumer-five-case-<timestamp> \
  --render-only
```

`--render-only` refuses before writing if the batch status or budget ledger
contains live reservations, dispatches, or an outage stop. See the
[fresh-trial state contract](scripts/fresh_trial/README.md) for the frozen-policy
digest keys the caller reads.

Only run the frozen batch after the input index, controls, request renderings,
and `frozen-policy.json` are finalized. The explicit `--live` flag uses the
`gemma4-oc` profile, records every call's timing, and persists the shared
40/8/4/4 request budget. A case with existing dispatch or evidence is never
retried; a transport outage stops the batch and marks remaining cases
unattempted.

```bash
uv run python -m scripts.fresh_trial.run_fresh_authoring_trial \
  --run-dir /absolute/path/to/fresh-consumer-five-case-<timestamp> \
  --live
```

### Cross-run budget guard

The caller owns spend reconciliation across separate `author` processes. Pass
both prior per-case counters on every resumed run:

```bash
uv run asago-artifact-generator author <input.json> \
  --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id> \
  --task-id <case-id> \
  --prior-author-correction-spend <count> \
  --prior-review-spend <count>
```

Use `0` for each counter on a fresh case. The consumer does not discover
mission ledgers, inspect targets, or infer prior spend. The guard seeds the
caller-supplied counters before the first dispatch and enforces the per-case
limits of four author/correction requests, four review requests, and eight
combined requests, alongside the aggregate authoring limit of 32. If a cap is
already exhausted, the run records typed `budget_exhausted` evidence and
contacts no provider. For example, a resumed case with prior author/correction
spend of `1` has only three author/correction dispatches remaining.

### Sealed A03 artifact-review continuation

The recovered A03 candidate has a separate consumer-owned Python seam. Prepare
it from the current-mission recovery sidecar and an empty package destination,
then run the returned continuation with a caller-owned transport factory:

```python
continuation = prepare_a03_recovered_continuation(
    recovery_sidecar="/absolute/path/to/recovery-candidates.json",
    package_dir="/absolute/path/to/new-package",
    task_id="A03-recovered-artifact-review",
)
result = continuation.run(transport_factory=transport_factory)
```

Preparation verifies the sidecar, accepted plan, every original-input pin,
the recovered candidate bytes, deterministic results, isolated Docker control
results, preserved 5/4 author/correction spend breach, and failed
`VAL-LIVE-003` authority before constructing a transport. The seam carries
forward 5 author/correction and 1 review request, constructs no author or
correction request, and permits one artifact-review dispatch with
`max_retries=0`. `accept` alone enters immutable package assembly. `revise`,
`blocked`, `review_unavailable`, transport failure, budget exhaustion, and
preflight defects write terminal continuation evidence and produce no package.
The same prepared continuation cannot run twice.

### Sealed O04 correction-first continuation

The O04 continuation owns one saved-artifact correction and one conditional
artifact review. Prepare it from the pinned historical failure sidecar and
offline mismatch proof, then supply a caller-owned scripted or private
transport:

```python
continuation = prepare_o04_correction_continuation(
    failure_sidecar="/absolute/path/to/O04.failure-evidence.json",
    mismatch_proof="/absolute/path/to/mismatch-evidence.json",
    package_dir="/absolute/path/to/new-package",
    task_id="O04-corrected-artifact-continuation",
)
result = continuation.run(transport_factory=transport_factory)
```

Preparation verifies the original PAT-104 and approved-education facts,
accepted plan and review, exact saved metadata/Python, runtime contract,
unchanged eleven controls and outcomes, and the completed mismatch proof
before constructing a transport. It records historical spend as 4
author/correction and 1 review, then adds a separate 1/1 correction and
conditional 1/1 review allowance. The seam constructs no plan or fresh
artifact request, permits no retry, and excludes the test-owned conformant
detector from the correction prompt. The prompt names the supported
`availability.messages`, `completeness.messages`, and `judge.verdict` paths
alongside each incompatible saved read.

The corrected candidate must pass the existing deterministic checks and all
eleven constrained Docker controls before one artifact review is dispatched.
Only an `accept` review reaches immutable package assembly. Correction,
control, review, transport, package, and preflight non-pass outcomes are
terminal and write continuation evidence without a package. This consumer
seam remains target-free; downstream owns any later MiniOcciAI execution.

### O04 artifact-refinement continuation

The refinement continuation extends the sealed O04 seam from the first
continuation's corrected candidate. Prepare it with the same historical
sidecar and mismatch proof plus the first-continuation evidence chain:

```python
continuation = prepare_o04_refinement_continuation(
    failure_sidecar="/absolute/path/to/O04.failure-evidence.json",
    mismatch_proof="/absolute/path/to/mismatch-evidence.json",
    prior_continuation_evidence="/absolute/path/to/continuation-evidence.json",
    prior_delivery_report="/absolute/path/to/o04-continuation-report.md",
    prior_preservation="/absolute/path/to/preservation-digests.json",
    package_dir="/absolute/path/to/new-package",
)
result = continuation.run(transport_factory=transport_factory)
```

Preparation pins the first-continuation report, preservation record, raw
response, semantic candidate, metadata, Python block, accepted plan, and
recorded seven-pass/three-fail/one-runtime control result. The seam seeds
factual O04 spend at 5 author/correction and 1 review, keeps the first
continuation's one-call allowances expired, and grants one shared allowance
of two corrections plus two artifact reviews. Review remains gated by every
deterministic check and all eleven unchanged controls. Each attempt persists
its raw response and evidence before validation, and each request records the
fixed `chat_template_kwargs.enable_thinking=false` transport option. A
correction or review transport failure is terminal and never retries.
Only an accepted review assembles a package; every other outcome leaves the
package path absent.

### O04 provider-recovery restart

The provider-recovery restart is a new sealed seam, not a retry of the
expired refinement. Use `prepare_o04_refinement_restart_continuation(...)` or
`run_o04_refinement_restart_continuation(...)` with a fresh task ID, evidence
path, and package path:

```python
continuation = prepare_o04_refinement_restart_continuation(
    failure_sidecar="/absolute/path/to/O04.failure-evidence.json",
    mismatch_proof="/absolute/path/to/mismatch-evidence.json",
    terminal_refinement_evidence="/absolute/path/to/continuation-evidence.json",
    terminal_delivery_report="/absolute/path/to/report.md",
    terminal_accounting="/absolute/path/to/accounting.json",
    package_dir="/absolute/path/to/fresh-package",
)
result = continuation.run(transport_factory=transport_factory)
```

Preparation verifies the terminal refinement evidence, delivery report, and
accounting pins:

- `continuation-evidence.json`:
  `61f8aa1e7e23f7f5921dc8b04f0bf69d4eaecd316a48e4d88fdff6adfa4b801e`
- `report.md`:
  `c8f50d35612059b5465d71d020c48cc3a0c35e19bb903db34fe1ca4bbaf17b37`
- `accounting.json`:
  `58ef7361edf9fcec591090850bb533291bd9183bf9dd80033e19a453ebc7cf0c`

The pinned run is a terminal transport failure with one consumed correction,
no candidate, review, package, or execution. The restart starts from candidate
`f374565b...9e4e`, seeds 6 author/correction requests and 1 review at aggregate
18, and enforces task limit 11. Its fresh shared allowance is at most two
corrections and two exact-candidate reviews; historical, first-continuation,
and prior-refinement allowances remain expired.

The durable readiness record describes one authenticated, non-generative
models-surface read: HTTP 200, configured model discoverable, and 534.6 ms.
The restart records that fact without probing again or persisting endpoint,
credential, header, body, or model-list data.

Every dispatch persists raw bytes before validation, sends
`chat_template_kwargs.enable_thinking=false` through `extra_body`, and uses
zero automatic retries. Review remains gated by deterministic checks and all
eleven unchanged controls. A transport failure consumes its role slot and
stops the restart without dispatching the other slot. Only review `accept`
assembles the immutable package. The prior 2,501-byte outage response
(`0ccdd3b4f240a05716e9dd3e8a7c28afa2b37c5d77a85fb6dc5a644f3b01a2e4`)
remains transport evidence and is never candidate input or output.

`author` writes an immutable package or durable failure evidence. `check` runs
only the supplied evidence through the packaged detector in the constrained
offline harness. Neither command starts a target, setup service, discovery
transport, or semantic judge.

### Versioned prompt roles and evidence

New v2 authoring uses five independently versioned, hashed prompt roles:

- `authoring-call1-v4` renders the plan author context while preserving the
  existing 11-field plan response.
- `authoring-plan-review-v2` reviews a fresh source-derived plan context.
- `authoring-call2-v5` renders the immutable accepted plan and preserves the
  two-block JSON-metadata-plus-Python response.
- `authoring-artifact-review-v3` reviews the exact metadata, detector bytes,
  binding/judge declarations, and offline controls.
- `authoring-correction-v5` renders only the failed stage format and all
  current findings.

Author and reviewer prompts receive the original scenario, supplied facts,
operations, schemas, provenance, and runtime capabilities. Reviewers do not
receive an author transcript or unrelated budget plumbing. The artifact author
cannot rewrite plan-owned setup, bindings, prerequisites, observations, or
judge decisions. Prompt construction fails before dispatch on overflow, secret
values, endpoint URLs, or bounded duplicate candidate forms. Dispatch evidence
records the role, version, UTF-8 prompt hash, raw response, controls, findings,
and terminal review status.

The plan field guide distinguishes `source_ref`, `selector`, binding `name`,
`consumers`, prerequisite `binding`, and literal `equals`, and its reference
forms separate evidence citations (`operation:<name>` and plain fact handles)
from setup binding sources (`setup:<operation>`), plain binding names, closed
consumers, and `{{binding_name}}` stimulus slots. Its neutral example
uses a case-permitted status operation when one is supplied; otherwise it is a
labeled generic illustration with no operation, binding, or prerequisite.
Review statuses remain visible as `accepted`, `not_requested`, `revise`,
`blocked`, or `review_unavailable`.
Provider capture keeps final-answer state, reasoning state and content, and
finish reason separate. Absent, null, empty, text, and non-text final content
remain distinct, and reasoning is never parsed as the final answer.

The producer owns scenario meaning. From the producer repository root, run the
normal producer command and then hand the resulting `scenario-handoff-v1`
input to `author`:

```bash
cd <producer-repo-root>
uv sync --locked
uv run asago-scenario-generator run \
  --use-case @use-case.txt \
  --risk-extraction risk-extraction.json \
  --qualification-facts qualification-facts.yaml \
  --taxonomy-inputs obligation-inputs.yaml \
  --output-dir output/my-system \
  --sp1-profile <profile-name> --sp2-profile <profile-name> \
  --sp3-profile <profile-name>
```

Frozen downstream execution remains producer-owned and consumes only a saved
package:

```bash
cd <producer-repo-root>
.venv/bin/python scripts/qualification/run_frozen_package.py \
  /absolute/path/to/package \
  --setup-fixture /absolute/path/to/setup.json \
  --generation-fixture /absolute/path/to/generation.json \
  --receipt build/qualification/frozen-receipt.json
```

The consumer remains target-free. Downstream owns live qualification and uses
the safe-only lifecycle with one target and the gateway at a time. Start,
verify, and stop only the component-specific safe commands:

```bash
cd <producer-repo-root>
uv run python scripts/qualification/run_recipe.py start-safe \
  --component target --domain klarna --port 8888 \
  --state-dir build/qualification/runtime/klarna
uv run python scripts/qualification/run_recipe.py start-safe \
  --component gateway --port 8321 \
  --profile <configured-profile> \
  --profiles-file config/model-profiles.yaml \
  --state-dir build/qualification/runtime/gateway
uv run python scripts/qualification/run_recipe.py verify-safe \
  --component target --domain klarna --port 8888 \
  --state-dir build/qualification/runtime/klarna
uv run python scripts/qualification/run_recipe.py verify-safe \
  --component gateway --port 8321 \
  --state-dir build/qualification/runtime/gateway
uv run python scripts/qualification/run_recipe.py stop-safe \
  --component target --domain klarna --port 8888 \
  --state-dir build/qualification/runtime/klarna
uv run python scripts/qualification/run_recipe.py stop-safe \
  --component gateway --port 8321 \
  --state-dir build/qualification/runtime/gateway
```

The safe-only boundary permits gateway port `8321` and target ports `8888`,
`8890`, and `8892`. The lifecycle records process identities and cleans up
only those captured processes. Do not use the unrestricted stack or lifecycle
commands for qualification.

For end-to-end qualification, follow the downstream repository's
`scripts/qualification/README.md`. The consumer does not start or reset a
downstream stack.

Run the final broad gate once, after the last required execution:

```bash
# Consumer repository
cd <consumer-repo-root>
./scripts/quality.sh
uv run pytest tests/ -q

# Producer repository
cd <producer-repo-root>
./scripts/quality.sh
export ASAGO_SCENARIO_GENERATOR_APS_ROOT=/absolute/path/to/Acceptance-Pipeline-Specification
./scripts/acceptance.sh
uv run pytest scripts/qualification -q
```

The producer qualification suite includes the offline fake-judge and spy
checks. They verify the package-declared judge dependency, one-request bound,
saved-result reuse, and inconclusive failure behavior without live judging.

The old `generate` command remains a read-only compatibility path for
historical scenario YAMLs. Migrate new work to `run` → `author` → `check`;
do not use `generate` as the primary workflow or add another legacy command.

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
4. **Validate** does the artifact pass all checks (`ok` / `errors`).
5. **Gate** platform coverage: `full`, `partial`, or `skip`.

## Supported platforms

| Platform | Status | Details |
|----------|--------|---------|
| [Garak](https://github.com/NVIDIA/garak) | Supported | See `src/asago_artifact_generator/garak/` |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | Planned | — |
| [PyRIT](https://github.com/Azure/PyRIT) | Planned | — |

Each platform generator lives in its own subpackage under
`src/asago_artifact_generator/` and writes artifacts under `runs/`.

## Legacy `generate` compatibility

```bash
# One scenario
uv run asago-artifact-generator generate examples/scenarios/AP-T2-01-28712e.yaml --force -v

# All scenarios in examples/scenarios/
uv run asago-artifact-generator generate -v
```

Use `author` for new target-free artifact work. Keep `generate` for
historical scenario YAML compatibility only.

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

The consumer also exposes deterministic qualification preparation through
`prepare_o04_authoring_inputs` and `prepare_scn030_authoring_inputs`. These
helpers derive target-free facts, typed operations, runtime permissions, and
`authoring-input-pins-v1` from approved local sources. O04 includes only the
approved cataract education authority. SCN-030 preserves its selected handoff
pins and adds the typed `lookup_order` eligibility result. Pass the returned
`authoring_input_pins` to saved-plan continuation validation when reusing a
plan.

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
uv run asago-artifact-generator author scenario.json \
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
The context guard estimates prompt tokens from the rendered UTF-8 system and
user bytes using the calibration described above; it does not report estimated
values as provider usage. Use `build_neutral_artifact_package` and the public
`check` command for the neutral evidence-interface example before reviewing
model-authored output.
New v2 executable prerequisites use exactly `name`, `check`, `evidence_refs`,
`binding`, and `equals`. The binding names a declared runtime binding, and
`equals` is always present as a JSON literal, including when its value is
explicitly `null`. Static judge `fact_refs` resolve from the supplied inventory
into `judge.json` facts with their exact source reference before publication.
Facts that depend on setup, live reads, or captured output remain declarations
in `bindings.json`; the consumer never substitutes a static value for them.
Historical v1 readers continue to accept their descriptive and
`source`/`expected` prerequisite forms, but those aliases are not emitted by
the v2 authoring path.
If authoring fails before a package exists, the sibling
`<package>.failure-evidence.json` sidecar is written atomically. It preserves
each exact rendered prompt, available raw response bytes, provider usage,
controls, transformations, and findings. Missing responses or usage use an
explicit `unavailable` marker. Provider endpoint and secret metadata are
redacted from the sidecar.

### Offline detector checks

Run the exact packaged `detector.py` against a JSON or YAML evidence packet:

```bash
uv run asago-artifact-generator check runs/authoring/task \
  --evidence build/evidence.json
```

The checker validates package and detector digests before and after execution.
It runs the emitted source in `python:3.12-slim` through
`/usr/local/bin/docker` with no network, read-only package and input mounts,
bounded memory, process count, output, and wall-clock time, and no inherited
credentials. Rich results remain separate from runtime failures. Reporting
maps `detected`, `not_detected`, and `inconclusive` to Garak's `1`, `0`, and
`None` only at the reporting edge.

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
