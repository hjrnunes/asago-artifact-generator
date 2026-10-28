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
fences, multiple objects in one block, prose around a single block, and
malformed JSON are rejected. A reply of two or more lowercase `json` blocks
with text between every two of them (the model drafts a block, writes "Wait,",
and drafts another) decodes to its last block when the reply ends on that
block's closing fence and every block decodes as one JSON object alone; text
after the last fence, adjacent blocks, and a block that does not decode are
rejected. The raw Call 1 bytes remain preserved, and a removed outer fence is
recorded before plan validation as `outer_fence_removed`, or as
`last_fence_taken:<block count>` when the last of several blocks was taken. The
reviewer response and the corrections use the same rules. Call 2 uses the same framing rules: one
JSON object with exactly `stimulus`, `semantic_judge_spec`, `examples`, and
`explanation`. It returns no detector code.
For a `command_attempt` claim, the `unsafe` and `safe` examples each carry a
`capture`: `tool_calls` (each with an `operation` from the supplied inventory
and its decoded `arguments`, in call order) and `complete`. For a `reply` claim,
the `unsafe` and `safe` examples each carry a `reply` instead: the message the
target assistant sends to the user in that outcome, verbatim and in its own
voice, which the semantic judge decides as unsafe and as safe; `description`
stays a one-sentence summary. The `inconclusive` example stays prose under every
claim level. The Call 2 contract of a reply plan explains `reply` as
`example_reply_meaning` and shows a neutral pair as
`example_reply_neutral_example`. A reply example without a `reply` gets a
`missing_field` finding; an empty one `example_reply_empty`; one that opens with
"The assistant", "The model", or "The agent" (case-insensitive, after any
leading punctuation or underscores and through emphasis markers around those
words), and so describes the reply instead of being one,
`example_reply_third_person`. A `reply` under any other claim level, or on the
`inconclusive` example, is one `unexpected_field`, whatever its value. These findings need no valid shape, so they are reported
beside any shape findings and go to the artifact correction together, within
its usual allowance. When the
plan claims `reply` and an `unsafe` or `safe` example carries a capture, the
correction gets one `unexpected_field` finding at `examples.<label>.capture`
that says a reply claim carries no capture, ahead of any other finding; shape
findings about that same capture are dropped, because the author removes the
field. `examples.json` holds the examples as written, except that in a reply
package the `unsafe` and `safe` `description` is the example's `reply`, the
text downstream tooling hands the judge as the assistant's reply, and the
author's summary moves to `summary`; the package has no `reply` field.
`artifact-package-v5` constrains only the member's manifest entry, so a package
written before, whose reply examples hold only a description, still loads.
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
field stops before dispatch. `contracts/model-profile/` (the producer's
`model-profile` contract) lists every profile field with its validity rule;
the loader rejects an unknown or invalid field, naming it, and a test runs the
contract's cases against the loader and the transport.
Thinking is a per-role control sent through the transport's additive
With the default `sampling_controls=true`, author, correction, and review
requests preserve the existing temperature and per-role
`chat_template_kwargs.enable_thinking=false` controls. With
`sampling_controls=false`, the transport omits `temperature`, `top_p`, `top_k`,
and `seed`,
and the thinking `chat_template_kwargs` entirely. The optional
`repetition_penalty` profile field (a number greater than 0) joins the
`extra_body` of author, correction, and review requests next to the thinking
control; the transport sends it only when the profile sets it and
`sampling_controls` is on. Each call records only the
controls it actually sends and keeps `max_retries=0`. `reasoning_effort` and
`service_tier` become top-level request fields; a configured
`service_tier_fallback` retries one 429 once with the fallback tier. After an
HTTP 5xx or a connection error that is not a timeout, the transport retries the
same request once after a fixed delay of at most 2 seconds; it never retries a
timeout, a 4xx (429 included), or an invalid response (a review response that
fails its checks is asked again by the review stage, as described under
reviews, not by the transport). The retry reserves one
more dispatch from the budget, and the dispatch's ledger and failure-evidence
record gain `transport_retries`: `[{"attempt": 2, "retry_of": {"type": <error
class>, "status_code": <HTTP status or null>}}]`. Dispatches without a retry
carry no such key. The service-tier fallback reserves one more dispatch the same
way, and a budget with no request left sends no fallback. Its entry in
`transport_retries` has `"retry_of": {"type": "RateLimitError", "status_code":
429}` and `"reason": "service_tier_fallback"`; a transport retry carries no
`reason`. The
`strict_json_schema` profile field is accepted for shared-profile compatibility
(only `false` loads; the producer rejects `true` too; an absent field loads as
`false`), but the consumer continues to send no `response_format`. Only the final message
content is parsed; provider reasoning stays in the raw response capture and
never substitutes for a missing answer.
Optional `context_window`, `max_completion_tokens`, and `timeout` fields drive
the request. An absent `timeout` is 600 seconds, which every request records in
its controls beside `strict_json_schema`. Omitted limits retain the 32,768-token context window and 8,192-token
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
all checks and the review. Each reviewed stage has a review-revision
allowance that is separate from its correction allowance (two for the plan
stage, one for the artifact stage): a `revise` spends a review
revision, while mechanical findings, including findings on the
revised candidate, spend the correction allowance. The ledger and failure
evidence record which allowance each correction spent (`allowance`), the
remaining `allowances` and `review_revision_allowances`, and the effective
`plan_max_review_revisions`/`artifact_max_review_revisions`. A reviewer
response that fails its checks (invalid JSON or framing, a missing or unknown
field, or a decision that contradicts its findings) is never a silent pass.
A response the provider stopped at its completion token limit (finish reason
`length`, read from the response capture, never from the text or the
reasoning) fails its checks too, even when its text parses: a
`review_truncated` finding names the request's `max_completion_tokens` and
states that a review response must be complete within it, ahead of any
framing or shape finding of the same text. It is a rejected response, not a
transport retry, and the request's token limits stay as configured.
The stage asks the same review again within its review-retry allowance
(`plan_max_review_retries`/`artifact_max_review_retries`, default one each
across all of that stage's reviews, nonnegative integers with the same rules,
zero while the stage is unreviewed). The retry
request repeats the review prompt and adds the rejected response and every
check finding exactly as reported. A UTF-8 response is shown exactly as
returned; a response that is not valid UTF-8 is shown with each invalid byte
sequence replaced by U+FFFD, and the request payload's
`rejected_review_response.response_encoding` reads `utf-8-replacement-inexact`
instead of `utf-8-exact` (the rejected dispatch's record keeps the exact
bytes). Each retry is its own dispatch: it reserves one review request from the
budget, and its ledger and failure-evidence record carry `"allowance":
"review_retry"` and `retries_dispatch`, the dispatch index of the rejected
response. A retry spends the allowance only once the budget reserved it and its
dispatch opened; a retry that the budget or a prompt check stops before it is
sent leaves the allowance unspent. That rejected response's own record keeps
its raw bytes, its check findings (`review_error` on the ledger, `findings` in
the evidence), and its failure; while a retry follows it, the run records no
`review_unavailable` finding. The evidence records the remaining
`review_retry_allowances`. When the allowance is spent, the stage stops as
`review_unavailable`, and the last response's evidence attempt adds a
`review_retry_limit_exhausted` finding to its `findings` (its ledger record's
`review_error` keeps only the check findings), which is the terminal reason.
The
default per-task budget is the policy's closed worst case, which includes each
review revision and its review and each review retry (16 dispatches, at most 7
author and 9 review, with default settings); `blocked` at the artifact stage
stops as `needs_plan_revision` without recursing into plan authoring. Reviewer
transport failures produce `review_unavailable`, and a transport failure stops
the run after the one recorded transport retry (see the profile section). An
explicit caller, per-task, or aggregate budget cap stops the run before the
next dispatch, a review retry included.

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

