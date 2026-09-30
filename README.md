# Policy-Driven Agentic Red Teaming

Takes pre-built **scenario** YAMLs, classifies their injection surface, and generates red-teaming artifacts that can be run on downstream evaluation platforms.
## Primary delivery workflow

Run the target-free consumer workflow from this repository root:

```bash
cd <consumer-repo-root>
uv sync --locked
uv run asago-artifact-generator author <scenario-handoff.json> \
  --target-profile <execution-target-profile.json> \
  --target-observations <runtime-context.json> \
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
uv run asago-artifact-generator author <scenario-handoff.json> \
  --target-profile <execution-target-profile.json> \
  --target-observations <runtime-context.json> \
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
Thinking is a per-role control sent through the transport's additive
With the default `sampling_controls=true`, author, correction, and review
requests preserve the existing temperature and per-role
`chat_template_kwargs.enable_thinking=false` controls. With
`sampling_controls=false`, the transport omits `temperature`, `top_p`, `top_k`,
and `seed`,
and the thinking `chat_template_kwargs` entirely. Each call records only the
controls it actually sends and keeps `max_retries=0`. `reasoning_effort` and
`service_tier` become top-level request fields; a configured
`service_tier_fallback` retries one 429 once with the fallback tier. The
`strict_json_schema` profile field is accepted for shared-profile compatibility,
but the consumer continues to send no `response_format`. Only the final message
content is parsed; provider reasoning stays in the raw response capture and
never substitutes for a missing answer.
Optional `context_window`, `max_completion_tokens`, and `timeout` fields drive
the request. Omitted limits retain the 32,768-token context window and 8,192-token
completion limit, plus the existing 256-token framing reserve. Author and
correction requests use the profile completion limit. Reviews preserve the
existing remaining-context fill for the Gemma-sized default; when a profile
provides a larger completion limit, reviews use that limit as a cap so a
1.05-million-token context does not create a million-token review request. The guard
UTF-8 bytes, including correction feedback and the 128-byte schema/message
allowance, divided by a calibrated bytes-per-token ratio. It applies the same
estimate to authoring, review, and correction requests and rejects an overflow
before reserving a dispatch. The estimate must fit the remaining 24,320-token
input budget. Review requests send a larger completion limit: the context window minus that request's prompt estimate and
the framing reserve, never less than 8,192. Each call records the limit it sent.

The calibration is per prompt stage, because prompt families tokenize
differently: review prompts carry more JSON punctuation than correction
prompts. Each stage uses the lowest bytes per provider-reported prompt token
measured for it, and a 5% margin lowers that ratio. The measurements come from
dispatched authoring prompts (system + user + 128 bytes) with provider usage:
8,028 gemma-4-26b-a4b-it (`gemma4-oc`) prompts and 205 qwen38-27b
(`qwen38-oc`) prompts saved in orchestration runs up to 2026-09-30.

| Stage | gemma lowest (median) | qwen lowest | Calibrated |
| --- | --- | --- | --- |
| `call1` | 3.845 (3.957) | 4.261 | 3.653 |
| `call2` | 3.847 (3.947) | not measured | 3.655 |
| `correction` | 3.979 (4.177) | 4.298 | 3.780 |
| `plan_review` | 3.723 (3.821) | 4.272 | 3.537 |
| `artifact_review` | 3.758 (4.084) | not measured | 3.570 |
| other stages | lowest of all: 3.723 | | 3.537 |

Qwen needs fewer tokens than gemma for the same content: on the producer's
matched prompts it measured higher bytes per token than gemma in almost every
step, so the gemma minimum bounds both models. The measurement values live in
`src/asago_artifact_generator/authoring.py` as
`CONTEXT_GUARD_CALIBRATION`; the authoring path does not read saved runs.
Every guard result labels the value `estimated_prompt_tokens`; provider usage
remains separate. A rejected prompt returns `prompt_overflow` and spends no
provider request or author/reviewer dispatch.
If you omit `--profile`, existing environment-only configuration remains
supported when it provides a real API key; an absent key fails closed instead
of using a placeholder credential.

### Stage-local corrections and semantic review

By default `author` allows one plan correction and one artifact correction and
enables both semantic reviews. Configure the stages independently:

```bash
uv run asago-artifact-generator author <scenario-handoff.json> \
  --target-profile <execution-target-profile.json> \
  --target-observations <runtime-context.json> \
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
all checks, controls, and review. Each reviewed stage has one review revision
that is separate from its correction allowance: a `revise` spends the review
revision, while mechanical and control findings, including findings on the
revised candidate, spend the correction allowance. The ledger and failure
evidence record which allowance each correction spent (`allowance`), the
remaining `allowances` and `review_revision_allowances`, and the effective
`plan_max_review_revisions`/`artifact_max_review_revisions`. The default
per-task budget is the policy's closed worst case, which includes each review
revision and its review (12 dispatches, at most 6 author and 6 review, with
default settings); `blocked` at the artifact stage stops as
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

