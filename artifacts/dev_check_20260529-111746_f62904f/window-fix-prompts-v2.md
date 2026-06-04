# 10 个窗口修复提示词（基于 2026-05-29 复检）

适用报告：
- `artifacts/dev_check_20260529-111746_f62904f/overall-summary.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-01-plugin-entry-and-bootstrap.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-02-conversation-mainline.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-03-attention-and-compaction.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-04-gateway-provider-runtime.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-05-memory-system.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-06-state-relationship-private-chat.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-07-proactive-heartflow-cron.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-08-webui-admin-backend.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-09-presentation-and-ingress.md`
- `artifacts/dev_check_20260529-111746_f62904f/agent-10-architecture-boundaries.md`

使用原则：
- 这是“修复提示词”，不是重新做检查。
- 每个窗口都必须先读总报告和本窗口报告，再回到真实代码、调用链和测试核对。
- 报告只是线索，不是事实本身；如果前面窗口已修掉某个问题，必须明确写“报告已漂移/本轮未复现”，不要机械重复修改。
- 每个窗口只修自己的问题，不越界扩修。
- 修改前先跑最小相关基线测试；修改后立刻回归。
- 若发现需要改动共享核心文件且可能影响前后窗口，先说明，不要静默扩大范围。

推荐发送顺序：
`9 -> 7 -> 3 -> 6 -> 5 -> 4 -> 2 -> 8 -> 1 -> 10`

如果你坚持按编号发送也可以，但推荐顺序更贴近本轮 `P1 -> P2 -> 架构收口` 的优先级。

统一输出格式：
- 修复目标：
- 已读依据：
- 实施内容：
- 修改文件：
- 验证：
- 剩余风险：

---

## 窗口 1：插件入口与装配层

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

本窗口任务：修复插件入口与装配层残留问题，不做重新检查。

必须先读取：
- `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final\artifacts\dev_check_20260529-111746_f62904f\overall-summary.md`
- `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final\artifacts\dev_check_20260529-111746_f62904f\agent-01-plugin-entry-and-bootstrap.md`

本窗口只处理：
1. `life.enable_proactive` 热配置后 runtime 不补建/不启动 `proactive_task`
2. `terminate()` 缺少异常安全清理
3. `APPLY_STATUS` / `ACTIVE_FACADE` 这类进程级全局状态串味
4. 与以上问题直接相关的测试缺口

优先文件：
- `main.py`
- `astrmai/app/bootstrap.py`
- `astrmai/app/plugin_facade.py`
- `astrmai/app/runtime_context.py`
- `astrmai/app/lifecycle.py`
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/webui/plugin_pages.py`

允许只读参考：
- `astrmai/proactive/proactive_task.py`
- `tests/test_webui_backend_refactor.py`
- `tests/test_plugin_pages_admin_refactor.py`
- `tests/unit/conversation/test_context_runtime_wiring.py`
- `tests/integration/runtime/test_runtime_contracts_migrated.py`

执行规则：
1. 先读报告，再读真实代码与相邻调用点。
2. 如果前面窗口已让本报告失效，明确写“报告已漂移”，不要重复改。
3. 只修这 4 类问题，不做大规模 facade/compat 重构。
4. 先跑最小基线测试，再改，再回归。

建议先跑：
- `PYTHONPATH=. pytest tests/unit/conversation/test_context_runtime_wiring.py tests/integration/runtime tests/test_webui_backend_refactor.py tests/test_plugin_pages_admin_refactor.py -q`
```

---

