# Asago Artifact Generator — agent guide

Turn supplied scenarios into useful, testable artifacts. Keep this guide short;
contracts and historical experiment details belong in their own documents.

## Start here

- Check the working directory, branch, and `git status --short`. Use the checkout
  named by the task; several worktrees contain different implementations.
- Read the current task specification, the relevant README section, and the
  implementation. User decisions take precedence over superseded designs.
- Preserve unrelated changes and historical evidence. Stage only your own work;
  commit or push when requested.

## Key concepts

- **Scenario** — the producer's statement of one failure: narrative, attack tree,
  Gherkin, failure criterion and safe alternative. You do not invent it.
- **Artifact package** (`artifact-package-v5`) — the frozen test material
  authoring returns: stimulus, setup, bindings, checks and judge facts.
- **Planted item** — the lower-trust data item an indirect attack uses to carry
  the payload.
- **Injection spec** (`InjectionSpec`) — the runtime description of one injection
  test; the MiDojo adapter compiles a planted-item package into one.
- **Slot** — a place in the target's data that can hold a payload;
  `slot_writable_by` names the parties that can write it.
- **Controlling party** — the party the scenario assumes controls the planted
  item; `shape_controlling_party` states it.
- **Binding placeholder** — a `{{name}}` in the user text that preparation fills
  from a binding.
- **Judge** — a model-based assessment of a check, run from a judge spec and its
  rubric; the judge spec is data, not code.
- **Oracle** — the package's rule for the unsafe outcome: a tool-call condition
  or a rubric judge.

## Ownership and workflow

- The producer supplies scenario meaning: narrative, attack path, Gherkin,
  failure criterion, safe alternative, and metadata.
- This consumer designs concrete stimuli, setup declarations, runtime bindings,
  and semantic judge specifications using supplied facts and documented
  capabilities. Packages (`artifact-package-v5`, from a `scenario-handoff-v5`
  input) contain no detector code; the loader refuses a v4 package.
- `generate` is target-free. The plan owns the experiment; artifact authoring turns
  the accepted plan into a frozen package. Code checks run before the applicable
  semantic review. Corrections stay within their stage.
- Downstream tooling alone performs live setup, target/Garak generation and
  detection, runtime judging, evidence collection, and cleanup. This repo does not do those actions
  during authoring.
- Preserve saved package and evidence formats where required by the task. Do
  not redirect new work into an older workflow because its code still exists.

## Commands

Run from this checkout; read `generate --help` for configuration and limits.

```bash
uv sync --locked
uv run asago-artifact-generator generate --help
uv run asago-artifact-generator report --help
uv run pytest tests/path_to_changed_test.py -q
./scripts/quality.sh
uv run pytest tests/ -q
```

Use focused tests during implementation and the required broad gates once at
final delivery. Repeat only for relevant changes or unresolved failures.
Deterministic tests must not contact a model endpoint or target.

The `dev` dependency group names the producer sibling as an editable path source,
`../asago-scenario-generator`, so `uv sync --locked` needs that checkout next to
this one. `tests/test_handoff_writer_reader_pair.py` drives the producer's real
handoff writer through it; without the sibling the module skips and names the
missing path.

## Model-facing work

- Let the LLM design and reason. Code validates structure, resolves documented
  references, preserves accepted inputs, and executes the declared checks. Do
  not replace semantic reasoning with keyword or phrase matching.
- Give every requested field, identifier, source, and destination an explanation
  and a consistent example. Inspect the actual dispatched system/user messages.
- Keep generic instructions separate from scenario facts and failure feedback.
  Case-specific repairs are assisted results, not proof of autonomous design.
- Deliver owner-specified correction text verbatim. Include the exact previous
  output and all relevant validation findings. Preserve
  valid sibling fields; a shortened paraphrase can change the requested fix.
- An accepted plan is fixed during artifact authoring. If it needs revision,
  report that explicitly rather than silently changing the experiment.
- Keep final content, reasoning, finish reason, and usage separate. Missing final
  content is not proof that no tokens were generated. Never parse reasoning as
  a substitute final answer.
- A review is fallible evidence. Check findings against the supplied contract;
  an acceptance still needs deterministic checks. Keep genuine
  unknowns explicit rather than weakening tests to obtain a pass.

## Live requests and saved work

- Before dispatch, apply the current task's provider/data approval and explicit
  request allowance. Send an endpoint only the project data the owner approved
  for it, never real customer data, and do not publish the material or use
  another provider without approval.
