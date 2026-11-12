The compiled documents each case's package produces, pinned byte for byte; the compile
tests compare what `compile_injection_spec` writes with them, so a change in the compiled
document's bytes is a visible decision. Regenerate them with the case builder's entry
point: `uv run python packages/asago-injection-spec/tests/injection_spec_cases.py
packages/asago-injection-spec/tests/fixtures/compiled`.
