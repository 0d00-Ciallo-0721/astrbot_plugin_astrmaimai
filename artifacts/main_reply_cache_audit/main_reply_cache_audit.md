# 主回复 LLM 构造 / 注入 / Token Cache 深度分析

## 结论摘要

- 可缓存性：主回复当前具备部分 cache-friendly 结构，但 `prefix_hash` 只覆盖 `frozen_prefix`，不能代表最终 provider-visible system 全稳定。
- 真实命中证据：当前只有 provider 返回 `usage.input_cached` 时才能证明命中；其余 `prefix_stable / cache_affinity_ready / provider_session_usage` 都只是准备度指标。
- token 节约效果：当前主回复更明确的是“输入缩短节约”，缓存命中节约仍需 provider usage 证据支持。

## 主链调用图

```text
message_entry -> system2_runner -> planner.plan_and_execute
  -> context_engine.build_prompt
  -> planner_side_inputs/_apply_private_jump_context/_append_mode_instructions
  -> prompt_refiner.refine_prompt
  -> executor.execute
  -> gateway.chat_in_lane_result / gateway.tool_chat_in_lane_result
  -> context.llm_generate / context.tool_loop_agent
  -> main.on_llm_request (Gemini reverse-session post-hook)
```

## 关键指标

- prefix_hash: `ff4af047ac839146964e924ff7ea94e7`
- semantic_system_hash: `ff4af047ac839146964e924ff7ea94e7`
- semantic_system_length: `240`
- provider_visible_system_hash: `4cb6670d963c7992`
- post_hook_system_hash: `4cb6670d963c7992`
- provider_visible_prompt_hash: `e3a3a6765ad797a9`
- frozen_prefix_length: `240`
- semi_stable_length: `0`
- dynamic_prompt_length: `102`
- dynamic_prompt_blocks.soft_background: `0`
- request_provider_family: `native_chat`
- request_model_id: `offline-audit-model`
- request_session_id: ``
- request_cache_control: ``
- cache_ready: `False`
- cache_ready_reasons: `[]`
- cache_hit: `False`
- cache_hit_evidence_supported: `False`
- usage_input_cached: `0`

## 注入矩阵

| 注入位置 | 注入目标 | 是否每轮变化 | 是否 provider 可见 | 是否被 prefix_hash 统计 | 是否影响 lane/session/cache |
| --- | --- | --- | --- | --- | --- |
| context_engine.frozen_prefix | system | False | True | True | yes |
| context_engine.soft_background_block | prompt | False | True | False | dynamic_background_tail |
| prompt_refiner.time_anchor | prompt | True | True | False | dynamic_tail_only |
| prompt_refiner.cognitive_drive_block | prompt | True | True | False | dynamic_tail_only |
| prompt_refiner.soft_background_block | prompt | False | True | False | budgeted_background_tail |
| prompt_refiner.situational_context_block | prompt | True | True | False | dynamic_tail_only |
| planner_side_inputs.planner_runtime_instruction_block | runtime_prompt | True | True | False | runtime_control_tail |
| gateway.request_kwargs | request_kwargs | False | True | False | session_or_cache_hint |
| main.on_llm_request.gemini_reverse_session | system | provider_dependent | True | False | post_hook_system_change |

## Provider Family 判定

| Provider Family | 显式 cache hint | remote session | 原生 prompt cache | 主回复当前代码路径 | 判定 |
| --- | --- | --- | --- | --- | --- |
| anthropic | True | False | True | cache_control available on chat/tool lane request kwargs | best-prepared among current main-reply families |
| gemini | False | False | True | no cache_control; post-hook reverse-session sentinel can modify provider-visible system | potentially cacheable but trace must distinguish pre-hook and post-hook system hashes |
| native_chat | False | False | True | depends on provider native behavior; code only records usage.input_cached when provider returns it | code can be cache-friendly but cannot force or prove cache hit without provider usage evidence |
| runner | False | True | False | session reuse may improve continuity, not equivalent to token cache hit | do not count provider_session_reuse as cache hit |
| sample_from_trace | False | False | unknown_without_provider_usage | native_chat | {'provider_family': 'native_chat', 'request_model_id': 'offline-audit-model', 'usage_input_cached': 0} |

## 主要发现

- [high] prefix_hash 只覆盖 frozen_prefix，不覆盖整个 provider-visible system: prefix_stable 不等于最终 system prompt 稳定，不能直接当作 token cache 命中前提。
- [medium] semantic_system_hash 稳定但 provider_visible_system_hash 波动时，应归因为 hook/provider 层: 这说明主回复硬系统语义层稳定，但 provider 最终可见字符串仍可能被 hook 或 provider 特定处理改变。
- [medium] 主回复软背景已迁到 prompt，但仍需预算裁剪避免背景主导当前回复: system 抖动已经下降，但如果软背景不做预算和优先级控制，仍会干扰当前用户问题的主线回复。
- [medium] Gemini reverse-session hook 是 post-gateway 的 system 注入，天然属于统计盲区风险: 若 post_hook_system_hash 与 gateway 侧 system hash 不一致，则 context_economy 对最终可缓存前缀的判断会失真。
- [high] provider_session_usage_rate / prefix_stable / cache_affinity_ready_rate 都不是 cache hit 证据: 只有 provider 返回 cached input tokens 或等价 usage 字段，才能证明真实命中。
- [medium] cold summary / recent transcript 裁剪 / memory budget 属于缩短输入，不等于缓存命中: 报告里必须把“输入变短节约”与“缓存命中节约”分开统计。

## 节约 Token 口径

- 缓存型节约：只有 provider usage 明确返回 `input_cached` 或等价字段才算。
- 缩短输入型节约：`cold summary`、`recent transcript` 裁剪、`memory budget`、动态块迁到 prompt 都属于这类。

## 验证边界

- 这份报告默认使用离线 replay，不证明真实 provider 已命中缓存。
- 若需要证明真实命中，必须在真实 provider 上复跑，并观察 `usage.input_cached` 连续变化。