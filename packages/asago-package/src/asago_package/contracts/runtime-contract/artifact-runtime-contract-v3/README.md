# runtime-contract

The contract a pipeline stage reads to learn what downstream execution offers a package:
the deliveries it can send, the turn and time limits, the setup operations it may run, what
it observes during a run, and the fields of the target's records that can hold planted text.
Authoring is target-free, so this contract is all it knows about execution.

`asago-package` owns this contract and ships this version directory as package data, so the
authority copy and the installed reader copy are the same files. The orchestrator writes one
contract per target under `data/targets/<target>/runtime-contract.json`, its
`scripts/gen_runtime_contracts.py` renders them from each target's runtime specification, and
`asago_package.declared_runtime_contract` builds a declared target's contract from the shared
base, and the orchestrator runs it in the consumer's interpreter. The consumer's `generate
--runtime-contract`, that builder and the orchestrator's drift check all read a contract
through `asago_package.runtime_contract_spec`.

## The document

`artifact-runtime-contract-v3` states the authoring transports (each unavailable: authoring
reaches no discovery, judge, setup or target), the deliveries execution can send (always
including one direct user message), the turn and time limits, what execution observes, the
target operations a package may run as setup, `target_access: downstream_only`, and the
optional seed slots. A seed slot names one field of the target's records, the operation that
returns it, the parties that can write it, what it holds, how the target writes planted text
there (`replace` or `append`), and optionally the record key that names a list item.

`seed_slots[].writable_by` is a non-empty list of distinct party names. The parties are one
shared enum, the scenario-handoff's `ItemController`: `third_party`, `counterparty`,
`operator_insider` and `unknown`. A slot several parties can write lists all of them; a
reader that compares a slot with one party matches when the list contains that party. The
field replaces v2's scalar `controller`, which v3 refuses as an unknown slot key.

`schema.json` is the runtime schema, `cases/` holds the valid and invalid documents with
`expected-violations.json`, and `spec.json` names them.

## The split

`spec.json` and `schema.json` are the runtime spec. Their bytes are the contract's identity:
`spec_digest` covers `spec.json` without its digest field and the bytes of `schema.json`.
Changing a case or this README never changes the identity.

## Identity and compatibility

A v3 document carries the self-identifying envelope. `artifact-runtime-contract-v3` is
published with `"envelope": "required"`, so a reader refuses a v3 document without one:

```json
{
  "contract": {
    "id": "runtime-contract",
    "version": "artifact-runtime-contract-v3",
    "spec_digest": "sha256:...",
    "requires": [],
    "provenance": {
      "writer": "asago_package.declared_runtime_contract",
      "writer_version": "0.1.0",
      "validator_version": "asago-contracts-core 0.1.0"
    }
  }
}
```

A reader selects one of its installed specs by the document's contract id and version, so it
reads both `artifact-runtime-contract-v2` (whose `envelope` is `optional`) and v3. It rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (neither version supports any);
- a top-level field outside the schema and the spec's declared extensions;
- a `spec_digest` that is not exactly the installed spec's, for a draft as for a published
  version;
- a v3 document without the envelope, naming the missing `contract` field.

The orchestrator keeps no copy of this contract: `asago_package` carries it, and the
orchestrator's doctor checks each target's recorded contract against the contract its runtime
specification builds.
