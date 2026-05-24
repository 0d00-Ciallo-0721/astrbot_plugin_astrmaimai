# 窗口 7：主动行为 / Heartflow / 定时能力

模块：
主动行为 / Heartflow / 定时能力（`astrmai/proactive/*`、`astrmai/workmode/cron_guard/*`）

职责：
通过 `ChatLoopKernel` 心跳为活跃会话选择主动信号，分别调度 `wakeup`、`heartflow`、`dream`、`memory/compaction`，并用 `cron_guard` 兜底恢复丢失的 cron 任务。

关键文件：
- `astrmai/proactive/proactive_task.py`
- `astrmai/proactive/dispatcher.py`
- `astrmai/proactive/wakeup_service.py`
- `astrmai/proactive/heartflow/manager.py`
- `astrmai/proactive/heartflow/feedback_bridge.py`
- `astrmai/proactive/dream_scheduler.py`
- `astrmai/workmode/cron_guard/heartbeat.py`
- `astrmai/conversation/loop/chat_loop_kernel.py`
- `astrmai/conversation/planning/prompt_refiner.py`
- `astrmai/conversation/planning/planner_prompt_context.py`

现有测试：
- `tests/test_proactive_scheduler_refactor.py`
- `tests/test_cron_guard_refactor.py`
- `tests/regression/proactive/test_dream_maintenance_migrated.py`
- 实跑：`PYTHONPATH=C:\\Users\\zlj\\Desktop\\mai\\astrmai_plugin_refactored_final pytest tests/test_proactive_scheduler_refactor.py tests/test_cron_guard_refactor.py tests/regression/proactive/test_dream_maintenance_migrated.py -q`
- 结果：`21 passed, 1 failed`
- 失败原因：`tests/helpers/astrbot_stubs.py:72-73` 把 `astrmai.workmode` 伪造成非 package，导致 `tests/test_cron_guard_refactor.py:68` 无法导入 `astrmai.workmode.cron_guard.heartbeat`

主要发现：
1. 主动唤醒 / heartflow 的 synthetic event 会污染当前主链 prompt 的“眼前消息锚点”。
   - 依据：`astrmai/proactive/dispatcher.py:248-271` 把 `intent.guidance` 直接塞进 `message_str`。
   - 进一步依据：`astrmai/conversation/attention/gate.py:25-53,504-516` 把它包装成 `_SyntheticExternalEvent` 并走 `external` 主链；`planner_prompt_context.py:136-137,192-205` 用它构造 `focus_message_text/raw_user_text`；`prompt_refiner.py:894-904` 又把它放进“眼前正在对我说的”；`planner.py:164-170` 还会写 `reply_prompt_focus_anchor`。
   - 边界：`astrmai/memory/services/memory_turn_pipeline.py:97-100` 会忽略 `is_proactive` turn，所以污染主要停留在 prompt 级。
2. Heartflow 已经实质影响行为，但当前 cooldown 判定以“事件入队”为准，不以“真的发出可见回复”为准。
   - 依据：`astrmai/proactive/heartflow/manager.py:327-345,762-825` 负责生成可见候选；`planner.py:678-717`、`think_level_policy.py:170-176,343-394`、`cognitive_loop.py:388-391` 都会消费 heartflow state。
   - 进一步依据：`astrmai/proactive/proactive_task.py:487-499` 只要 `synthetic_event_queued` 就进入 cooldown，但这个标记只表示 attention gate 接收了事件。
3. `wakeup` 与 `dream` 的节流 / 冷却时机都存在“先占坑、后确认”的偏差。
   - 依据：`astrmai/proactive/wakeup_service.py:145-155` 的 `performed` 取自 `dispatcher.dispatch(...).allowed` 而不是 `reply_sent`；`proactive_task.py:462-472` 随即写 kernel cooldown。
   - 进一步依据：`astrmai/proactive/dream_scheduler.py:79-85` 在 `run_dream_cycle` 成功前就先更新 `_last_dream_time`。

未实现/不完整项：
1. `cron_guard` 的验证入口目前不稳定，导致实现入口和验证入口没有稳定对齐。
2. 主动事件 trace 只能做到 intent 级可追，还没有统一链路 trace。
   - 依据：`astrmai/proactive/dispatcher.py:260` 注入 `astrmai_proactive_intent_id`，但 `astrmai/conversation/attention/gate.py:529-531` 会为 synthetic external event 新建 `astrmai_trace_id`。

高风险点：
1. 主链 prompt 被内部 guidance 顶替成当前焦点消息，会直接影响 `focus_message_text`、`raw_user_text`、`reply_prompt_focus_anchor` 和时间锚使用方式。
2. wakeup / heartflow 当前偏向“已入队即冷却”，在 planner 最终没发出任何可见输出时，调度器也会把该 chat 压住。

建议下一步：
1. 先补一条回归测试，明确 synthetic proactive event 不应进入 `focus_message_text/raw_user_text/reply_prompt_focus_anchor`，只应进入隐藏 guidance 区。
2. 再补两条时序测试：验证 wakeup 只有在 `reply_sent=True` 后才写 kernel cooldown，heartflow 只有在最终有可见发送时才进入 visible cooldown；随后修复 `cron_guard` 测试桩导入问题。