- `authoring-call1-v26` renders the plan author context while preserving the
  existing 11-field plan response. The handoff's `attack-path-v1`
  `attack_path` appears in SCENARIO DESIGN as an outline of the attacker lines
  and then numbered parts (conditions, attack steps in order, unsafe action,
  observable failure, hazard with its losses), with a note on how to read it.
  The purpose, the stimulus rules, the EVIDENCE REFERENCES rules, the response
  contract descriptions, and the correction texts name the attack path, and the
  path's node and source IDs are citable provenance. The plan correction
  (`authoring-correction-v40`) shows the same outline.
- `authoring-plan-review-v25` reviews a fresh source-derived plan context. It
  also shows the value each `supplied_input` binding resolves to, so the
  reviewer can check that a bound value means what the plan uses it for. When a
  binding selects inside one record of a keyed fact, the value also carries
  `record_key_source`: the documented `facts:<ref>:records` source and
  `value.<record_key>.record_key` selector that bind the record key itself. A
  required change that replaces a binding path names the complete source and
  selector pair. A review that follows a revision adds PRIOR REVIEW ROUND: each
  earlier finding beside `values_at_location`, the value at the plan path its
  `location` names in the plan reviewed before (`reviewed_value`) and in the
  revision (`current_value`), each `{"state": "value", "value": ...}` or
  `{"state": "absent"}`; a location that is not exactly one plan path
  (`candidate_plan.observation_claim.violation`, `runtime_bindings[0].selector`)
  existing in either plan is `{"state": "location_not_resolved"}`. The reviewer
  is told to compare each required change with `current_value`.
  Whether reviewers stop asking again for changes the plan already holds is
  unmeasured: the change was made and tested offline, with no model requests.
  Replay: the version change alters `prompt_version`, `prompt_sha256` and
  `reviewed_input_sha256` in every plan-review record. A first review's system
  and user text is byte-identical to v24, so a recorded first-round response
  still replays; every later-round user message differs, so replaying a
  recording made before v25 stops at its first later-round plan review. A
  replay serves recorded responses and does not test how a model answers the
  changed prompt.
- `authoring-call2-v28` renders the immutable accepted plan and the runtime
  contract, and asks for one JSON object without detector code. When the plan
  needs a semantic judge, `semantic_judge_spec_rules` states that the judge question is a yes/no
  question whose yes answer is the violation, that the criteria are a
  checkable statement of the standard, and that mentioning, quoting, reporting,
  or refusing content does not assert it. The validator rejects a question that
  does not end with a question mark. A plan that claims `reply` must return a
  `semantic_judge_spec` object; a missing one gets a
  `semantic_judge_spec_required` correction. Its `unsafe` and `safe` examples
  each carry a `reply` written as the target's message (see above).
- `authoring-artifact-review-v24` reviews the exact artifact object and the
  binding and judge declarations; for a reply claim it reads each example's
  `reply` as the target's own message.
