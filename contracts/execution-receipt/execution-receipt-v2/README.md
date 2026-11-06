# execution-receipt

The tool-neutral record of one artifact package's execution by one testing
tool. The Garak adapter and the MiDojo adapter write it as
`execution-receipt.json` beside the launcher's `receipt.json`, and every reader
of execution evidence reads it: the orchestrator projects and composes it, and
the consumer's conformance runner validates what each adapter wrote.

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

## The recorded runtime

`evaluation.runtime` is optional. A receipt whose caller recorded no runtime
leaves the key out, so it reads as one written before the field existed. When
the key is there, it holds exactly two members:

| member | value |
| --- | --- |
| `type` | `openshell` when the caller started the target in OpenShell, `unmanaged` when it started the target without a sandbox |
| `evidence` | the facts behind the type, in the caller's order; the list may be empty |

`asago-bundle-midojo parse` records the type from `--runtime` and each fact
from a repeatable `--runtime-evidence`; without the flags it records neither.
The Garak adapter records no runtime.

## What v2 renames

v2 states the same evidence as v1 under the names the workspace settled on. It
changes no other rule:

| v1 | v2 |
| --- | --- |
| the `episode` block | the `evaluation` block |
| `episode.route`, the client that drove the run | `evaluation.driver` |
| `probe_key`, the instruction's place in the plan | `slot_key` |

A reader of this contract accepts `execution-receipt-v1` and
`execution-receipt-v2`, so a receipt recorded before the rename still reads.
`midojo-delivery` carries the same two fields under the same new names.

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

`execution-receipt-v1` stays published with `"envelope": "optional"`, because
its writers predate the envelope; `execution-receipt-v2` is published with
`"envelope": "required"`, and every v2 writer stamps it with
`asago_contracts_core.write_envelope`. A reader selects one of its installed
specs by the document's contract id and version, and rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (neither version
  supports one);
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
