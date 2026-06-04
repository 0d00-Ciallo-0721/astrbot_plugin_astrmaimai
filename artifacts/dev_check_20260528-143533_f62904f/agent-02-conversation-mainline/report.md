# Agent 02

Agent ID:
`019e6d48-31f1-7de1-a420-b0c3bfe6d352`

状态：
已完成

发现：
- `[P2]` `executor` 的 native vision tool-mode 失败回退仍然混着两条异常通道走，存在重复执行有副作用工具的风险。[executor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/executor.py:829) 在 native-direct 失败后，会根据 `astrmai_execution_channel == "tool"` 决定是中止还是切回 relay；但 [_run_tool_mode](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/executor.py:625) 只有部分终态才会标这个 channel，模型耗尽/可见输出校验失败最后是直接 `RuntimeError` 抛回去。[tests/test_executor_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_executor_refactor.py:467) 只覆盖了 text-mode fallback，[tests/test_executor_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_executor_refactor.py:517) 只覆盖了 tool-mode success，没有覆盖 tool-mode native 失败后是否会重放工具链。
- `[P2]` `planner` 的“无回复”收尾链路已经分叉，shared finalize 不是同一套逻辑。[planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:990) 的 think-level 环境 wait 分支只做 `settle_no_send`、`finalize_proactive`、`remember_turn_trace` 就返回；而 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:1061) 的 cognitive wait/ignore 分支还会额外写 `agency_runtime` 和 `conversation_continuity`。同样都是“不发回复”，后续冷却、连续性摘要、turn_count 的更新口径却取决于是哪一级 gate 先拦住了请求。
- `[P2]` `turn_context` 还在承担运行时控制语义，不只是 contracts 层的观测快照。[turn_context.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/contracts/turn_context.py:85) 里已经塞了大量 continuity/runtime 字段，而 [think_level_policy.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/think_level_policy.py:231)、[prompt_refiner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/prompt_refiner.py:407)、[reply_post_send.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/execution/reply_post_send.py:132) 都直接读它来决定行为。测试也已经把它当成运行时 API 在锁定，[tests/test_planner_side_inputs_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_planner_side_inputs_refactor.py:537) 和 [tests/test_planner_side_inputs_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_planner_side_inputs_refactor.py:600) 都在直接断言 `turn_context` 内部字段。这会让后续继续瘦身 contracts 很难，也会放大 synthetic/replay event 的状态不一致风险。
- `[P3]` prompt 分层整体已经比旧版清楚，但 `ContextEngine` 还在反向写 `planner_runtime_instruction_block`，层次没有彻底切开。[planner_side_inputs.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner_side_inputs.py:599) 本来负责 side inputs/runtime instruction，[context_engine.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/context_engine.py:613) 却又把 stable rule candidate 追加回同一个 block，最后再由 [prompt_refiner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/prompt_refiner.py:121) 按 360 chars 的 runtime cluster 预算统一截断。这样“系统层规则”实际上被塞进了“动态侧输入预算”，后续很难保证它和 private jump/mode guidance 的优先级关系稳定。

补充判断：
- `planner` 仍然偏胖。不是单纯因为 `plan_and_execute` 长，而是 trace 汇总、continuity、agency、proactive finalize、dialogue store 写入都还挂在同一个类里，`PlanningInputLoader` 只拆掉了一部分前置读取，没有把收尾和 runtime bookkeeping 一起拆出去。
- `trace/cache` 这轮没有看到“为了写 trace/cache 反向改主语义”的明显证据。[planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:614) 和 [planner.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/planning/planner.py:619) 的 store 写入是 append-only，失败也只是吞掉；`cache_ready/cache_hit` 主要是观测派生值，不反向驱动 planner/executor 的主分支。

验证：
- 已只读检查目标目录与相关辅助文件，无代码修改。
- 已运行：
  ```powershell
  python -m pytest tests/test_planner_side_inputs_refactor.py tests/test_planner_cognitive_loop_refactor.py tests/test_executor_refactor.py tests/test_planning_input_loader_refactor.py tests/original_ported/test_prompt_refiner_focus_layout_ported.py tests/regression/conversation
  ```
  结果：`74 passed, 1 warning`
- 已补跑：
  ```powershell
  python -m pytest tests/unit/conversation/test_context_runtime_wiring.py tests/test_turn_context_refactor.py tests/test_main_reply_request_trace_refactor.py
  ```
  结果：`22 passed, 4 warnings`
- warning 都是现有测试环境噪音：`after_nonebot_init was never awaited` 和 `pkg_resources` deprecation，没有看到这次审查点对应的失败。
