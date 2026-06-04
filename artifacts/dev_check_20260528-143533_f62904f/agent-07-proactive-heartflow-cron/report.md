# Agent 07

Agent ID:
`019e6d5c-20ca-79c2-95df-2b7cce79a302`

状态：
已完成

模块：
主动行为 / Heartflow / 定时能力

职责：
负责 heartbeat 选路下的主动唤醒、Heartflow 预判与可见候选、dream 维护触发，以及 cron snapshot 恢复。

关键文件：
[proactive_task.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/proactive_task.py:438)
[dispatcher.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dispatcher.py:225)
[wakeup_service.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/wakeup_service.py:114)
[manager.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/heartflow/manager.py:327)
[dream_scheduler.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dream_scheduler.py:76)
[heartbeat.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/workmode/cron_guard/heartbeat.py:20)

现有测试：
[tests/test_proactive_scheduler_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_proactive_scheduler_refactor.py:88)
[tests/test_heartflow_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_heartflow_refactor.py:86)
[tests/test_cron_guard_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_cron_guard_refactor.py:77)
[tests/regression/proactive/test_dream_maintenance_migrated.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/regression/proactive/test_dream_maintenance_migrated.py:56)

实跑结果：
直接 `pytest ...` 收集失败，缺少 `tests.helpers` 的 `PYTHONPATH`。
补 `PYTHONPATH=.` 后，这 4 组测试为 `32 passed / 6 failed / 1 warning`；6 个失败全部来自 `tests/test_heartflow_refactor.py`，属于测试入口与当前实现签名失配。

主要发现：
1. `PROACTIVE_WAKEUP` 的 kernel-mediated 真路径现在基本失效。[proactive_task.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/proactive_task.py:472) 传入 `signal={"on_visible_send": ...}`，而 [wakeup_service.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/wakeup_service.py:116) 会把“传了 signal”当成“不要再 `build_signal()`”，`pop` 掉回调后剩空字典，直接返回 `reason='ineligible'`。我做了最小复现，`handle_wakeup_signal()` 在本应可唤醒的 state 上返回 `performed=False, allowed=False, reason='ineligible'`。
2. 主动 synthetic event 仍会污染 runtime activity 时间锚与活跃计数。[dispatcher.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dispatcher.py:255) 已把 `message_str` 清空，但同时仍设置 `astrmai_force_engage`；[gate.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/gate.py:393) 会走 `_engage_immediately()`，随后 [gate.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/gate.py:257) 无条件调用 `runtime_coordinator.mark_activity()`；而 [chat_runtime_coordinator.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/runtime/chat_runtime_coordinator.py:70) 会更新 `latest_activity_ts` 和 `activity_times`，不区分 bot/proactive/空文本。结果是主动唤醒会把 chat 重新标成“刚刚活跃”，反向影响后续 wakeup/heartflow/freshness 判定。
3. Heartflow 可见发送后的 cooldown 在真实路径会被写两次。[manager.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/heartflow/manager.py:805) 的 `on_visible_send` 成功后会先调一次 kernel cooldown；[proactive_task.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/proactive_task.py:522) 又在 `visible_dispatch_performed=True` 时再写一次。我做了最小复现，`set_cooldown()` 被调用了两次，而且第二次会重算时间戳，存在重复副作用和冷却时间冲突风险。

未实现/不完整项：
1. [tests/test_heartflow_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_heartflow_refactor.py:264) 等 6 处仍按旧签名调用 `_build_impulse_decision(state, pulse, snapshot, now=...)`，而当前实现要求先传 `session`，所以这套核心验证入口已经失配。
2. `tests/test_proactive_scheduler_refactor.py` 里对 wakeup/heartflow bridge 的测试大多使用旧桩或 fallback 路径，没有覆盖真实的 `signal={"on_visible_send": ...}` 和 `tick_chat(..., on_visible_send=...)` 行为，所以前两条问题都未被现有测试拦住。
3. [tests/test_cron_guard_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_cron_guard_refactor.py:77) 只覆盖了 `reload_all_lost_jobs()` 的 happy path；`run_heartbeat()` 循环、无 `cron_manager` 时的清理、过期 `run_once` snapshot 去激活、`stop()` 后的收敛都没有验证。

高风险点：
1. 主动事件污染 runtime activity 后，会同时影响 `ChatLoopKernel` 的 due selection、Heartflow session/materialize、reply freshness 和“最近活跃”统计，这不是纯观测问题，而是会回写调度输入。
2. 当前 wakeup bridge 的“调度已选中，但子服务恒返回 ineligible”会让表面状态看起来正常，实际主动唤醒长期不落地，属于静默退化。

建议下一步：
1. 修 [proactive_task.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/proactive_task.py:472) 的 wakeup 调用方式，先 `build_signal()` 再合并 `on_visible_send`，并补一个真实 bridge 路径回归测试。
2. 给 proactive synthetic event 加 runtime-activity 隔离，至少不要更新 `latest_activity_ts/activity_times`；再补测试断言 proactive external event 不会改变 runtime snapshot 的时间锚。
3. Heartflow cooldown 只保留一个写入口，并把 [tests/test_heartflow_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_heartflow_refactor.py:264) 与 [tests/test_cron_guard_refactor.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_cron_guard_refactor.py:77) 一起补齐到真实签名和真实循环路径。