## 窗口 2：对话主链路

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-02-conversation-mainline.md`

本窗口只处理：
1. tool 模式普通异常不会切第二模型
2. `skipped_wait/ignore` 无回复分支没有走共享收尾
3. `TurnContext` 仍承担过多运行时语义
4. planner fire-and-forget 副作用缺少失败路径保护或测试

优先文件：
- `astrmai/conversation/execution/executor.py`
- `astrmai/conversation/planning/planner.py`
- `astrmai/conversation/contracts/turn_context.py`
- `astrmai/conversation/planning/prompt_refiner.py`
- `astrmai/conversation/planning/planning_input_loader.py`
- `astrmai/conversation/execution/reply_post_send.py`

允许只读参考：
- `astrmai/conversation/planning/context_engine.py`
- `astrmai/conversation/planning/think_level_policy.py`
- `tests/test_executor_refactor.py`
- `tests/test_planner_cognitive_loop_refactor.py`
- `tests/test_turn_context_refactor.py`

注意：
1. 先修 `P1` 行为问题，再考虑 `TurnContext` 收口。
2. 不要顺手重写整个 planner；只做可验证的最小闭环。

建议先跑：
- `python -m pytest tests/test_planner_side_inputs_refactor.py tests/test_planner_cognitive_loop_refactor.py tests/test_executor_refactor.py tests/test_turn_context_refactor.py tests/original_ported/test_prompt_refiner_focus_layout_ported.py tests/regression/conversation tests/unit/conversation/test_context_runtime_wiring.py -q`
```

---

## 窗口 3：注意力 / 上下文压缩

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-03-attention-and-compaction.md`

本窗口只处理：
1. `near_context_followup` 假阳性导致无关群聊抢主线
2. warm 层“捞回”过旧 assistant 回合，造成 social/recent transcript 漂移
3. compaction provider session 契约测试漂移

优先文件：
- `astrmai/conversation/attention/gate.py`
- `astrmai/conversation/attention/focus_selector.py`
- `astrmai/conversation/attention/group_dialogue_store.py`
- `astrmai/conversation/attention/context_compaction.py`
- `astrmai/conversation/planning/planner_prompt_context.py`

允许只读参考：
- `astrmai/conversation/attention/thread_builder.py`
- `astrmai/conversation/planning/planner.py`
- `tests/unit/conversation/test_group_dialogue_store_and_compaction.py`
- `tests/regression/attention/test_attention_focus_thread_selection_migrated.py`

注意：
1. 先补假阳性和 warm/recent 语义测试，再调评分/回填逻辑。
2. 本窗口不要回头修已不再复现的旧 `focus_reason` 问题。

建议先跑：
- `PYTHONPATH=. pytest tests/unit/conversation/test_group_dialogue_store_and_compaction.py tests/regression/attention/test_attention_focus_thread_selection_migrated.py tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py tests/regression/conversation/test_dialog_continuity_regression_migrated.py -q`
```

---

## 窗口 4：网关 / Provider / Runtime 观测

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-04-gateway-provider-runtime.md`

本窗口只处理：
1. provider remote session 未跟随 `prefix_hash` 触发的 lane 旋转一起切断
2. `prefix_hash` 多口径混用
3. `cache_affinity_enabled` 把 enabled/ready 混在一起
4. reverse-session hack 仍在主链

优先文件：
- `astrmai/infrastructure/runtime/lane_storage.py`
- `astrmai/infrastructure/runtime/lane_manager.py`
- `astrmai/infrastructure/gateway/gateway_lane.py`
- `astrmai/infrastructure/gateway/gateway_result.py`
- `astrmai/infrastructure/gateway/gateway_call.py`
- `astrmai/infrastructure/context_economy/center.py`
- `main.py`

允许只读参考：
- `astrmai/conversation/planning/context_engine.py`
- `astrmai/conversation/planning/planner.py`
- `astrmai/infrastructure/gateway/provider_capabilities.py`
- `tests/test_gateway_context_passthrough_refactor.py`
- `tests/test_main_reply_request_trace_refactor.py`

注意：
1. 先修 session / trace 语义，再考虑更深的 gateway 收口。
2. 不做整条观测链大改，只修坐实的残留问题。

建议先跑：
- `python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_main_reply_request_trace_refactor.py tests/test_main_reverse_session_hook_refactor.py tests/test_reverse_session_refactor.py tests/test_prompt_metrics_compare_refactor.py tests/test_context_economy_benchmark_refactor.py tests/test_context_economy_refactor.py tests/unit/conversation/test_context_runtime_wiring.py -q`
```

---

