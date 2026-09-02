# STPA Execution Bundle Consumer Specification

Status: proposed implementation specification
Date: 2026-09-02
Repository: `asago-artifact-generator`
Companion specification: `asago-scenario-generator/ai/findings/stpa-execution-bundle-producer-spec.md`

## 1. Decision

The artifact generator shall consume validated STPA execution bundles as its
primary scenario input and shall turn their semantic intent into explicitly
bound, platform-supported execution artifacts.

The artifact generator owns runtime binding, platform readiness, platform
planning, compilation, artifact traceability and runtime observation receipts.
It does not own causal reasoning, unsafe-control-action meaning, scenario
admission, taxonomy-obligation interpretation, or structural STPA identities.

The primary input seam is the producer-owned pair:

1. `stpa-execution-projection-v2`; and
2. `stpa-execution-bundle-v1`.

The consumer vendors and pins the producer's JSON schemas and conformance
fixtures. It must not import the scenario generator's Python package or read a
sibling checkout during normal operation or deterministic tests.

## 2. Current incompatibility

The existing artifact path is shaped around retired taxonomy-era scenario
YAML:

- `narrative` is assumed to be a mapping containing `summary`, `entry_point`,
  `zone_sequence` and `steps`;
- injection surface is inferred from the parenthetical suffix of prose;
- tool names are inferred from backticked names in narrative text;
- one model call authors transcript behavior and detector meaning together;
- `full | partial | skip` mixes platform coverage with artifact readiness; and
- `--force` can write an artifact after structural validation fails.

Current STPA scenario envelopes use a narrative string and a separate
canonical projection. Passing a normal current STPA scenario to the old loader
raises an incidental `AttributeError`. Making that loader more permissive
would preserve the wrong architecture: prose would remain the execution
authority.

The implementation shall introduce a new strict module rather than extending
the old dictionary extractor with additional shape guesses.

## 3. Domain language

The following terms are normative and match the producer specification.

### 3.1 Execution intent

The consumer's immutable internal representation of a verified projection and
its paired scenario context. Projection sequence, identities, semantic
conditions and requirements are authoritative; scenario narrative is
presentation context only.

### 3.2 Semantic binding

A human-reviewed value filling an exact typed placeholder inside a semantic
condition. The producer still owns the condition's type, subject, semantic
property and operator. The artifact generator may collect and persist the
placeholder's value, but cannot infer it from prose, taxonomy IDs, a model
response or platform defaults.

### 3.3 Runtime binding

A deployment-specific mapping from structural semantic references to concrete
surfaces, tools, schemas, events, clocks, state observers and detector
implementations.

### 3.4 Platform capability

A typed claim about what a target execution adapter can write, invoke, measure
or observe. Capabilities are adapter-owned facts, not model opinions.

### 3.5 Readiness

The deterministic result of checking source validity, semantic completeness,
runtime-binding completeness and platform support. Readiness is established
before content authoring or artifact compilation.

### 3.6 Platform plan

A complete typed plan for one adapter. It fixes step order, surfaces, tools,
values, timing behavior and the success oracle before any optional model call.

### 3.7 Observation receipt

A separate runtime result that points back to the source projection, binding
set and compiled artifact. It never modifies the source projection.

## 4. Ownership

### 4.1 The artifact generator owns

- independent bundle, schema, digest and pair verification;
- normalization into an immutable `ExecutionIntent`;
- collection and attestation of operator-supplied semantic bindings;
- concrete prompt/input/tool/event/state surfaces;
- target tool names, schemas and safe argument values;
- event, clock, state and detector bindings;
- platform capability descriptions;
- binding and support readiness;
- platform-specific execution planning and compilation;
- optional presentation text authoring within a fixed plan;
- output artifact validation and traceability; and
- runtime observation receipts.

### 4.2 The artifact generator does not own

