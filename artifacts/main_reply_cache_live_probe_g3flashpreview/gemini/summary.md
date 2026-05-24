# Main Reply Cache Live Replay

- provider_family: `gemini`
- model: `gemini-3-flash-preview`
- validation_verdict: `unsupported_usage_reporting`
- endpoint_kind: `gemini_generate_content`
- upstream_model_label: `gemini-3-flash-preview`
- provider_family_explainer: Provider family `gemini` means the request path targets Gemini's native generateContent API.
- provider_supports_cache_hint: `False`
- provider_supports_usage_reporting: `False`
- provider_supports_session_id: `False`
- session_reuse_validation_deferred: `True`
- sample_count: `2`
- cache_ready_count: `1`
- cache_ready_rate: `0.5`
- cache_hit_count: `0`
- cache_hit_rate: `0.0`
- unsupported_usage_reporting_count: `2`
- cache_hint_enabled_rate: `0.0`
- cache_hint_observed_enabled: `False`
- cache_ready_but_hit_miss_count: `1`
- cache_ready_reason_frequency: `{'provider_visible_hash_stable': 1, 'semantic_system_hash_stable': 1}`
- hash_stable_count: `1`
- hash_stable_but_cache_miss_count: `1`
- semantic_hash_stable_count: `1`
- semantic_stable_but_provider_visible_changed_count: `0`
- hook_changed_system_case_ids: `[]`

| Case | Cache Ready | Ready Reasons | Cache Hit | input_cached | semantic_system_hash | cache_control | session_id | gateway_system_hash | provider_visible_system_hash | post_hook_system_hash | semantic_hash_stable_vs_previous | hash_stable_vs_previous | hook_changed_system |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| same_chat_turn_1 | False |  | False | 0 | ff4af047ac839146964e924ff7ea94e7 |  |  | 4cb6670d963c7992 | 4cb6670d963c7992 | 4cb6670d963c7992 | False | False | False |
| same_chat_turn_2 | True | semantic_system_hash_stable,provider_visible_hash_stable | False | 0 | ff4af047ac839146964e924ff7ea94e7 |  |  | 4cb6670d963c7992 | 4cb6670d963c7992 | 4cb6670d963c7992 | True | True | False |