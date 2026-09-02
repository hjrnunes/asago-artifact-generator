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

`planning.bind.bind_and_plan` accepts an `ExecutionIntent`, an optional
`RuntimeBindingSet`, and `PlatformCapabilities`. It has no filesystem,
provider, model, plugin, or network behavior. Semantic bindings identify an
exact producer `condition_ref` and `binding_ref`; they fill only the typed
placeholder value. Every temporal factor condition is independently bound to
an observation and is included in the resulting observer plan.

Readiness retains independent source, semantic-binding, runtime-binding, and
platform-support axes. Overall precedence is `invalid`,
`needs_semantic_binding`, `needs_runtime_binding`, `unsupported`, then `ready`.
Only a ready result carries `ReadyExecutionPlan`. That plan includes all
source identities, lowercase SHA-256 bundle/projection/source-byte/binding
digests, fixed ordered steps, reviewed semantic values, required observers,
typed clock facts, and frozen presentation context. Platform compilers consume
the plan only after this seam succeeds.

## Vendored contract

The producer-owned schemas and deterministic conformance metadata live under
`contracts/stpa-execution/`. `CONTRACT.lock` identifies the vendored schema
versions and files; `UPSTREAM.lock` records the producer revision. Refresh both
locks and the fixture tree together when the producer publishes the
authoritative v2 conformance kit.