- selecting causal factors or changing their order;
- changing controller, control action, ICA, candidate or scenario identity;
- choosing the UCA type or unsafe semantic outcome;
- adding a scenario because a taxonomy obligation exists;
- treating taxonomy pattern or technique IDs as executable instructions;
- filling unknown semantic values without explicit operator review;
- weakening an unsupported condition into a prose approximation;
- inferring authoritative behavior from narrative, Gherkin or ID prefixes; or
- writing runtime observations back into producer artifacts.

## 5. External interfaces

The implementation shall expose three deep interfaces.

```python
def load_execution_bundle(index_path: Path) -> VerifiedExecutionBundle:
    ...

def bind_and_plan(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities,
) -> ExecutionPlanResult:
    ...

def compile_execution_artifact(
    plan: ReadyExecutionPlan,
    author: PresentationAuthor | None = None,
) -> CompiledArtifact:
    ...
```

### 5.1 `load_execution_bundle`

This interface owns all filesystem reading, contract validation, digest
verification, identity reconciliation and conversion into internal values.
Callers do not separately load scenario and projection files.

### 5.2 `bind_and_plan`

This interface is pure. It accepts already verified intent, a typed binding set
and one adapter's declared capabilities. It returns complete diagnostics on all
readiness axes and either no plan or one typed ready plan.

### 5.3 `compile_execution_artifact`

This interface accepts only a `ReadyExecutionPlan`. It cannot be called with a
raw scenario, raw projection, partial binding or unsupported plan. Optional
model-backed authoring is injected and limited to designated content slots.

## 6. Vendored wire contract

The producer repository is the contract authority. Vendor its versioned
contract tree under:

```text
contracts/stpa-execution/
  projection-v2/
    schema.json
    valid/
    invalid/
    expected-violations.json
    canonical-digests.json
  bundle-v1/
    schema.json
    valid/
    invalid/
    expected-violations.json
  CONTRACT.lock
```

Rules:

- Vendor complete immutable versions, not selected fixture fragments.
- Record the upstream repository, revision and `CONTRACT.lock` digest in a
  local `UPSTREAM.lock`.
- Deterministic tests read only the vendored copy.
- Do not use symlinks or relative paths to a sibling checkout.
- A contract update is an explicit consumer change with fixture and adapter
  review.
- Unknown schema versions fail closed before any model call.
- The consumer may implement equivalent inward models, but its acceptance is
  defined by the wire schema and conformance fixtures, not by producer Python.

## 7. Verified bundle loading

### 7.1 Input

`load_execution_bundle` accepts only the path to
`stpa-execution-bundle-v1` canonical JSON. YAML is not accepted as the primary
machine interchange.

### 7.2 Required verification order

For each bundle:

1. Read bytes with an explicit size limit.
2. Parse JSON and reject duplicate object keys.
3. Validate the closed bundle schema.
4. Recompute and verify `bundle_digest`.
5. Validate every run-relative path before opening it.
6. Reject absolute paths, `..`, empty components and symlink escapes.
7. Read the exact canonical scenario and projection bytes.
8. Verify both `content_sha256` values.
9. Parse both documents with duplicate-key rejection.
10. Validate the projection against the vendored v2 schema.
11. Recompute and verify its framed `semantic_digest`.
12. Reconcile run, scenario, candidate, slot and ICA identities across index,
    projection and scenario envelope.
13. Require index validation status `valid`.
14. Convert the pair into one immutable `ExecutionIntent`.

Any failure aborts that entry before readiness, authoring or artifact output.
The batch result may report other independently valid entries, but a malformed
bundle index itself invalidates the entire bundle.

### 7.3 Security and resource limits

The loader shall define conservative configurable limits for:

- index bytes;
- entry count;
- individual scenario/projection bytes;
- JSON nesting depth; and
- total bytes read from one bundle.

Paths are interpreted relative to the index directory after normalization.
Loading may not contact the network, execute code, import plugins or resolve
environment variables from input.

## 8. Internal `ExecutionIntent`

`ExecutionIntent` is a closed, frozen inward model. It contains:

- bundle and projection schema versions;
- bundle and projection digests;
- run, scenario, candidate, slot and ICA identities;
- controller, control action and UCA type;
- ordered causal factors and steps;
- typed unsafe outcome;
- neutral execution requirements;
- source trace references;
- selected presentation context from the scenario envelope; and
- source file byte digests.

