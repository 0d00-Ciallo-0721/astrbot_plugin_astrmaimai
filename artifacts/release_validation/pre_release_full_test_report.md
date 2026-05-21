# Pre-release Full Test Report

- generated_at: `2026-05-21T01:28:31`
- release_ready: `False`
- overall_status: `failed`

## Static Gate
- status: `passed`
- compiled_count: `989`

## Local Regression Groups
- `state_and_reply_chain`: `passed` (8 modules, 1.417s)
- `scheduler_and_proactive`: `passed` (5 modules, 1.079s)
- `memory_review_learning_persona`: `passed` (10 modules, 7.314s)
- `webui_plugin_fixture`: `passed` (4 modules, 8.412s)
- `umbrella_regression_suites`: `passed` (3 modules, 9.183s)
- `architecture_contracts`: `passed` (4 modules, 2.550s)

## Real Provider Core Chain
- mood live: `not_run`
- host ingress matched: `False`
- host post-send matched: `False`

## Provider Matrix
- recommended: `none`
- backup: `none`
- not recommended: `none`

## Plugin Model Pool
- status: `not_run`
- distinct fallback available: `False`

## Fallback Validation
- status: `not_run`
- mode: ``

## Scheduler and Admin Smoke
- status: `passed`
- pending reviews: `1`
- page acceptance artifacts: `10`

## Business Smoke
- `group_at_bot_normal_qa`: `failed` (host_mood_chain_audit)
- `negative_or_sarcasm_input`: `failed` (host_reply_post_send_audit)
- `tool_memory_intent`: `failed` (live_mood_semantic_audit + local planner regressions)
- `private_chat_energy_exception_and_mood_update`: `failed` (host_reply_post_send_audit + state/reply tests)
- `scheduler_and_proactive`: `passed` (fresh scheduler benchmark + fixture runtime diagnostics)
- `admin_console_pages`: `passed` (existing browser acceptance artifacts + current fixture smoke)

## Notes
- 本报告把真实 provider、宿主 mood 主链、reply_post_send/social_score、scheduler fixture、管理台验收资产整合到同一份上线前证据里。
- 浏览器点击流本轮复用了已存在的宿主页/直开页验收产物；本次执行重点补的是 provider、多模型、fallback 和统一汇总。
