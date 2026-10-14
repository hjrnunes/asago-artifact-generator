# model-profile-v1

A model profile is one named entry in a model-profiles YAML file. The entry holds
a model's connection (`base_url`, `model`, `api_key`) and its request settings.
The producer owns this contract. Every code path that turns a profile entry into
loaded values or request fields (a reader) runs the cases in this directory as a
parametrized test. Consumers copy the directory byte for byte.

`fields.json` is the single field list. Adding a model means adding a profile to
the YAML file. Adding a field means changing this contract first (the field
table and its cases), then each reader.

## Files

| File | Content |
| --- | --- |
| `fields.json` | One row per field: type, validity rule, which readers require and use it, the default per reader, a list of valid and a list of invalid example values. |
| `load-cases.json` | Whole-entry cases: the minimal and full entry, null as absent, unknown fields, missing required fields, a value that must not appear in a message, the nested file shape, an unknown profile name. |
| `request-cases.json` | A profile plus call parameters, with the `create()` request each reader sends, including the second request after a 429. |

`../CONTRACT.lock` records the SHA-256 digest of each file.

## Readers

| Id | Reader |
| --- | --- |
| `producer` | `model_profiles.load_profile`, `resolve_llm_client_from_profile`, `LLMClient` |
| `consumer` | `profiles.load_authoring_profile`, `cli._transport_options` and `_request_body`, `PrivateModelAuthoringTransport` |
| `orch-gateway` | `qualification/model_profiles.read_profile_settings` (the OGX gateway and the judge) |
| `orch-client` | `qualification/model_runtime.LLMClient`, built by `resolve_llm_client_from_profile` (capture's planner) |

A reader accepts every field in the table and uses only the fields in its
`used_by` list. Each repository's test runs the cases for its own readers.

## Load rules

- **Unknown field.** Every reader rejects a field outside the table, also with a
  null value. The rejection names the field and never its value.
- **Validity.** Each field has one rule in the table. Every reader applies it to
  every field of the entry at load time, whether or not the reader uses the field.
  A rejection names the field and never its value.
- **Null.** A null value counts as absent.
- **Required.** `required_by` lists the readers that reject an absent field.
  `orch-gateway` and `orch-client` default an absent `api_key` to `unused`.
- **Defaults.** `default` gives the loaded value of an absent field per reader:
  `*` is the default, any other key overrides it for that reader. No field has
  an override today. An absent `timeout` is 600 seconds and an absent
  `strict_json_schema` is false in every reader that uses them.
- **Numbers** are finite. Booleans, strings and floats never stand in for an
  integer.
- **File shape.** The producer reads a flat mapping of profile names. The
  consumer and orch also read a mapping nested under `profiles`.
- **Loaded value.** The loaded value of a used field is the profile value, or
  the default when the entry omits it. `headers` loads as the producer's
  `extra_headers` and orch's `headers`; the producer adds the OpenRouter headers
  only for an `openrouter.ai` URL, which no case uses.

## Request rules

A request case lists, per reader (or `*` for all readers without their own row),
the `create()` keywords the reader sends. The comparison leaves out `messages`
and replaces `response_format` with its `type`, `name` and `strict` entries.

- `sampling_controls: false` drops `temperature`, `top_p`, `top_k`, `seed`, the
  thinking switch and the repetition penalty; `max_completion_tokens`,
  `reasoning_effort` and `service_tier` stay.
- A profile with `reasoning_effort` raises a call-site completion cap to the
  profile cap when the call-site cap is smaller.
- With `service_tier` and `service_tier_fallback` set, a 429 on the first request
  triggers a second request that differs only in `service_tier`. Without a
  fallback tier the 429 propagates.
- `repetition_penalty`, `top_k` and `enable_thinking` go into `extra_body`;
  `temperature`, `top_p`, `seed`, `reasoning_effort` and `service_tier` stay top
  level.

## Intended differences

| Id | Difference |
| --- | --- |
| I1 | Orch defaults an absent `api_key` to `unused`. |
| I2 | The consumer redacts connection values in `repr`. |
| I3 | Consumer authoring sends the transport's temperature (0.0), not the profile's. |
| I4 | The consumer sets thinking per role (author and review bodies send `enable_thinking: false`) and ignores `enable_thinking`, `top_p`, `top_k`, `seed`, `headers`, `use_guided_decoding`, `json_schema_strict` and `safety_margin` in a request. |
| I5 | The consumer has no call-site cap. A review fills the remaining context when the profile cap is at most 8192, and is capped at the profile cap when it is larger. The case number depends on the consumer's prompt estimate for the texts `S` and `U`. |
| I6 | `orch-gateway` uses only `base_url`, `model` and `api_key`. |
| I7 | The consumer and orch read a nested `profiles` mapping; the producer does not. |
| I8 | The producer fills an absent field from its `ASAGO_SCENARIO_GENERATOR_*` variables. The cases run with them unset. |

## Call parameters

`call_defaults` in `request-cases.json` lists the parameters and their defaults:
the texts `system` and `user`, the `role` (`author` or `review`, used by the
consumer), `structured` (the call passes a `Reply` response model), the call-site
`max_completion_tokens` and `temperature`, and `first_error` (a status code the
fake client raises as the SDK's rate-limit error on the first request).
