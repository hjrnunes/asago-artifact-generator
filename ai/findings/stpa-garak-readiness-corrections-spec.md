# STPA-to-Garak Readiness Corrections — Consumer Specification

Status: approved for implementation
Date: 2026-09-03

This is the artifact-generator companion to the producer specification
`asago-scenario-generator/ai/findings/stpa-garak-readiness-corrections-spec.md`.

The Garak adapter deterministically supplies mechanics it owns: direct prompts
use a user turn, conversation-context stimuli use prompt history, model output
uses chat completion plus the target response, and model-output conditions use
an output-text semantic observer. These defaults are merged with an optional
typed `RuntimeBindingSet`; explicit values retain authority for their exact
keys and contradictions remain invalid.

Only the causal factor selected by the stimulus and the final target action
need runtime surfaces. Other structural factors remain trace provenance and do
not become prompt messages or writable deployment surfaces.

The adapter never derives target tools, resources, credentials, endpoints,
semantic placeholder values, tool arguments, indirect carrier invocations,
clocks, persistent state, multi-agent channels, or external state observers.
Tool and other external actions therefore continue to require an explicit
target or simulation profile before Garak runtime defaults are considered.

For model output, the compiled oracle carries the semantic property, operator,
and expected literal. Its judge description asks whether the target response
semantically satisfies that proposition; it is not a byte-for-byte response
comparison. A producer placeholder remains `needs_semantic_binding`.

The normal `generate` command applies these deterministic defaults, including
in `--readiness-only` mode. Probe changes and Garak campaign execution remain
outside this repository and are deferred.
