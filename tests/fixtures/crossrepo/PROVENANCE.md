# Cross-repository smoke fixture provenance

These fixtures let the committed test suite exercise the real public chain
(bundle loading, case resolution, default Garak runtime bindings, readiness
planning, artifact compilation, and public validation) without another
developer's checkout, an ignored build directory, or a live endpoint.

Two fixture classes live here:

1. **Sealed run bytes.** The projection, scenario, and target-profile files
   are byte-identical copies of files inside sealed producer runs. Only the
   bundle index is rebuilt (entries filtered to one scenario; the bundle
   digest recomputed over the `stpa-execution-bundle-v2` frame).
2. **Contract-kit derivatives.** Small re-stamped bundles built from this
   repository's vendored, producer-owned contract kits in
   `contracts/stpa-execution/`.

The loader validates bundle-internal framed digests at test time. The raw
source-file SHA-256 values below record the independently checked provenance;
the suite does not re-verify these documentary hashes against the source runs.

## `klarna-ordering/` — sealed run bytes

Source: producer run `20260914-miniklarna-boundary-correction`
(scenario `SCN-007`, candidate `EXEC:RESP-1:CA-1-5:WRONG_TIMING`, the
structured event-ordering case). Byte-identical files:

| File | SHA-256 |
| --- | --- |
| `scenarios/canonical/SCN-007.projection.json` | `1be1c598305abc606086fc69fc7b0c1edf5ae05b709f9d92e87ea6172dd78557` |
| `scenarios/SCN-007.scenario.json` | `0890ecc52b8a01576e8c3197f3439fe6fd19187b19bdca799cc48de94cd3d11a` |
| `execution-target-profile.json` | `1c94ba4febb1ff023fc931b81b9a94359acc7e7cc999adbfadf4171191f390cb` |

`execution-bundle.json` is rebuilt: the real run's index filtered to the
`SCN-007` entry, with `bundle_digest` recomputed. Projection digests
(`semantic_digest` `fd2ed3b3…`, content hash `1be1c598…`) are unchanged from
the sealed run.

## `occiai-output-text/` — sealed run bytes

Source: producer run `20260914-miniocciai-baseline-regression`
(scenario `SCN-001`, candidate `EXEC:RESP-1:CA-1-10:INCORRECT`, the
model-output case whose oracle requires a semantic proposition).
Byte-identical files:

| File | SHA-256 |
| --- | --- |
| `scenarios/canonical/SCN-001.projection.json` | `e857cd1e963376eed329a8806c08e9029c47d43bb90938ede78e8ab47ac13cdf` |
| `scenarios/SCN-001.scenario.json` | `41fef91b9cc58221a7e552e540b204c44c05a4711df1a2acfbce54e6c0cadce3` |
| `execution-target-profile.json` | `e8d4ff6ccdbed635855c8e4ea1b6cff3c34a93d6a9e99c95dd5ef0a0552031f4` |

`execution-bundle.json` is rebuilt exactly as for `klarna-ordering/`.

## `occiai-omission/` — contract-kit derivative

Source: vendored kit
`contracts/stpa-execution/bundle-v2/valid/minimal-run/` whose projection is
the kit's `projection-v3/valid/structured-omission-direct.json` (OcciAI
shaped: `escalate_to_clinician` total omission, obligation `SC-10/O2`).
The scenario file is the kit's own `SCN-001.scenario.json` (SHA-256
`8012457f3c4c89ce92686e58c5e8c511cc32baef34b1979e66468bd62d701921`),
unchanged.

Derivation operations:

1. Build the derived simulation profile committed here as
   `execution-target-profile.json` (SHA-256 `2781b3e3c680a9f292df814a10e10160d375ccf3c35472260e67622c4e3a6041`):
   one `simulation`-basis resource for `escalate_to_clinician` with
   `patient_id`/`note` string arguments, reviewed semantic authority.
2. Re-stamp the projection's `execution_classification.target_profile_digest`
   to the derived profile's `semantic_digest` and re-derive
   `classification_digest` (the model validator computes it).
3. Recompute the projection `semantic_digest` over the
   `stpa-execution-projection-v3` frame and rewrite the file as canonical
   JSON (SHA-256 `f07290e8b83e2c701df77ace137176223412a920768bd5327bed4696e2c77e66`).
4. Rebuild the bundle index: update the entry's projection content hash and
   semantic digest, then recompute `bundle_digest`.

## `occiai-conversation/` — contract-kit derivative

Source: vendored kit
`contracts/stpa-execution/projection-v3/valid/structured-omission-conversation.json`
(OcciAI shaped, `conversation_context` delivery with two prepared user turns
`T-1`/`T-2`). Same derivation operations, same derived simulation profile,
and the same kit scenario file as `occiai-omission/`. Projection SHA-256
`61b248d3873b3ec38ba22dae39eea24c520c95ce81a3d72cc282c74ad6f9ffe7`.

## Recorded coverage limitations

- No saved producer run publishes a structured-omission projection: the
  OcciAI omission scenario (`SCN-005`,
  `RESP-1:CA-1-3:NOT_PROVIDED`) resolved as a functional test
  (`adversary.kind: none`) and never entered an execution bundle. The
  omission-carrier chain coverage here therefore rides on the OcciAI-shaped
  contract-kit fixtures, not on saved bundle bytes.
- The MiniAirbnb run (`20260914-miniairbnb-first-baseline`) has no execution
  bundle. This fixture suite records that absence without inferring the
  disposition of every candidate.
- Neither saved bundle carries a `conversation_context` stimulus; that
  coverage comes from the kit-derived `occiai-conversation/` fixture.