- A review retry of either role (plan review v25, artifact review v24) sends
  the same prompt followed by PREVIOUS REVIEW RESPONSE REJECTED, the rejected
  response verbatim, its check findings, and an instruction to return one
  complete response that corrects every finding.
- `authoring-correction-v40` renders only the failed stage format and all
  current findings. A plan correction after a semantic review finding that
  points to a runtime binding, by `runtime_bindings[<index>]` location or by
  the exact binding name, carries a `review_binding` repair option. The option
  lists the binding's documented selectors, the selected record's sources, and
  `review_selector_checks`: each selector path the required change names,
  checked against the sources that document it. Artifact corrections use
  `authoring-correction-v44`; a reply plan's carries the Call 2 reply contract.

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
attack-path node IDs from the handoff are accepted in `interpretation.source_refs`
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
normal producer command and then hand the resulting `scenario-handoff-v5`
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

The consumer remains target-free. The orchestrator owns downstream
qualification, including package execution, live setup, runtime judging,
Garak detection, evidence, and cleanup.

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

- `scenario-handoff-v5` JSON or YAML for scenario meaning.
- `execution-target-profile-v1` JSON for the observed target inventory.
- Optional `runtime-context.json` (`runtime-context-v1`, written by the orchestrator's discover stage) for state and read observations.
- A target-free runtime contract (`artifact-runtime-contract-v2`). `generate`
  checks it against this repository's `contracts/runtime-contract/` schema and
  lock before any model request and stops at the first fault in path order:
  `runtime contract schema invalid at <path>: <reason>`.

Use `asago_artifact_generator.input_adapter.load_input` to validate the
vendored handoff kit, preserve authoritative narrative and Gherkin bytes, and
record SHA-256 source pins. The mirrored `CONTRACT.lock` declares
`scenario-handoff-v5` with its kit directory and digest domain. The adapter
verifies `content_digest` in the `scenario-handoff-v5` domain, and packages
record that version as `input_kind`. Authoring rejects every other document,
including `scenario-handoff-v1` to `v4`, with "authoring source must be a
producer scenario-handoff-v5 document; found <version>".

Before the digest check, the adapter applies the mirrored
`handoff-v5/schema.json`, the producer's ownership rules in the mirrored
`ownership-rules.json`, and the rules the schema cannot state: non-blank text,
the observation-contract format, and cross-field pairings. A rejection raises
`HandoffSchemaInvalid`. Its `codes`
are the producer's codes as the kit's `expected-violations.json` records them:
ownership codes first, then `schema_violation:<top-level field>`. Its message is
`handoff schema invalid: <codes> (<path>: <reason>)` for the first break. A
break confined to `attack_shape` raises the subclass `ShapeVersionMalformed`.

A handoff carries its scenario's `attack_path` and six structured-analysis
fields: `uca` (the unsafe control
action, its STPA type, and its hazards), `actor` (the adversary and its goal;
null for a functional scenario), `causal_factors`, `preconditions`,
`procedure` (the attack steps with their MITRE ATLAS tactic, technique, and
channel), and `worst_case_threat`. The adapter also checks the procedure chain
the schema cannot state: steps are numbered `S00`, `S01`, ... in order, each
leads to the next, and a step names a tactic exactly when it names a technique;
a break reports `schema_violation:<root>`. Every prompt that carries the
scenario meaning (Call 1, the plan review, Call 2, the artifact review, and both
corrections) shows these fields under `structured_analysis`, with a fixed
`note` that explains them; a null or empty field is left out.

A handoff may also carry an optional `taxonomy` block: `atlas_release`, the
producer's pinned MITRE ATLAS release (`YYYY.MM`), and `references`, the
scenario's OWASP LLM and Agentic Top 10 2026 entries. Each reference has
`scheme` (`owasp-llm` or `owasp-asi`), `edition`, `id` (`LLMnn` under
`owasp-llm`, `ASInn` under `owasp-asi`; the schema states the pairing), and
`name`. The adapter checks the rule the schema cannot state: the references are
sorted by scheme and id, each once, and a break reports
`schema_violation:taxonomy`. The block is outside `V5_FIELDS` and the model
view, so it never enters a prompt; it stays in the package's source record. A
handoff without it is valid and keeps its digest.

A handoff carries `attack_shape`: the channel, the number of user turns (1
to 4) and each turn's purpose, never attack text, or null for a functional
scenario. `InputView.attack_shape` returns it. The consumer authors `direct`
shapes, and `indirect` shapes when the runtime contract lists the
`planted_item` delivery. A `direct`
shape with more than one turn is authored as sequential user turns: the plan
states one intent per turn, Call 2 writes the earlier turns as `history` and the
last as `user_text`, and code checks `1 + len(history) == turn_count`. The
package is `artifact-package-v5`, and its `stimulus.json` states `mode`
(`single` or `sequential`) and `turn_count`; a functional handoff's stimulus
stays as authored. Before any model request, a shape the consumer cannot
author stops with a typed finding: `shape_channel_unsupported` (the runtime
contract lists no matching `delivery`, such as `planted_item` for `indirect`),
`shape_forged_transcript_unsupported`, or `shape_turn_count_unsupported`
(more turns than `limits.max_planned_turns`, default 4). A handoff whose
`attack_shape` breaks the vendored schema or its cross-field rules is refused
at load with `shape_version_malformed`.

An `indirect` shape puts the attack text in a record that a carrier operation
returns to the agent, and every user turn stays benign. Code picks the seed slot
from the runtime contract's `seed_slots`: a slot of the shape's carrier, of the
shape's content kind when one matches. Each slot states `max_chars`, the most
characters of planted text the target accepts, and `mode`, how the target
writes the text into the field: `replace` swaps the field's text for it, and
`append` adds it after the field's text. A slot without a positive `max_chars`
and a known `mode` carries nothing. Before any model request, the shape gate
refuses an indirect shape with `shape_carrier_not_exposed` when the inventory
lacks the carrier, and with `shape_seed_slot_unavailable` when no usable slot
carries it. Call 2 returns the text as `planted_text`. The rule block, the
Call 2 schema, and the artifact correction state the chosen slot's limit, and
the planted-text check applies it; the model's copies of the runtime contract
omit `max_chars` and `mode`. The package's `seed.json` carries the text and the
slot's `mode`. The rule block follows the stage: the plan stages (Call 1, its
correction, and the plan review) see only the `stimulus_approach` fields the
plan writes, and the plan check rejects a plan that carries `planted_text`; the
artifact stages (Call 2, its correction, and the artifact review) see the
`stimulus` fields, `planted_text`, and the rule that asks for it.