It does not contain:

- mutable raw dictionaries;
- unverified paths;
- inferred injection surfaces;
- inferred tool names;
- a platform coverage score;
- an LLM client;
- runtime observations; or
- taxonomy obligations converted into operations.

Narrative, attack tree and Gherkin may be retained as context fields but are
never consulted to choose sequence, semantics, surface, tool or oracle.

## 9. Runtime binding contract

### 9.1 `runtime-binding-set-v1`

One `RuntimeBindingSet` applies to exactly one projection digest:

```yaml
schema_version: runtime-binding-set-v1
binding_set_id: BIND-klarna-test-SCN-001
projection_semantic_digest: <projection digest>
target_environment_id: klarna-test
semantic_bindings: []
surface_bindings: []
control_action_bindings: []
observation_bindings: []
clock_binding: null
review:
  reviewed_by: operator@example
  reviewed_at: '2026-09-02T12:00:00Z'
  rationale: Bind the verified scenario to the test environment.
semantic_digest: <binding-set digest>
```

The document and every nested record are closed. Its semantic digest uses
frame `runtime-binding-set-v1` over the complete canonical object excluding
the digest.

### 9.2 Semantic bindings

```yaml
condition_ref: OUTCOME-1
binding_ref: SEM-1
value: TEST-ACCOUNT-001
reviewed_by: operator@example
rationale: The test tenant's authorized account identity.
evidence_refs: [change-request-123]
```

Rules:

- A semantic binding is allowed only where the source projection has
  `semantic_binding_required: true`.
- `binding_ref` must name a placeholder already present in `condition_ref`.
- The value must satisfy the placeholder's declared type and constraints.
- It cannot replace the condition discriminator, subject, semantic property or
  operator.
- It requires non-empty reviewer identity, rationale and evidence references.
- Model output, narrative extraction and platform defaults cannot populate it.
- It becomes part of binding-set identity and artifact traceability.
- It does not alter or rewrite the source projection.

### 9.3 Surface bindings

```yaml
source_ref: PM-3-1
surface: user_turn
locator: conversation.input
writable: true
```

Closed initial runtime surfaces are:

- `system_prompt`;
- `user_turn`;
- `assistant_turn`;
- `tool_call`;
- `tool_result`;
- `tool_definition`;
- `memory`;
- `data_store`;
- `agent_message`; and
- `environment_event`.

The source reference must exist in the projection. Surface selection is
explicit; it is never inferred from structural ID prefixes or narrative text.

### 9.4 Control-action bindings

```yaml
control_action_id: CM-1
adapter_operation: tool_call
tool_name: authorize_transaction
tool_schema:
  type: object
  properties:
    account_id: {type: string}
  required: [account_id]
safe_defaults:
  account_id: TEST-ACCOUNT-001
```

Rules:

- The control action must equal the projection's target or an explicitly
  referenced action.
- Tool schemas are deployment facts supplied through configuration.
- The model cannot create tool names or schemas.
- Safe defaults must not contain credentials or production identifiers.
- Secrets are referenced through runtime secret handles and are never stored
  in the binding artifact.

### 9.5 Observation bindings

```yaml
condition_ref: OUTCOME-1
observer_kind: tool_argument
event_source: trace.tool_calls
semantic_property: synchronized_intent
field_path: arguments.account_id
comparison: not_equals
expected_from: semantic_binding:SEM-1
```

Initial observer kinds are closed:

- `tool_call`;
- `tool_argument`;
- `output_text`;
- `state_value`;
- `event_presence`;
- `event_absence`;
- `event_order`;
- `elapsed_time`; and
- `duration`.

Bindings describe how to observe an existing semantic condition. They do not
replace the condition with a detector prompt.

### 9.6 Clock binding

A clock binding is required only when the projection needs real delay,
duration, window or absence-over-time semantics:

```yaml
clock_kind: monotonic
resolution_ms: 10
event_timestamp_source: harness.monotonic_timestamp
```