- Read profile credentials in process; keep them out of prompts, packages,
  logs, shell output, and Git. Use the task's explicit profile path.
- Record actual model, thinking, token limits, finish state, and spend. Thinking
  is a per-role control, not a universal improvement. Respect the request's
  context limit and stop policy; do not enlarge allowances. Every physical
  request goes through `RequestBoundary.dispatch` in `authoring/transport.py`,
  the only caller of the SDK's create method: it reserves budget through the
  dispatch's `RequestSession`, records the attempt with its reason, and sends
  once. Retry and fallback rules only ask for another dispatch. The transport
  makes one recorded retry after a transport error (HTTP 5xx or a non-timeout
  connection error) and nothing else retries: not timeouts, 4xx, or invalid
  responses. The retry appears in the ledger as `transport_retries`; SDK
  `max_retries` stays 0. The one exception is the service-tier fallback after
  a 429: it appears in `transport_retries` with `"reason":
  "service_tier_fallback"` (a transport retry carries no reason). The replay
  gate replays both from a recording. A review response that fails its checks
  (one stopped at the token limit included) is not a transport retry: the
  review stage asks again as a new dispatch, bounded by the stage's
  review-retry allowance (`allowance: "review_retry"`).
- Reuse intact accepted plans or artifacts when authorized. Verify their exact
  hashes and provenance; do not regenerate them merely because a later stage
  failed. A new task name does not reset historical spend.
- Preserve saved failures and frozen packages. New attempts use new evidence
  locations. Do not hand-edit generated output and label it model-authored.

## Where to look and when to stop

- `README.md` and CLI help: authoring usage, profiles, policy, and package format.
- `src/asago_artifact_generator/authoring/`: `orchestrator.py` runs the
  stages; `prompt_context.py`, `prompt_packets.py`, `review.py`, and
  `correction.py` build prompts; `contracts.py` and `checks.py` hold response
  contracts and structural checks; `transport.py` is the provider client.
- `src/asago_artifact_generator/binding_rules.py` and `bindings.py`: re-exports of the
  binding and slot rules the `asago-package` member owns. `asago_package.binding_rules`
  holds the rules authoring and package preparation share, and the one
  declaration-validation engine (`check_declarations`): a `BindingPolicy` names its
  checks and their order, and each caller words the `Violation`s. `asago_package.bindings`
  adds the binding sources and destinations. Authoring's policies sit beside its wording
  (`asago_package.bindings.AUTHORING_POLICY`, `checks._FINDINGS_POLICY`); change a rule
  in the engine, for both.
- `src/asago_artifact_generator/package_io.py`: a re-export of the artifact-package-v5
  writer and loader the `asago-package` member owns. The loader refuses any other schema
  version, naming it.
- `src/asago_artifact_generator/authoring/condition_eval.py`: a re-export of the
  tool-call-condition-v2 validator and evaluator the producer's
  `packages/asago-contracts-core` member owns (`asago_contracts_core.condition_eval`,
  re-exported as `asago_handoff.condition_eval`). Authoring's self-test,
  `tests/test_condition_eval.py` and the injection-spec compiler read it; orch's probe names
  the same module.