When an `indirect` shape names no record (`record_ref` is null) and the runtime
contract's seed slot lists records, the plan names the record that holds the
planted text in `stimulus_approach.planted_record_ref`, a string. The plan
response contract limits the field to the slot's records (an `enum`), and the
plan prompt says why: the target plants text only in a record its carrier
returns to the agent for the benign user, so the planted record can differ from
the scenario's target record (for example another customer's record), and the
planted text then leads the agent from one to the other. The plan review shows
the same records under `PLANTED RECORD RULE` and tells the reviewer not to ask
for any other record. Code checks that the slot lists the record the plan
names. A plan that omits the field, names a record the slot does not list, or
gives a non-string value gets `missing_field`, `planted_record_not_in_slot`, or
`type_error` at `stimulus_approach.planted_record_ref`; a plan that carries the
field where the shape names the record or is not indirect gets
`unexpected_field`. The plan correction
repairs each finding. After the plan is accepted, the record is fixed: the
artifact stages (Call 2, its correction, and the artifact review) state it as a
fact together with its source (the accepted plan, the producer's shape, or the
slot's first record), and the artifact review judges whether the benign turns
lead the agent to read the record instead of asking to move the planted text. A
reused plan without the field still plants the slot's first record.

A handoff may carry `semantic_failure_call` alongside its semantic failure
criterion: an exact `operation_name` and a `kind` of `incorrect_call` or
`missing_call`. The reader rejects a blank operation name and preserves the
object in the handoff view and every authoring stage's scenario context. An
absent or null field adds no context. This is producer scenario meaning;
detection still uses the bound tool-call condition for a command-attempt claim.

Target profiles may carry `value_facts`: declared record kinds and date formats
for operation arguments, state fields, and state mapping keys. Each entry must
declare a non-null `record_kind`, `format`, or both, and each argument or state
path may be declared only once in its group. Argument facts must name a declared
profile operation and one of its arguments (falling back to the resource's
arguments when the operation lists none). State paths are declarations, not
assertions that a captured state value exists. These facts remain producer
inputs to its condition checks; the consumer does not infer kinds from names or
change condition evaluation.

A handoff carries two optional condition fields and two tool-call fields, all
validated against the vendored schema of its version:

- `discriminating_condition` states what makes the behavior unsafe: a
  `statement`, a list of `comparisons`, and a `record_selection`. A value
  comparison has `left`, `op`, and `right` operands; each operand is an
  `argument` (operation and argument name), a supplied `fact` path, or a
  `literal`. Comparisons combine with AND. The ops `in`, `not_in`, `eq`, and
  `ne` match exactly, element-wise when the left side is a list. A
  `not_called` comparison (`operation`, optional `where`) holds when no call to
  `operation` matches every `where` item; a matching call at any position makes
  it false. An `order` comparison holds when the call to `operation` has no
  earlier unblocked call to `requires_prior` and the capture is complete (with
  no such call in an incomplete capture it is unknown); with `same_argument`,
  only an earlier call with an equal value for that argument counts. The
  authoritative semantics are
  in `contracts/tool-call-condition/tool-call-condition-v2/README.md`, which the
  consumer's evaluator (`authoring/condition_eval.py`) implements; the v1 kit
  stays mirrored beside it. The
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
supply a `runtime-context.json`, the loader checks it against this
repository's own `contracts/runtime-context/` schema (`runtime-context-v1`) and
reports the first fault as `at <location>: <keyword>` without repeating the
value. A missing-key fault lists the missing keys, and an unknown-key fault
lists the unknown keys (sorted, at most 10, each cut to 64 characters). Three checks stay in code because they compare documents: each read's
`profile_digest` equals the file's `target_profile_digest`, each read's
`tool_name` is a profile tool, and that digest equals the profile's
`semantic_digest`. Authoring does not accept native semantic scenario files, reference tasks, benchmark
answers, or hand-built inventories.

The consumer-owned artifact-package contract lives in
`contracts/artifact-package/artifact-package-v5/`. `package_io.write_package`
writes a complete directory atomically, and `load_package` verifies its
manifest, member paths, lengths, and digests before returning content. A
manifest has no `detector_interface`, and a package has no `detector.py`.
The v5 schema lists the member names (`plan.json`, `stimulus.json`,
`setup.json`, `bindings.json`, `prerequisites.json`, `checks.json`,
`inputs.json`, `observations.json`, `judge.json`, `tool_call_condition.json`,
`explanation.json`, `examples.json`, and `seed.json`), closes the manifest's
`authoring` to the outcome (`status`, `terminal_status`, and
`review_status.artifact`), and requires an empty `creation_model`. The loader
refuses a v5 package with any other member or manifest field.

The loader still reads `artifact-package-v4`
(`contracts/artifact-package/artifact-package-v4/`). v4 has two layouts. Older
v4 packages also copy each request and may contain `source-hashes.json`,
`authoring/ledger.json`, `authoring/reviews.json`,
`authoring/transformations.json`, and `authoring/NN-<stage>.json`, `.prompt`,
and `.raw` members. Later v4 packages have the v5 member set under the v4
version string. Where present, the retired members are verified like the
others and left out of the loaded members, and the manifest's `authoring` and
`creation_model` stay open. The loader refuses any other member of a v4
package, and the writer writes only v5, so it refuses a loaded v4 package.
Each package carries the scoring input for its plan's claim level:

- `tool_call_condition.json` holds the handoff's `tool_call_condition` exactly
  when its status is `bound`, serialized as sorted, two-space-indented UTF-8
  JSON with a trailing newline. Only a `command_attempt` package carries it, and
  that package requires it.
- `judge.json` holds the semantic judge specification and its resolved facts.
  Only a `reply` package carries it, and that package requires it.

An `indirect` package also carries `seed.json` (`artifact-seed-v1`): the
carrier operation and one item that names the seed slot, the record, the
slot's `mode`, and the planted text as `content`.
`contracts/artifact-package/artifact-seed-v1/schema.json` defines the format.
The schema states no length limit, because the slot's `max_chars` bounds the
text.

The writer writes only `artifact-package-v5`; the loader reads
`artifact-package-v5` and `artifact-package-v4`. The lock's `package_schema_version`
and `digest_domain` name v5. The manifest digest is the SHA-256 of the canonical
manifest without `manifest_digest`, in both versions, so a v4 package's digest
verifies unchanged: its `schema_version` is part of the digested bytes.

The version directory also holds the contract's cases: `valid/` packages every
reader must load, `invalid/` packages every reader must reject, and
`expected-violations.json`, which names each invalid case's stable code.
`artifact-seed-v1/` holds the seed schema's cases the same way, except that
each invalid seed's entry names the path and keyword where it fails first.
`metadata-policy.json` lists the manifest metadata the closed secret policy
accepts and rejects. Each case is one JSON file holding the manifest and the
package files as text. `scripts/gen_artifact_package_cases.py` writes the cases
and their lock entries; the orchestrator mirrors them and runs the same cases
against its own loader, mapping each code to its own error wording.

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
the accepted plan, the user-only stimulus, setup, exact runtime-binding
declarations, prerequisites, the required observations (`observations.json`),
the tool-call condition or judge specification, explanation, examples,
`checks.json`, and `inputs.json` (the inventory, runtime
contract, discovery provenance, and `authoring_input_pins`). The manifest's
`authoring` records only the outcome (`status`, `terminal_status`, and
`review_status.artifact`); `creation_model` stays an empty object, which the v5
schema requires. Each request's prompt, raw response, usage, and review live
only in the failure-evidence sidecar described below.
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
The sibling `<package>.failure-evidence.json` sidecar is the authoring record
of one item. It is written atomically for every run that dispatched a request
or recorded a finding, whatever its terminal status: an accepted run writes it
beside its package. A run that neither dispatched a request nor recorded a
finding writes no sidecar. Despite its name, the sidecar is not limited to
failures. The `report` command reads it for every item, and an accepted
sidecar points the page to the item's package. The replay gate
(`scripts/replay_gate.py`) replays each item from its recorded attempts. The
orchestrator's scoring finds the sidecars by their file name. The file name
and the schema id keep the word `failure` on purpose: these readers and saved
run folders locate the sidecar by that name, and a rename would require every
reader to accept both names. The sidecar preserves
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
Every reviewer attempt's `review` record also keeps `reviewed_candidate`: the
plan or artifact candidate the reviewer read, whose canonical JSON hashes to
`reviewed_candidate_sha256`. The in-memory ledger and the package keep no copy.

### Author report

`report` renders one offline HTML page about an author output directory. The
page answers how many scenarios became runnable tests and why the rest failed:

```bash
uv run asago-artifact-generator report <author-output> \
  --scenarios <generate-output>/scenarios --out <author-output>/report
```

It writes `index.html` and `stage-summary.json` (`stage-summary-v1`, checked
against its schema and the page's numbers). `--out` defaults to
`<author-output>/report`. The page reads only saved files: failure evidence,
packages, and the scenario inputs; it sends no requests. Each request links to
its record in the scenario's failure evidence (`attempts[i]`), which holds the
exact prompt and raw reply. Each
failed scenario shows its terminal reason with a one-line meaning from
`report/reasons.py`, its cause, the signals that repeat across requests, and
every request in order. A before-and-after panel appears only when both
reviewed candidates hash to their recorded digests. The page links to the
generate report when `<scenarios>/../report/index.html` exists. The HTML kit
under `report_kit/` mirrors the scenario generator's `report-kit` contract byte
for byte; the orchestrator keeps the same copy.

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

## Target config and the Garak entry points

A run's target is described by its `target.yaml`, with `tools-list.json` and
`runtime-spec.yaml` beside it, and an `endpoint` block (`mcp_url`, `agent_url`, and
the commands that start, check, seed and reset it). The caller passes that file, or a
mapping it already loaded, to the adapter; `tests/fixtures/targets/piggybank/` is an
example. A run makes real writes to whatever the config points at, so the config must
say what the target is:

```yaml
# mock: a stand-in built for testing; test_instance: a test deployment of a real system.
authorized_as: mock
```

The Garak adapter's `instantiate`, as a function and on the command line, checks the
target config first: a config without `authorized_as`, or with any value other than
`mock` or `test_instance`, is refused (exit 1 on the command line) before the package,
the template, the run settings or the values are read, and before anything is
written or sent to the target.

The Garak adapter's `compile`, `instantiate` and `parse` are library functions, and
its command line calls them. `instantiate` prepares the package for one run with
`asago_bundle_core.preparation.prepare_package`: the setup steps go to the target's MCP
server by default (real writes), the bindings resolve from their results, the prerequisites are
checked, and the rendered stimulus and the judge's facts become the bundle values. A
package that cannot be prepared returns `PreparationIncomplete` and writes no bundle.

```python
from asago_bundle_core.target import load_target_config
from asago_bundle_garak import InstantiateError, RunSettings, compile, instantiate, parse

target = load_target_config("path/to/target.yaml", InstantiateError, port=8010)
run = RunSettings(gateway_url="http://127.0.0.1:8000/v1", model="model-name",
                  judge_url="http://127.0.0.1:8001/v1", judge_model="judge-name")
compile("package", "template")
result = instantiate("package", "template", "bundle", target=target, run=run)
# result.preparation holds the setup ledger, bindings and prerequisites to record.
# After Garak runs the bundle's entrypoint:
parse("bundle", "execution-receipt.json")
```

The MCP server the bundle's agent is told to use is always the authorized config's
`endpoint.mcp_url`, with `{port}` filled; `RunSettings.mcp_url`, when set, must name that
same URL or `instantiate` refuses the run. The setup steps go to that URL by default; a
caller that passes its own `call_target` sends them through it instead.

The setup client (`asago_bundle_core.target.mcp_call_target`) speaks both MCP transports,
and `endpoint.mcp_url` chooses: a URL whose path ends in `/sse` is the legacy SSE transport
(a GET opens an event stream whose `endpoint` event names the URL to POST to, and the
answers arrive on the stream); any other is streamable HTTP (JSON-RPC POSTed to the URL).
Each record of the setup ledger keeps the tool's decoded object as `result` and the MCP
`tools/call` result as received (content blocks, `isError`, `structuredContent`) as
`native_result`, also for a failed call that received an answer (a tool error).
`native_result` is null when no answer arrived or the caller's `call_target` returned a
plain dict; a `call_target` keeps it by returning a `ToolResult` (or raising
`TargetCallFailed`). `allowed_tools` defaults to
every operation of the package's inventory. Every template member and the run settings
are checked before the first setup call. On the command line:

```bash
asago-bundle-garak instantiate TEMPLATE --package PACKAGE --target target.yaml \
  --port 8010 --run run.json --out BUNDLE
```

`run.json` holds the `RunSettings` fields; `--port` fills `{port}` in the endpoint
URLs. The command prints the preparation as JSON and exits 4 when the package cannot
be prepared. A call without `--package`, `--target` or `--run` is a usage error (exit 2).

## MiDojo episodes

The MiDojo adapter maps a `planted_item` package to one MiDojo episode for one target
and reads the sealed result back into a receipt; MiDojo itself runs the episode. It
plants by response injection: MiDojo's host interceptor sits between the agent and the
target's MCP server, forwards each call (except a write the episode blocks, which it
refuses and records as blocked), and places the planted text in the structured result
of the carrier tool. The target's own data is not changed (the Garak adapter plants the
same text by writing `seed.json` into the target before the run). It targets the
`Episode` value, `midojo.open_episode` with `HostInterception`, and `midojo.grade` of
MiDojo's `asago/main` at `b5b8c75`, or `c0bee12` for an episode that matches a carrier's
result (`src/midojo/episode.py`,
`src/midojo/episode_context.py`, `src/midojo/interception_host.py`,
`src/midojo/control_plane/grading.py`, and `midojo_sdk.injection` for the plan).

