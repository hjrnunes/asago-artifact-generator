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
