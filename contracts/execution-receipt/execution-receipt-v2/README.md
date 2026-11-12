# execution-receipt

The tool-neutral record of one artifact package's execution by one testing
tool. The Garak adapter writes it as `execution-receipt.json` beside the
launcher's `receipt.json`, and every reader of execution evidence reads it: the
orchestrator projects and composes it, and the consumer's conformance runner
validates what the adapter wrote.

The consumer owns this contract and `asago-bundle-core` ships the copy its
reader enforces; that package's test refuses a copy which differs from this
directory.

## The document

`execution-receipt-v2` names the tool that ran the package, the package and
bundle digests, the execution and runtime status, the incomplete reason, one
attempt per repeat, the native files the tool wrote, and, for a planted-item
run, the `evaluation` block. `schema.json` is the runtime schema, `cases/`
holds the valid and invalid documents with `expected-violations.json`, and
`spec.json` names them.

## The reserved utility record

`evaluation.utility` is optional and reserved: no tool measures utility yet. A
receipt that records no utility leaves the key out. An absent key means the
receipt does not record utility; it never means not applicable. The block lives
inside `evaluation`, so a receipt with no `evaluation` block records no
utility. When the key is there, it holds exactly five members, all written:

| member | value |
| --- | --- |
| `value` | `true` or `false` when the check was decided, `null` when it was not; always `null` when `applicability` is `not_applicable` |
| `applicability` | `applicable` or `not_applicable` |
| `reason` | for `not_applicable`: `no_utility_check` (the evaluation carries no utility check), `baseline_security` (the run is the paired baseline of a security measurement) or `intermediate_turn` (a turn of a chain graded at its end); `null` when `applicable` |
| `snapshot_id` | the snapshot the record was graded on, `null` when the run sealed none |
| `pairing_id` | the identifier that pairs an attacked run with its benign baseline, `null` when the run has no pair |

Three rules follow:

- A `value` set while `applicability` is `not_applicable` is invalid. Unknown
  stays `value: null` under `applicable`; it is never recorded as
  `not_applicable`.
- A grade's own `utility` value never becomes `value`. MiDojo writes
  `utility: false` beside a `not_applicable` utility validity on every grade
  that has no utility check, so copying it would record a failed utility check
  that never ran.
- The consumer's parse writes the constant record for every `evaluation` block
  it writes: `value: null`, `applicability: not_applicable`,
  `reason: no_utility_check`, the block's own `snapshot_id` (null for a chain
  sealed short) and `pairing_id: null`. The orchestrator copies the block as it
  is and writes none.

A grade whose utility validity is not `not_applicable` means a utility check
ran. The parse refuses it with its own reason, `verdict_utility_graded`, and a
compile that would write a non-null `utility` member refuses too, until a tool
measures utility.

## What v2 renames

v2 states the same evidence as v1 under the names the workspace settled on. It
changes no other rule:

| v1 | v2 |
| --- | --- |
| the `episode` block | the `evaluation` block |
| `episode.route`, the client that drove the run | `evaluation.driver` |
| `probe_key`, the instruction's place in the plan | `slot_key` |

The first receipt version used these earlier field names. The installed reader
accepts `execution-receipt-v2` only.

## The split

`spec.json` and `schema.json` are the runtime spec. Their bytes are the
contract's identity: `spec_digest` covers `spec.json` without its digest field
and the bytes of `schema.json`. Changing a case or this README never changes
the identity.

## Identity and compatibility

A document written from the self-identifying envelope carries one `contract`
block:

```json
{
  "contract": {
    "id": "execution-receipt",
    "version": "execution-receipt-v2",
    "spec_digest": "sha256:...",
    "requires": [],
    "provenance": {
      "writer": "asago_bundle_garak.report_parse",
      "writer_version": "0.1.0",
      "validator_version": "asago-contracts-core 0.1.0"
    }
  }
}
```

`execution-receipt-v2` is published with `"envelope": "required"`, and every
writer stamps it with `asago_contracts_core.write_envelope`. The reader selects
the installed spec by the document's contract id and version, and rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (the installed version
  supports none);
- a top-level field outside the schema and the spec's declared extensions;
- a `spec_digest` that is not exactly the installed spec's, for a draft as for
  a published version;
- a v2 document without the envelope.

The orchestrator's run reader stays lenient on purpose: it also reads receipts
recorded before this schema removed the `target` field, and the contract reader
is not on that path. The contract gate runs where a receipt is handed on (each
adapter's `parse`) and in the conformance runner both adapters use.

`asago_bundle_core.receipt_contract` ships the installed copy of `spec.json`
and `schema.json` with the cases and this README, and its test refuses a copy
that differs from this directory.