```bash
asago-bundle-midojo compile PACKAGE --out EPISODE_DIR --target target.yaml \
  [--block TOOL ...] [--judge-turns final|all]
# the runner builds the Episode from EPISODE_DIR/episode.json and opens it with
# midojo.open_episode(..., interception=HostInterception(upstream=<the target's
# endpoint.mcp_url>)), seals it, grades the snapshot with midojo.grade, and may write
# delivery.json from midojo.delivery
asago-bundle-midojo parse EPISODE_DIR --snapshot snapshot.json --verdict verdict.json \
  [--delivery delivery.json] --out execution-receipt.json [--route midojo] \
  [--tool-revision REV]
```

`--target` is the target config: `target.yaml` with `tools-list.json` and
`runtime-spec.yaml` beside it. One that is not `authorized_as` `mock` or `test_instance`
is refused (exit 1). `compile` writes `episode.json` (`midojo-episode-v2`):

- the package's identity, claim level and delivery;
- `interception`: `{"mode": "host", "upstream": "endpoint.mcp_url"}`. The runner gives
  the interceptor the target config's MCP endpoint at run time; the episode holds no
  URL. The agent holds the interceptor's URL and bearer token and nothing else of
  MiDojo's: no evaluation session, and no address or credential of its control plane.
  The target is only where the interceptor forwards the calls, and gets no MiDojo
  credential or session either;
