# Agent 04

Agent ID:
`019e71b7-b285-79f0-9fff-bd26f206b5cd`

状态：
已完成

发现：
- `[P1]` 远端 provider session 没有跟随 “`prefix_hash` 触发的 lane 旋转” 一起切断。lane 的旋转判定和新 conversation 创建发生在 [lane_storage.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/lane_storage.py:36) 和 [lane_storage.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/lane_storage.py:59)，但 remote session key 只包含 `provider_family:model_id:lane_umo`，见 [lane_manager.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/lane_manager.py:146)。对 `runner` 家族 provider 而言，这会让“新 lane / 新 conversation”继续复用旧 remote session。
- `[P2]` `prefix_hash` 在 turn trace 和 gateway/lane trace 里仍然不是同一个口径。主回复侧的 `semantic_system_hash/prefix_hash` 来自 frozen prefix 的 MD5，见 [context_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/context_engine.py:206) 和 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:236)；而 gateway/context-economy 里的 `prefix_hash` 写的是 `effective_prefix_hash`，cache-priority 家族下它又会退化成 `stable_prefix_hash`。
- `[P2]` `cache_affinity_enabled` 的 turn-trace 观测仍把 “enabled” 和 “ready” 混在一起。request trace 里这两个字段原本分开，但事件 extra `astrmai_cache_affinity_enabled` 实际写入的是 `cache_affinity_ready`，planner 再把它当成 `cache_affinity_enabled` reason 回填 turn trace。
- `[P2]` reverse-session hack 仍然侵入主链。实际改写 `request.system_prompt` 的入口还在全局 `on_llm_request` hook，[main.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:78)；执行层还会继续搬运这组 escape-hatch extras，见 [executor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/executor.py:130)。

测试缺口：
- 现有回归只验证了 `model_id` 变化会切 lane / 切 remote session，没有覆盖 `prefix_hash` 变化、`template/version/schema` 变化时 remote session 也必须切断的场景。
- 自动化覆盖了 turn-trace 汇总字段和手工审计脚本字段存在性，但没有测试 gateway 侧 `GatewayUsage.cache_ready_reasons` 和 planner turn-trace reasons 一致。
- 当前 gateway 代码也没有给 `_build_cache_observation` 填 `prefix_stable/provider_visible_hash_stable`，见 [gateway_result.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_result.py:75) 和 [gateway_call.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/gateway/gateway_call.py:271)。

补充：
`prompt_templates.py` 这轮没看到新的明显渲染断裂；当前残留问题主要集中在 trace 口径和 session/cache affinity，而不是模板内容本身。

验证：
补跑了：
`tests/test_gateway_context_passthrough_refactor.py`、`tests/test_main_reply_request_trace_refactor.py`、`tests/test_main_reverse_session_hook_refactor.py`、`tests/test_reverse_session_refactor.py`、`tests/test_prompt_metrics_compare_refactor.py`、`tests/test_context_economy_benchmark_refactor.py`、`tests/unit/conversation/test_context_runtime_wiring.py`、`tests/test_context_economy_refactor.py`

结果：合计 `68` 个用例通过。
