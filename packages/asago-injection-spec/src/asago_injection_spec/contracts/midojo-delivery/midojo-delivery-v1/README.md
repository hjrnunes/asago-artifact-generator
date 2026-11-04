# midojo-delivery

The document orch's MiDojo episode runner writes beside a sealed snapshot as
`delivery.json`, and the consumer's MiDojo `parse --delivery` reads. The
consumer owns this contract; `asago-orch/contracts/midojo-delivery/` mirrors it
byte for byte.

## The document

`midojo-delivery-v1` names the evaluation and snapshot the delivery describes,
MiDojo's `delivery_facts` of the episode's plan and the sealed calls, and the
`exposure` it derives from them. `schema.json` is the runtime schema, `cases/`
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
    "id": "midojo-delivery",
    "version": "midojo-delivery-v1",
    "spec_digest": "sha256:...",
    "requires": [],
    "provenance": {
      "writer": "asago-orch.qualification.midojo_episode_runner",
      "writer_version": "0.1.0",
      "validator_version": "asago-contracts-core 0.1.0"
    }
  }
}
```

`midojo-delivery-v1` is published with `"envelope": "optional"`, because its
writers predate the envelope. A reader accepts a v1 document without one and
the same document with one. A reader selects one of its installed specs by the
document's contract id and version, and rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (v1 supports none);
- a top-level field outside the schema and the spec's declared extensions;
- a `spec_digest` that is not exactly the installed spec's, for a draft as for
  a published version.

The consumer's `asago_injection_spec.delivery_contract` ships the installed copy
of `spec.json` and `schema.json`, and its test refuses a copy that differs from
this directory.
