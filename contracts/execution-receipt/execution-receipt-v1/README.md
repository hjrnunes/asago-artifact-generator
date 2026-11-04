# execution-receipt

The tool-neutral record of one artifact package's execution by one testing
tool. The Garak adapter and the MiDojo adapter write it as
`execution-receipt.json` beside the launcher's `receipt.json`, and every reader
of execution evidence reads it: the orchestrator projects and composes it, and
the consumer's conformance runner validates what each adapter wrote.

The consumer owns this contract; `asago-orch/contracts/execution-receipt/`
mirrors it byte for byte, and `asago-bundle-core` ships the copy its reader
enforces.

## The document

`execution-receipt-v1` names the tool that ran the package, the package and
bundle digests, the execution and runtime status, the incomplete reason, one
attempt per repeat, the native files the tool wrote, and, for a planted-item
episode, the `episode` block. `schema.json` is the runtime schema, `cases/`
holds the valid and invalid documents with `expected-violations.json`, and
`spec.json` names them.

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
    "version": "execution-receipt-v1",
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

`execution-receipt-v1` is published with `"envelope": "optional"`, because its
writers predate the envelope. A reader accepts a v1 document without one and
the same document with one. A reader selects one of its installed specs by the
document's contract id and version, and rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (v1 supports none);
- a top-level field outside the schema and the spec's declared extensions;
- a `spec_digest` that is not exactly the installed spec's, for a draft as for
  a published version.

The orchestrator's run reader stays lenient on purpose: it also reads receipts
recorded before this schema removed the `target` field, and the contract reader
is not on that path. The contract gate runs where a receipt is handed on (each
adapter's `parse`) and in the conformance runner both adapters use.

`asago_bundle_core.receipt_contract` ships the installed copy of `spec.json`
and `schema.json` with the cases and this README, and its test refuses a copy
that differs from this directory.
