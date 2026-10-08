# Policy-Driven Agentic Red Teaming

Turns producer scenario handoffs into target-free, testable artifact packages:
a stimulus, setup declarations, runtime bindings, and the scoring input that
downstream Garak detection uses: the producer's tool-call condition for a
`command_attempt` claim, or a semantic judge specification for a `reply` claim.
Packages contain no detector code.

## Primary delivery workflow

Run the target-free consumer workflow from this repository root:

```bash
cd <consumer-repo-root>
uv sync --locked
uv run asago-artifact-generator generate <scenario-handoff.json> \
  --target-profile <execution-target-profile.json> \
  --target-observations <runtime-context.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id> \
  --profile <profile-name> \
  --profiles-file /absolute/path/to/asago-scenario-generator/config/model-profiles.yaml
```

`generate` uses the versioned v2 authoring wire. Call 1 returns one closed plan
root as either one bare JSON object or exactly one lowercase `json` fenced
object, with optional surrounding whitespace. Untagged, uppercase, or other
fences, multiple objects or blocks, prose, trailing content, and malformed JSON
are rejected. The raw Call 1 bytes remain preserved, and a removed outer fence
is recorded before plan validation. Call 2 uses the same framing rules: one
JSON object with exactly `stimulus`, `semantic_judge_spec`, `examples`, and
`explanation`. It returns no detector code.
For a `command_attempt` claim, the `unsafe` and `safe` examples each carry a
`capture`: `tool_calls` (each with an `operation` from the supplied inventory
and its decoded `arguments`, in call order) and `complete`. The `inconclusive`
example and every reply example stay prose. The Call 2 prompt says so. When the
plan claims `reply` and an `unsafe` or `safe` example carries a capture, the
correction gets one `unexpected_field` finding at `examples.<label>.capture`
that says a reply claim carries no capture, ahead of any other finding; shape
findings about that same capture are dropped, because the author removes the
field. `examples.json` holds the examples
as written; `artifact-package-v4` constrains only its manifest entry.
Each v2 prompt carries the selected case meaning once under `case_meaning`;
the input projection retains scenario/reference identities and narrative/Gherkin
SHA-256 digests without repeating those texts.
The accepted Call 1 plan owns setup, bindings, prerequisites, evidence,
assumptions, observation requirements, and judge decisions; Call 2 cannot
resubmit those fields.

### Private authoring profiles

For live private authoring, pass the approved named profile and the producer
profile file directly to `generate`:

```bash
uv run asago-artifact-generator generate <scenario-handoff.json> \
  --target-profile <execution-target-profile.json> \
  --target-observations <runtime-context.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id> \
  --profile <profile-name> \
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
`service_tier_fallback` retries one 429 once with the fallback tier. After an
HTTP 5xx or a connection error that is not a timeout, the transport retries the
same request once after a fixed delay of at most 2 seconds; it never retries a
timeout, a 4xx (429 included), or an invalid response. The retry reserves one
more dispatch from the budget, and the dispatch's ledger and failure-evidence
record gain `transport_retries`: `[{"attempt": 2, "retry_of": {"type": <error
class>, "status_code": <HTTP status or null>}}]`. Dispatches without a retry
carry no such key. The
`strict_json_schema` profile field is accepted for shared-profile compatibility,
but the consumer continues to send no `response_format`. Only the final message
content is parsed; provider reasoning stays in the raw response capture and
never substitutes for a missing answer.
Optional `context_window`, `max_completion_tokens`, and `timeout` fields drive
the request. Omitted limits retain the 32,768-token context window and 8,192-token
completion limit, plus the existing 256-token framing reserve. Author and
correction requests use the profile completion limit. Reviews preserve the
existing remaining-context fill for the default completion limit; when a profile
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
8,028 prompts from one open-weight model and 205 prompts from a second,
saved in orchestration runs up to 2026-09-30.

| Stage | Lowest, model A (median) | Lowest, model B | Calibrated |
| --- | --- | --- | --- |
| `call1` | 3.845 (3.957) | 4.261 | 3.653 |
| `call2` | 3.847 (3.947) | not measured | 3.655 |
| `correction` | 3.979 (4.177) | 4.298 | 3.780 |
| `plan_review` | 3.723 (3.821) | 4.272 | 3.537 |
| `artifact_review` | 3.758 (4.084) | not measured | 3.570 |
| other stages | lowest of all: 3.723 | | 3.537 |

Model B needs fewer tokens than model A for the same content: on the producer's
matched prompts it measured higher bytes per token in almost every step, so
the model A minimum bounds both. A model whose tokenizer needs more tokens per
byte than both measured models can exceed the estimate; the provider then
rejects the request. The measurement values live in
`src/asago_artifact_generator/authoring/context_budget.py` as
`_CONTEXT_GUARD_CALIBRATION_SOURCES`; the authoring path does not read saved runs.
Every guard result labels the value `estimated_prompt_tokens`; provider usage
remains separate. A rejected prompt returns `prompt_overflow` and spends no
provider request or author/reviewer dispatch.
`generate` requires `--profile`; it reads no endpoint, model, or API key from
the environment.

### Stage-local corrections and semantic review

By default `generate` allows one plan correction and one artifact correction and
enables both semantic reviews. Configure the stages independently:

```bash
uv run asago-artifact-generator generate <scenario-handoff.json> \
  --target-profile <execution-target-profile.json> \
  --target-observations <runtime-context.json> \
  --runtime-contract <runtime-contract.json> \
  --profile <profile-name> \
  --profiles-file <profiles.yaml> \
  --plan-max-corrections 1 \
  --artifact-max-corrections 1 \
  --review-plan/--no-review-plan \
  --review-artifact/--no-review-artifact \
  --review-model-profile <already-authorized-profile>
