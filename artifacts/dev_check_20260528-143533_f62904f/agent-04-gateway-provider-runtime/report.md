# Agent 04

Agent ID:
`019e6d48-676a-7dd3-bf74-e5a6c9f11bba`

状态：
已完成

发现：
1. `[P1]` `prefix_hash` 在 turn trace、gateway trace、context economy trace 里不是同一个口径，当前观测面已经发生语义混用。  
[context_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/context_engine.py:207) 生成的是 frozen-prefix 的 MD5；[planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:236) 把它写进 `continuity.prefix_hash`；[gateway_lane.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_lane.py:86) 的 `debug_meta["prefix_hash"]` 实际写的是 `effective_prefix_hash`；[center.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/context_economy/center.py:228) / [models.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/context_economy/models.py:87) 里的 `WorkloadTrace.prefix_hash` 又固定代表 `stable_prefix_hash`。这会让跨层追 trace 时把“语义前缀”“有效前缀”“稳定壳前缀”误当成同一个字段。

2. `[P1]` 主回复 `chat` lane 没有和 `tool` lane 同级别的 gateway stage trace，gateway/lane/turn trace 口径不一致。  
`tool` 路径会写 `gateway_tool_call` / `gateway_tool_call_failure`（[gateway_lane.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_lane.py:538), [gateway_lane.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_lane.py:662)），但普通 `chat` 路径把核心调用下沉到 [gateway_call.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_call.py:113)，这里既没有 `event` 形参也没有 trace append；[gateway_lane.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_lane.py:237) 只在成功后补写 request trace。结果是主回复链看得到 turn trace，但看不到和 tool 模式同粒度的 provider 尝试/切换/失败轨迹。

3. `[P2]` reverse-session hack 仍然侵入主链，而且把 `provider_visible_system_hash` 和 `post_hook_system_hash` 绑定在 `main.py` 的全局 hook 上。  
[main.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:78) 仍通过 `on_llm_request` 注入；[main.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:102) 会统一回写 `provider_visible_system_hash` 与 `post_hook_system_hash`。即使不是 Gemini reverse，请求也会在这里重算 post-hook hash，只是 prompt 可能不变。这意味着“最终 provider 可见 system”的事实来源不在 gateway/runtime 内闭合，当前两类 hash 在数据上也基本被写成同一个值。

4. `[P2]` lane/session/cache affinity 是真实落地的，但观测命名会高估“可缓存性”。  
真实落地点在 [lane_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/lane_manager.py:124) 的 lane rotate 条件、[lane_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/lane_manager.py:143) 的远端 session 拆分，以及 [gateway_lane.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_lane.py:105) / [gateway_lane.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_lane.py:112) 的 provider-aware `session_id`/`cache_control` 注入。  
但 [center.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/context_economy/center.py:155) 的 `cache_affinity_enabled` 只按 policy 置位，随后 [gateway_result.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_result.py:79) 和 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:340) 又把它当成 `cache_ready` 证据之一；而 `runner` 家族明明在 [provider_capabilities.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/provider_capabilities.py:36) 声明 `supports_native_prompt_cache=False`。所以这里更像“affinity 已准备”而不是“缓存能力已成立”。

验证：
- `python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_context_economy_refactor.py tests/test_main_reverse_session_hook_refactor.py tests/test_main_reply_request_trace_refactor.py -q` 通过，`26 passed`。
- `python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_gateway_policy_refactor.py tests/test_gateway_vision_refactor.py -q` 通过，`14 passed`。
- `python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_context_economy_refactor.py tests/test_main_reverse_session_hook_refactor.py tests/test_main_reply_request_trace_refactor.py tests/original_ported/test_gateway_lane_request_kwargs_ported.py -q` 有 `1` 个失败：旧移植测试 [test_gateway_lane_request_kwargs_ported.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/original_ported/test_gateway_lane_request_kwargs_ported.py:99) 仍假设 `session_id` 以 lane identity 结尾，但当前实现会追加 provider 维度后缀，这和 [lane_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/lane_manager.py:149) 的现设计一致，更像测试预期漂移，不像现行代码回归。

补充判断：
- `prompt_templates` 注册表虽然大，但在当前仓内没有明显“只注册不调用”的脱节迹象；代表性生产调用点能在 [persona_summarizer.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/persona/persona_summarizer.py:354)、[memory_processor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_processor.py:79)、[wakeup_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/wakeup_service.py:228)、[context_compaction.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/context_compaction.py:1872) 找到。
- 本窗口未修改任何文件。