## 窗口 5：Memory 主系统

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-05-memory-system.md`

本窗口只处理：
1. Dream 通用检索串入 `feedback/tool_only`
2. 长期记忆主链仍双写 legacy `MemoryEvent`
3. auto injection 的 `selected_ids` 仍按预选中而非真实渲染记账
4. light/jargon 路径 retrieval trace 不可解释

优先文件：
- `astrmai/memory/dream/dream_agent.py`
- `astrmai/memory/services/memory_engine.py`
- `astrmai/memory/services/v2_store.py`
- `astrmai/memory/services/session_memory_summarizer.py`
- `astrmai/memory/services/memory_injection_service.py`
- `astrmai/memory/services/memory_context_builder.py`
- `astrmai/memory/services/memory_tool_service.py`
- `astrmai/memory/services/memory_retrieval_service.py`
- `astrmai/memory/retrieval/react_retriever.py`

允许只读参考：
- `astrmai/conversation/planning/prompt_refiner.py`
- `tests/unit/memory/test_memory_v2_services.py`
- `tests/regression/memory/test_react_retriever_traces_migrated.py`
- `tests/regression/memory/test_memory_v2_tool_injection.py`

注意：
1. 先修 dream/feedback 边界和真实注入记账，再处理 trace 丰富度。
2. 不要求一口气清空所有 legacy/v2 双轨，只做最小闭环。

建议先跑：
- `python -B -m pytest -q -p no:cacheprovider tests/unit/memory/test_memory_v2_services.py tests/regression/memory/test_react_retriever_traces_migrated.py tests/regression/memory/test_memory_v2_tool_injection.py tests/unit/memory/test_memory_contracts_migrated.py`
```

---

## 窗口 6：State / Relationship / Private Chat

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-06-state-relationship-private-chat.md`

本窗口只处理：
1. `mood` 同 chat 并发更新覆盖
2. `social_score` 与 `relationship_vector` 分叉回滚
3. user profile cache 与 prompt bundle 不一致
4. private wait 跨 origin 串线

优先文件：
- `astrmai/state/chat_state_service.py`
- `astrmai/state/relationship/relationship_engine.py`
- `astrmai/state/private_chat/private_chat_manager.py`
- `astrmai/state/user_profile_service.py`
- `astrmai/infrastructure/persistence/state_profile_persistence.py`
- `astrmai/infrastructure/persistence/orm_models.py`

允许只读参考：
- `astrmai/conversation/planning/context_engine.py`
- `astrmai/conversation/loop/chat_loop_kernel.py`
- `astrmai/webui/backend/services/user_ui_service.py`
- `tests/unit/state`
- `tests/regression/state`

注意：
1. 优先修并发/持久一致性，不要顺手改 prompt 内容本身。
2. 重点是让“外部改了状态后，运行时读取不再回滚/吃旧缓存”。

建议先跑：
- `PYTHONPATH=. pytest tests/unit/state tests/regression/state -q`
```

---

## 窗口 7：主动行为 / Heartflow / 定时能力

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-07-proactive-heartflow-cron.md`

本窗口只处理：
1. dispatcher 成功发送后又被旧 decision 覆盖
2. dream scheduler 全局冷却并发窗口
3. cron guard `job_id` 归一化不一致

优先文件：
- `astrmai/proactive/dispatcher.py`
- `astrmai/proactive/wakeup_service.py`
- `astrmai/proactive/dream_scheduler.py`
- `astrmai/workmode/cron_guard/heartbeat.py`

允许只读参考：
- `astrmai/proactive/proactive_task.py`
- `astrmai/proactive/heartflow/manager.py`
- `tests/test_proactive_scheduler_refactor.py`
- `tests/test_cron_guard_refactor.py`
- `tests/test_heartflow_refactor.py`

注意：
1. 本轮不再把重点放在旧的 proactive activity 污染问题上，优先处理当前复检坐实的 3 个残留问题。
2. 先补真实 callback / 并发 / 类型归一化测试，再改实现。

建议先跑：
- `python -m unittest tests.test_proactive_scheduler_refactor tests.test_cron_guard_refactor tests.test_heartflow_refactor tests.regression.proactive.test_dream_maintenance_migrated -q`
```

---

