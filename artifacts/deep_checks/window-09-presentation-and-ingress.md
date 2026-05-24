# 窗口 9：Presentation / 命令 / 事件接入

模块：
`astrmai/presentation/*` + `astrmai/conversation/ingress/*`

职责：
负责 `main.py -> PluginFacade -> presentation.events -> ingress/* -> chat_loop_kernel/attention/planner` 这条入口链的接线，处理 command/event 入口、外部结果嗅探、出站错误拦截，以及 message 的 dedupe / permission / poke 预处理。

关键文件：
- `main.py`
- `astrmai/app/plugin_facade.py`
- `astrmai/presentation/events/message_entry.py`
- `astrmai/presentation/events/result_sniffer.py`
- `astrmai/presentation/events/error_interceptor.py`
- `astrmai/conversation/ingress/external_result_bridge.py`
- `astrmai/conversation/ingress/poke_handler.py`
- `astrmai/conversation/ingress/sensors.py`

现有测试：
- `tests/integration/host/test_host_mock_validation.py`
- `tests/test_chat_loop_kernel_refactor.py`
- `tests/test_external_result_bridge_refactor.py`
- `tests/test_outbound_error_policy_refactor.py`
- `tests/test_sensors_refactor.py`
- `tests/regression/architecture/test_import_boundaries_refactor.py`
- `tests/original_ported/test_host_bridge_ported.py`
- `tests/original_ported/test_planner_prompt_context_guards_ported.py`

主要发现：
1. `poke` 分支没有走统一 ingress 合约。
   - 依据：`astrmai/presentation/events/message_entry.py:25` 先做 dedupe，再在权限校验前调用 `astrmai/conversation/ingress/poke_handler.py:14`。
   - 进一步依据：`astrmai/conversation/ingress/sensors.py:308` 的 `process_poke_event()` 最终直接 `await attention_gate.process_event(event)`（同文件 `473` 行），绕过 `astrmai/conversation/ingress/permission_guard.py:6` 和正常 message 使用的 `chat_loop_kernel.tick()`（`message_entry.py:77`）。
2. 外部结果嗅探的“外部”判定过宽，存在越界吸收路径。
   - 依据：`main.py:115` 对所有 `on_decorating_result` 都挂了 sniffer，而 `astrmai/conversation/ingress/external_result_bridge.py:31` 只靠 `astrmai_is_self_reply` 排除自身回复。
   - 进一步依据：`/work` 路径会显式打标（`astrmai/app/plugin_facade.py:195`），但 `/mai` 路径只是直接 `yield plain_result`（`astrmai/presentation/commands/mai_help.py:13`）。
   - 进一步依据：bridge 不复用 `permission_guard.py:6`，所以其他插件在非白名单会话里的输出也可能被 AstrMai 注入 attention / learning。
3. 入口层还没有完全收瘦到 facade / planner / kernel。
   - 依据：`astrmai/app/plugin_facade.py:59` 基本只是转发，但 `astrmai/presentation/events/message_entry.py:45` 仍直接管理 `group_reply_wait_manager` 的 resume/expire/arm、`reflect_tracker` 反馈短路、用户统计异步派发、学习记录和 ghost 输出判定。
   - 进一步依据：command 判定分散在 `astrmai/app/plugin_facade.py:121` 与 `astrmai/conversation/ingress/sensors.py:253` 两套实现里，不是单一真源。

未实现/不完整项：
1. 缺少针对 `duplicate_message`、`group_not_whitelisted`、`private_chat_disabled`、未授权 poke、自身命令输出不应被 sniff 的负例测试。
2. `result_sniffer` 与 `error_interceptor` 的协作顺序没有集成验证。
   - 依据：`main.py:115` 和 `main.py:124` 只声明了两个 `on_decorating_result` hook，现有测试分别测各自单元，没有测组合行为。

高风险点：
1. 非白名单群或禁私聊场景下，poke 仍可能触发 `send_poke` 和 attention 注入。
   - 依据：poke 在权限前执行，并直连 `astrmai/conversation/ingress/sensors.py:447-473`。
2. `astrmai/conversation/ingress/dedupe.py:12` 对空 `message_str` 的消息只用 `obj_len_{len(str(message))}` 做指纹。
   - 风险：同发送者 1.5 秒内发两条不同但长度相同的纯图片 / 附件消息，可能在 `message_entry.py:25` 被误判重复。

建议下一步：
1. 先补 4 个负例集成测试：未授权 poke 不应进入 attention、非白名单会话的 external result 不应注入、自身 `/mai` 输出不应被 sniff、纯媒体消息不应因长度相同被 dedupe 误杀。
2. 把 `poke` 和 `external_result_bridge` 收敛到统一 ingress contract：复用同一套 scope / permission / source 校验，并把 command 判定收口到单一实现，避免 `message_entry`、`facade`、`sensors` 三处分叉。