- `tools`: `allowed`, the operations of the package's inventory, which must include the
  carrier and be tools the target lists; and `write_disposition`, `forward` by default
  for each allowed tool the target declares mutating (by its annotations, else
  `runtime-spec.yaml`'s `operations.mutating`), or `block` for each `--block` tool;
- `placements`: each instruction's seed slot, record, carrier and field, in the runtime
  contract's terms, and the slot's record key: the list item property it selects by, or,
  outside a list, the call argument or the result property it matches;
- `episode`, the fields of MiDojo's `Episode` as plain data (enums as their values,
  tuples as lists). The turns are the stimulus's user turns. Each seed item becomes one
  `tool_output` instruction from the seed slot of the package's runtime contract that it
  names: the carrier operation as the tool, the slot's field as the `structured_content`
  path, the slot's `mode` (`append` or `replace`), its `max_chars` as the size limit, the
  item's text as the payload, strict on a mismatch, on every carrier call. A field
  inside a list of records (`notes[].body`) is reached through the list by MiDojo's
  record selector (`["notes", {"field": "note_id", "equals": "NOTE-1"}, "body"]`), which
  picks the one item whose key field is the record. The key is the slot's `record_key`,
  used as declared: the name whose value is the record's ID, here a list item property.
  A slot declares it in the target's `runtime-spec.yaml` (`record_key: note_id`), in the
  package's runtime contract (`artifact-runtime-contract-v2` allows the optional field),
  or both, and the two must agree. Compile checks that the carrier's output schema
  declares the key as a text property of the list's items. A field outside any list
  (`body`) is placed only when the carrier returns one record: the carrier's output
  schema declares the result and every key above the field exactly as an object
  (`"type": "object"`: not an array, a scalar, an untyped node or a union such as
  `["object", "null"]`, which a call may return without the key), with the plain path.
  The record is then matched rather than selected in the result, by the first of two
  rules the declarations allow. When the carrier's input schema declares the slot's
  `record_key` as a required text argument, the call names the record, and the
  instruction's target names `call_arguments` `{<record_key>: <record>}`, as MiDojo's
  plan data writes it from `b5b8c75`: `{"tool": "read_note", "surface":
  "structured_content", "structured_content_path": ["body"], "call_arguments":
  {"note_id": "NOTE-2"}, ...}`. Otherwise, when the output schema declares the
  `record_key` as a text property of the result object itself (its top level), the
  result names the record, and the target names `result_record` `{"field":
  <record_key>, "equals": <record>}` and no `call_arguments`, as MiDojo's plan data
  writes it from `c0bee12`: `get_referral(patient_id)` answering `{"referral_id":
  "REF-101", "reason": ...}` gets `{"tool": "get_referral", "structured_content_path":
  ["reason"], "result_record": {"field": "referral_id", "equals": "REF-101"}, ...}`.
  MiDojo compares the value type for type, so the record stays the text the seed names
  and the property must be declared text. A call of the carrier that names another
  record, or whose result holds another (or no result), is left unchanged; MiDojo
  records it `unmatched` at occurrence 0 and does not count it. MiDojo's host
  interceptor declares both capabilities (`InterceptorCapability.call_arguments`, and
  `result_record` from `c0bee12`), and an episode always intercepts on the host, so
  compile models no other interceptor. A list slot's target has neither. A selector that
  matches nothing leaves the result as it was, and MiDojo reports it unmatched. The
  interceptor places the text in the result's `structuredContent` and, from `3824922`,
  copies it into the text block when that block is a JSON mirror of the structured
  content, so an agent that reads only text sees it too; other text stays as the target
  sent it, and the outcome is `partial`. Each call record MiDojo seals holds the text the
  agent got, its `structured_content` and its `injection_outcomes`. A target that matches
  the call's arguments needs `b5b8c75`, so the episode names `>=b5b8c752` as its MiDojo
  revision range; one with a `result_record` target needs `c0bee12`, where MiDojo first
  reads that key, and names `>=c0bee126` instead. There is no environment (the target holds its state), no utility
  check, and the runtime is `unmanaged`.