```

Correction allowances are nonnegative integers; booleans, negatives, and
non-integers are rejected before dispatch and values are never clamped. Each
allowance defaults to 1. Stage allowances are independent: a plan correction
never consumes artifact allowance, and zero disables only its own stage. Pass
`--plan-max-corrections 0 --artifact-max-corrections 0` to disable corrections
in both stages.

With reviews enabled, each stage runs deterministic checks before its semantic
review of a mechanically valid candidate. A reviewer returns one JSON object
with `decision` (`accept`, `revise`, or `blocked`), a nonblank `summary`, and
findings with exactly `location`, `problem`, `basis`, and `required_change`;
the framing rules match Call 1. An `accept` that omits `findings` entirely
counts as an empty findings list, and the run records the
`review_findings_omitted_defaulted_empty` transformation (with the review
stage) in its transformations and on that review's ledger entry; `findings`
set to `null` or any non-list value, and an omitted `findings` on `revise` or
`blocked`, stay invalid. `revise` feeds a stage correction that repeats
all checks and the review. Each reviewed stage has one review revision
that is separate from its correction allowance: a `revise` spends the review
revision, while mechanical findings, including findings on the
revised candidate, spend the correction allowance. The ledger and failure
evidence record which allowance each correction spent (`allowance`), the
remaining `allowances` and `review_revision_allowances`, and the effective
`plan_max_review_revisions`/`artifact_max_review_revisions`. The default
per-task budget is the policy's closed worst case, which includes each review
revision and its review (12 dispatches, at most 6 author and 6 review, with
default settings); `blocked` at the artifact stage stops as
`needs_plan_revision` without recursing into plan authoring. Malformed or
contradictory reviewer responses and reviewer transport failures produce
`review_unavailable`, never a silent pass, and a transport failure stops the run
after the one recorded transport retry (see the profile section). An explicit caller, per-task, or aggregate budget cap
stops the run before the next dispatch.

### Omission plans

When the handoff's `discriminating_condition` has a `{kind: "not_called",
operation: X}` comparison, the unsafe behavior is an omission. The trigger is
every other operation in the plan's `selected_evidence`, named by an
`operation:<name>` ref or by an `observation:<...>` ref whose supplied fact
records that operation (`provenance.tool_name`). When the inventory supplies
such an observation but the plan cites none, plan validation reports
`omission_trigger_observation_uncited` so the plan correction can cite it. The
plan marks a trigger as established before the run by citing its supplied
observation with `selected_evidence` role `established_trigger`; plan
validation reports `established_trigger_not_observation` when that role cites
anything other than a supplied result observation.

### Versioned prompt roles and evidence

New v2 authoring uses five independently versioned, hashed prompt roles:

- `authoring-call1-v21` renders the plan author context while preserving the
  existing 11-field plan response.
- `authoring-plan-review-v20` reviews a fresh source-derived plan context. It
  also shows the value each `supplied_input` binding resolves to, so the
  reviewer can check that a bound value means what the plan uses it for. When a
  binding selects inside one record of a keyed fact, the value also carries
  `record_key_source`: the documented `facts:<ref>:records` source and
  `value.<record_key>.record_key` selector that bind the record key itself. A
  required change that replaces a binding path names the complete source and
  selector pair.
- `authoring-call2-v25` renders the immutable accepted plan and the runtime
  contract, and asks for one JSON object without detector code. When the plan
  needs a semantic judge, `semantic_judge_spec_rules` states that the judge question is a yes/no
  question whose yes answer is the violation, that the criteria are a
  checkable statement of the standard, and that mentioning, quoting, reporting,
  or refusing content does not assert it. The validator rejects a question that
  does not end with a question mark. A plan that claims `reply` must return a
  `semantic_judge_spec` object; a missing one gets a
  `semantic_judge_spec_required` correction.
- `authoring-artifact-review-v20` reviews the exact artifact object and the
  binding and judge declarations.
- `authoring-correction-v34` renders only the failed stage format and all
  current findings. A plan correction after a semantic review finding that
  points to a runtime binding, by `runtime_bindings[<index>]` location or by
  the exact binding name, carries a `review_binding` repair option. The option
  lists the binding's documented selectors, the selected record's sources, and
  `review_selector_checks`: each selector path the required change names,
  checked against the sources that document it. Artifact corrections use
  `authoring-correction-v33`.

Author and reviewer prompts receive the original scenario, supplied facts,
operations, schemas, provenance, and runtime capabilities. Reviewers do not
receive an author transcript or unrelated budget plumbing. The artifact author
cannot rewrite plan-owned setup, bindings, prerequisites, observations, or
judge decisions. Prompt construction fails before dispatch on overflow, secret
values, credential patterns, or bounded duplicate candidate forms, and on any URL
that does not occur verbatim in the supplied scenario handoff or in model-authored
candidate content (a plan, an artifact, or a review finding).
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
`facts:state:loans:LN-101:borrower_id`; validation resolves it only when the
record key and field exist, then persists the documented
`facts:state:loans` plus `value.LN-101.borrower_id` paths. A shorthand
source whose selector repeats its record key, such as
`facts:state:loans:LN-101` with `value.LN-101.borrower_id`, resolves the
same way. Unknown keys, fields, a selector that names a different key or
field, and incompatible types remain invalid. A selector follows object
properties only: one that steps into an array with `items`, such as
`value.content.items.text`, gets a `selector_through_array` finding that names
the array path, and the repair enumeration of documented selectors omits array
items. The `artifact-package` contract defines no array traversal, and the
orchestrator's selector resolver reads dictionary keys only. Valid scenario lineage and
attack-tree IDs from the handoff are accepted in `interpretation.source_refs`
and `assumptions[].ref`; correction feedback lists the valid provenance IDs,
while invented IDs remain invalid.
If a `:records` companion documents only the record key, a binding that names a
field on that companion may resolve to the matching keyed field on the base
fact. The fallback runs only when exactly one documented target exists;
otherwise validation remains closed and reports the original error. The
reverse holds for the key itself: `value.LN-101.record_key` written on the
base fact `facts:state:loans` resolves to `facts:state:loans:records` when
that companion documents the key and the base record has no `record_key`
field; an unknown key or a placeholder such as `value.<record_key>.record_key`
remains invalid. A binding
on a `:records` companion must select `value.<key>.record_key`; selecting a
record wrapper such as `value.LN-101` or the whole companion is invalid, and
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
normal producer command and then hand the resulting `scenario-handoff-v4`
input to `generate`:

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
Garak detection, evidence, and cleanup. Read
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

## Setup

Asago Artifact Generator requires Python 3.11 or newer. The lock file is the
authoritative development environment.

```bash
uv sync --locked
```

The installed command is `asago-artifact-generator` and the Python package is
`asago_artifact_generator`. Live authoring reads its endpoint, model, and
credential from the named profile passed to `generate --profile`.

## Source-pinned artifact foundation

The authoring path accepts producer-owned discovery outputs and one scenario
handoff:

- `scenario-handoff-v4` JSON or YAML for scenario meaning.
- `execution-target-profile-v1` JSON for the observed target inventory.
- Optional `runtime-context.json` (`runtime-context-v1`, written by orch's discover stage) for state and read observations.
- A target-free runtime contract.

Use `asago_artifact_generator.input_adapter.load_input` to validate the
vendored handoff kit, preserve authoritative narrative and Gherkin bytes, and
record SHA-256 source pins. The adapter verifies `content_digest` in the
`scenario-handoff-v4` digest domain, and packages record `input_kind`
`scenario-handoff-v4`. Authoring rejects every other document, including
`scenario-handoff-v1` to `v3`, with "authoring source must be a producer
scenario-handoff-v4 document; found <version>"; their kits stay vendored and
frozen.

Before the digest check, the adapter applies the mirrored v4 `schema.json`, the
producer's ownership rules in the mirrored `ownership-rules.json`, and the
rules the schema cannot state: non-blank text, the observation-contract format,
and cross-field pairings. A rejection raises `HandoffSchemaInvalid`. Its `codes`
are the producer's codes as the kit's `expected-violations.json` records them:
ownership codes first, then `schema_violation:<top-level field>`. Its message is
`handoff schema invalid: <codes> (<path>: <reason>)` for the first break. A
break confined to `attack_shape` raises the subclass `ShapeVersionMalformed`.

A v4 handoff carries `attack_shape`: the channel, the number of user turns (1
to 4) and each turn's purpose, never attack text, or null for a functional
scenario. `InputView.attack_shape` returns it. The consumer authors only
`direct` shapes. A `direct`
shape with more than one turn is authored as sequential user turns: the plan
states one intent per turn, Call 2 writes the earlier turns as `history` and the
last as `user_text`, and code checks `1 + len(history) == turn_count`. The
package is `artifact-package-v4`, and its `stimulus.json` states `mode`
(`single` or `sequential`) and `turn_count`; a functional handoff's stimulus
stays as authored. Before any model request, a shape the consumer cannot
author stops with a typed finding: `shape_channel_unsupported` (the runtime
contract lists no matching `delivery`, such as `planted_item` for `indirect`),
`shape_forged_transcript_unsupported`, or `shape_turn_count_unsupported`
(more turns than `limits.max_planned_turns`, default 4). A v4 handoff whose
`attack_shape` breaks the vendored schema or its cross-field rules is refused
at load with `shape_version_malformed`.

A handoff carries two optional condition fields and two tool-call fields, all
validated against the vendored v4 schema:

- `discriminating_condition` states what makes the behavior unsafe: a
  `statement`, a list of `comparisons`, and a `record_selection`. A value
  comparison has `left`, `op`, and `right` operands; each operand is an
  `argument` (operation and argument name), a supplied `fact` path, or a
  `literal`. Comparisons combine with AND. The ops `in`, `not_in`, `eq`, and
  `ne` match exactly, element-wise when the left side is a list. A
  `not_called` comparison (`operation`, optional `where`) holds when no call to
  `operation` matches every `where` item; a matching call at any position makes
  it false. An `order` comparison holds when the call to `operation` has no
  earlier call to `requires_prior`; with `same_argument`, only an earlier call
  with the same value for that argument counts. The authoritative semantics are
  in `contracts/tool-call-condition/tool-call-condition-v1/README.md`. The
  `record_selection` is `observed`, with a `record_path` and optional
  `argument_values` paths, or `unavailable`, with a `reason`.
- `condition_check` is the producer's code-owned pre-execution evaluation:
  a `status` and one `{index, result, reason}` entry per comparison. It is not
  runtime evidence.
- `tool_call_condition_status` states whether the producer bound the condition
  to tool-call arguments: `status` `bound` or `not_executable`, a `reason`, and
  an optional `detail`.
- `tool_call_condition` is the bound condition over tool-call arguments and
  literals; it is present exactly when the status is `bound`.

`InputView.tool_call_condition_status` and `InputView.tool_call_condition`
expose the tool-call fields to the orchestrator. Model prompts never receive
them.

When either condition field is non-null, authoring passes it into the scenario context of
plan authoring, plan review, artifact authoring, artifact review, and
correction. The model-facing view states each fact once: when the Gherkin step
`Given the discriminating condition holds: <statement>` already carries the
statement, the condition replaces `statement` with a `statement_location`
pointer, and `condition_check` keeps `status` and each comparison's `index` and
`result` without the producer's `reason` text. `build_scenario_handoff_view`
keeps both fields unchanged. Only when a condition is present, plan
authoring and both reviews also state the generic rule: the violation checks the
condition on captured evidence rather than only the operation, a fact operand
holds before the run because the producer resolves it to its supplied value (a
`command_attempt` claim needs no runtime binding for it), the plan uses the
selected record's `argument_values` paths when the record is observed, and it
keeps a runtime binding while still checking captured arguments when the record
is unavailable.

The plan-author prompt renders SOURCE CONTEXT as compact JSON and lists the
runtime contract and each binding rule once; EXECUTION CAPABILITIES and the
runtime-binding schema descriptions point to those single copies. To check a
scenario against the context guard without a model call, run
`uv run python scripts/measure_call1_budget.py --target-profile PROFILE
--target-observations RUNTIME_CONTEXT --runtime-contract CONTRACT SCENARIO...`.

Plan validation accepts `observation_claim.claim_level` values listed in
`runtime_contract.observation.claim_levels`. When the runtime contract omits
that list, only `command_attempt` and `reply` are accepted, because downstream
execution rejects other levels even when it captures decoded results. A plan
with another level gets an `unsupported_claim_level` correction that names the
supported levels.

A `command_attempt` package needs a bound tool-call condition. When the
handoff's `safe_observable_outcome.claim_level` is `command_attempt` and its
`tool_call_condition_status.status` is not `bound`, `generate` makes no model
call: it writes the failure evidence with status `failed` and one
`tool_call_condition_missing` finding whose `details` carry the handoff's
`status`, `reason`, and `detail`, then exits 1. The same finding stops a run
whose accepted plan claims `command_attempt` for such a handoff, before
artifact authoring.

A bound condition must also be decidable. For a `command_attempt` claim,
`generate` compares the declared operand types of each `value` comparison (a
literal's JSON type, an argument's type in the operation's input schema) and
stops the same way, with a `condition_operand_type_mismatch` finding that names
the comparison, when no capture can satisfy it or decide it: `eq` or `ne` of
disjoint types, `in` or `not_in` with a right side that is not a list or a list
whose items cannot equal the left side, and `gt`, `ge`, `lt`, or `le` with an
operand that is not numeric. An operand without a declared type passes. The
check reads types only; what a value means stays with the model.

After Call 2, a `command_attempt` package runs the condition on its own
captures: the `unsafe` capture must be detected and the `safe` capture must not
be (`oracle_quiet_on_unsafe_example`, `oracle_fires_on_safe_example`,
`oracle_inconclusive_on_safe_example`). A correction cannot change the
producer's condition or the fixed plan, so a disagreement ends the package at
once with no correction call: status `failed`, terminal reason
`oracle_condition_defective`. The failure evidence keeps the per-example
findings and ends with one `oracle_condition_defective` finding whose `details`
carry the evaluated `condition` and, for each captured example, the `outcome`,
the evaluator's `reason`, the `matched_calls`, and the outcome the example
`required`.

Plan and artifact checks reject a stimulus that carries a bracketed
placeholder, such as `[Obscene/Harmful Content Placeholder]`, in
`stimulus_approach.request`, `stimulus_approach.history`, `stimulus.user_text`,
or a history message. The finding is `placeholder_stimulus`; the correction
asks for the message itself. Only a bracketed span that contains the word
`placeholder` matches, so plain words and `{{binding}}` slots pass.

Use
`asago_artifact_generator.target_inputs.load_target_inputs` to validate the
producer profile, verify its semantic digest, map observed tools to
operations, infer fact schemas, and record discovery provenance. When you
supply a `runtime-context.json`, the loader checks it against the mirrored
orch-owned `contracts/runtime-context/` schema (`runtime-context-v1`) and
reports the first fault as `at <location>: <keyword>` without repeating the
value. A missing-key fault lists the missing keys, and an unknown-key fault
lists the unknown keys (sorted, at most 10, each cut to 64 characters). Three checks stay in code because they compare documents: each read's
`profile_digest` equals the file's `target_profile_digest`, each read's
`tool_name` is a profile tool, and that digest equals the profile's
`semantic_digest`. Authoring does not accept native semantic scenario files, reference tasks, benchmark
answers, or hand-built inventories.

The consumer-owned artifact-package contract lives in
`contracts/artifact-package/`: `artifact-package-v4/` for the packages the
writer writes and `artifact-package-v3/` for earlier packages. The v2 schema
file stays for reference but is no longer in `CONTRACT.lock`. `package_io.write_package`
writes a complete directory atomically, and `load_package` verifies its
manifest, member paths, lengths, and digests before returning content. A
manifest has no `detector_interface`, and a package has no `detector.py`.
Each package carries the scoring input for its plan's claim level:

- `tool_call_condition.json` holds the handoff's `tool_call_condition` exactly
  when its status is `bound`, serialized as sorted, two-space-indented UTF-8
  JSON with a trailing newline. Only a `command_attempt` package carries it, and
  that package requires it.
- `judge.json` holds the semantic judge specification and its resolved facts.
  Only a `reply` package carries it, and that package requires it.

The writer writes only `artifact-package-v4`; the loader reads both v4 and
the `artifact-package-v3` packages written before.

Each version directory also holds the contract's cases: `valid/` packages every
reader must load, `invalid/` packages every reader must reject, and
`expected-violations.json`, which names each invalid case's stable code.
`metadata-policy.json` lists the manifest metadata the closed secret policy
accepts and rejects. Each case is one JSON file holding the manifest and the
package files as text. `scripts/gen_artifact_package_cases.py` writes the cases
and their lock entries; orch mirrors them and runs the same cases against its
own loader, mapping each code to its own error wording.

Runtime receipts remain outside the immutable package.

### Target-free authoring

Use `generate` to run the bounded Call 1 plan and Call 2 package sequence. Supply
the scenario handoff, producer discovery profile, and runtime contract:

```bash
uv run asago-artifact-generator generate scenario-handoff.json \
  --target-profile execution-target-profile.json \
  --target-observations runtime-context.json \
  --runtime-contract runtime-contract.json \
  --output-dir runs/authoring \
  --profile <profile-name> \
  --profiles-file <profiles.yaml>
