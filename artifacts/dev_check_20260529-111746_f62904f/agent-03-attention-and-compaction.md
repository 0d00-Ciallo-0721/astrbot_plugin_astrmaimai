# Agent 03

Agent ID:
`019e71b7-9e68-7480-9cbb-2ad6d1fd7050`

状态：
已完成

发现：
1. `[P1]` `near_context_followup` 触发词过宽，仍会把无关群聊接话抢成主线焦点。[gate.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/gate.py:182>) 把“这个 / 那个 / 不可以”等普通指代词直接判成 near-context，[focus_selector.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/focus_selector.py:4>) 又给这类消息额外 `+900` 的 assistant-followup 加权。最小复现里，`Alice` 的 `reply_to_bot` 主线提问后，`Bob` 一句“这个电影真好看”会被选成 focus，原因是 `near_context_followup`，分数 `1360 > 1050`。
2. `[P1]` warm 层仍会“捞回”过旧 assistant 回合，导致 `social/recent transcript` 语义漂移，并和 recent fallback 职责重叠。[group_dialogue_store.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:451>) 的 warm summary 只优先保留 `score > 0` 的片段，普通用户续聊很容易被全丢掉；同时 [group_dialogue_store.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/group_dialogue_store.py:539>) 会从“全量 warm 窗口”补回最新 assistant，即使它已经不在最近 8 条里。复现中 1 条 assistant 回复后接 9 条普通用户消息，`quote_text` 仍包含旧 assistant。
3. `[P2]` `context_compaction` 的 provider session 复用契约当前仍有一处明确红灯。[context_compaction.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/attention/context_compaction.py:1719>) 生成的 `session_id` 现在带 `@@provider-session:runner` 后缀；但 `tests/unit/conversation/test_group_dialogue_store_and_compaction.py::test_compaction_provider_kwargs_use_dedicated_lane_and_reuse_session` 仍断言它应以 `compaction_summary_v2:v2:section_summary` 结尾。功能上同一次 lane 复用同一个 session 还成立，但实现和测试契约已经不一致。

测试缺口：
- `tests/regression/attention/*` 没有锁住“无关 deictic 小聊句误判为 near-context”这类主线漂移。
- `tests/unit/conversation/test_group_dialogue_store_and_compaction.py` 没有覆盖“assistant 已掉出最近 8 条但仍在 warm TTL 内”时 warm summary/quotes 的语义稳定性。
- planner 对 compaction 状态的行为性消费，当前实际只看到 `post_compaction_recovery_rounds` 被用来放宽 recent fallback；其他 trace 字段缺少行为回归约束。

验证：
运行了：
```powershell
$env:PYTHONPATH='C:\\Users\\zlj\\Desktop\\mai\\astrmai_plugin_refactored_final'; pytest tests/unit/conversation/test_group_dialogue_store_and_compaction.py tests/regression/attention/test_attention_focus_thread_selection_migrated.py tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py tests/regression/conversation/test_dialog_continuity_regression_migrated.py
```

结果：`32 passed, 1 failed`。失败项就是 provider session-id 契约测试；另外两条问题已用最小复现脚本确认。
