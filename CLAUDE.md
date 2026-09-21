# Asago Artifact Generator

Policy-driven agentic red-teaming: takes pre-built scenario YAMLs, classifies
their injection surface, and generates red-teaming artifacts for downstream
evaluation platforms.

## Commands

```bash
uv sync --locked
./scripts/quality.sh
uv run pytest tests/ -q
uv run asago-artifact-generator author <scenario-handoff-or-input.json> \
  --inventory <inventory.json> \
  --runtime-contract <runtime-contract.json> \
  --output-dir runs/authoring/<case-id>
uv run asago-artifact-generator check <package-dir> --evidence <evidence.json>
```

`author` is the target-free primary workflow. It defaults to one plan
correction, one artifact correction, and both semantic reviews enabled; the
stages are configured independently through `--plan-max-corrections`,
`--artifact-max-corrections`, `--review-plan/--no-review-plan`,
`--review-artifact/--no-review-artifact`, and `--review-model-profile`.
Deterministic checks and artifact Docker detector controls run before each
semantic review; a reviewer `revise` consumes its own stage's allowance, and a
reviewer `blocked` at the artifact stage stops as `needs_plan_revision`.
`check` executes only supplied evidence through the immutable package.
Deterministic tests do not require an LLM endpoint. Live authoring requires
a configured provider (Gemini, OpenAI, Ollama, Hugging Face, or OpenRouter)
via `.env` or environment variables.

For the approved private endpoint, invoke `author` with
`--profile gemma4-oc --profiles-file /absolute/path/to/asago-scenario-generator/config/model-profiles.yaml`.
The consumer resolves the named profile in process and passes its base URL,
API key, and model directly to `PrivateModelAuthoringTransport`. Credentials
and endpoint values stay in memory and never enter shell output, prompts,
ledgers, packages, or failure evidence. Without `--profile`, environment-only
configuration still works when it supplies a real API key; missing credentials
fail before dispatch.

Prompt roles are versioned independently from the response wire:
`authoring-call1-v3`, `authoring-plan-review-v1`,
`authoring-call2-v3`, `authoring-artifact-review-v1`, and
`authoring-correction-v3`. Each rendered packet exposes a SHA-256 hash and
dispatch evidence records the role, version, hash, raw response, controls,
findings, and review status. The v3 author prompts keep the v2 11-field plan
and two-block artifact contracts unchanged.

Build reviewer contexts from the original scenario and supplied facts,
operations, result schemas, provenance, and runtime capabilities. Do not pass
an author transcript or unrelated plumbing to a reviewer. Artifact authoring
receives the accepted plan as read-only and cannot rewrite plan-owned setup,
bindings, prerequisites, observations, or judge decisions. Correction prompts
show only the active plan or artifact format and all current findings.
Overflow, endpoint/credential values, and bounded duplicate candidate forms
fail before dispatch. The neutral plan example uses a case-permitted status
binding when supplied operations provide one; otherwise it is a labeled
generic illustration with no operation, binding, or prerequisite.

For fresh or resumed runs, pass explicit caller-owned prior spend with
`--prior-author-correction-spend` and `--prior-review-spend`; use `0` for both
fresh counters. The consumer does not discover mission ledgers. It seeds the
two counters before dispatch, enforces four author/correction requests, four
review requests, and eight combined requests per case, and records typed
`budget_exhausted` evidence without provider contact when a cap is exhausted.
The aggregate authoring ceiling remains 32 requests. A resumed run with prior
author/correction spend of `1` therefore has only three author/correction
dispatches available.

An `author` run either writes an immutable package after the configured checks
and reviews pass or writes durable failure evidence. Failure evidence keeps
the terminal stage and typed status, including `unresolved`, `blocked`,
`review_unavailable`, `needs_plan_revision`, transport stop, and budget stop.
The consumer never starts a target, performs setup or generation, calls a
runtime judge, or owns downstream cleanup. Downstream qualification uses the
component-specific `start-safe`, `verify-safe`, and `stop-safe` lifecycle on
gateway port `8321` and target ports `8888`, `8890`, or `8892`.

The recovered A03 candidate uses a separate
`prepare_a03_recovered_continuation(...)` /
`A03RecoveredArtifactContinuation.run(...)` seam. It hash-verifies the
current-mission recovery sidecar, accepted plan, original inputs, exact
candidate, deterministic checks, isolated controls, preserved 5/4 spend
breach, and failed `VAL-LIVE-003` result before transport construction. It
seeds 5 author/correction and 1 review requests, exposes no author or
correction path, permits one artifact-review dispatch with zero retries, and
writes terminal evidence for every non-accept or preflight outcome. Only
`accept` reaches the existing immutable package assembly path; a prepared
continuation cannot run twice.

