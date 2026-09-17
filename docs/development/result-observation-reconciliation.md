# Result-observation reconciliation — M4 cutover (consumer repo)

Recorded: 2026-09-15T10:23:29Z (programmatic `date -u`). Feature:
`m4-consumer-cutover-reconciliation` (VAL-CUT-005, VAL-CONS-015). Repo
ownership: consumer only; producer-repo items are recorded here and handed to
the orchestrator.

## Current workflow reminder

The producer's current `run` path publishes only the semantics-only
`scenario-handoff-v1`. This consumer's current `design` path owns concrete
messages or user-only history, target binding, setup, detector and fidelity
decisions, freezing, and compilation. Runtime delivery and command, reply,
backend, and state receipts remain separate concerns. The bundle `generate`
and taxonomy-era `generate-legacy` paths below are historical compatibility
seams, not current ownership.

The M4 cutover retires the producer's artifact-authoring and detector-admission
responsibilities. This record reconciles the existing result-observation work
with that cutover: what is reused, what moves downstream, and what happens to
each in-progress item. Every inventory item carries a recorded decision;
nothing is silently dropped.

## 1. Reused runtime/observer capability (decision: reuse, unchanged)

The cutover keeps the existing observation stack; the consumer replaces none of
it and re-implements none of it.

| Capability | Where it lives | How the consumer path reuses it |
| --- | --- | --- |
| Runtime-context capture (`capture_runtime_context.py`) | producer repo `scripts/qualification/` | Its output is the `--runtime-context` design input; the design record and every compiled artifact pin `environment.runtime_context_digest` and `state_digest`. |
| Ledger diff (MiniKlarna refund/payment ledger adapter) | runner (`garak_case_runner.py`), producer repo | Execution-time only. The consumer never claims effect-level evidence; designs record `observation_level: command` with explicit limits ("money movement not observed"). |
| Deterministic predicates (`tool_argument` / `event_order` / `action_absence`) | runner, producer repo | The consumer's detector design compiles to the `tool_argument` structured oracle (`design/compile.py::_structured_oracle`); the runner owns evaluation. No consumer-side predicate duplication. |

Evidence: executed cases in `build/adaptive-e2e/` (producer worktree) —
`m3-klarna-regression/execution/qualification.json`,
`m3-occiai-scn017/execution/`, `af-scn033/execution/` — trace back through
these components: the qualification receipts carry the compiled `case_digest`
and `frozen_content_digest` from the consumer plan, before/after state, and the
ledger/predicate verdicts with observation levels.

## 2. Executable contracts and detector design: assigned downstream (decision: consumer-owned)

Since M2 the executable contract is the consumer's `artifact-design-plan-v1`
with its `DetectorDesign` (tool, field path, comparison, environment-derived
limit, distinguishing rationale), the fidelity assessment, and the
freeze-before-execution record. The compiled `structured_oracle` is derived
from the handoff's semantic failure criterion; stimulus provenance carries
`authored_by: consumer-design`. The producer publishes no detector expression,
oracle selection, or admission decision to depend on. Regression pins:
`tests/test_consumer_cutover_no_admission.py`.

## 3. Producer admission coupling (decision: removed; verified absent consumer-side)

The producer's `admit_oracle_kinds` pre-authoring admission seam was retired
producer-side (revision R28, producer commit `ba7a1ee`). The consumer side
carries no admission reference anywhere:

- The vendored handoff kit (`contracts/scenario-handoff/`) defines no
  admission field.
- The design/handoff modules reference no admission symbol.
- Every design output (design record, exclusion, freeze, plan, executable
  conversation, trace, validation, design manifest) scans free of admission
  markers — pinned by `test_design_outputs_reference_no_producer_admission`.

Consequently a scenario the retired producer path would have suppressed for
lacking a compilable oracle kind reaches the consumer like any other: it is
either designed (a supported detector shape exists) or typed-excluded
(`unsupported-criterion-shape`, `unsupported-observation`, and siblings) with
the scenario kept visible. The wrong-timing diagnostic shape (SCN-034) is the
standing representative of the suppressed class; the refund handoff (SCN-007)
is the compiling control.