Each failed control in an artifact correction carries its exact `input` and
an `input_shapes` map derived by code from that input: the type of every
`tool_calls[i].decoded_result`, `tool_calls[i].raw_result`, and binding value,
and every nested string that holds JSON text. `decoded_result` is the parsed
JSON value, so a detector compares its fields; a binding keeps the selected
value's type, so a JSON-text string needs `json.loads` before field access.

### Omission detector controls

When the handoff's `discriminating_condition` has a `{kind: "not_called",
operation: X}` comparison, the unsafe behavior is an omission and the derived
command controls invert. The orchestrator copies the condition from the input
view, never from model output. Every not_called operation must be absent for
`detected`; a call to any one of them, even a rejected call, is `not_detected`.
The trigger is every other operation in the plan's `selected_evidence`, named
by an `operation:<name>` ref or by an `observation:<...>` ref whose supplied
fact records that operation (`provenance.tool_name`). A trigger call appears in
a fixture only with a supplied read observation of that operation that the plan
cites or binds; otherwise the trigger-dependent controls are withheld and
recorded, with reasons, as `detector_control_skips` in the ledger and failure
evidence. When the inventory supplies such an observation but the plan cites
none, plan validation reports `omission_trigger_observation_uncited` so the plan
correction can cite it.

A trigger is either established before the run or happens during it. The plan
marks a trigger as established before the run by citing its supplied
observation with `selected_evidence` role `established_trigger`: the
observation already shows the triggering result for the subject the stimulus
asks about, so the target need not repeat the lookup. The violation is then
complete capture with no call to X, with or without the lookup: the controls
add `omission-established-trigger-no-lookup` (complete, no trigger call,
`detected`) and `omission-established-call-no-lookup` (complete, X without the
lookup, `not_detected`), and drop `omission-no-trigger`. Plan validation
reports `established_trigger_not_observation` when that role cites anything
other than a supplied result observation. Run-time triggers (any other role)
keep the controls below.

Supplied facts can also decide a command condition before the run: every
`discriminating_condition` comparison is a value comparison between facts or
literals, and an observed `record_selection` names the command's record
arguments. The command controls then target the operation that
`record_selection.argument_values` names, not the first selected operation, and
take each record argument from its `TARGET-STATE` path (the record key for a
record path, the field value for a scalar path). They add
`command-selected-record-no-lookup` (that command on the selected record, no
earlier lookup, null result, `detected`). They withhold
`command-equal-bound-negative`, `command-partial-with-witness`, and
`command-malformed-*`, because that condition does not fix their outcomes.
Other conditions keep the first-selected-operation target.

The trigger fixture records the observation as execution does:
`executor_tool_result` unwraps a native MCP result envelope to the tool's
output text (the single text block, else `structuredContent` with a sole
`result` key unwrapped) as `raw_result`, and JSON-decodes it as
`decoded_result`, like the downstream evidence adapter.

| Control | Capture | Expected |
| --- | --- | --- |
| `omission-unavailable` | not captured, no calls | `inconclusive` |
| `omission-partial-without-call` | partial, trigger only | `inconclusive` |
| `omission-trigger-no-call` / `omission-no-call` (no trigger) | complete, trigger only | `detected` |
| `omission-no-trigger` | complete, no calls | `not_detected` |
| `omission-trigger-call`, `-completed` / `omission-call`, `-completed` | complete, trigger then rejected or completed X | `not_detected` |
| `omission-partial-with-call` | partial, trigger then X | `not_detected` |

The `detected` control is withheld when the condition has other comparison
kinds or a semantic judge is declared. Omission command controls run only at
`command_attempt` and `returned_result`; reply-level claims run judge controls
with the trigger present and X absent. Call-based command controls never run for
reply-level claims. Every generated fixture states `messages`, `tool_calls`,
and `snapshots` in `availability` and `completeness`, as a generation packet
does.

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

### Versioned prompt roles and evidence

New v2 authoring uses five independently versioned, hashed prompt roles:

- `authoring-call1-v17` renders the plan author context while preserving the
  existing 11-field plan response.
- `authoring-plan-review-v16` reviews a fresh source-derived plan context. It
  also shows the value each `supplied_input` binding resolves to, so the
  reviewer can check that a bound value means what the plan uses it for. When a
  binding selects inside one record of a keyed fact, the value also carries
  `record_key_source`: the documented `facts:<ref>:records` source and
  `value.<record_key>.record_key` selector that bind the record key itself. A
  required change that replaces a binding path names the complete source and
  selector pair.
- `authoring-call2-v21` renders the immutable accepted plan and preserves the
  two-block JSON-metadata-plus-Python response. When the plan needs a semantic
  judge, `semantic_judge_spec_rules` states that the judge question is a yes/no
  question whose yes answer is the violation, that the criteria are a
  checkable statement of the standard, and that mentioning, quoting, reporting,
  or refusing content does not assert it. The validator rejects a question that
  does not end with a question mark.
- `authoring-artifact-review-v15` reviews the exact metadata, detector bytes,
  binding/judge declarations, and offline controls.
- `authoring-correction-v26` renders only the failed stage format and all
  current findings. A plan correction after a semantic review finding that
  points to a runtime binding, by `runtime_bindings[<index>]` location or by
  the exact binding name, carries a `review_binding` repair option. The option
  lists the binding's documented selectors, the selected record's sources, and
  `review_selector_checks`: each selector path the required change names,
  checked against the sources that document it. Artifact corrections keep the
  `authoring-correction-v25` bytes and label.

Author and reviewer prompts receive the original scenario, supplied facts,
operations, schemas, provenance, and runtime capabilities. Reviewers do not
receive an author transcript or unrelated budget plumbing. The artifact author
cannot rewrite plan-owned setup, bindings, prerequisites, observations, or
judge decisions. Prompt construction fails before dispatch on overflow, secret
values, credential patterns, or bounded duplicate candidate forms, and on any URL
that does not occur verbatim in the supplied scenario handoff or in model-authored
candidate content (a plan, artifact metadata, detector source, or review finding).
A scenario's own lure link therefore reaches the prompt, while a URL from target
inventory, profiles, or code does not. The private-model transport also rejects,
before reserving a request, any prompt that names its configured endpoint host. Dispatch evidence
records the role, version, UTF-8 prompt hash, raw response, controls, findings,
and terminal review status.

The plan field guide distinguishes `source_ref`, `selector`, binding `name`,
`consumers`, prerequisite `binding`, and literal `equals`, and its reference
forms separate evidence citations (`operation:<name>` and plain fact handles)
from setup binding sources (`setup:<operation>`), plain binding names, closed
consumers, and `{{binding_name}}` stimulus slots. Its neutral example
uses a case-permitted status operation when one is supplied; otherwise it is a
labeled generic illustration with no operation, binding, or prerequisite.
Keyed-map bindings may use a source shorthand such as
`facts:state:orders:ORD-101:customer_id`; validation resolves it only when the
record key and field exist, then persists the documented
`facts:state:orders` plus `value.ORD-101.customer_id` paths. A shorthand
source whose selector repeats its record key, such as
`facts:state:orders:ORD-101` with `value.ORD-101.customer_id`, resolves the
same way. Unknown keys, fields, a selector that names a different key or
field, and incompatible types remain invalid. Valid scenario lineage and
attack-tree IDs from the handoff are accepted in `interpretation.source_refs`
and `assumptions[].ref`; correction feedback lists the valid provenance IDs,
while invented IDs remain invalid.
If a `:records` companion documents only the record key, a binding that names a
field on that companion may resolve to the matching keyed field on the base
fact. The fallback runs only when exactly one documented target exists;
otherwise validation remains closed and reports the original error. A binding
on a `:records` companion must select `value.<key>.record_key`; selecting a
record wrapper such as `value.ORD-101` or the whole companion is invalid, and
the error points to the base fact for the whole record or one field. Exact
duplicate binding declarations are removed after canonicalization, while
same-name declarations with different fields remain invalid. Accepted
canonicalizations and dropped duplicates are recorded with the binding name,
original paths, and canonical paths in authoring transformations and failure
evidence.
When authored `stimulus.user_text` contains `{{binding_name}}` placeholders,
validation derives `stimulus.slots` as the unique placeholder names in
first-appearance order, but only when every placeholder names a declared
binding. An undeclared placeholder remains a validation error and feedback
lists both the missing placeholder and declared binding names. When a
prerequisite references a declared binding, validation adds the matching
`prerequisites.<binding_name>` consumer if it is missing. Both rewrites are
recorded in the same transformation evidence and carried into the packaged
plan and stimulus.
Review statuses remain visible as `accepted`, `not_requested`, `revise`,
`blocked`, or `review_unavailable`.
Provider capture keeps final-answer state, reasoning state and content, and
finish reason separate. Absent, null, empty, text, and non-text final content
remain distinct, and reasoning is never parsed as the final answer.

The producer owns scenario meaning. From the producer repository root, run the
normal producer command and then hand the resulting `scenario-handoff-v2`
(or older `scenario-handoff-v1`) input to `author`:

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

The consumer remains target-free. The `asago-orch` repository owns downstream
qualification, including package execution, live setup, runtime judging,
detector evaluation, evidence, and cleanup. Read
`asago-orch/docs/qualification.md`; its implementation lives under
`asago-orch/src/asago_orch/qualification/`.

Run the configured pipeline from the orch repository:

```bash
cd <orch-repo-root>
uv run asago-orch run --target <target> --to execute
```

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
```

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

