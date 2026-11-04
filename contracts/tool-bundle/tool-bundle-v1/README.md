# tool-bundle

The manifest a tool adapter writes as `bundle.json`: `compile` writes a template and
`instantiate` a concrete bundle, and the orchestrator runs the concrete one. The Garak and
MiDojo adapters read this contract. The consumer owns it; `asago-orch/contracts/tool-bundle/`
mirrors it byte for byte.

## The document

`tool-bundle-v1` names the package and scenario the bundle runs, the tool and the revision
range it needs, the plugins it loads, the values `values.json` must carry (`requires`), the
environment variables its entrypoint reads, the template files, the argument vector, and the
native outputs it writes. `schema.json` is the runtime schema, `cases/` holds the valid and
invalid documents with `expected-violations.json`, and `spec.json` names them.

## The top-level `requires`

`requires` lists the keys `values.json` must carry so the templates and the entrypoint can
fill every slot. It is the document's own field and has nothing to do with the
self-identifying envelope's `requires`, which lists contract features. v1 keeps the field
name so every recorded manifest reads; `tool-bundle-v2` must rename or nest it (see
`asago-term.5b`) so the two cannot be confused.

## The split

`spec.json` and `schema.json` are the runtime spec. Their bytes are the contract's identity:
`spec_digest` covers `spec.json` without its digest field and the bytes of `schema.json`.
Changing a case or this README never changes the identity.

## Identity and compatibility

A document written from the self-identifying envelope carries one `contract` block:

```json
{
  "contract": {
    "id": "tool-bundle",
    "version": "tool-bundle-v1",
    "spec_digest": "sha256:...",
    "requires": [],
    "provenance": {
      "writer": "asago_bundle_garak.compiler",
      "writer_version": "0.1.0",
      "validator_version": "asago-contracts-core 0.1.0"
    }
  }
}
```

`tool-bundle-v1` is published with `"envelope": "optional"`, because its writers predate the
envelope. A reader accepts a v1 document without one and the same document with one. A
reader selects one of its installed specs by the document's contract id and version, and
rejects:

- an unsupported version, with the error listing the supported versions;
- a `requires` entry the selected spec does not support (v1 supports none);
- a top-level field outside the schema and the spec's declared extensions;
- a `spec_digest` that is not exactly the installed spec's, for a draft as for a published
  version.

The consumer's `asago_bundle_core.bundle_contract` ships the whole version directory as
package data, and `tests/test_bundle_contract.py` refuses a copy that differs from this
directory. `asago_bundle_core.schema` reads every manifest through it before it applies the
schema, so `instantiate` and the conformance helper keep their existing error wording.

The orchestrator's run reader stays lenient on purpose: it also reads bundles recorded before
v1 dropped fields the adapters no longer write.