## 4. In-progress work inventory and decisions (cutover start 2026-09-15)

Inventory taken at cutover start across both repos. Decisions:

| # | Item | Location | Decision |
| --- | --- | --- | --- |
| 1 | Result-sensitive observation design, revision 2 (`tool_result` check kind, `result` observation label; projection-v4/bundle-v3 kits + profile-v1 `result_contracts` amendment) | original checkout `docs/development/designs/result-sensitive-observation-spec-2026-09-14.md` (producer repo) | **Preserved, not integrated at cutover.** Unimplemented, awaiting owner review. Its authority gate is framed as a producer-side admission (reviewed obligation + profile contract admitted before authoring); under the cutover ownership that coupling is retired. If revived, it must land as a consumer detector shape designed from the handoff's semantic criterion, with the reviewed per-operation result contract supplied through the explicit environment (target profile), one-pattern-at-a-time per the adaptation rule. Producer/owner item — handed to the orchestrator. |
| 2 | Result-observation prototype (commit `7c077dc`, `feature/track2-result-observation` worktree) | separate producer worktree | **Preserved untouched** as historical proposal evidence. Off-limits worktree; no integration. |
| 3 | Track-integration review, read-observation context comparison, qualification-reports | original checkout `docs/development/qualification-reports/`, `ai/findings/` | **Preserved** as historical records (immutable). They document the pre-mission observation boundary decisions the new path respects. |
| 4 | Cross-repo smoke driver (`crossrepo_smoke.py` + `test_crossrepo_smoke.py`) | original checkout `scripts/qualification/`; consumer mirror `tests/test_crossrepo_smoke.py` | **Reused.** The mission's integration-test pattern per the testing strategy; the consumer-side mirror remains green. |
| 5 | Qualification runner, runtime-context capture, ledger adapters, deterministic predicates | producer repo `scripts/qualification/` | **Reused unchanged** (see section 1). No producer-side integration needed. |
| 6 | Consumer legacy result-observation machinery (`garak/classify.py`, `garak/gate.py`, `garak/gen.py` legacy oracle-kind classification; presentation-slot `authoring.py`; historical bundle execution path) | consumer repo `src/asago_artifact_generator/` | **Preserved as isolated historical seams.** Excluded from the design path's authority chain: design outputs reference no legacy classification, and the legacy `generate` bundle path stays historical-only. |
| 7 | Consumer legacy verbatim delivery (`_verbatim_slot_ids`, `_messages_and_carrier_tools`) | consumer repo `garak/conversation.py` | **Retired from the authority chain** (retired M2 per architecture; historical bundle compilations unchanged). |
| 8 | Vendored scenario-handoff kit (`contracts/scenario-handoff/`, `UPSTREAM.lock` @ producer `3467611`) | consumer repo | **Preserved** byte-for-byte; no kit extension needed for the cutover. |

No inventory item was dropped, rewritten, or silently superseded.

## 5. Producer-repo items handed to the orchestrator

- Inventory item 1 (result-sensitive observation design) is an owner decision
  on unimplemented scope. No producer-side change is required for this
  cutover; revival requires coordinated kit work (profile-v1 amendment plus
  the consumer detector shape) and stays out of this mission.

## 6. Verification pointers

- `tests/test_consumer_cutover_no_admission.py` — 5 pins: typed exclusion for
  the no-admission scenario, compiling control, zero admission references in
  all design outputs, source-level admission-seam scan, kit admission-field
  absence.
- Executed-case evidence chains: `build/adaptive-e2e/` (producer worktree,
  untracked): `m3-klarna-regression/`, `m3-occiai-scn017/`, `af-scn033/`.
- Design-path provenance records: `design-record.json` and
  `artifact-trace.json` per design (`design_id`, `case_digest`,
  `handoff_digest`, `environment.runtime_context_digest`, authoring attempts).