Prompt text containing simulated timestamps is not a clock binding.

## 10. Platform capabilities

Each platform adapter returns a typed immutable `PlatformCapabilities` value:

```yaml
platform: garak
adapter_version: <version>
writable_surfaces: [system_prompt, user_turn, tool_result]
invocable_operations: [chat_completion, tool_call]
observer_kinds: [tool_call, tool_argument, output_text]
supports_multi_turn: true
supports_persistent_state: false
supports_multi_agent: false
supports_real_clock: false
supports_state_observation: false
```

Capabilities are deterministic adapter facts. They are not accepted from the
scenario bundle, generated by a model or inferred from a testability score.

## 11. Readiness model

`ExecutionPlanResult` preserves independent axes:

```yaml
source_status: valid
semantic_binding_status: complete
runtime_binding_status: incomplete
platform_support_status: unsupported
artifact_status: not_attempted
overall: needs_runtime_binding
diagnostics: []
plan: null
```

Closed axis values are:

- source: `valid | invalid`;
- semantic binding: `complete | incomplete | not_required`;
- runtime binding: `complete | incomplete`;
- platform support: `supported | unsupported | indeterminate`;
- artifact: `not_attempted | generated | failed`; and
- overall: `invalid | needs_semantic_binding | needs_runtime_binding |
  unsupported | ready`.

Overall precedence is:

1. `invalid`;
2. `needs_semantic_binding`;
3. `needs_runtime_binding`;
4. `unsupported`; and
5. `ready`.

All applicable diagnostics are retained even when an earlier overall state
wins. For example, a projection may report both a missing observer binding and
unsupported persistent state.

Only `overall: ready` carries `ReadyExecutionPlan` and may reach authoring or
compilation.

## 12. Deterministic planning algorithm

`bind_and_plan` shall perform, in order:

1. Assert integrity of `ExecutionIntent` and optional bindings.
2. Require the exact projection digest match.
3. Resolve each required semantic binding and reject unsolicited bindings.
4. Resolve every factor source and step to a concrete runtime surface.
5. Resolve the target control action to an adapter operation.
6. Resolve the unsafe condition to a concrete observer.
7. Resolve required clocks, state stores and agent channels.
8. Compare neutral execution requirements with adapter capabilities.
9. Produce complete diagnostics on every readiness axis.
10. If ready, construct a fixed ordered platform plan.

The algorithm must not consult scenario prose to fill a missing result. It
must not call a model, access a provider, write files or load plugins.

## 13. `ReadyExecutionPlan`

A ready plan contains:

- immutable source and binding digests;
- target platform and adapter version;
- one ordered plan step for every projection step;
- concrete runtime surface for each causal step;
- concrete control action/tool operation;
- resolved semantic values;
- observer/oracle plan derived from the unsafe outcome;
- clock/state/agent channels when required;
- named content slots where presentation text may be authored;
- trace mapping from plan-step IDs to projection-step/factor IDs; and
- no unresolved placeholders.

The ready plan is the sole input to a platform compiler. It is serializable and
testable without an LLM.

## 14. Presentation authoring seam

The optional model-backed author is constrained by an interface such as:

```python
class PresentationAuthor(Protocol):
    def author(self, request: PresentationRequest) -> PresentationResult:
        ...
```

`PresentationRequest` contains:

- fixed slot IDs;
- purpose and allowed role of each slot;
- scenario narrative and loss context;
- exact tool names and argument values that may be mentioned; and
- length and content constraints.

It does not ask the model to choose:

- injection surface;
- step order;
- tools or schemas;
- semantic thresholds;
- UCA type or unsafe outcome;
- observer kind; or
- success criteria.

`PresentationResult` is a closed map from requested slot ID to text. Missing,
extra or duplicated slots fail. The compiler escapes and inserts text; it does
not parse executable instructions back out of prose.

When bindings already provide complete messages, deterministic compilation
without a model is valid and preferred.

## 15. Garak adapter

### 15.1 Honest initial capability

The current Garak adapter may claim support for:

