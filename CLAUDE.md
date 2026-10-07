# Asago Artifact Generator — agent guide

Turn supplied scenarios into useful, testable artifacts. Keep this guide short;
contracts and historical experiment details belong in their own documents.

## Start here

- Check the working directory, branch, and `git status --short`. Use the checkout
  named by the task; several worktrees contain different implementations.
- Read the current task specification, the relevant README section, and the
  implementation. User decisions take precedence over superseded designs.
- Preserve unrelated changes and historical evidence. Stage only your own work;
  commit or push when requested.

## Ownership and workflow

- The producer supplies scenario meaning: narrative, attack tree, Gherkin,
  failure criterion, safe alternative, and metadata.
- This consumer designs concrete stimuli, setup declarations, runtime bindings,
  and semantic judge specifications using supplied facts and documented
  capabilities. Packages (`artifact-package-v3`) contain no detector code.
- `generate` is target-free. The plan owns the experiment; artifact authoring turns
  the accepted plan into a frozen package. Code checks run before the applicable
  semantic review. Corrections stay within their stage.
- Downstream tooling alone performs live setup, target/Garak generation and
  detection, runtime judging, evidence collection, and cleanup. This repo does not do those actions
  during authoring.
- Preserve saved package and evidence formats where required by the task. Do
  not redirect new work into an older workflow because its code still exists.

## Commands

Run from this checkout; read `generate --help` for configuration and limits.

```bash
uv sync --locked
uv run asago-artifact-generator generate --help
uv run pytest tests/path_to_changed_test.py -q
./scripts/quality.sh
uv run pytest tests/ -q
```

Use focused tests during implementation and the required broad gates once at
final delivery. Repeat only for relevant changes or unresolved failures.
Deterministic tests must not contact a model endpoint or target.

## Model-facing work

- Let the LLM design and reason. Code validates structure, resolves documented
  references, preserves accepted inputs, and executes the declared checks. Do
  not replace semantic reasoning with keyword or phrase matching.
- Give every requested field, identifier, source, and destination an explanation
  and a consistent example. Inspect the actual dispatched system/user messages.
- Keep generic instructions separate from scenario facts and failure feedback.
  Case-specific repairs are assisted results, not proof of autonomous design.
- Deliver owner-specified correction text verbatim. Include the exact previous
  output and all relevant validation findings. Preserve
  valid sibling fields; a shortened paraphrase can change the requested fix.
- An accepted plan is fixed during artifact authoring. If it needs revision,
  report that explicitly rather than silently changing the experiment.
- Keep final content, reasoning, finish reason, and usage separate. Missing final
  content is not proof that no tokens were generated. Never parse reasoning as
  a substitute final answer.
- A review is fallible evidence. Check findings against the supplied contract;
  an acceptance still needs deterministic checks. Keep genuine
  unknowns explicit rather than weakening tests to obtain a pass.

## Live requests and saved work

- Before dispatch, apply the current task's provider/data approval and explicit
  request allowance. The paired producer records standing private-model approval
  in `docs/development/private-live-model-approval.md`.
- Read profile credentials in process; keep them out of prompts, packages,
  logs, shell output, and Git. Use the task's explicit profile path.
- Record actual model, thinking, token limits, finish state, and spend. Thinking
  is a per-role control, not a universal improvement. Respect the request's
  context limit and stop policy; do not enlarge allowances. The transport makes
  one recorded retry after a transport error (HTTP 5xx or a non-timeout
  connection error) and nothing else retries: not timeouts, 4xx, or invalid
  responses. The retry counts against the budget and appears in the ledger as
  `transport_retries`; SDK `max_retries` stays 0.
- Reuse intact accepted plans or artifacts when authorized. Verify their exact
  hashes and provenance; do not regenerate them merely because a later stage
  failed. A new task name does not reset historical spend.
- Preserve saved failures and frozen packages. New attempts use new evidence
  locations. Do not hand-edit generated output and label it model-authored.

## Where to look and when to stop

- `README.md` and CLI help: authoring usage, profiles, policy, and package format.
- `src/asago_artifact_generator/authoring/`: `orchestrator.py` runs the
  stages; `prompt_context.py`, `prompt_packets.py`, `review.py`, and
  `correction.py` build prompts; `contracts.py` and `checks.py` hold response
  contracts and structural checks; `transport.py` is the provider client.
- `src/asago_artifact_generator/bindings.py`: binding sources and destinations.
- `src/asago_artifact_generator/package_io.py`: the artifact-package-v3 writer and loader.
- `tests/`: executable examples and regression coverage for the changed behavior.
- Orch qualification runbook `asago-orch/docs/qualification.md`; its modules
  live under `asago-orch/src/asago_orch/qualification/`.

Finish the requested slice once its behavior and required checks pass. Report
case outcomes, blockers, actual spend, and evidence paths. A passing suite is
not a successful experiment; a command attempt is not a completed effect.
Keep unrelated formatting, new frameworks, and speculative hardening out of the
critical path.

`AGENTS.md` is a symlink to this file. Edit `CLAUDE.md`; preserve the symlink.
