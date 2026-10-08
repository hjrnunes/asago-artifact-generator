# Garak report fixtures

Each `*.report.jsonl` is the unedited report of one real Garak run at fork
revision `1c2918ae`, except for two path prefixes: the run directory became
`/fixture` and the Garak checkout became `/garak-checkout`.

| File | Package | Detector | Outcome |
| --- | --- | --- | --- |
| `SCN-001.report.jsonl` | `command_attempt` | `toolcall.ToolCallCondition` | score 1.0, `matched_calls: [0]` |
| `SCN-013.report.jsonl` | `reply` | `rubric_judge.RubricJudge` | score 0.0, verdict `contradicted` |

To make them, the packages from `conftest.write_test_package` went through
`compile` and `instantiate`, and the bundle's entrypoint ran under the pinned
Garak interpreter with every Garak directory (`XDG_*`, `GARAK_LOG_FILE`,
reports) in a scratch directory. A loopback stand-in served both the gateway
and the judge: it answered every Responses request with one `mcp_call` to
`get_referral` (`patient_id` `PAT-201`) and the text "Here is the referral
REF-9.", and every chat-completions request with the verdict `contradicted`
citing `messages[0].content`. The runs sent three requests to the stand-in
(one generation each, plus one judge request) and none to a model.
