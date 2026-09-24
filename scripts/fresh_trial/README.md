# Fresh-trial caller state

The fresh-trial caller reads `frozen-policy.json` before live dispatch. Freeze
the raw-file SHA-256 digests under the following exact keys:

```json
{
  "digests": {
    "input_index_sha256": "<SHA-256 of input-index.json bytes>",
    "rendered_requests_index_sha256": "<SHA-256 of rendered-requests/index.json bytes>",
    "controls": {
      "G07": "<SHA-256 of controls/G07.json bytes>",
      "A03": "<SHA-256 of controls/A03.json bytes>",
      "O03": "<SHA-256 of controls/O03.json bytes>",
      "O04": "<SHA-256 of controls/O04.json bytes>",
      "SCN-030": "<SHA-256 of controls/SCN-030.json bytes>"
    }
  }
}
```

`_read_frozen_policy` reads `digests.input_index_sha256`. Before dispatch, the
caller also reads `digests.rendered_requests_index_sha256` and the five values
under `digests.controls`. It accepts a few legacy aliases, but new freeze
writers should use the keys above. These entries must live directly under the
top-level `digests` object; the caller does not read them from
`freeze_artifact_digests`. Control digests cover the exact file bytes, not a
canonicalized JSON object.

## Render-only safety

`--render-only` writes the Call 1 renderings and batch status only when no live
reservation, dispatch, or outage stop exists. It checks
`authoring/batch-status.json` and `authoring/budget-ledger.json` before creating
or writing trial outputs. If either file shows live work, or its state cannot
be validated, the caller exits non-zero without changing any file. A live
outage stop remains in the batch status and continues to block later `--live`
dispatches.

## Prompt-overflow estimates

Both calibrated context-budget rejections and the one-megabyte rendered-prompt
guard produce the case-local `prompt_overflow` result before dispatch. The
finding reports `estimated_prompt_tokens`,
`remaining_input_budget_estimate`, and the model-facing UTF-8 byte estimate.
These values are estimates, not provider-reported token usage.

## Reproducible freeze preparation

`freeze_trial.py` creates a new run directory from the previous freeze without
writing to the previous directory. It verifies the previous input loader and
control digests, copies relative handoff sources and exact control bytes,
regenerates only the G07/A03 fact schemas with
`qualification_inputs._schema`, updates the derived inventory pins, and records
the current consumer and downstream revisions. Dirty worktrees are recorded in
the policy and must be resolved or explicitly reviewed before a real live run.

Create the real freeze with a fresh, nonexistent run directory:

```bash
uv run python -m scripts.fresh_trial.freeze_trial \
  --create \
  --previous-freeze /absolute/path/to/previous-freeze \
  --run-dir /absolute/path/to/fresh-consumer-five-case-<timestamp> \
  --consumer-root /absolute/path/to/asago-artifact-generator \
  --downstream-root /absolute/path/to/asago-scenario-generator
```

Render Call 1 without constructing a transport, then pin the rendered-request
index digest in `frozen-policy.json`:

```bash
uv run python -m scripts.fresh_trial.run_fresh_authoring_trial \
  --run-dir /absolute/path/to/fresh-consumer-five-case-<timestamp> \
  --render-only
uv run python -m scripts.fresh_trial.freeze_trial \
  --finalize-renderings \
  --run-dir /absolute/path/to/fresh-consumer-five-case-<timestamp>
```

Read all five saved requests and complete the specification's request
inspection and route-compatibility checks before using `--live`.

Each new policy replaces prior refreeze keys with one `previous_freeze` record.
That record preserves the previous freeze digests, saved authoring spend, and
the exact saved execution outcome without copying `superseded/` paths. The
policy also rehashes the specification, measures the listed loopback ports, and
records whether route compatibility was verified against the current downstream
revision. A live run still requires an immediate local port check and explicit
owner approval for the new freeze's separate allowance.
