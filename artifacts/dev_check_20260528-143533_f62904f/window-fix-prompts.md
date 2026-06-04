# 10 个窗口修复提示词

说明：
- 下列提示词用于“按窗口逐个修复”，不是重新做检查。
- 每个窗口都应先读取：
  - `artifacts/dev_check_20260528-143533_f62904f/overall-summary.md`
  - 对应窗口自己的 `report.md`
- 报告只是入口，不是事实本身；必须回到真实代码、调用链和测试验证后再改。

推荐发送顺序：
`7 -> 3 -> 5 -> 6 -> 4 -> 2 -> 8 -> 9 -> 1 -> 10`

如果你坚持按窗口编号发送，也可以 `1 -> 10` 顺序发送，但每个窗口都必须先确认前面窗口是否已经让报告漂移。

---

## 窗口 1：插件入口与装配层

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

本窗口任务是修复插件入口与装配层问题，不是重新做检查。

必须先读取：
- `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final\artifacts\dev_check_20260528-143533_f62904f\overall-summary.md`
- `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final\artifacts\dev_check_20260528-143533_f62904f\agent-01-plugin-entry-and-bootstrap\report.md`

本窗口只处理以下问题：
1. 热配置导致的“配置态 / 运行态漂移”
2. `terminate()` 状态位复位不对称
3. 插件页重复注册 / 旧 facade 悬挂风险
4. 与以上问题直接相关的测试缺口

优先文件：
- `main.py`
- `astrmai/app/bootstrap.py`
- `astrmai/app/plugin_facade.py`
- `astrmai/app/runtime_context.py`
- `astrmai/app/lifecycle.py`
- `astrmai/webui/plugin_pages.py`
- `astrmai/webui/backend/adapters/plugin_api.py`

允许只读参考：
- `astrmai/webui/backend/services/admin_ui_service.py`
- `tests/test_webui_backend_refactor.py`
- `tests/test_plugin_pages_admin_refactor.py`
- `tests/unit/conversation/test_context_runtime_wiring.py`

执行规则：
1. 先读报告，再读真实代码与相邻调用点。
2. 如果报告内容已被前序窗口修掉，必须明确说明“报告已漂移”，不要重复改。
3. 只修本窗口问题，不做大规模 facade/compat 重构。
4. 先跑最小基线测试，再改，再回归。

建议先跑：
- `python -m pytest tests/unit/conversation/test_context_runtime_wiring.py tests/test_webui_backend_refactor.py tests/test_plugin_pages_admin_refactor.py tests/integration/runtime/test_runtime_contracts_migrated.py -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 2：对话主链路

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-02-conversation-mainline\\report.md`

本窗口只处理以下问题：
1. executor native vision tool-mode 失败回退混道
2. planner “无回复”收尾链路分叉
3. `turn_context` 承担过多运行时控制语义
4. `ContextEngine` 反向写 `planner_runtime_instruction_block`

优先文件：
- `astrmai/conversation/execution/executor.py`
- `astrmai/conversation/planning/planner.py`
- `astrmai/conversation/contracts/turn_context.py`
- `astrmai/conversation/planning/context_engine.py`
- `astrmai/conversation/planning/planner_side_inputs.py`
- `astrmai/conversation/planning/prompt_refiner.py`

允许只读参考：
- `astrmai/conversation/execution/reply_post_send.py`
- `astrmai/conversation/planning/think_level_policy.py`
- `astrmai/conversation/planning/planning_input_loader.py`

注意：
1. 只修报告里已坐实的问题，不要顺手重写 planner。
2. 若需要拆大结构，只做到最小闭环并补测试。

建议先跑：
- `python -m pytest tests/test_planner_side_inputs_refactor.py tests/test_planner_cognitive_loop_refactor.py tests/test_executor_refactor.py tests/test_planning_input_loader_refactor.py tests/original_ported/test_prompt_refiner_focus_layout_ported.py tests/regression/conversation tests/test_turn_context_refactor.py -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 3：注意力 / 上下文压缩

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-03-attention-and-compaction\\report.md`

本窗口只处理以下问题：
1. direct turn 抢占主线，后续自然追问抢不回 focus
2. warm/recent 交接不稳定，最新追问不补位
3. `get_trace_status()` 返回“旧 state + 新 reason”的混合快照

优先文件：
- `astrmai/conversation/attention/focus_selector.py`
- `astrmai/conversation/attention/thread_builder.py`
- `astrmai/conversation/attention/group_dialogue_store.py`
- `astrmai/conversation/attention/context_compaction.py`
- `astrmai/conversation/planning/planner_prompt_context.py`

允许只读参考：
- `astrmai/conversation/attention/gate.py`
- `astrmai/conversation/planning/planner.py`

注意：
1. 本窗口不要顺手回头改旧报告里已经确认不存在的 `focus_reason` 覆盖问题。
2. 先补最小回归，再改评分/交接逻辑。

