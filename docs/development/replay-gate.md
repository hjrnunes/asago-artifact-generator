# Replay gate

The replay gate proves that a code change leaves recorded `generate` items
unchanged. It re-runs every item of a recorded orch author stage through the
current code, offline, and compares every output file and console log with the
recording.

## Run it

Run it from the checkout under test:

```bash
./scripts/replay-check.sh [--jobs N] [--item SCN-ID] \
  ../../hjrnunes/asago-orch/runs/<run-id>/stages/author [more stage dirs ...]
```

The script calls `python scripts/replay_gate.py check`. The gate is
development tooling: it lives in `scripts/`, outside the shipped package, and
imports the package. Its options:

| Option | Meaning |
| --- | --- |
| `--jobs N` | Replay N items in parallel (default 1). |
| `--item ID` | Replay only this item; repeat for more. |
| `--work-dir DIR` | Use DIR as scratch space; it is kept. |
| `--keep` | Keep the temporary scratch directory after a pass. |
| `--json FILE` | Also write per-item results to FILE. |

The six Phase 0 re-baseline runs (219 authored items, 1,008 dispatches) take
about 10 minutes with `--jobs 1`, 4.5 minutes with `--jobs 2`, and 3 minutes
with `--jobs 4` on an Apple-silicon laptop. Those timings include the
detector controls that recordings made before artifact-package-v3 ran in
Docker; those recordings no longer replay (see Limits).

A pass means that, for every replayed item:

- every request the code sent matched a recorded prompt, and every recorded
  response was used;
- the exit code and the terminal status equal the recorded ones;
- every output file and the console log equal the recording, apart from the
  allowed differences below;
- the replay made no network attempt.

A refactor that must not change behavior has to pass on all chosen
recordings. A change that alters any prompt, request control, finding, or
package byte fails. Record a new run after an intended change.

## What the gate does

For each orch author stage directory, the gate reads `stage.json`. Its
`items` list holds each authored item's exact command line and exit code;
items that orch skipped have no command and are counted, not replayed. Then,
for each item, the gate:

1. Copies each input file named on the command line into scratch space,
   keeping its file name, and copies the Gherkin companion beside a scenario.
   It reads `--profiles-file` in place, because that file holds endpoint
   credentials. It points `--output-dir` at a scratch output directory. The
   recorded stage is never written.
2. Loads the dispatches from the item's `<ID>.failure-evidence.json`, which
   every item writes. Each dispatch holds the system and user prompt, the
   requested and returned model, the raw response bytes, the response capture,
   and usage.
3. Runs `generate` with the rewritten arguments in a new process, as orch does,
   with stdout and stderr in one log. The process:
   - starts from an environment without `ASAGO_*`, `OPENAI_*`,
     `OPENROUTER_*`, `REDTEAM_*`, `*_API_KEY`, `GEMINI_API_KEY`,
     `GOOGLE_API_KEY`, and `FORCE_COLOR`;
   - refuses and logs every IPv4/IPv6 connection and DNS lookup;
   - replaces only the OpenAI client inside `PrivateModelAuthoringTransport`,
     through the transport factory that `cli.run` accepts.
     The client checks that each request carries the recorded model and the
     exact recorded system and user messages, then answers with the recorded
     response. A mismatch or an extra request is refused and reported.

   Everything else runs for real: argument parsing, profile loading, request
   controls, response capture, validation, corrections, reviews, and
   packaging.
4. Compares the item's files (`output/<ID>/…` and `output/<ID>.*`) and
   `items/<ID>.log`. A file passes if its bytes are equal. A JSON file or a
   JSON log line that differs is compared as parsed data, including key order,
   after applying only the allowed differences.

## Allowed differences

| File | Field | Reason |
| --- | --- | --- |
| `*.failure-evidence.json` | `package_path` | The replay writes into a scratch output directory. |
| `*.blocked.json` | `package_path` | The same. |
| `items/*.log` | `package` in the result line | The same. |

The gate does not ignore these fields. It maps the scratch output directory
back to the recorded one, so each value must still name the same place inside
the output directory. The list lives in `ALLOWED_DIFFERENCES` in
`scripts/replay_gate.py`. Add an entry only for a value
that legitimately differs between two executions of the same code on the same
responses. Fix nondeterminism in the code instead of listing it.

## Limits

- The recorded inputs must still exist. Orch does not snapshot the runtime
  contract (`asago-orch/data/targets/shared/runtime-contract.json`); if it
  changed since the recording, the prompts differ and the gate fails.
- The model profile must be unchanged. Its controls are part of each recorded
  dispatch, and the outputs compare them.
- The gate cannot replay a provider exception: failure evidence records only a
  failure code and detail. It rejects a recording with an unavailable response.
- The replay starts at the SDK client. It does not cover the `openai` SDK
  itself or the service-tier fallback after a rate limit.
- Recordings made before artifact-package-v3 no longer replay. Their Call 2
  prompts asked for detector Python, their packages carry `detector.py` and a
  `detector_interface`, and their items read scenario-handoff-v1 or v2 input,
  which `generate` now rejects. Record a new run to get a replayable baseline.
- Recordings made with `authoring-call1-v19` or earlier no longer replay. The
  plan field meanings, the binding consumer spelling (`judge.<name>`), and the
  comparison semantics changed in every prompt role except Call 2, so each item
  that dispatches a request mismatches at its first Call 1 prompt. Only items
  that stop before any request still replay.
