# Rubric judge fixtures

`turns.json` is MiDojo's (`midojo:tests/fixtures/rubric/turns.json`), kept here byte for
byte because `test_judge_service.py` reads it: the fixture holds the conversations the
judge service grades and the judge records Garak made for them.

The rubric judge now lives in `midojo_spec.rubric_judge`; its original vectors and
fidelity cases moved into MiDojo's fixture set. The Garak adapter keeps `turns.json`
because its judge-service test reads the exact conversations and requests.

The generators stay here because they need Garak's interpreter and, for
`generate_fidelity.py` and `generate_turns.py`, this package's sink and adapter:

- `generate_vectors.py GARAK_REVISION SINK_MODULE vectors.json` writes the vectors
  `midojo:tests/test_rubric_judge_vectors.py` reads.
- `generate_fidelity.py RUNS_DIR SINK_MODULE fidelity.json` writes the pairs
  `midojo:tests/test_rubric_judge_fidelity.py` reads.
- `generate_turns.py cases|judge ...` writes the conversations
  `midojo:tests/test_rubric_judge_turns.py` and `test_judge_service.py` here read.