- ordered static transcript events;
- system, user and tool-result placement;
- an incorrect tool call;
- an incorrect tool argument; and
- an output-text condition, with explicitly documented observation limits.

It must initially report unsupported for:

- actual delay or duration;
- real timing windows;
- absence over a real time window;
- persistent-state mutation or comparison;
- multi-agent coordination;
- actuator effects not observable in the emitted call; and
- any outcome needing an unavailable observer.

Simulated timestamps or prose instructions do not make temporal behavior
supported.

### 15.2 Garak planning

The Garak plan fixes:

- transcript turn order;
- exact injection role;
- tool declaration and call pairing;
- bound tool name and schema;
- unsafe argument or output condition;
- detector implementation; and
- trace handles.

The existing one-shot prompt that creates transcript and detector meaning from
the full scenario is not used for STPA bundles.

### 15.3 Garak compilation

The compiler deterministically produces the platform JSON and validates:

- every source step appears exactly once and in order;
- tool calls and tool-result messages pair correctly;
- all used tools occur in the bound schema;
- unsafe values come from semantic bindings or declared projection values;
- the detector derives from the bound unsafe condition;
- no unbound placeholder remains; and
- traceability metadata is complete.

## 16. Compiled artifact and traceability

Every platform artifact shall carry or accompany a closed trace record:

```yaml
source:
  bundle_digest: <digest>
  projection_schema_version: stpa-execution-projection-v2
  projection_semantic_digest: <digest>
  scenario_content_sha256: <digest>
  run_id: <id>
  scenario_id: <id>
  candidate_id: <id>
  ica_slot_id: <id>
  ica_id: <id>
binding:
  binding_set_id: <id>
  semantic_digest: <digest>
compiler:
  platform: garak
  adapter_version: <version>
  compiler_version: <version>
step_map:
- artifact_element_id: turn-1
  projection_step_id: S-1
  factor_id: CF-1
oracle_map:
- artifact_oracle_id: detector-1
  condition_ref: OUTCOME-1
```

Changing projection or binding identity produces a different compiled artifact
identity. Presentation-only text changes are recorded through an author result
digest and compiler output digest without altering source projection identity.

## 17. Observation receipts

Runtime adapters may publish `execution-observation-receipt-v1`:

```yaml
schema_version: execution-observation-receipt-v1
artifact_digest: <digest>
projection_semantic_digest: <digest>
binding_set_digest: <digest>
execution_id: <id>
started_at: <timestamp>
completed_at: <timestamp>
observations: []
oracle_result: satisfied
receipt_digest: <digest>
```

Rules:

- receipts are append-only evaluation outputs;
- they reference but never modify source scenario, projection or binding
  artifacts;
- raw secrets and credentials are excluded;
- observer facts retain event/time provenance; and
- an absent or inconclusive observation is not silently converted to a safe or
  unsafe result.

Runtime execution itself may remain outside the first implementation slice;
the receipt contract should be specified before adapters persist results.

## 18. CLI contract

The primary command becomes:

```text
asago-artifact-generator generate \
  --bundle <run>/execution-bundle.json \
  --bindings <bindings.yaml> \
  --platform garak \
  --output-dir <directory>
```

Useful modes:

```text
--readiness-only    validate, bind and plan without authoring or compilation
--no-llm            compile only when all presentation slots are already bound
--entry SCN-001     select a scenario entry after bundle verification
```

CLI rules:

- The bundle argument is explicit; input shape is never autodetected.
- Bundle verification occurs before binding parsing that depends on its
  identities.
- Readiness output is produced for every selected entry.
- Model clients are not created until an entry is `ready` and requires
  presentation authoring.
- `--force` cannot bypass STPA source, binding, readiness or compiler errors.
- Batch processing may compile ready entries while reporting other entries as
  unbound or unsupported, but the manifest retains exact denominators and
  states.
- Exit status is nonzero for invalid source or failed compilation. A separate
  configurable policy may decide whether unbound/unsupported entries make a
  batch command nonzero, but their statuses cannot be hidden.