建议先跑：
- `python -m pytest tests/unit/conversation/test_group_dialogue_store_and_compaction.py tests/regression/attention/test_attention_focus_thread_selection_migrated.py tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py tests/regression/conversation/test_dialog_continuity_regression_migrated.py -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 4：网关 / Provider / Runtime 观测

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-04-gateway-provider-runtime\\report.md`

本窗口只处理以下问题：
1. `prefix_hash` / `effective_prefix_hash` / `stable_prefix_hash` 口径混用
2. chat lane 缺少和 tool lane 同粒度的 gateway stage trace
3. reverse-session hook 继续侵入主链
4. cache affinity 观测名义高估“可缓存性”

优先文件：
- `astrmai/infrastructure/gateway/gateway_lane.py`
- `astrmai/infrastructure/gateway/gateway_call.py`
- `astrmai/infrastructure/gateway/gateway_result.py`
- `astrmai/infrastructure/runtime/lane_manager.py`
- `astrmai/infrastructure/context_economy/center.py`
- `astrmai/infrastructure/context_economy/models.py`
- `main.py`

允许只读参考：
- `astrmai/conversation/planning/context_engine.py`
- `astrmai/conversation/planning/planner.py`
- `astrmai/infrastructure/gateway/provider_capabilities.py`

注意：
1. 先统一字段语义，再补缺失 trace。
2. 不做大规模网关重构，只做最小闭环。

建议先跑：
- `python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_context_economy_refactor.py tests/test_main_reverse_session_hook_refactor.py tests/test_main_reply_request_trace_refactor.py tests/test_gateway_policy_refactor.py tests/test_gateway_vision_refactor.py -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 5：Memory 主系统

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-05-memory-system\\report.md`

本窗口只处理以下问题：
1. dream 仍是 legacy-first 双轨执行
2. memory injection 可能把指令型文本抬升进最终提示词
3. persona summarizer destructive 地重建长期记忆
4. v2 检索 trace 不够可解释

优先文件：
- `astrmai/memory/dream/dream_agent.py`
- `astrmai/memory/services/memory_write_service.py`
- `astrmai/memory/services/memory_context_builder.py`
- `astrmai/conversation/planning/prompt_refiner.py`
- `astrmai/memory/persona/persona_summarizer.py`
- `astrmai/memory/services/memory_engine.py`
- `astrmai/memory/contracts/retrieval_trace.py`
- `astrmai/memory/retrieval/react_retriever.py`

允许只读参考：
- `astrmai/memory/services/memory_injection_service.py`
- `astrmai/memory/services/memory_retrieval_service.py`

注意：
1. 不要求一口气消灭所有 legacy/v2 双轨，只修最直接影响正确性的链路。
2. 优先把“会污染回复”与“会阻断 dream 主轨”的问题落地。

建议先跑：
- `python -B -m pytest -q -p no:cacheprovider tests/unit/memory/test_memory_v2_services.py tests/regression/memory/test_react_retriever_traces_migrated.py tests/regression/memory/test_memory_v2_tool_injection.py tests/unit/memory/test_memory_contracts_migrated.py`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 6：State / Relationship / Private Chat

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-06-state-relationship-private-chat\\report.md`

本窗口只处理以下问题：
1. ChatState 衰减 / 重置不能持久一致
2. private wait 在同尾号群聊 heartbeat 上串线
3. relationship 衰减写出“双真相”

优先文件：
- `astrmai/state/chat_state_service.py`
- `astrmai/state/private_chat/private_chat_manager.py`
- `astrmai/state/relationship/relationship_engine.py`
- `astrmai/infrastructure/persistence/state_profile_persistence.py`
- `astrmai/infrastructure/persistence/orm_models.py`
- `astrmai/proactive/decay_service.py`

允许只读参考：
- `astrmai/conversation/loop/chat_loop_kernel.py`
- `astrmai/state/user_profile_service.py`
- `astrmai/conversation/planning/context_engine.py`

注意：
1. 不要顺手改“私聊画像进入 prompt 的位置”，报告已明确那条链不是当前核心缺陷。
2. 重点修后台/重启/持久化后的状态一致性。

建议先跑：
- `PYTHONPATH=. pytest tests/unit/state tests/regression/state -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 7：主动行为 / Heartflow / 定时能力

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-07-proactive-heartflow-cron\\report.md`

本窗口只处理以下问题：
1. `PROACTIVE_WAKEUP` 真桥接路径失效
2. proactive synthetic event 污染 runtime activity
3. Heartflow visible cooldown 双写
4. heartflow / cron_guard 测试签名或入口漂移

优先文件：
- `astrmai/proactive/proactive_task.py`
- `astrmai/proactive/wakeup_service.py`
- `astrmai/proactive/dispatcher.py`
- `astrmai/proactive/heartflow/manager.py`
- `astrmai/proactive/dream_scheduler.py`
- `astrmai/workmode/cron_guard/heartbeat.py`

允许只读参考：
- `astrmai/conversation/attention/gate.py`
- `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`
- `tests/test_heartflow_refactor.py`
- `tests/test_cron_guard_refactor.py`

注意：
1. 先修 wakeup bridge 和 runtime activity 污染，再处理 cooldown 双写。
2. 测试有 6 个旧签名失配，修代码时也要一并修测试入口。

建议先跑：
- `PYTHONPATH=. pytest tests/test_proactive_scheduler_refactor.py tests/test_heartflow_refactor.py tests/test_cron_guard_refactor.py tests/regression/proactive/test_dream_maintenance_migrated.py -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 8：WebUI / 管理页 / 调试后端

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-08-webui-admin-backend\\report.md`

本窗口只处理以下问题：
1. 独立 persona 页面与后端契约脱节
2. `clear_heartflow_cooldowns` 与 `clear_chat_runtime` 语义不一致
3. `routes.py` 影子聚合器与 route/service/schema 漂移
4. dashboard/debug payload 缺少 schema 契约

优先文件：
- `astrmai/webui/frontend/js/pages/persona.js`
- `astrmai/webui/backend/services/persona_ui_service.py`
- `astrmai/webui/backend/routes/persona_routes.py`
- `astrmai/webui/backend/services/admin_ui_service.py`
- `astrmai/webui/backend/routes/__init__.py`
- `astrmai/webui/backend/routes.py`
- `astrmai/webui/backend/routes/config_routes.py`
- `astrmai/webui/backend/routes/memory_routes.py`

允许只读参考：
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/webui/frontend/js/pages/dashboard.js`
- `astrmai/webui/frontend/pages/dashboard/index.html`

