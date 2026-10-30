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

## Ownership and workflow

- The producer supplies scenario meaning: narrative, attack path, Gherkin,
  failure criterion, safe alternative, and metadata.
- This consumer designs concrete stimuli, setup declarations, runtime bindings,
  and semantic judge specifications using supplied facts and documented
  capabilities. Packages (`artifact-package-v5`, from a `scenario-handoff-v5`
  input; the loader still reads `artifact-package-v4`) contain no detector code.
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
  transport's `attempt_gate`, records the attempt with its reason, and sends
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
- `src/asago_artifact_generator/bindings.py`: binding sources and destinations.
  `binding_rules.py` holds the binding and slot rules authoring and package preparation share,
  and the one declaration-validation engine (`check_declarations`): a `BindingPolicy` names its
  checks and their order, and each caller words the `Violation`s. Authoring's policies sit
  beside its wording (`bindings.AUTHORING_POLICY`, `checks._FINDINGS_POLICY`); change a rule
  in the engine, for both.
- `src/asago_artifact_generator/package_io.py`: the artifact-package-v5 writer and the
  loader, which also reads artifact-package-v4.
- `packages/asago-bundle-core/`: the tool-neutral core every tool adapter builds on (the
  `tool-bundle-v1` schema, slot markers, the capability-gap record, `BundleError`, the
  `testing` package builders, the `mcp_records` readers of the orchestrator's boundary
  records, and the `conformance` helper that round-trips `compile`, `instantiate` and
  `parse` of an adapter and returns failure messages). A new bundle adapter runs
  `check_round_trip` in its tests; the MiDojo adapter writes episodes, not bundles.
  `preparation.prepare_package(package, call_target=...)` prepares a package for one run:
  it runs setup through a target call (real writes, not graded, returned as a
  ledger), resolves the bindings, checks the prerequisites, renders the stimulus and returns
  the judge's facts, or an incomplete reason. So far only the Garak adapter's public
  `instantiate` runs it; the lower-level template filler (`instantiate_bundle`) takes
  values already resolved. Recording the result is the caller's job. `target`
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
  (one `{episode, metadata}` entry, the episode the MiDojo adapter's `episode_document`
  builds) and a template named `garak-ipi-episode` that runs `ipi_episode.IPIEpisode` and
  `ipi_episode.EpisodeVerdict` with one generation against that `endpoint.mcp_url`; a package
  the episode compile cannot place is a `garak`-tooled capability gap. The manifest `files`
  list names the static bundle files `instantiate` copies in beside the templates. `parse`
  reads an IPI report into a receipt where an unknown, invalid or undelivered chain is
  inconclusive, and carries the attempt's `source_metadata`. A report of one chain also
  fills the receipt's `episode` block: route `garak.ipi_episode.IPIEpisode`, the evaluation
  and snapshot the probe sealed, the evidence status of that snapshot, and its verdict; a
  chain sealed short names its evaluation but no snapshot or verdict, and a report of
  several chains leaves the block out. `parse` exits 1 and names the path on stderr when the
  bundle directory or the report is not there: missing input is not a failed run.