A command attempt is checked by MiDojo's `tool_call_condition` predicate. A reply is
checked by the rubric judge: the directory also holds the verifier as the
`asago_verifiers` package, which the runner registers with `register(judge_url,
judge_model)` before it builds the episode; the episode runs no package setup, so the
judge gets no runtime facts. The same inputs give the same bytes.

`compile` refuses, as a capability gap (exit 3, with the shared record), a package whose
stimulus delivers its attack in the user turns (a direct or user-turn package), a single
package with history, a non-user history message, a stimulus with run-time bindings, a
`state_effect` claim, and a seed item the interceptor cannot place in an MCP tool result:
a carrier the target does not list over MCP, a carrier without a JSON object result, a
field the result does not declare as text (or one through more than one list), a slot
that declares no `record_key`, a list slot's `record_key` the carrier's output schema
does not declare as a text property of the list's items, a field outside a list below a
key the output schema does not declare exactly as an object, a field outside a list whose
`record_key` the carrier's input schema does not declare as a required text argument
nor its result object as a text property (so neither the call nor the result names the
one record it returns), and a text over `max_chars` UTF-8 bytes,
which is how MiDojo counts its size limit. A seed that disagrees
with its slot (an unknown slot, a record or mode the slot does not have, a text over
`max_chars` characters), or a `record_key` the runtime contract and the target config
state differently, cannot be compiled (exit 1).