The sealed
`prepare_o04_correction_continuation(...)` /
`O04CorrectionContinuation.run(...)` seam consumes the exact saved O04
artifact, accepted plan, original PAT-104 and approved-education facts,
runtime contract, eleven controls, historical outcomes, and offline mismatch
proof. It seeds historical 4/1 spend separately from one artifact correction
and one conditional artifact-review allowance. It constructs no plan or fresh
artifact authoring request, uses zero retries, and builds the correction
prompt from the supported `availability.messages`, `completeness.messages`,
and `judge.verdict` packet paths plus incompatible saved reads. The
test-owned conformant detector stays outside the prompt. Unchanged
deterministic checks and controls gate one review, and only review `accept`
reaches existing immutable package assembly; every other outcome writes
terminal evidence without a package.

The `prepare_o04_refinement_continuation(...)` /
`O04RefinementContinuation.run(...)` seam extends that sealed path from the
first continuation's corrected candidate. It verifies the first-continuation
report and preservation chain, seeds factual O04 spend at 5
author/correction and 1 review, and keeps the expired first-continuation
allowances separate. The refinement grants one shared allowance of at most two
corrections and two artifact reviews. Every request uses the latest candidate,
persists raw bytes before validation, records per-attempt findings and budget
state, and sends `chat_template_kwargs.enable_thinking=false` through the
existing transport's additive `extra_body`. Deterministic checks and all
eleven unchanged controls gate review; transport failures, blocked or
unavailable reviews, and exhausted allowances stop without retry. Only
`accept` assembles the immutable package.

The `generate` command remains a read-only compatibility path for historical
scenario YAMLs. New work uses producer `run`, consumer `author`, consumer
`check`, and frozen downstream execution.

The sealed
`prepare_o04_refinement_restart_continuation(...)` /
`run_o04_refinement_restart_continuation(...)` seam is a fresh provider-
recovery continuation, not a retry or reopening of the expired refinement.
Preparation hash-pins the terminal refinement evidence
(`61f8aa1e7e23f7f5921dc8b04f0bf69d4eaecd316a48e4d88fdff6adfa4b801e`),
delivery report
(`c8f50d35612059b5465d71d020c48cc3a0c35e19bb903db34fe1ca4bbaf17b37`), and
accounting
(`58ef7361edf9fcec591090850bb533291bd9183bf9dd80033e19a453ebc7cf0c`), and verifies the prior transport failure consumed one
correction without creating a candidate, review, package, or execution. It
starts from candidate `f374565b...9e4e`, seeds 6 author/correction and 1 review
at aggregate 18 with task limit 11, and requires fresh task, evidence, and
package paths. Earlier historical, first-continuation, and prior-refinement
allowances remain expired and separate.

The readiness record carries one authenticated, non-generative models-surface
read with HTTP 200, the configured model discoverable, and 534.6 ms latency.
The restart records classification and timing without issuing another probe or
persisting endpoint, credential, header, body, or model-list data.

The restart grants one shared allowance of at most two artifact corrections
and two exact-candidate product reviews. Every dispatch persists raw bytes
before validation, fixes
`chat_template_kwargs.enable_thinking=false` through the existing
`extra_body`, and uses zero automatic retries. Review requires all unchanged
deterministic checks and eleven controls to pass. A correction or review
transport failure consumes its role slot and stops the restart. Only review
`accept` assembles the immutable package. The prior 2,501-byte outage HTML
(`0ccdd3b4f240a05716e9dd3e8a7c28afa2b37c5d77a85fb6dc5a644f3b01a2e4`)
remains transport evidence and never becomes candidate input or output.

## Architecture

- `src/asago_artifact_generator/` contains shared domain models, the LLM
  client, the exact-source Docker detector harness, and the `typer` CLI.
- `src/asago_artifact_generator/garak/` contains the Garak platform generator:
  classification, gating, artifact specification, artifact I/O, prompt templates,
  and Garak plugin sources (probe + detector).
- `examples/scenarios/` contains committed input scenario YAMLs.
- `examples/demo/` contains the interactive Jupyter walkthrough and runtime.
- `runs/` holds generated artifacts (gitignored).

The `check` command runs only supplied or synthetic evidence. It executes the
packaged `detector.py` bytes in constrained `/usr/local/bin/docker` using
`python:3.12-slim`; target, model, setup, and discovery transports stay outside
the consumer runtime.

Read `README.md` before changing the pipeline interface. Each platform
generator lives in its own subpackage (`garak/`, future `agentdojo/`, `pyrit/`)
and shares the parent package modules (`extract`, `llm`).

## Development

- Track durable work in GitHub Issues and PRs.
- Run `./scripts/quality.sh` before pushing; CI enforces `ruff` + `pytest`.
- Update `README.md` and this file when an interface or workflow changes.
- `AGENTS.md` is a symlink to this file.