- `packages/asago-bundle-midojo/`: the MiDojo adapter workspace member. It maps a
  `planted_item` package to one MiDojo episode for a target config and parses the sealed
  result; MiDojo runs the episode, and the adapter holds no proxy or interception code. It
  targets MiDojo `m90/main` at `8e8208f` and names the range `>=8e8208f`, the first
  revision that reads the v1 episode document; a target can match the call's arguments from
  `b5b8c75`, the interceptor copies a placement into a text block that mirrors the
  structured content from `3824922`, an episode can carry a `result_record` target from
  `c0bee12`, and a reply from `cab2a35`. The adapter imports `midojo_sdk` only (the episode
  schema, the injection plan and the snapshot digest) and never `midojo`: its pinned SDK
  runs the consumer's checks, and the runner's MiDojo runs the episode. It plants by
  response injection. `compile_episode` (`episode_compile.py`, CLI `compile --target
  [--block TOOL]`) reads the target config's `tools-list.json` and `runtime-spec.yaml`
  (`declared.py`) and writes one `episode.json` as a package envelope (`document.py`,
  `asago-midojo-package-v1`): the Asago fields `package_id`, `scenario_id`,
  `package_digest`, `claim_level`, `delivery`, `placements` and `tool_revision_range`, and
  `episode`, exactly MiDojo's
  `midojo-episode-v1` document (`episode.py`, validated against
  `midojo_sdk.episode_schema`): host `interception` (the runner gives the interceptor the
  target's `endpoint.mcp_url`; the agent gets only the interceptor's endpoint and token, and
  neither it nor the target sees a MiDojo session), the allowed `tools` and their write
  disposition (`forward` by default), `verifiers` `[]`, and `Episode`'s fields as plain
  data with no environment. `read_envelope` also reads the older `midojo-episode-v2`
  wrapper, the bare episode with `tools` and `placements` at the top level, for one
  release, and refuses any other version, naming it. Each seed item becomes one strict
  `tool_output` instruction from its runtime contract slot (`placement.py`): carrier, field
  path, mode, `max_chars`, and the record by the slot's declared `record_key` (from the
  target's `runtime-spec.yaml` or the runtime contract, never inferred): a field inside a
  list through a record selector on that item property;
  a field outside any list, of a carrier whose output schema declares the result and every
  key above the field exactly `"type": "object"` (no array, scalar, union or nullable
  object), by the plain path and `call_arguments` `{record_key: record}` when its input
  schema requires `record_key` as a text argument, else `result_record` `{"field":
  record_key, "equals": record}` when its result object declares `record_key` as a text
  property (the host interceptor declares that capability). Any other field, or a slot
  without a `record_key`, is a gap. So is a record the conversation cannot lead the agent
  to (`reachable.py`): for a carrier matched by `call_arguments`, a user turn must name
  the record exactly as written, or an allowed tool whose object input schema is built only
  from keywords that cannot require an argument (any other keyword, such as a composition or
  `$ref`, makes it not count) must declare `record_key` as a text property in its result; a structural check, not semantic. A
  command attempt uses MiDojo's `tool_call_condition` predicate; a reply MiDojo's
  `reply_judge` (`security.reply_judge`: the judge's question as `claim`, a `rubric` of
  criteria, facts, `runtime_facts`, the user `turns` and `judge_turns`, `JUDGE_URL`/
  `JUDGE_TOKEN`, earlier turns and calls sent, 180 s per judged turn + 10), answered by
  `judge_service.py` (CLI `judge-serve`, 127.0.0.1; model from `JUDGE_MODEL_URL`,
  `JUDGE_MODEL`, `OPENAICOMPATIBLE_API_KEY`; token `JUDGE_SERVICE_TOKEN`), which runs
  `authoring/rubric_judge.py` per judged turn, three-valued, its model requests checked
  against Garak's (`tests/fixtures/rubric/`); a conversation that is not the rubric's
  turns is `unresolved`. Its `details` summarize each turn (no evidence or model text),
  the model key and token are redacted, and its log is method and status only. `verifiers` is `[]`; compile writes `episode.json` alone. Direct
  and user-turn packages, a single package with history, non-user history, run-time bindings,
  state_effect claims and a seed the interceptor cannot place in an MCP tool result are
  capability gaps (exit 3). `parse_sealed` (`sealed.py`, CLI `parse --snapshot --verdict
  [--delivery] [--route]`) reads the snapshot (`GET .../snapshot`) and the verdict (`evaluation_id`,
  `snapshot_id`, `grade`) into `execution-receipt-v1`, after recomputing the snapshot ID,
  MiDojo's digest of the content (`midojo_sdk.snapshot`, `verified`), and requiring every
  call to be the
  interceptor's record: security true/false/null is detected/not_detected/inconclusive,
  never coerced, explained by the grade's `security_reason` (a true or false grade without
  one is `verdict_malformed`), and the receipt's `episode` block carries the route
  (`route.py`: only `midojo` today), the evaluation and snapshot IDs, the evidence status,
  the verdict and `call_injections` (each call's `injection_outcomes` as sealed, null when
  the record does not say). A call record sealed from 3824922 has `structured_content` and
  `injection_outcomes`, one sealed before neither; both are shape-checked (occurrence 0
  only for `unmatched`, a call whose arguments, or from c0bee12 result, did not match).
  From 13d5487 an outcome also has `selected`/`transformation`/`emitted` and the grade `attempted`/`blocked`/
  `effect_observed`/`injection_delivered` (each all or none, optional); a missing one is
  null in `episode.verdict`, never false. From a19562e a
  placement (applied/partial/fallback) may also seal `payload_digest`, `placed_digest` and
  `slot` (`{id, target, mode}`, `id` the target's digest), each optional, also on
  `delivery.json` facts; shape-checked, left out of the snapshot ID when null, and not
  carried into the receipt as sealed. Each receipt outcome that emitted gets
  `payload_sha256` and `slot` (`{id, record, mode}`, the slot also `selected`): snapshot
  first (the sealed digest's hex; the sealed slot's mode and target record, its `id` named
  by the `placements` of the plan instructions with that target), for static and generated
  plans alike; a key not sealed falls back to a static plan's payload and `placements`; a
  static plan that disagrees with the sealed digest nulls both; else null. `--delivery`
  names the runner's `delivery.json` (`contracts/midojo-delivery/`, `midojo-delivery-v1`:
  the evaluation and snapshot IDs and `midojo.delivery`'s facts and exposure), carried into
  `episode.delivery`/`exposure` (null without it); missing, malformed or another snapshot's
  fails the receipt.
  The tests compile against the fixture target (`tests/fixtures/targets/piggybank/`);
  `tests/fixtures/episode/` holds compiled episodes and the snapshots and verdicts MiDojo
  produced from them at 3824922 through its interceptor in front of that target
  (`tools/fixture-generators/episode.py`, run in MiDojo's interpreter; `tests/episode_cases.py`
  says how to regenerate; the episodes were recompiled offline since, into the package
  envelope), `tests/fixtures/episode-a5f81e9/` the same runs sealed at a5f81e9 with the v0
  wrapper compile wrote then, and `tests/fixtures/regraded-13d5487/` those runs as MiDojo
  13d5487 reads, grades and reports them (`tools/fixture-generators/regraded_13d5487.py` and
  the other scripts beside it, offline and in MiDojo's interpreter; `reported/` derives
  reported facts).
  `tests/fixtures/placed-a19562e/` holds static and generated-payload runs of a list slot
  and a `result_record` slot, sealed by MiDojo a19562e in front of a scripted upstream, with
  their `delivery.json` (`tests/placed_cases.py`).
  `tests/fixtures/result-record/` holds episodes compiled for this package's clinic fixture
  target (its `tests/fixtures/targets/clinic/`, record IDs in the result only) and what MiDojo
  c0bee12 sealed from them in front of a scripted upstream (`tests/result_record_cases.py`).
  `tests/fixtures/openshell-4e5176a/` holds its referral run sealed by MiDojo 4e5176a with
  OpenShell observations, whose `http_requests` the snapshot ID leaves out while empty;
  `parse` copies them as sealed into the attempt's `runtime_observations.http_requests`
  (left out when not observed: no such observation or field, or an empty list from an
  OCSF read that is not `complete`; Garak's receipt always leaves it out).
  `tests/fixtures/reply-judge-4e5176a/` holds the reply episode as compiled now and the
  reply runs MiDojo 4e5176a graded through `judge-serve` and a fake model, verdicts with
  `judgements` (which `parse` accepts and does not carry).
  `tests/fixtures/midojo/` holds byte copies of MiDojo's own golden vectors
  (`episode_document/`, `snapshot_digest/vectors.json`), and `tests/fixtures/midojo.lock`
  pins their sha256 and the SDK revision the copies come from, so a diverging copy fails.
  `tests/fixtures/inconclusive_vectors/` holds the sealed runs MiDojo owns, with its
  `MANIFEST.json` beside them: four runs MiDojo cannot decide (an unknown grade, an
  interrupted seal, an incomplete capture and an undelivered payload) and their control,
  each with the outcome every route owes it. `tests/test_inconclusive_vectors.py` runs them
  through `parse`, and the Garak package's `test_bundle_garak_ipi_parse.py` runs them through
  `report_parse`; both check the copy's digests. Refresh the copy with MiDojo's
  `scripts/sync_inconclusive_vectors.py --write`, never by editing a fixture.
- Each `packages/*` member runs in its own pytest session, for example
  `uv run pytest packages/asago-bundle-core/tests -q` and
  `uv run pytest packages/asago-bundle-garak/tests -q`.
- `tests/`: executable examples and regression coverage for the changed behavior.

Finish the requested slice once its behavior and required checks pass. Report
case outcomes, blockers, actual spend, and evidence paths. A passing suite is
not a successful experiment; a command attempt is not a completed effect.
Keep unrelated formatting, new frameworks, and speculative hardening out of the
critical path.

`AGENTS.md` is a symlink to this file. Edit `CLAUDE.md`; preserve the symlink.