注意：
1. 优先做“页面契约对齐”和“清理语义收敛”，不要一口气重构整个 WebUI。
2. 正式检查/修复中继续排除 `astrmai/webui/venv/**` 与 `mock_frontend_server*`。

建议先跑：
- `python -m pytest tests/test_plugin_pages_admin_refactor.py tests/test_webui_backend_refactor.py tests/unit/webui -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 9：Presentation / 命令 / 事件接入

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-09-presentation-and-ingress\\report.md`

本窗口只处理以下问题：
1. `result_sniffer` 与 `error_interceptor` 边界穿透
2. poke 入口不统一，notice-only poke 可能漏处理
3. framework command 判定过宽
4. 对应联动测试缺失与顺序敏感

优先文件：
- `main.py`
- `astrmai/presentation/events/message_entry.py`
- `astrmai/presentation/events/result_sniffer.py`
- `astrmai/presentation/events/error_interceptor.py`
- `astrmai/conversation/ingress/external_result_bridge.py`
- `astrmai/conversation/ingress/poke_handler.py`
- `astrmai/conversation/ingress/sensors.py`
- `astrmai/conversation/ingress/permission_guard.py`
- `astrmai/app/plugin_facade.py`

允许只读参考：
- `astrmai/conversation/ingress/command_guard.py`
- `tests/test_external_result_bridge_refactor.py`
- `tests/test_outbound_error_policy_refactor.py`
- `tests/test_sensors_refactor.py`
- `tests/integration/host/test_host_mock_validation.py`

注意：
1. 优先修“ghost / error text 被错误注入 attention”这一条。
2. 再修 poke 统一入口和未知 `/xxx` 的拦截策略。

建议先跑：
- `python -m pytest tests/test_external_result_bridge_refactor.py tests/test_outbound_error_policy_refactor.py tests/test_sensors_refactor.py tests/test_presentation_commands_refactor.py tests/test_chat_loop_kernel_refactor.py tests/integration/host/test_host_mock_validation.py -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

---

## 窗口 10：架构与边界专项

```md
你现在在项目 `C:\Users\zlj\Desktop\mai\astrmai_plugin_refactored_final` 中工作。

必须先读取：
- `...\\overall-summary.md`
- `...\\agent-10-architecture-boundaries\\report.md`

本窗口目标不是大重构，而是做“边界测试补强 + 最小收口”。

本窗口只处理以下问题：
1. `conversation -> presentation` 反向依赖缺少架构回归
2. `webui/backend/services` 直接碰 `sqlite3` / `db_factory` / `default_db_path()` / `get_runtime()` 缺少约束
3. `legacy_compat.py`、`LEGACY_RUNTIME_ATTRS`、`ACTIVE_FACADE` 缺少收缩测试

优先文件：
- `tests/regression/architecture/test_import_boundaries_refactor.py`
- `tests/regression/architecture/test_memory_runtime_boundaries_refactor.py`
- `astrmai/conversation/ingress/command_guard.py`
- `astrmai/presentation/dto/message_scope.py`
- `astrmai/webui/backend/services/admin_ui_service.py`
- `astrmai/webui/backend/services/memory_ui_service.py`
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/infrastructure/compat/legacy_compat.py`
- `astrmai/app/runtime_context.py`

注意：
1. 优先补边界测试，把隐式允许面显式收紧。
2. 不要尝试一口气拆完 God Object，只做能验证、能收口的最小改动。

建议先跑：
- `PYTHONPATH=. pytest tests/regression/architecture -q`

输出格式：
修复目标：
已读依据：
实施内容：
修改文件：
验证：
剩余风险：
```