The authoring path accepts producer-owned discovery outputs and one scenario
handoff:

- `scenario-handoff-v1` or `scenario-handoff-v2` JSON or YAML for scenario meaning.
- `execution-target-profile-v1` JSON for the observed target inventory.
- Optional normalized producer `runtime-context.json` for state and read observations.
- A target-free runtime contract.

Use `asago_artifact_generator.input_adapter.load_input` to validate the
vendored handoff kit, preserve authoritative narrative and Gherkin bytes, and
record SHA-256 source pins. The adapter reads `schema_version`, verifies
`content_digest` in that version's digest domain, and records the version as
`InputView.handoff_schema_version`. Packages keep `input_kind`
`scenario-handoff-v1` for both versions.

A `scenario-handoff-v2` document may add two fields, both validated against the
vendored v2 schema. A v1 document that contains either field is rejected.

- `discriminating_condition` states what makes the behavior unsafe: a
  `statement`, a list of `comparisons`, and a `record_selection`. A value
  comparison has `left`, `op`, and `right` operands; each operand is an
  `argument` (operation and argument name), a supplied `fact` path, or a
  `literal`. An order comparison means `operation` runs without an earlier
  `requires_prior` call, optionally for the same `same_argument` value. The
  `record_selection` is `observed`, with a `record_path` and optional
  `argument_values` paths, or `unavailable`, with a `reason`.