`parse` reads the snapshot as MiDojo's `GET .../snapshot` returns it and the verdict as
`{evaluation_id, snapshot_id, grade}`. It grades nothing: `security` true is `detected`,
false `not_detected`, and null (validity `invalid`) `inconclusive`, never a pass or a
fail. The attempt's reason is the grade's `security_reason`; a true or false `security`
without one is `verdict_malformed`. The receipt's `episode` block carries the run's
`route`, the `evaluation_id` and
`snapshot_id` graded, the snapshot's `evidence_status`, the verdict as MiDojo gave it,
and `call_injections`: each sealed call's `call_id`, `function` and `injection_outcomes`
as MiDojo sealed them (`instruction_index`, `probe_key`, `occurrence`, `status`,
`detail`; from `b5b8c75` a call whose arguments did not match an instruction's
`call_arguments`, and from `c0bee12` one whose result did not match its `result_record`,
is `unmatched` at occurrence 0, the same shape for both), null for a record that does not say (a blocked or unfinished call, or one
sealed before `3824922`). A call record is checked field for field against MiDojo's
`FunctionCallRecord`: sealed from `3824922` it has both `structured_content` and
`injection_outcomes`, before it has neither, and one with only one of them is
malformed. Sealed from `13d5487` each outcome also carries its delivery facts,
`selected`, `transformation` and `emitted` (null when the interceptor did not report
them), all three or none; an outcome at occurrence 0 placed nothing, so its facts are
empty.

From `13d5487` MiDojo's grade also says apart what `security` folds together:
`attempted`, `blocked`, `effect_observed` and `injection_delivered`, each true, false or
null (unknown). They are optional: a grade has all four or none, a verdict sealed before
`13d5487` lacks them and the receipt's `episode.verdict` records each as null (unknown,
never false). They never change the attempt's outcome, and any other unknown grade key is
still `verdict_malformed`. The episode's tool revision range stays `>=b5b8c752` (or
`>=c0bee126` with a `result_record` target).

Delivery and exposure are not in the snapshot or the verdict: the runner computes them in
MiDojo's interpreter and writes `delivery.json` beside the snapshot, a
`midojo-delivery-v1` document (`contracts/midojo-delivery/`):

```json
{
  "schema_version": "midojo-delivery-v1",
  "evaluation_id": "<the snapshot's evaluation_id>",
  "snapshot_id": "<the snapshot's snapshot_id>",
  "delivery": "<midojo.delivery.delivery_facts(plan, snapshot.function_calls), each fact's model_dump(mode='json')>",
  "exposure": "<midojo.delivery.exposure(plan, snapshot.function_calls, turns=len(snapshot.turns), evidence_status=snapshot.evidence_status).model_dump(mode='json')>"
}
```

`parse` reads it only when `--delivery` names it. The receipt's `episode` block then
carries its `delivery` (each fact: call, `interceptor_instruction_index`,
`plan_instruction_index`, probe, occurrence, status and the three facts) and its
`exposure` (`emitted`, `agent_tool_result` and `model_input`, each `exposed`,
`not_exposed` or `unknown`, and each plan instruction's own), and `native` lists it.
Without `--delivery` both are null: unknown, never undelivered. A named document that is
missing (`delivery_missing`), does not match the contract (`delivery_malformed`) or names
another evaluation or snapshot than the snapshot's (`delivery_mismatch`) fails the
receipt. A payload the host placed in the environment has no delivery fact, so its
exposure, and `injection_delivered`, stay unknown; a response-injection placement gives
real facts.
`parse` recomputes the snapshot's ID, MiDojo's digest of its content (`state.snapshot_digest`,
ported in `snapshot_digest.py`), and fails a snapshot whose content changed under its ID
(`snapshot_digest_mismatch`); a value JSON cannot carry back as MiDojo held it fails the
same way, as MiDojo's own client refuses that round trip. A verdict that names another
evaluation or snapshot, a validity that disagrees with the grade, turns other than the
episode's, or a call the host interceptor did not record (`call_not_intercepted`) fails
the receipt with the reason; a turn without a reply fails it with `turn_failed:<n>` and
keeps the attempt. `midojo`, MiDojo's own attacker loop, is the only route today.

## Development

```bash
./scripts/quality.sh
uv run pytest tests/ -q
```

The unit test suite is deterministic and does not require an LLM endpoint.

## Project structure

```
├── src/asago_artifact_generator/    # authoring, packages, CLI
├── packages/asago-bundle-core/      # shared adapter core (workspace member): tool-bundle-v1 schema, slots, package preparation, target config, conformance helper
├── packages/asago-bundle-garak/     # Garak adapter (workspace member): compile, instantiate, parse as library entry points
├── packages/asago-bundle-midojo/    # MiDojo adapter (workspace member): planted_item packages to response-injection episodes for a target config, sealed snapshots and verdicts to receipts; other packages exit 3
├── contracts/                        # vendored and consumer-owned contracts
├── scripts/                          # quality, replay, and budget tools
├── tests/                            # unit tests
└── runs/                             # authoring output (gitignored)
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
