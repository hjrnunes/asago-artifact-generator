# STPA execution-bundle consumer

The consumer accepts only the canonical JSON index for
`stpa-execution-bundle-v1`. `bundle.loader.load_execution_bundle` reads the
index and its run-relative scenario/projection files under conservative size
limits. It rejects duplicate keys, non-canonical JSON bytes, unknown fields,
unsafe paths, symlink escapes, digest or identity mismatches, runtime data in a
projection, and an index validation tuple other than
`{status: valid, validator_version: stpa-execution-projection-v2}`.

The return value is an immutable `VerifiedExecutionBundle`. Each entry carries
an `ExecutionIntent`, exact persisted byte digests, the projection semantic
digest, and frozen presentation-only context. The raw documents are retained
only as recursively immutable values for trace inspection; they are never an
authority for planning.

The inward intent preserves the producer's `semantic_proposition` exactly. The
field is present on every v2 unsafe outcome (nullable for machine-only
conditions), and model-output/output-text routes require a bounded non-empty
plain line. Executable outcomes also retain duplicate-free hazard and
constraint references, while the trace retains the exact selected loss
references. The consumer checks only projection closure: outcome references
must equal the corresponding trace references, and loss lineage must be
present. It does not decide whether the proposition is semantically true of a
hazard; that authority remains with the producer.

## Readiness seam

`planning.bind.bind_and_plan` accepts a resolved `BoundExecutionCase`, an
effective `RuntimeBindingSet`, and `PlatformCapabilities`. It has no filesystem,
provider, model, plugin, or network behavior. Semantic bindings identify an
exact producer `condition_ref` and `binding_ref`; they fill only the typed
placeholder value. Every temporal condition on the factor actively selected by
the adversarial stimulus is independently bound to an observation. Conditions
on other structural factors remain provenance and do not become runtime work.

Readiness retains independent source, semantic-binding, runtime-binding, and
platform-support axes. Overall precedence is `invalid`,
`needs_semantic_binding`, `needs_runtime_binding`, `unsupported`, then `ready`.
Only a ready result carries `ReadyExecutionPlan`. That plan includes all
source identities, lowercase SHA-256 bundle/projection/source-byte/binding
digests, fixed ordered steps, reviewed semantic values, required observers,
typed clock facts, frozen presentation context, and (for a profile-backed
target) the independent `inventory_authority` and `semantic_authority` axes.
Target-realized source pins are retained as `target_realization_digest` when
present. Platform compilers consume the plan only after this seam succeeds.

Before readiness, `garak.default_bindings.complete_garak_runtime_bindings`
merges optional explicit bindings with deterministic adapter facts. Direct
prompts use `user_turn`; conversation-context stimuli use conversation history;
model output uses `chat_completion`, read-only `assistant_turn`, and an
`output_text` semantic observer. The adapter derives none of the target or
deployment facts it cannot know, including internal agent channels and
indirect-content carriers. It also binds surfaces only for the selected
stimulus factor and final target action; other structural factors stay in the
ready plan's provenance trace without becoming prompt messages.

Readiness copies the exact proposition and hazard/constraint/loss references
onto each outcome observer. An `output_text` observer without the producer
proposition is `needs_runtime_binding`, even when all runtime bindings are
otherwise complete; a runtime binding cannot supply or replace that meaning.

## Case resolution

`planning.resolve_case.resolve_execution_case` accepts one verified
`ExecutionIntent` plus an optional `ExecutionTargetProfile`. A
target-agnostic, resource-free contract produces a `BoundExecutionCase`
without a profile. A resource-backed contract with an omitted requested basis
and no profile is retained as a typed `needs_environment_binding` exclusion:
no environment has been selected yet. Explicit target and simulation requests
produce the distinct `needs_target_binding` and `needs_simulation_binding`
exclusions when their profile is absent. Supplying a profile for an omitted
request explicitly selects that profile's basis and resolves the exact
requirement/resource/operation tuples. MCP target profiles must carry observed
inventory and inferred or reviewed semantic authority; their selected
`resource_id` and `operation_id` are matched exactly, without semantic
operation remapping. A complete simulation profile remains an explicit mock
contract with reviewed semantic authority and a simulation-only claim.
Target-realized projections carry paired `execution_target_profile` and
`target_realization` source pins; exact-resource contracts are rejected when
either pin is absent. Explicit profile requests cannot consume the opposite
profile basis.

The requested basis is therefore a four-state contract value, not a nullable
spelling of `target_profile`:

| Producer request | Domain resources | Supplied profile | Consumer result |
|---|---:|---|---|
| `target_agnostic` | no | ignored | model-only bound case |
| `null` | yes | none | `needs_environment_binding` |
| `null` | yes | target or simulation | resolve against the supplied basis |
| `target_profile` / `simulation_profile` | yes | matching basis required | bound case or basis-specific exclusion |

Resource-free executable contracts normalize to `target_agnostic`.
`target_agnostic` with domain-resource requirements is invalid source data.
The generic `null` case deliberately remains pending so the consumer never
turns an omitted environment choice into real-target authority.

## Vendored contract

The producer-owned schemas and deterministic conformance metadata live under
`contracts/stpa-execution/`. `CONTRACT.lock` identifies the vendored schema
versions and files; `UPSTREAM.lock` records the producer revision. Refresh both
locks and the fixture tree together when the producer publishes the
authoritative v2 conformance kit.
