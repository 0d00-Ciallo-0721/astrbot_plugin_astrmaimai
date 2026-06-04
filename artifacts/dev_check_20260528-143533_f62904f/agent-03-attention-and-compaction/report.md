# Agent 03

Agent ID:
`019e6d48-534c-7603-b0c3-c9c0250c0952`

状态：
已完成

模块：
`astrmai/conversation/attention/*`

职责：
负责 focus event 选择、focus thread 构造、warm/recent/cold 上下文拼装、以及 compaction 状态向 planner 的透传。

关键文件：
[gate.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/gate.py:673)
[focus_selector.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/focus_selector.py:4)
[thread_builder.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/thread_builder.py:9)
[context_compaction.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/context_compaction.py:1094)
[group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:232)

现有测试：
`tests/unit/conversation/test_group_dialogue_store_and_compaction.py`
`tests/unit/conversation/test_context_runtime_wiring.py`
`tests/regression/attention/test_attention_focus_thread_selection_migrated.py`
`tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py`
`tests/regression/conversation/test_dialog_continuity_regression_migrated.py`

主要发现：
1. `[高]` focus 选择会卡在更早的 direct turn，上下文主线会被后续自然追问“抢不回来”。证据在 [focus_selector.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/focus_selector.py:14) 到 [focus_selector.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/focus_selector.py:43)：`reply_to_bot/at_bot/direct_wakeup` 固定加 `700-1000`，而普通 follow-up 只有 `20 + recency + 最多120`。我用只读复现了 `@ you keep helping -> bot 回复 -> Why?`，旧 direct turn 得分 `850`，最新追问只有 `230`，最终 focus 仍停在旧消息。`thread_builder` 也没有把这种追问强制绑定回最近 assistant turn，见 [thread_builder.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/thread_builder.py:19) 和 [thread_builder.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/thread_builder.py:62)。

2. `[高]` warm/recent 交接不稳定，最新追问会被 warm 层挤掉，但 recent 又常常不补位，导致 prompt 锚点回退到旧 direct turn。warm 侧的问题在 [group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:232) 到 [group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:245)、[group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:417) 到 [group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:427)、[group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:499) 到 [group_dialogue_store.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:560)：未带 `at/reply/direct_vision` 的最新追问很容易拿到 `0` 分，被 summary/quotes 丢掉。recent 侧的问题在 [planner_prompt_context.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner_prompt_context.py:288) 到 [planner_prompt_context.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner_prompt_context.py:321)：`_should_include_recent_transcript()` 只在很窄的 follow-up 模式下返回 `True`。我复现的同一条链路里，warm `summary_text/quote_text` 都没有最新的 `Why?`，而 `_should_include_recent_transcript('Alice: Why?', ...)` 返回 `(False, 'warm_sufficient')`。这会直接影响 `social/recent transcript` 的语义稳定性。

3. `[中]` compaction trace 的 `state` 会被强行回写成旧状态，planner 读到的是“陈旧 state + 当前 reason/score”的混合快照。问题在 [context_compaction.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/context_compaction.py:1134) 到 [context_compaction.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/context_compaction.py:1141)：`get_trace_status()` 先重新评估 eligibility，再把 `result.state` 覆盖成 `last_state`。planner 又在 [planner.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:262) 到 [planner.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:302) 直接消费这个字典。只读复现里，我把状态置成 `DEFERRED_FOR_STABILITY` 且安全窗口已经恢复，`get_trace_status()` 返回的仍是 `state=DEFERRED_FOR_STABILITY`，但同时 `is_safe_to_compact=True`、`reason=safe_window_reopened`。这会让 planner trace 和实际恢复条件不一致。

未实现 / 不完整项：
1. 本轮没有再发现旧底稿里提到的 `focus_reason` 被 `gate` 覆盖问题；`reply_to_bot` 透传链路现在有回归覆盖。
2. 现有回归没有覆盖 `direct turn -> bot 回复 -> natural followup` 这类最容易漂移主线的场景。
3. 现有回归也没有覆盖 “warm 丢掉最新追问时，recent 是否必须补位” 和 “`get_trace_status()` 的 state/eligibility 一致性”。

高风险点：
1. 一旦群聊里先出现一次明确 `@/reply`，后续自然追问很可能继续围着旧 direct turn 打转，planner 回答对象会滞后半拍。
2. compaction 恢复窗口其实已经打开时，planner trace 仍可能显示旧状态，后续如果有策略开始依赖 `compaction_status` 字段，会出现误判。

验证结果：
1. `2026-05-28` 实跑：`python -m pytest tests/unit/conversation/test_group_dialogue_store_and_compaction.py tests/unit/conversation/test_context_runtime_wiring.py tests/regression/attention/test_attention_focus_thread_selection_migrated.py tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py tests/regression/conversation/test_dialog_continuity_regression_migrated.py -q`
2. 结果：`44 passed, 1 failed`
3. 失败项：`test_compaction_provider_kwargs_use_dedicated_lane_and_reuse_session`
4. 现状是 `session_id` 实际结尾为 `@@provider-session:runner`，与测试断言的 `...compaction_summary_v2:v2:section_summary` 已经漂移；这是当前基线红灯，不是我本轮修改引入。

建议下一步：
1. 先补 3 条回归：`old direct -> bot answer -> plain followup`、`warm 丢最新追问 -> recent 强制补位`、`get_trace_status state 与 safe_window_reopened 一致`。
2. 再调整 focus 评分与 thread root 规则，让“bot 已回应后的自然追问”能接管主线，而不是永远输给更早的 direct 标记。
3. 最后收敛 `get_trace_status()` 的语义，只返回“当前评估态”或“上次执行态”其中一种，不要混用。