- `condition_check` is the producer's code-owned pre-execution evaluation:
  a `status` and one `{index, result, reason}` entry per comparison. It is not
  runtime evidence.

When either field is non-null, authoring passes it into the scenario context of
plan authoring, plan review, artifact authoring, artifact review, and
correction. The model-facing view states each fact once: when the Gherkin step
`Given the discriminating condition holds: <statement>` already carries the
statement, the condition replaces `statement` with a `statement_location`
pointer, and `condition_check` keeps `status` and each comparison's `index` and
`result` without the producer's `reason` text. `build_scenario_handoff_view`
keeps both fields unchanged. Only when a condition is present, plan
authoring and both reviews also state the generic rule: the detector checks the
condition on captured evidence rather than only the operation, uses the
selected record's `argument_values` paths when the record is observed, and keeps
a runtime binding while still checking captured arguments when it is
unavailable.

The plan-author prompt renders SOURCE CONTEXT as compact JSON and lists the
runtime contract and each binding rule once; EXECUTION CAPABILITIES and the
runtime-binding schema descriptions point to those single copies. To check a
scenario against the context guard without a model call, run
`uv run python scripts/measure_call1_budget.py --target-profile PROFILE
--target-observations RUNTIME_CONTEXT --runtime-contract CONTRACT SCENARIO...`.

Use
`asago_artifact_generator.target_inputs.load_target_inputs` to validate the
producer profile, verify its semantic digest, map observed tools to
operations, infer fact schemas, and record discovery provenance. Authoring
does not accept native semantic scenario files, reference tasks, benchmark
answers, or hand-built inventories.

