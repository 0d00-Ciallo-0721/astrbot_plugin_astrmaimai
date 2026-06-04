# Agent 09

Agent ID:
`019e6d5c-49ed-7a30-8665-544e22f4b046`

状态：
已完成

模块：
`astrmai/presentation/*` + `astrmai/conversation/ingress/*`

职责：
`presentation` 负责把 AstrBot 的命令和事件 hook 适配到 `PluginFacade`；`ingress` 负责入口去重、命令避让、白名单/私聊权限、poke 归一化，以及把外部插件结果桥接进 attention/kernel。

关键文件：
[main.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:111), [plugin_facade.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/plugin_facade.py:71), [message_entry.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/events/message_entry.py:19), [result_sniffer.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/events/result_sniffer.py:1), [external_result_bridge.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/external_result_bridge.py:33), [permission_guard.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/permission_guard.py:6), [poke_handler.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/poke_handler.py:14), [sensors.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/sensors.py:326)

现有测试：
已实跑通过：`tests/test_external_result_bridge_refactor.py`、`tests/test_outbound_error_policy_refactor.py`、`tests/test_sensors_refactor.py`、`tests/test_presentation_commands_refactor.py`、`tests/test_chat_loop_kernel_refactor.py::test_message_entry_routes_through_kernel_after_guards`、`tests/test_chat_loop_kernel_refactor.py::test_message_entry_self_message_stops_before_kernel`、`tests/integration/host/test_host_mock_validation.py::test_message_entry_and_command_paths_work_with_mock_events`、`tests/regression/architecture/test_import_boundaries_refactor.py::test_presentation_does_not_reach_into_persistence_internals`。间接参考：`tests/original_ported/test_attention_interaction_narrative_ported.py`、`tests/original_ported/test_host_bridge_ported.py`。

主要发现：
1. `result_sniffer` 与 `error_interceptor` 的边界有穿透风险。`main.py:116-127` 里结果嗅探先挂在 `on_decorating_result()`，而错误拦截在同一 hook 的 `priority=90`；`message_entry.py:101-103` 又会产出 `HostBridge.GHOST_SENTINEL`，但没有打 `astrmai_is_self_reply`。结果是 `external_result_bridge.py:33-68` 可能先把 ghost sentinel 或后续本应被 `outbound_error_policy.py:20-35` 丢弃的错误文本，当成“外部插件回复”注入 `attention_gate` 并记入 bot reply 记录。这已经越过了“只嗅探外部插件正常结果”的职责边界。
2. `poke` 入口并不统一。`poke_handler.py:14-27` 只有在 `message_obj.message` 里存在 `Comp.Poke` 时才会调用 `process_poke_event()`；但 `sensors.py:351-396` 又实现了对 OneBot 原始 `notify/poke` payload 的深度回溯。全仓搜索下，`process_poke_event()` 只有这一个入口调用，所以 notice-only 的 poke 事件实际上进不到那段 raw payload fallback，适配器差异下会直接漏掉归一化、反戳和 `is_virtual_poke` 元数据。
3. 命令守卫存在过宽风险。正常 runtime 会在 `bootstrap.py:159` 构建 `PreFilters`，而 `plugin_facade.py:141-143` 一旦拿到 `runtime.sensors` 就直接用 `sensors.is_command_sync()`；`sensors.py:279-281` 对任何带命令前缀的文本都会返回 `True`，导致 `message_entry.py:29-31` 的“framework_command”判定在运行时退化为“任何 `/...` 文本都拦”。如果产品语义是“只避让已注册宿主/插件命令”，这里就把业务判断提前并放宽了。

未实现/不完整项：
1. 缺少 `result_sniffer` 和 `error_interceptor` 联动顺序的集成测试。现有 `tests/test_external_result_bridge_refactor.py` 与 `tests/test_outbound_error_policy_refactor.py` 只验证单点行为，没有验证同一 `on_decorating_result` 链上的先后顺序与互斥关系。
2. 缺少 `check_message_dedup`、`check_message_scope_access`、`handle_poke_if_needed` / `PreFilters.process_poke_event` 的直接用例。当前 `tests/test_sensors_refactor.py` 只覆盖图片/视觉直达，`tests/original_ported/test_attention_interaction_narrative_ported.py` 只覆盖 poke 归一化后的下游叙事。
3. 当前结果链回归测试存在顺序敏感。我组合运行相关测试时，`tests/test_external_result_bridge_refactor.py` 与 `tests/test_outbound_error_policy_refactor.py` 会因 `sys.modules` stub 污染出现 suite-only 假失败，说明这一段验证还不够稳。

高风险点：
1. ghost sentinel 或错误文本一旦先被 `external_result_bridge` 注入，会直接污染 attention window / chat loop / bot reply recorder，这不是单纯日志问题，而是运行时行为风险。
2. 未知 `/xxx` 文本如果被入口层当作 framework command 提前拦掉，会表现成“planner/kernel 完全收不到消息”，这类问题在群聊里很难靠现象快速定位。

建议下一步：
1. 先补一条端到端测试：同一事件先产出 ghost sentinel 或错误文本，再验证 `sniff_external_plugin_results` 不会注入、不写 bot reply，`intercept_and_notify_errors` 仍按预期丢弃结果。
2. 再补两条入口测试：一条覆盖 raw notice poke 无 `Comp.Poke` 的路径，一条覆盖未知命令前缀文本，明确产品预期到底是“拦所有前缀”还是“只拦已注册命令”。