- `packages/asago-package/`: the package library workspace member (`asago_package`), which
  depends only on pydantic, jsonschema and `asago-contracts-core`. It owns `package_io` (the
  v5 writer, loader and values), the contract kit (canonical JSON, framed digests, schema
  errors, lock checks, the package's own `contract_dir`), the `metadata_policy` secret
  walker, and the binding and slot rules (`binding_rules`, plus authoring's `bindings`). It
  ships the authority copies of `artifact-package`, `runtime-contract` and `capability-gap`
  as package data under `src/asago_package/contracts/`, the only authority copies in this
  repository; the app's `contract_dir` keeps serving the seven kits the consumer still owns.
  `runtime_contract_spec` reads every runtime contract through the installed spec set, and
  the app's loader and the producer's builder both call it.
- `packages/asago-bundle-core/`: the tool-neutral core every tool adapter builds on (the
  `tool-bundle-v1` runtime spec, slot markers, the capability-gap record, `BundleError`, the
  `testing` package builders, the `mcp_records` readers of the orchestrator's boundary
  records, and the `conformance` helper that round-trips `compile`, `instantiate` and
  `parse` of an adapter and returns failure messages). `receipt_contract` and
  `bundle_contract` ship the consumer's `execution-receipt-v1` and `tool-bundle-v1` specs as
  package data and read every receipt and bundle an adapter writes through `SpecSet.read`;
  `contracts/execution-receipt/` and `contracts/tool-bundle/` are the authorities, and a
  test refuses a copy that differs. `schema.manifest_errors` reads a manifest through
  `bundle_contract` before it applies the schema, and `check_round_trip` reads the receipt
  it parses through that reader; both adapters' `parse` reads back the receipt it writes. A
  new bundle adapter runs `check_round_trip` in its tests; the MiDojo adapter writes
  injection specs, not bundles.
  `preparation.prepare_package(package, call_target=...)` prepares a package for one run:
  it admits the setup and binding declarations against the package's inventory and runtime
  contract (no target call), runs setup through a target call (real writes, not graded,
  returned as a ledger), resolves the bindings, checks the prerequisites, renders the
  stimulus and returns the judge's facts, or an incomplete reason. So far only the Garak
  adapter's public `instantiate` runs it; the lower-level template filler
  (`instantiate_bundle`) takes values already resolved. Recording the result is the
  caller's job. `target`
  holds the target config an adapter's `instantiate` takes (a `target.yaml` path or a loaded
  mapping): one whose `authorized_as` is not `mock` or `test_instance` is refused before any
  work. `mcp_call_target` sends a setup step to the target's MCP server over streamable HTTP
  or, when the `mcp_url` path ends in `/sse`, legacy SSE; the Garak `instantiate` uses it for
  `endpoint.mcp_url` by default, and a caller's `call_target` replaces it. Each setup ledger record keeps the decoded `result` and the raw
  MCP result as `native_result` (null for a `call_target` returning a plain dict). The
  optional schema fields `repeats` and `serve` stay out of a bundle that does not need them:
  Garak omits both, because `bundle.json` feeds the receipt's `bundle_digest`.
- `packages/asago-bundle-garak/`: the Garak adapter workspace member. Its library entry points
  `compile`, `instantiate` and `parse` (`entry_points.py`) are what its CLI calls. The public
  `instantiate` takes the target config and `RunSettings`, checks every input and the whole
  template (`read_template`) before the first setup call, runs `prepare_package`, and fills
  that same template with the resolved values (`fill_template`, which `instantiate_bundle`
  also uses). The bundle's MCP server is the authorized config's `endpoint.mcp_url`.
  Its CLI `instantiate` requires `--package`, `--target` and `--run`.
  A `planted_item` package takes the IPI route: `compile --target` writes `ipi-episodes.json`
  (one `{injection_spec, placements, metadata}` entry, the injection spec the shared
  injection-spec library's `injection_spec_document` builds and that envelope's
  `placements`; the file keeps its name)
  and a template named
  `garak-ipi-episode` that runs
  `midojo.InjectionSpecProbe` with `midojo.EvaluationVerdict`, `midojo.EvaluationEffect` and
  `toolcall.ToolCallCondition`, one generation against that `endpoint.mcp_url`; a package
  the injection-spec compile cannot place is a `garak`-tooled capability gap. The manifest
  `files` list names the static bundle files `instantiate` copies in beside the templates.
  `parse` reads an IPI report into a receipt where an unknown, invalid or undelivered chain is
  inconclusive, carries the attempt's `source_metadata` and refuses a report whose
  `source_metadata` package digest or ID is not the bundle's own. A reply episode's
  `judge.verdict` states that same decision in the receipt's judge vocabulary:
  `supported` for a detection, `contradicted` for a decided resistance, and `unresolved`
  for every other grade, so an obeyed or undecided reply never reads as resisted. A report
  of one chain also fills the receipt's `episode` block: route `garak.midojo.InjectionSpecProbe`,
  the evaluation and snapshot the probe sealed, the evidence status of that snapshot, and
  its verdict, which states `attempted`, `blocked` and `effect_observed` as sealed; a chain
  sealed short names its evaluation but no snapshot or verdict, and a report of several
  chains leaves the block out. A report recorded before the rename names the old plugins and
  notes; `parse` reads both forms. `parse` exits 1 and names the path on stderr
  when the bundle directory or the report is not there: missing input is not a failed run.
  **Garak planted receipt.** The `episode` block is a planted-item receipt's evidence
  record, and every member comes from the trace the probe sealed, never from the run's
  intent. `route` is `garak.midojo.InjectionSpecProbe`; `evaluation_id` and `snapshot_id`
  name the evaluation and the snapshot MiDojo sealed and graded; `evidence_status` is that
  snapshot's own map of how completely each source was collected; `verdict` states the
  grade as sealed, with `security` three-valued and `validity` (`valid`/`invalid`) saying
  whether the grade could be decided, the reason it gives, and `attempted`, `blocked`,
  `effect_observed` and `injection_delivered` (each true, false or null when the seal does
  not state it); `delivery` is `midojo.delivery.delivery_facts` of the plan and the sealed
  calls, one fact per instruction placed on a call; `exposure` is `midojo.delivery.exposure`
  of the same, stating per boundary whether the payload was `exposed`, `not_exposed` or
  unknown; and `call_injections` copies each sealed call with its `injection_outcomes`, each
  carrying `payload_sha256` (the digest of the payload text before its transformation) and
  `slot` (`id`, `record`, `mode`: the seed slot the payload was placed in). A chain sealed
  short names its evaluation but no snapshot or verdict, and a report of several chains
  leaves the block out. The delivery guard decides the outcome before the grade does: a
  security grade of false whose payload never reached the agent, and a false grade whose
  delivery nothing recorded, are `inconclusive` and their reason names the delivery, never
  `not_detected`. So a report that records no delivery fact cannot read as resisted,
  whatever the target did. A seal the reader cannot verify fails the receipt with MiDojo's
  own reason and states no attempt. That covers a snapshot or verdict that is missing or
  malformed (`snapshot_missing`, `snapshot_malformed`, `verdict_missing`,
  `verdict_malformed`), a tampered seal whose digest does not hold
  (`snapshot_digest_mismatch`), and one that names another evaluation
  (`delivery_mismatch`): a verdict read from a seal nothing verified would decide an attack
  on unverified evidence. A chain the probe never sealed, or a generation that errored,
  keeps its attempt and fails as `interrupted` or `generation_error`. A sealed chain whose
  release MiDojo's service never acknowledged keeps its attempt and its verdict too, and
  fails as `release_lost`: the probe records the loss on its trace, so the receipt never
  reports a clean completion. Together the fixture
  README and the parity test are the executable spec of that receipt:
  `packages/asago-bundle-garak/tests/fixtures/ipi/README.md` names each case's expected
  outcome and what each sealed file holds, and
  `packages/asago-bundle-garak/tests/test_bundle_garak_parity.py` reads all seven cases
  through `bundle-garak parse` and `bundle-midojo parse --delivery`, requiring the same
  `evaluation_id`, `snapshot_id`, `evidence_status`, `delivery`, `exposure`, `verdict` and
  `call_injections`, the same `execution_status` and incomplete reason, and the same
  attempts, with the tampered case stating no attempt on either route.
  `packages/asago-bundle-garak/tests/fixtures/ipi/` pins those seven sealed cases
  (delivered, undelivered, incomplete, blocked, sequential, reply-judged and a tampered
  snapshot), each with the report, the snapshot, the verdict and the delivery document
  MiDojo produced, plus `expected.json` naming the outcome the route must state.
  `tools/fixture-generators/garak_ipi.py` reseals them with the MiDojo interpreter
  (`--check` reproduces every file byte for byte), and its README says what each case is
  real evidence of and what the report composes.
- `packages/asago-injection-spec/`: the injection-spec library workspace member
  (`asago_injection_spec`): the compiler from a `planted_item` package to one MiDojo
  injection spec, the consumer envelope, the placement and reachability checks, the
  sealed-result readers, and the judge-turn vocabulary compile and the judge service share.
  Both adapters read it, so neither depends on the other; `bundle-garak`'s parity test is the
  only test that also reads `bundle-midojo`, and it is marked `parity` because the MiDojo
  adapter is a test-only dependency.
- `packages/asago-bundle-midojo/`: the MiDojo adapter workspace member (the compile and
  parse CLIs and the judge service). Its deprecated modules re-export the injection-spec
  library under the old paths until M9. Its revision history, fixture provenance
  and implementation notes are in `docs/midojo-changelog.md`.
- Each `packages/*` member runs in its own pytest session, for example
  `uv run pytest packages/asago-bundle-core/tests -q` and
  `uv run pytest packages/asago-package/tests -q`.
- `tests/`: executable examples and regression coverage for the changed behavior.

Finish the requested slice once its behavior and required checks pass. Report
case outcomes, blockers, actual spend, and evidence paths. A passing suite is
not a successful experiment; a command attempt is not a completed effect.
Keep unrelated formatting, new frameworks, and speculative hardening out of the
critical path.

`AGENTS.md` is a symlink to this file. Edit `CLAUDE.md`; preserve the symlink.
