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
typed clock facts, and frozen presentation context. Platform compilers consume
the plan only after this seam succeeds.

Before readiness, `garak.default_bindings.complete_garak_runtime_bindings`
merges optional explicit bindings with deterministic adapter facts. Direct
prompts use `user_turn`; conversation-context stimuli use conversation history;
model output uses `chat_completion`, read-only `assistant_turn`, and an
`output_text` semantic observer. The adapter derives none of the target or
deployment facts it cannot know. It also binds surfaces only for the selected
stimulus factor and final target action; other structural factors stay in the
ready plan's provenance trace without becoming prompt messages.

## Vendored contract

The producer-owned schemas and deterministic conformance metadata live under
`contracts/stpa-execution/`. `CONTRACT.lock` identifies the vendored schema
versions and files; `UPSTREAM.lock` records the producer revision. Refresh both
locks and the fixture tree together when the producer publishes the
authoritative v2 conformance kit.
