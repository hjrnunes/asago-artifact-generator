# scenario-handoff

The frozen scenario handoff a producer run publishes as `scenarios/SCN-*.yaml`: the
narrative, the attack path, the Gherkin, the semantic failure criterion, the safe alternative
and the metadata the consumer needs. The producer owns this contract; the consumer's
`contracts/scenario-handoff/` and the shared core's package data carry byte-identical copies.

## The document

`scenario-handoff-v5` states one failure: the unsafe outcome, the procedure chain that leads
to it, the taxonomy references, the preconditions, the discriminating condition and the tool
call the failure needs. `schema.json` is the runtime schema, `cases/` holds the valid and
invalid documents with `expected-violations.json`, the conformance manifest and the feature
companions, and `spec.json` names them.

## The reader rules

`asago_contracts_core.validate_handoff` runs the ownership walker and then the composed
schema: the safe-outcome call rules, the procedure tactic/technique pairing, the reference
order and uniqueness, the closed enums and the non-blank text. A break is one `Violation`
carrying the producer's code (`schema_violation:<field>`, `artifact_design_field:<name>`,
`prose_hiding:<pattern>` or `feature_companion:`), which both repositories' runners report
unchanged.

## The split

`spec.json` and `schema.json` are the runtime spec. Their bytes are the contract's identity:
`spec_digest` covers `spec.json` without its digest field and the bytes of `schema.json`.
Changing a case or this README never changes the identity.

## Identity and compatibility

A document written from the self-identifying envelope carries one `contract` block:

```json
{
  "contract": {
    "id": "scenario-handoff",
    "version": "scenario-handoff-v5",
    "spec_digest": "sha256:...",
    "requires": [],
    "provenance": {
      "writer": "asago_scenario_generator.stpa.scenario_prod.handoff",
      "writer_version": "0.1.0",
      "validator_version": "asago-contracts-core 0.1.0"
    }
  }
}
```

`scenario-handoff-v5` is published with `"envelope": "optional"`, because every recorded
handoff predates the envelope. A reader accepts a v5 document without one and the same
document with one. A reader selects one of its installed specs by the document's contract id
and version, and rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (v5 supports none);
- a top-level field outside the schema and the spec's declared extensions;
- a `spec_digest` that is not exactly the installed spec's, for a draft as for a published
  version.

`packages/asago-contracts-core` ships the whole contract directory as package data, and
`packages/asago-contracts-core/tests/test_handoff_contract.py` refuses a copy that differs
from this directory. `asago_contracts_core.validate_handoff` reads every handoff through the
installed spec before the hand-written rules, so the producer's Stage 6 check and the
consumer's input adapter keep their existing error wording.

The orchestrator keeps no copy of this contract and reads `schema_version` directly, so its
handoff reader stays lenient on purpose.