If historical taxonomy-era artifact generation is temporarily retained, move
it to an explicit `generate-legacy` command. It remains clearly marked
heuristic and cannot share the authoritative STPA adapter. Do not add automatic
fallback from STPA to legacy parsing.

## 19. Output layout

Recommended output:

```text
runs/<source-run-id>/
  artifact-manifest.json
  SCN-001/
    readiness.json
    execution-plan.json
    SCN-001-garak.json
    artifact-trace.json
    validation.json
    observations/
```

Write order for a ready artifact:

1. readiness result;
2. canonical execution plan;
3. compiled platform artifact;
4. validation result and trace;
5. atomically updated batch manifest.

No platform artifact is written for invalid, unbound or unsupported entries.
Readiness diagnostics may still be written.

## 20. Suggested implementation modules

Names may vary, but responsibilities shall remain local:

- `contracts/stpa-execution/`: vendored producer wire contract;
- `models/execution_intent.py`: frozen normalized intent;
- `models/runtime_binding.py`: binding and review models;
- `models/readiness.py`: independent readiness axes and diagnostics;
- `bundle/loader.py`: strict path, byte, schema, digest and identity verification;
- `planning/bind.py`: pure `bind_and_plan` implementation;
- `platforms/base.py`: internal platform plan/compiler interface;
- `garak/capabilities.py`: deterministic Garak facts;
- `garak/plan.py`: ready intent to typed Garak plan;
- `garak/compile.py`: deterministic platform JSON compiler;
- `authoring.py`: optional presentation-author seam;
- `trace.py`: compiled trace and receipt contracts; and
- `cli.py`: orchestration through the three public interfaces.

Do not extend `extract.py` or `garak/classify.py` to parse STPA execution
semantics. Their prose and taxonomy inference remains legacy-only until
deleted.

## 21. Acceptance specification

Feature scenarios shall prove:

### Bundle ingestion

1. A vendored valid bundle produces an immutable execution intent.
2. Unknown schema versions fail before model-client construction.
3. Bundle, scenario, projection or semantic digest mismatch fails closed.
4. Scenario/projection/index identity mismatch fails closed.
5. Absolute paths, traversal and symlink escape fail closed.
6. Unknown fields, duplicate JSON keys and runtime-observation contamination
   fail closed.
7. Narrative and Gherkin changes cannot alter execution intent.

### Binding and readiness

8. A fully declared projection without bindings reports the exact missing
   runtime bindings.
9. An unresolved semantic condition reports `needs_semantic_binding`.
10. An operator-reviewed semantic binding completes only its exact condition.
11. A model-authored or unreviewed semantic value is rejected.
12. A binding-set projection-digest mismatch is invalid.
13. Missing surfaces, tools or observers report `needs_runtime_binding`.
14. Unsupported requirements remain `unsupported`; they are not weakened.
15. All readiness axes and diagnostics remain visible together.
16. Only a completely bound and supported intent produces a ready plan.

### Authoring and compilation

17. Invalid, unbound or unsupported entries make zero author/model calls.
18. A presentation author can fill only requested text slots.
19. Missing or extra author slots fail compilation.
20. The author cannot alter surface, tool, sequence, condition or oracle.
21. Deterministic no-model compilation works when content is pre-bound.
22. Every compiled turn/action maps to a projection step.
23. Every detector maps to the exact unsafe-outcome condition.
24. Tool declaration/call/result and bound schema consistency are validated.
25. `--force` cannot publish an invalid STPA artifact.

### Garak capability honesty

26. Ordered user/tool-result scenarios may be supported when fully bound.
27. Forbidden tool calls and arguments compile from exact bindings.
28. Real delay, duration, window and absence scenarios are unsupported without
    a clock-capable adapter.
29. Persistent state and multi-agent requirements are unsupported by the
    current Garak adapter.
30. Simulated timestamps in prose never change support to supported.

### Traceability and compatibility

