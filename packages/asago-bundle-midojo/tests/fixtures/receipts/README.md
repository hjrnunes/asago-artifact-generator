# Saved receipts

`command-single-detected.json` is what `bundle-midojo parse` wrote for
`fixtures/episode/command-single/detected` at consumer `fd0de5a`, with
`--tool-revision 3824922f --adapter-revision abc123`. Tests compare a parse that records
no runtime against it: the receipt's content, and every byte of its formatting, must
still match.

The receipt carries the `execution-receipt-v2` envelope, whose `spec_digest` names the
contract's schema. The 9sy1 amendment added the optional `evaluation.runtime` to the
schema, so the digest moved and that one envelope field differs from this file; the
test carries the installed digest over and compares every other byte. The file also
carries the constant `evaluation.utility` record that every `evaluation` block states
since the utility reservation (`not_applicable`, `no_utility_check`, null value and
pairing); it was added to the saved text and no other byte changed. To regenerate the
file, run `tools/fixture-generators/`-style capture against a parse of the same episode
with no runtime flags, and update the digest the test expects.
