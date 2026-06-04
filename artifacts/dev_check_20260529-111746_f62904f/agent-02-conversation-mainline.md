# Agent 02

Agent ID:
`019e71b7-8a52-71d0-ba4b-9d430f4de4fd`

状态：
已完成

发现：
- `P1` Tool 模式的异常通道还没统一好。在 [executor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/executor.py:625) 里，tool 路径只把 `LLMCascadeFailureException` 和 `ValueError` 当作可切模重试；普通异常会落到 [executor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/executor.py:730) 直接走 fatal fallback。对比 text 路径在 [executor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/executor.py:537) 会继续切下一个模型。当前工作区用两模型桩复现时，首个 tool 模型抛 `RuntimeError("plain runtime boom")`，第二模型完全没尝试，事件直接变成 `fallback_sent`。
- `P1` `skipped_wait/ignore` 这类无回复分支仍然没有走共享收尾，trace/runtime 信息会断裂。早退 helper 在 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:877) 里只做 continuity/trace 记录，没有调用正常回复路径会走的 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:1254)。现有 planner 测试桩复现后，`skipped_wait` 的 turn trace 里 `compaction_status`、`recent_transcript_used`、`reply_prompt_focus_anchor` 都是空值，而执行分支会填上这些字段。
- `P2` `TurnContext` 仍然承担了过多运行时语义，边界还不够收紧。[turn_context.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/contracts/turn_context.py:85) 的 `ContinuitySnapshot` 仍混合了 agency、heartflow、goal、compaction、cache、execution 多个域，[turn_context.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/contracts/turn_context.py:298) 又把整个可变 `prompt_envelope` 挂进去。它现在能工作，但 planner / prompt_refiner / loader 之间仍是“共享可变运行时包”的耦合方式。

测试缺口：
- 现有 executor 测试没有覆盖“tool 模式首模抛普通异常时是否还能切第二模”。
- 现有 planner 测试只校验了 `skipped_*` 状态值，没有校验跳过分支的 trace/runtime 字段是否仍完整。
- planner 里仍有 fire-and-forget 副作用没有做 planner 级失败路径覆盖：agency flush 在 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:528)，compaction 调度在 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:856)。
- 相关套件虽然全绿，但两轮运行都出现了 `RuntimeWarning: coroutine 'after_nonebot_init' was never awaited`。

验证：
已执行：
- `tests/test_planner_side_inputs_refactor.py`
- `tests/test_planner_cognitive_loop_refactor.py`
- `tests/test_executor_refactor.py`
- `tests/test_turn_context_refactor.py`
- `tests/original_ported/test_prompt_refiner_focus_layout_ported.py`
- `tests/regression/conversation/*`
- `tests/unit/conversation/test_context_runtime_wiring.py`

结果：`97 passed`，但带有上述 warning。两条 `P1` 另外都已用当前仓库的现有测试桩手工复现。