## 窗口 8：WebUI / 管理页 / 调试后端

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-08-webui-admin-backend.md`

本窗口只处理：
1. settings UI 仍能直接热改核心 runtime
2. 调试/观测服务仍依赖 runtime 私有字段
3. learning/proactive/chat-runtime 路由仍混层、缺 schema 契约
4. 测试运行仍依赖手工 `PYTHONPATH`

优先文件：
- `astrmai/webui/backend/services/settings_ui_service.py`
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/webui/backend/services/admin_ui_service.py`
- `astrmai/webui/backend/services/memory_ui_service.py`
- `astrmai/webui/backend/services/review_ui_service.py`
- `astrmai/webui/backend/services/persona_ui_service.py`
- `astrmai/webui/backend/routes/proactive_routes.py`
- `astrmai/webui/backend/routes/learning_routes.py`
- `astrmai/webui/backend/routes/chats_routes.py`
- `astrmai/webui/backend/schemas.py`

允许只读参考：
- `astrmai/webui/frontend/js/pages/learning.js`
- `tests/test_webui_backend_refactor.py`
- `tests/test_webui_frontend_shell_refactor.py`
- `tests/unit/webui`

注意：
1. 本轮优先修“settings 直接热改 runtime”与“路由/返回体无契约”。
2. 不要去大拆整个 WebUI，只做能验证的边界收紧。

建议先跑：
- `PYTHONPATH=. pytest tests/unit/webui tests/test_plugin_pages_admin_refactor.py tests/test_webui_backend_refactor.py tests/test_webui_frontend_shell_refactor.py -q`
```

---

## 窗口 9：Presentation / 命令 / 事件接入

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-09-presentation-and-ingress.md`

本窗口只处理：
1. `/mai` 和 `/work` 仍绕过统一权限闸门
2. dedupe 仍在 poke 归一化前执行，空事件签名塌缩成 `obj_empty`
3. external result sniff fallback 分叉仍和主 ingress 口径不一致

优先文件：
- `main.py`
- `astrmai/presentation/events/message_entry.py`
- `astrmai/conversation/ingress/permission_guard.py`
- `astrmai/conversation/ingress/dedupe.py`
- `astrmai/conversation/ingress/poke_handler.py`
- `astrmai/conversation/ingress/external_result_bridge.py`
- `astrmai/conversation/ingress/command_guard.py`
- `astrmai/conversation/ingress/sensors.py`
- `astrmai/app/plugin_facade.py`

允许只读参考：
- `tests/test_presentation_commands_refactor.py`
- `tests/test_external_result_bridge_refactor.py`
- `tests/test_sensors_refactor.py`
- `tests/integration/host/test_host_mock_validation.py`

注意：
1. 这是当前优先级最高的修复窗口之一。
2. 先修权限绕过，再修 dedupe/poke 顺序，最后修 external bridge fallback。

建议先跑：
- `PYTHONPATH=. pytest tests/test_presentation_commands_refactor.py tests/test_external_result_bridge_refactor.py tests/test_sensors_refactor.py tests/test_chat_loop_kernel_refactor.py tests/integration/host/test_host_mock_validation.py -q`
```

---

## 窗口 10：架构与边界专项

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-10-architecture-boundaries.md`

本窗口目标不是大重构，而是“边界测试补强 + 最小收口”。

本窗口只处理：
1. `presentation` 直操 concrete runtime 的约束缺失
2. `webui/backend/services` 直接碰 runtime/persistence internals 的约束缺失
3. `legacy_compat.py`、`LEGACY_RUNTIME_ATTRS`、`ACTIVE_FACADE` 仍被测试白名单化
4. 架构回归套件入口不稳定（直接 `pytest` 失败）

优先文件：
- `tests/regression/architecture/test_import_boundaries_refactor.py`
- `tests/regression/architecture/test_memory_runtime_boundaries_refactor.py`
- `tests/regression/architecture/test_shared_test_support_refactor.py`
- `astrmai/presentation/events/message_entry.py`
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/webui/backend/services/admin_ui_service.py`
- `astrmai/webui/backend/services/memory_ui_service.py`
- `astrmai/infrastructure/compat/legacy_compat.py`
- `astrmai/app/runtime_context.py`

注意：
1. 先把“白名单冻结现状”的测试改成“显式收紧边界”的测试。
2. 不要求本窗口拆 God Object，只要求先建立更准确的边界约束和测试入口稳定性。

建议先跑：
- `PYTHONPATH=. pytest tests/regression/architecture -q`
```

