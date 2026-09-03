# STPA Execution Case Binding and Compilation Specification

Status: approved implementation companion to
`stpa-execution-classification-and-binding-spec.md`
Date: 2026-09-03
Repository: `asago-artifact-generator`

## 1. Decision

The artifact generator consumes the producer's semantic execution contract and
classification, verifies it, and resolves it against one explicitly supplied
execution target profile. The result is one immutable `BoundExecutionCase`, or
one typed `ExecutionCaseExclusion`. The resolver is deterministic, offline and
does not invent tools, operations, delivery paths, causal factors, unsafe
outcomes or oracles.

The bound case is a layer between a verified STPA projection and platform
readiness:

```text
verified bundle -> ExecutionIntent -> bound execution case
                                      -> runtime bindings/readiness
                                      -> Garak/PyRIT artifact
```

`PlatformCapabilities` remains a description of what an adapter can do. A
`TargetExecutionProfile` is a description of one reviewed target or explicit
simulation environment. `RuntimeBindingSet` remains the deployment binding
for surfaces, values, observers and clocks. These are separate authorities.

## 2. Consumer models

Add closed, frozen models in `models/execution_case.py` and
`models/target_profile.py`.

`TargetExecutionProfile` contains:

- `schema_version: execution-target-profile-v1`;
- `profile_id` and `environment_id`;
- `basis: target | simulation`;
- `authority: reviewed | inferred`;
- `inventory_completeness: unknown | inferred_partial | reviewed_complete`;
- reviewed evidence references;
- a unique, ordered set of typed resources; and
- a semantic digest over the complete document excluding that digest.

Each resource contains a stable resource ID, kind, role IDs, structural
references, attacker-influence state, supported surfaces and typed operations.
Each requirement names a nonempty set of `required_surfaces`; a profile match
must provide every one of those surfaces. Operations contain an operation ID,
semantic operation, argument names and observable properties. No credentials,
secret values or live endpoint configuration are stored in the profile.

`BoundExecutionCase` contains:

- source run/scenario/candidate/ICA identities and source digests;
- the producer binding-completeness/environment-basis classification and its
  digest;
- the selected profile identity, basis, environment and digest when present;
- exact requirement-to-resource/operation bindings;
- the resulting claim scope; and
- a content digest over the complete case excluding that digest.

`ExecutionCaseExclusion` contains the source identity, classification, one
closed exclusion code, exact requirement IDs and deterministic diagnostics.
Codes are `analytical_only`, `needs_target_binding`, `ambiguous`,
`unsupported`, `invalid_profile` and `invalid_source_binding`. Runtime
binding is intentionally not a case-resolution outcome: a semantic case may
be concrete while deployment credentials, locators, values or observers are
still incomplete. That later state belongs to `bind_and_plan`.

## 3. Public resolver

Expose one pure typed seam:

```python
def resolve_execution_case(
    intent: ExecutionIntent,
    profile: TargetExecutionProfile | None,
) -> BoundExecutionCase | ExecutionCaseExclusion:
    ...
```

Raw dictionaries, prose, taxonomy IDs and platform defaults are rejected as
binding authority. The resolver does not construct providers, read files,
contact a network, author text or compile an artifact.

Resolution rules:

1. Reject an invalid or tampered source classification before matching.
2. `analytical_only` returns `analytical_only` without profile or runtime
   processing. Its source classification is internally consistent only when
   `binding_completeness=analytical_only`, `environment_basis=none`,
   `profile_fit=invalid`, and `claim_scope=no_execution_claim`, with no
   bindings or target-profile pin.
3. A concrete target-agnostic case requires no domain resource and binds with
   no target profile.
4. A parameterized case with no profile returns `needs_target_binding`.
5. A target profile must have a valid digest and reviewed authority for a
   target-specific claim. An inferred profile can support parameterized
   analysis but cannot establish a target claim. An explicitly selected,
   complete simulation profile may be inferred: its deterministic behavior is
   still an explicit simulation fact, not a real-target claim.
6. An exact producer-selected resource ID is checked against the selected
   profile and its operation. With no profile it remains unresolved; with a
   profile, a missing or incompatible exact ID is `invalid_source_binding` and
   no fallback is allowed. An exact resource with its own reviewed evidence
   may resolve in an otherwise partial inventory.
7. A late-bound requirement matches only exact resource kind, role, required
   surfaces, semantic operation, required properties, attacker-influence
   state and structural owner reference. `requirement.operation` equals the
   profile operation's `semantic_operation`; the bound operation is that
   operation's exact `operation_id`. Names and descriptions are never matching
   evidence.
8. A role-based match resolves only in a `reviewed_complete` inventory. Zero
   or one matches in unknown/partial inventory is `needs_target_binding`; zero
   in reviewed-complete inventory is `unsupported`; multiple matches is
   `ambiguous`.
9. A simulation requires an explicitly selected simulation profile and a
   complete simulation behavior for every selected resource. Inferred
   authority is allowed only for this explicit simulation basis; missing
   target information never creates a mock profile.
10. The resolver preserves delivery class, selected factor, owner reference,
    operation, unsafe outcome and oracle identity byte-for-byte. It never fans
    out silently; one scenario contains one route and one action path.

## 4. Readiness and compilation

`bind_and_plan` consumes a bound case plus the separate runtime binding set and
keeps semantic binding, runtime binding and platform support as independent readiness axes. The existing
target-agnostic v2 path remains available while the producer contract is
updated in place; it must not fabricate a bound case from prose.

`ReadyExecutionPlan`, Garak plans, compiled artifacts, `ArtifactTrace` and
observation receipts pin the bound-case ID/digest, execution classification,
claim scope, selected profile/environment and profile digest. For a simulation,
they also carry the exact selected resource IDs and their immutable
`simulation_behavior` entries, so the compiled case is reconstructible rather
than identified only by a profile digest. A simulated artifact is visibly
simulation-scoped and can never claim target integration.

An exclusion produces a manifest entry and no author, compiler, provider or
network call. It does not produce a platform artifact.

## 5. CLI and persistence

The primary `generate` command accepts an explicit typed target-profile path.
For each entry it writes either the bound-case sidecar followed by normal
readiness/plan/artifact outputs, or an exclusion sidecar. Counts distinguish
concrete, parameterized, simulated, analytical-only, ready, unsupported,
ambiguous and unresolved cases; no blended score is emitted.
The closed manifest summary nests readiness categories under
`counts.readiness` and records `counts.execution_case_excluded` plus
`counts.analytical_only` independently. An analytical-only entry does not enter
any readiness bucket because readiness was never attempted.

Several artifacts for one scenario are possible only when the caller supplies
several explicit profiles or named alternatives. The CLI never chooses among
multiple matches implicitly.

## 6. Required verification

Tests must cover exact typed inputs, profile and case digest/tamper checks,
target-agnostic direct prompts, parameterized no-profile cases, exact target
matches, unknown/partial versus reviewed-complete zero matches, ambiguity,
stale exact IDs, explicit simulations, analytical exclusions, prose/tool-name
non-authority, preservation of producer semantics, no author/compiler calls for
exclusions, deterministic ordering, and explicit multi-profile behavior.

Contract tests consume only the vendored producer kit. The producer's revised
projection/bundle schemas and fixtures are synchronized byte-for-byte and the
consumer `UPSTREAM.lock` is refreshed. Source mutation is deferred; run Ruff,
format, diff, deterministic tests, DRY and CRAP after the path is operational.