```

Authoring uses only the configured private model client. It sets provider
retries to zero (the transport's one recorded retry after a transport error
is its own), records prompts, raw and decoded responses, usage, controls,
and the stage-local correction allowances, and never contacts a target, setup,
discovery, or runtime-judge transport. An essential unresolved requirement
produces a retained `*.blocked.json` plan and no package. The package contains
the user-only stimulus, exact runtime-binding declarations, observations, the
tool-call condition or judge specification, explanation, examples, and
digest-bound evidence.
The context guard estimates prompt tokens from the rendered UTF-8 system and
user bytes using the calibration described above; it does not report estimated
values as provider usage. Use the test helper `build_neutral_artifact_package`
(`tests/support.py`) to see a neutral package before reviewing model-authored
output.
New v2 executable prerequisites use exactly `name`, `check`, `evidence_refs`,
`binding`, and `equals`. The binding names a declared runtime binding, and
`equals` is always present as a JSON literal, including when its value is
explicitly `null`. When the binding is a `supplied_input` that resolves from the
inventory, `equals` must equal the whole resolved value, because execute fails
a prerequisite on any difference: a partial object or a JSON string that holds
the status never passes. The plan check reports `prerequisite_value_mismatch`
at `prerequisites[i].equals` and names the resolved value (cut at 200
characters). It skips `setup_output` and unresolvable bindings. Static judge `fact_refs` resolve from the supplied inventory
into `judge.json` facts with their exact source reference before publication.
Facts that depend on setup, live reads, or captured output remain declarations
in `bindings.json`; the consumer never substitutes a static value for them.
The plan validator rejects the older `source` and `expected` prerequisite
fields as unexpected, and reports `binding` and `equals` as missing when they
are absent. `load_package` does not parse `prerequisites.json`; it verifies
the manifest, the member set, and each member's digest and length.
If authoring fails before a package exists, the sibling
`<package>.failure-evidence.json` sidecar is written atomically. It preserves
each exact rendered prompt, available raw response bytes, provider usage,
controls, transformations, and findings. Missing responses or usage use an
explicit `unavailable` marker. Provider endpoint and secret metadata are
redacted from the sidecar. The sidecar uses
`authoring-failure-evidence-v2`.
`attempts` retains the full history, while top-level `findings` contains only
the findings that caused the terminal status. Each finding record names the
logical stage that raised it (`plan` or `artifact`) in `stage`. The `terminal`
object records the logical stage, zero-based attempt index, and terminal reason.
The sidecar first reads `status: in_progress` with `terminal: null`, and the
run replaces that when it ends. An exception the state machine does not handle
ends the sidecar as `failed` with one `authoring_crashed` finding: its detail
is the exception type and the redacted message cut at 200 characters, and its
`stage` is the stage that was running. The original exception then propagates.
A sidecar that still reads `in_progress` after the process exited means the
run stopped without an `Exception` (a kill or an interrupt) or the failed
sidecar write itself raised.

### Runtime bindings

A binding carries only a value that exists before the run, a supplied fact or
a permitted setup result: content the experiment chooses belongs directly in
the stimulus, and the target's tool-call arguments, results, and replies are
captured downstream. The same holds for a record the target creates during the
run. If no supplied fact or permitted setup provides a needed record but a
documented operation creates it, the plan author guidance lets the stimulus ask
the target to create it, and the plan does not list the record as an
unresolved requirement. An empty `setup_permissions` list permits no setup.
Binding repair options mark a supplied fact whose value is an empty list or
object with `supplied_value_empty`. When a binding `source_ref` names one record
of a keyed fact (`facts:<ref>:<record_key>`), or its selector selects inside one
record (`value.<record_key>...`), the options also list `named_record_sources`:
that record's full selectors in each fact documenting it, including the record
key at `value.<record_key>.record_key` in the `facts:<ref>:records` companion.
The whole-source selector list is sorted and capped at 40 entries, so it can
omit the named record.

Downstream detection reads bindings as follows, and the plan author guidance
states it. The producer's tool-call condition decides a `command_attempt` claim
and already holds the supplied values it compares, so it reads no binding. The
semantic judge decides a `reply` claim and receives every resolved binding, so
a binding only that judge reads lists `judge.<name>`. A `command_attempt`
plan needs no judge-only binding, and the plan check reports `judge.<name>` on
any claim level other than `reply`. The retired spelling `detector.<name>` is
not a closed destination on any claim level; the check reports it and names the
`judge.<name>` replacement.

Artifact authoring copies `runtime_bindings` from the accepted plan, so neither
call 2 nor an artifact correction can add, rename, or change a binding. The
artifact correction guidance states that rule.

Binding declarations list `stimulus.user_text` only when the resolved scalar
value occurs in authored user text or the text contains its `{{name}}` slot.
The authoring validator fails with correction feedback when a binding lists
that consumer for a session prerequisite or a value only a later check reads. The plan
validator applies the same rule to `stimulus_approach.request`, and rejects a
request slot that names no declared binding, so plan correction can repair the
binding before the artifact stage freezes the plan.

## Development

```bash
./scripts/quality.sh
uv run pytest tests/ -q
```

The unit test suite is deterministic and does not require an LLM endpoint.

## Project structure

```
├── src/asago_artifact_generator/    # authoring, packages, CLI
├── packages/asago-bundle-garak/     # Garak adapter (workspace member): compile, instantiate, parse
├── contracts/                        # vendored and consumer-owned contracts
├── scripts/                          # quality, replay, and budget tools
├── tests/                            # unit tests
└── runs/                             # authoring output (gitignored)
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