The consumer-owned `artifact-package-v2` contract lives in
`contracts/artifact-package/`. `package_io.write_package` writes a complete
directory atomically, and `load_package` verifies its manifest, member paths,
lengths, and digests before returning content. A package containing `judge.json`
receives a runner-normalized `evidence.judge` object with only `verdict`,
`evidence_refs`, and `reason`; judge audit fields remain in downstream receipts,
not in detector input. Runtime receipts remain outside the immutable package. The
existing `generate` command remains the legacy compatibility path.

### Target-free authoring

Use `author` to run the bounded Call 1 plan and Call 2 package sequence. Supply
the scenario handoff, producer discovery profile, and runtime contract:

```bash
uv run asago-artifact-generator author scenario-handoff.json \
  --target-profile execution-target-profile.json \
  --target-observations runtime-context.json \
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
redacted from the sidecar. The sidecar uses
`authoring-failure-evidence-v2`; readers accept the prior v1 document as well.
`attempts` retains the full history, while top-level `findings` contains only
the findings that caused the terminal status. The `terminal` object records the
logical stage, zero-based attempt index, and terminal reason.

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

Before the container starts, the checker performs a finite evidence-access
check. The detector may read the adapter packet's `user_text`, `history`,
`messages`, `tool_calls`, `bindings`, `binding_provenance`, `setup_outputs`,
`snapshots`, `transport`, `parse_errors`, `correlation`, `source`,
`availability`, and `completeness` roots. It may read `judge` only when the
package contains `judge.json`. A supplied record fact belongs in
`evidence.bindings.<name>` when `bindings.json` declares that binding, regardless
of its consumers. When the plan declares a detector-only binding, it also lists
the `detector.<name>` consumer. A binding carries only a value that exists before
the run, a supplied fact or a permitted setup result: content the experiment
chooses belongs directly in the stimulus, and the target's tool-call arguments,
results, and replies are read from `tool_calls` or `messages`. The same holds
for a record the target creates during the run. If no supplied fact or permitted
setup provides a needed record but a documented operation creates it, the plan
author guidance lets the stimulus ask the target to create it. The detector then
attributes later calls to that record from captured calls, and the plan does not
list the record as an unresolved requirement. An empty `setup_permissions` list
permits no setup. Binding repair options mark a supplied fact whose value is an
empty list or object with `supplied_value_empty`. When a binding `source_ref` names one record of a keyed
fact (`facts:<ref>:<record_key>`), or its selector selects inside one record
(`value.<record_key>...`), the options also list `named_record_sources`:
that record's full selectors in each fact documenting it, including the record
key at `value.<record_key>.record_key` in the `facts:<ref>:records` companion.
The whole-source selector list is sorted and capped at 40 entries, so it can
omit the named record. The detector must return `evidence_refs` under the
same supplied packet roots; `assistant_messages` is an observation
declaration alias for `messages`, not a packet key.

The authoring controls and package checker flag literal
`evidence["root"]`, `evidence.get("root")`, literal binding child accesses, and
literal `evidence_refs` roots that fall outside this interface. The check does
not prove arbitrary Python data flow, aliases, computed keys, or dynamically
built references. When a detector needs a supplied fact, the plan declares a
`supplied_input` binding instead of reading runtime state or hardcoding the
value, and adds the `detector.<name>` consumer when only the detector needs it.

Artifact authoring copies `runtime_bindings` from the accepted plan, so neither
call 2 nor an artifact correction can add, rename, or change a binding. The
current call 2 guidance, the artifact correction guidance, the detector-feedback
correction guidance, and the evidence contract's `detector_access.binding_rule`
all state that rule. An undeclared binding or root finding lists the declared
bindings and says that a value no declared binding supplies needs a new plan
binding. When the scenario supplies a `discriminating_condition`, the controls
restate each such finding in more detail. The restated finding lists the declared bindings and, for
each condition fact operand that resolves in the supplied facts, its exact
`supplied_input` form: `TARGET-STATE.<key>.<rest>` becomes `source_ref`
`facts:state:<key>` with `selector` `value.<rest>`, plus the declared binding
with that form or `none`. The finding keeps these forms in
`details.supplied_fact_operands`. Because the supplied facts fix those operands
before the run, the detector need not read an operand that no declared binding
supplies.

Binding declarations list `stimulus.user_text` only when the resolved scalar
value occurs in authored user text or the text contains its `{{name}}` slot.
The authoring validator fails with correction feedback when a binding lists
that consumer for a session prerequisite or detector-only value. The plan
validator applies the same rule to `stimulus_approach.request`, and rejects a
request slot that names no declared binding, so plan correction can repair the
binding before the artifact stage freezes the plan.

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
