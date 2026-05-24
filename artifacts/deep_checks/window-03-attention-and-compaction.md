# 窗口 3：注意力 / 上下文压缩链路

模块：
注意力 / 上下文压缩链路（`astrmai/conversation/attention/*`）

职责：
负责 focus event 选择、focus thread 构造、group dialogue store、context compaction 状态管理，以及为 planner 提供 recent/social/compaction 相关上下文。

关键文件：
- `astrmai/conversation/attention/gate.py`
- `astrmai/conversation/attention/focus_selector.py`
- `astrmai/conversation/attention/thread_builder.py`
- `astrmai/conversation/attention/context_compaction.py`
- `astrmai/conversation/attention/group_dialogue_store.py`
- `astrmai/infrastructure/compat/legacy_compat.py`

现有测试：
- `tests/unit/conversation/test_group_dialogue_store_and_compaction.py`
- `tests/regression/attention/test_attention_focus_thread_selection_migrated.py`
- `tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py`
- `tests/regression/conversation/test_dialog_continuity_regression_migrated.py`
- 实跑：`python -m pytest tests/unit/conversation/test_group_dialogue_store_and_compaction.py tests/unit/conversation/test_context_runtime_wiring.py tests/regression/attention/test_attention_focus_thread_selection_migrated.py tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py tests/regression/conversation/test_dialog_continuity_regression_migrated.py -q`
- 结果：`44 passed`

主要发现：
1. `[高]` focus 选择原因在主链路里被覆盖成固定值，导致下游把直达消息误当成普通焦点。
   - 依据：`astrmai/conversation/attention/focus_selector.py:47-82` 已能返回真实 `focus_reason`，例如 `reply_to_bot`、`at_bot`、`direct_wakeup`、`near_context_followup`。
   - 进一步依据：`astrmai/conversation/attention/gate.py:673-682` 只接 `focus_event, _, _`，随后又把 `focus_thread.focus_reason` 固定写成 `"selected_focus_event"`。
   - 影响链：`astrmai/infrastructure/compat/legacy_compat.py:15-37`、`astrmai/conversation/planning/behavior_tuning.py:75-92`、`astrmai/conversation/planning/think_level_policy.py:241-270`、`astrmai/conversation/planning/planner_side_inputs.py:731-735` 与 `955-960` 都直接消费该字段。

未实现/不完整项：
1. `warm/recent/cold` 三层虽然未发现坐实 bug，但边界仍偏软。
   - 依据：`planner_prompt_context.py:232-277` 中 recent 与 warm 的时间窗和来源不同，但缺少“部分重叠且渲染格式不同”时是否重复进入 prompt 的测试。
2. compaction 状态能被 planner 消费，但“focus_reason 保真”没有相应回归测试。
   - 依据：`planner_prompt_context.py:369-380` 与 `planner.py:240-290` 的 compaction 状态链路是通的，但会被上面的 focus_reason 覆盖问题污染。

高风险点：
1. focus thread 本身可能选对，但“为什么选中”这个关键信号已经丢失，会直接影响 planner 的直达判定、行为调优和群聊过滤。
2. 现有回归没有覆盖更易漂移的 focus 竞争场景。
   - 缺口示例：`near_context_followup` 对比更新但无关的新消息、`has_direct_vision` 对比较新的 plain chatter、跨 `thread_same_speaker_followup_sec` 边界的连续追问。

建议下一步：
1. 先修正 `gate.py` 对真实 `focus_reason` 的透传，并补一条覆盖 `focus_selector -> gate -> planner_side_inputs/think_level_policy` 的回归测试。
2. 再补 `warm/recent` 半重叠文本和复杂 focus 竞争场景的测试，确认不会双重进入 prompt 或错误偏离主线。