31. Output retains source, binding, compiler, step and oracle digests.
32. Runtime receipts are separate and cannot mutate source files.
33. Vendored conformance fixtures match `CONTRACT.lock`.
34. Tests pass without a sibling scenario-generator checkout.
35. Legacy input, if retained, is accepted only through `generate-legacy` and
    is labeled heuristic.
36. The primary `generate` command never falls back to legacy inference.

Acceptance mutation testing belongs after the complete producer and consumer
path is operational. Initial implementation shall run Gherkin, focused tests,
DRY and CRAP gates without spending a long cycle hardening partially built
modules.

## 22. Cross-repository acceptance

The coordinated acceptance fixture is generated deterministically by the
scenario generator and vendored here. It shall contain:

- one fully declared projection that Garak can execute;
- one projection requiring semantic binding;
- one projection requiring runtime binding;
- one valid projection unsupported by Garak;
- one malformed projection;
- one mismatched scenario/projection pair;
- one mismatched binding digest; and
- one unknown schema version.

The cross-repository gate proves:

1. producer canonical bytes match vendored fixture bytes;
2. producer and consumer agree on all valid/invalid cases and violation
   categories;
3. a ready entry compiles without semantic drift;
4. unsupported or unbound entries produce no platform artifact;
5. no model call occurs before readiness; and
6. no test depends on network access or a sibling checkout.

## 23. Versioning and contract updates

- Supported producer versions are explicit configuration, not best-effort
  parsing.
- An unknown projection or bundle version is invalid.
- Updating the vendored contract requires updating `UPSTREAM.lock`, fixtures,
  parser tests and compatibility documentation in one change.
- The consumer may support several frozen versions through separate adapters;
  it must not normalize an unknown version through a permissive dictionary.
- Runtime-binding and observation-receipt contracts have their own versions and
  digests because they are consumer-owned.
- Old outputs are never rewritten to claim a newer contract version.

## 24. Implementation sequence

1. Vendor the producer schema, fixtures and lock file.
2. Add acceptance scenarios for strict loading, readiness and zero pre-ready
   model calls.
3. Implement `load_execution_bundle` and immutable `ExecutionIntent`.
4. Implement `RuntimeBindingSet`, review evidence and framed digest.
5. Implement platform capabilities and pure `bind_and_plan`.
6. Add the Garak capability adapter and typed ready plan.
7. Replace semantic-inventor generation with presentation-only authoring and a
   deterministic compiler.
8. Add artifact traceability and manifests.
9. Cut the primary CLI over to explicit bundle input.
10. Move historical taxonomy behavior to `generate-legacy` only if it still has
    a concrete user; otherwise delete it.
11. Run the coordinated producer fixture end to end.
12. Update README, architecture documentation and operational examples.
13. After the entire path works, run focused mutation hardening.

## 25. Quality gates

Before consumer completion:

- generated Gherkin acceptance passes;
- focused and full deterministic tests pass;
- DRY analysis finds one digest recipe, one bundle loader and one readiness
  planner;
- changed functions have CRAP at or below the repository threshold;
- Ruff check and format pass;
- vendored fixture and lock digests are reproducible;
- deterministic tests make zero network/model calls;
- invalid, unbound and unsupported cases prove zero author/model calls;
- a normal producer bundle compiles at least one honestly supported Garak
  artifact; and
- unsupported producer projections remain explicit rather than approximated.

## 26. Non-goals

- changing producer causal factors, ordering, UCA type or scenario admission;
- importing producer Python models;
- inferring platform behavior from narrative, Gherkin, taxonomy IDs or
  structural ID prefixes;
- inventing semantic thresholds or unsafe values;
- treating a valid projection as automatically executable;
- claiming real temporal execution from prompt timestamps;
- storing secrets in binding or artifact files;
- mixing observations into source projections; or
- preserving legacy heuristic generation through the primary command.

## 27. Completion condition

The consumer work is complete when the artifact generator independently
verifies a producer bundle, reports semantic-binding, runtime-binding and
platform-support readiness without model assistance, compiles only a fully
ready intent into a platform artifact without changing its sequence or unsafe
outcome, and retains exact traceability from every artifact element and oracle
to the projection and binding set.
