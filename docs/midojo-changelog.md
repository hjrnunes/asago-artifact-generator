# MiDojo adapter changelog

This page holds the MiDojo adapter's revision history, fixture provenance and
implementation notes, moved out of the agent guide (`CLAUDE.md`) so that guide
stays short.

The adapter currently pins `midojo-sdk` at `7b2ca52`; `pyproject.toml` and
`uv.lock` hold the full revision. The note below keeps each revision as it was
recorded.

## Adapter notes

- `packages/asago-bundle-midojo/`: the MiDojo adapter workspace member. It maps a
  `planted_item` package to one MiDojo injection spec for a target config and parses the
  sealed result; MiDojo runs the injection spec, and the adapter holds no proxy or
  interception code. It names the MiDojo `asago/main` range `>=2a794f3`, the first revision
  that carries every capability the document uses and reads the document's ID, and pins
  `midojo-sdk` to the revision
  `pyproject.toml` and `uv.lock` record (`2a794f3`, the revision that renamed the document
  to the injection spec); a target can match the call's arguments from `b5b8c75`, the
  interceptor copies a placement into a text block that mirrors the structured content from
  `3824922`, an injection spec can carry a `result_record` target from `c0bee12`, and a
  reply from `cab2a35`. The adapter imports `midojo_sdk` only (the injection-spec schema,
  the injection plan and the snapshot digest) and never `midojo`: its pinned SDK runs the
  consumer's checks, and the runner's MiDojo runs the injection spec. It plants by
  response injection. `compile_injection_spec` (`injection_spec_compile.py`, CLI `compile
  --target` `[--block TOOL]`) reads the target config's `tools-list.json` and
  `runtime-spec.yaml` (`declared.py`) and writes one `injection-spec.json` as a package
  envelope (`document.py`, `asago-midojo-package-v1`): the Asago fields `package_id`,
  `scenario_id`, `package_digest`, `claim_level`, `delivery`, `placements` and
  `tool_revision_range`, and `episode` (the key the envelope keeps until M9), exactly
  MiDojo's `midojo-injection-spec-v1` document (`injection_spec.py`, validated against
  `midojo_sdk.injection_spec_schema`): host `interception` (the runner gives the
  interceptor the target's `endpoint.mcp_url`; the agent gets only the interceptor's
  endpoint and token, and neither it nor the target sees a MiDojo session), the allowed
  `tools` and their write disposition (`forward` by default), `verifiers` `[]`, and
  `InjectionSpec`'s fields as plain data with no environment. `read_envelope` also reads the
  older `midojo-episode-v2` wrapper, the bare episode with `tools` and `placements` at the
  top level, for one release, and it reads a document that keeps the old ID
  `midojo-episode-v1`, warning that it is deprecated; it refuses any other version, naming
  it. `parse` reads a directory compiled before the rename, which holds `episode.json`, for
  the same release. The pre-rename modules `episode_compile` and `episode` remain as
  deprecated aliases of `injection_spec_compile` and `injection_spec`. Each seed item
  becomes one strict `tool_output` instruction from its runtime contract slot
  (`placement.py`): carrier, field path, mode, `max_chars`, and the record by the slot's
  declared `record_key` (from the target's `runtime-spec.yaml` or the runtime contract,
  never inferred): a field inside a list through a record selector on that item property;
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
  the model key and token are redacted, and its log is method and status only.
  `verifiers` is `[]`; compile writes `injection-spec.json` alone. Direct and user-turn
  packages, a single package with history, non-user history, run-time bindings (a declared
  slot, or a `{{name}}` placeholder in a user turn), state_effect
  claims and a seed the interceptor cannot place in an MCP tool result are capability gaps
  (exit 3). `parse_sealed` (`sealed.py`, CLI `parse --snapshot --verdict
  [--delivery] [--route]`) reads the snapshot (`GET .../snapshot`) and the verdict (`evaluation_id`,
  `snapshot_id`, `grade`) into `execution-receipt-v1`, after recomputing the snapshot ID,
  MiDojo's digest of the content (`midojo_sdk.snapshot`, `verified`), and requiring every
  call to be the
  interceptor's record: security true/false/null is detected/not_detected/inconclusive,
  never coerced, explained by the grade's `security_reason` (a true or false grade without
  one is `verdict_malformed`); a false grade from a recording that states no delivery
  (`injection_delivered` null or absent, as one sealed before MiDojo recorded delivery
  facts does) is inconclusive at every claim level, its reason naming the recording, so
  re-scoring an old run directory flips those `not_detected` results to inconclusive by
  design. The receipt's `episode` block carries the route
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
  (`tools/fixture-generators/episode.py`, run in MiDojo's interpreter;
  `tests/injection_spec_cases.py` says how to regenerate; the episodes were recompiled
  offline since, into the package envelope), `tests/fixtures/episode-a5f81e9/` the same
  runs sealed at a5f81e9 with the v0 wrapper compile wrote then, and
  `tests/fixtures/regraded-13d5487/` those runs as MiDojo
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
  `judgements` (which `parse` accepts and does not carry); those runs predate MiDojo's
  delivery facts, so `parse` reads them inconclusive.
  `tests/fixtures/reply-judge-246dc73/` holds one reply run sealed at 246dc73, which
  records the delivery, graded `contradicted`: `parse` reads it `not_detected`
  (`tools/fixture-generators/reply_judge_delivered.py`).
  `tests/fixtures/midojo/` holds byte copies of MiDojo's own files
  (`injection_spec_document/`, `snapshot_digest/vectors.json`, and the documents MiDojo
  files under `legacy_documents/` and `verdict_document/`), and
  `tests/fixtures/midojo.lock` pins their sha256 and the SDK revision the copies come
  from, so a diverging copy fails.
  `tests/fixtures/inconclusive_vectors/` holds the sealed runs MiDojo owns, with its
  `MANIFEST.json` beside them: five runs MiDojo cannot decide (an unknown grade, an
  interrupted seal, an incomplete capture, an undelivered payload and an unrecorded
  delivery), their control, and a blocked call (the agent attempts the forbidden call and
  the episode's policy refuses it, which MiDojo detects with no effect),
  each with the outcome every route owes it. `tests/test_inconclusive_vectors.py` runs them
  through `parse`, and the Garak package's `test_bundle_garak_ipi_parse.py` runs them through
  `report_parse`; both check the copy's digests. Refresh the copy with MiDojo's
  `scripts/sync_inconclusive_vectors.py --write`, never by editing a fixture.

- **Pre-rename Garak reports.** A report recorded before the M2 rename names
  `ipi_episode.IPIEpisode`, its detectors `ipi_episode.EpisodeVerdict` and
  `ipi_episode.EpisodeEffect`, and its `ipi_episode` and `episode_trace` notes.
  `report_parse` reads both forms and gives the same outcome and verdict.
