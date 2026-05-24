# 窗口 2：对话主链路

模块：
对话主链路（`astrmai/conversation/planning/*`、`astrmai/conversation/execution/*`、`astrmai/conversation/contracts/*`）

职责：
负责 planning、prompt 组装、side inputs、memory 注入、执行器调用、follow-up、trace/continuity 更新，以及 turn/prompt 合同对象定义。

关键文件：
- `astrmai/conversation/planning/planner.py`
- `astrmai/conversation/planning/context_engine.py`
- `astrmai/conversation/planning/prompt_refiner.py`
- `astrmai/conversation/planning/planner_prompt_context.py`
- `astrmai/conversation/execution/executor.py`
- `astrmai/conversation/contracts/turn_context.py`
- `astrmai/conversation/contracts/prompt_envelope.py`

现有测试：
- `tests/test_executor_refactor.py`
- `tests/test_planner_side_inputs_refactor.py`
- `tests/test_planner_cognitive_loop_refactor.py`
- `tests/original_ported/test_prompt_refiner_focus_layout_ported.py`
- `tests/regression/conversation/*`
- 实跑：`python -m pytest tests/test_executor_refactor.py tests/test_planner_side_inputs_refactor.py tests/test_planner_cognitive_loop_refactor.py tests/original_ported/test_prompt_refiner_focus_layout_ported.py tests/regression/conversation -q`
- 结果：`69 passed, 1 warning`

主要发现：
1. `[高]` post-compaction recovery 信号在真实主链路里丢失。
   - 依据：`astrmai/conversation/planning/planner_prompt_context.py:370` 只把 `post_compaction_recovery_rounds` 当局部变量决定 recent transcript。
   - 进一步依据：`astrmai/conversation/planning/prompt_refiner.py:472` 已改为从 `TurnContext.continuity.post_compaction_recovery_rounds` 判定 time anchor，但该字段直到 `astrmai/conversation/planning/planner.py:1195` 调 `_update_turn_trace_runtime()` 后才写入。
   - 结果：压缩刚发生后的恢复轮次不会在同一轮 prompt 构造中生效。
2. `[高]` follow-up 第二条消息绕过 planner 收尾链路。
   - 依据：`astrmai/conversation/planning/planner.py:1234` 追发时直接再次调用 `executor.execute()`。
   - 进一步依据：`astrmai/conversation/planning/planner.py:1218-1231` 这套 `dialogue_store`、continuity、expression trace、turn trace 更新不会为第二条消息再跑一次。
   - 结果：第二条 assistant 消息不会进入 `dialogue_store`、`turn_trace_history`、continuity snapshot。
3. `[中]` executor 的“未成功产出”被 planner 一律记成 `executed`。
   - 依据：`astrmai/conversation/execution/executor.py:726`、`:741`、`:832` 多个失败或跳过分支都返回 `None`。
   - 进一步依据：`astrmai/conversation/planning/planner.py:1272` 仍无条件 `_remember_turn_trace(..., status="executed")`，且 `1221-1231` 还会继续更新 continuity。
4. `[中]` tool 异常、provider 异常、native-vision fallback 在 executor 里混成同一错误通道。
   - 依据：`astrmai/conversation/execution/executor.py:624-685` 对 `tool_chat_in_lane_result()` 抛出的所有 `Exception` 都按 model failure 处理并跨模型重试。
   - 进一步依据：`astrmai/conversation/execution/executor.py:841` 耗尽后与 provider outage 共用 `_handle_fatal_fallback()`；`791-805` 的 native main-reply vision 路径也会被一起打开 breaker。
5. `[中]` 稳定回复原则被放错层，预算压力下会被当成 runtime 文本裁掉。
   - 依据：`astrmai/conversation/planning/context_engine.py:613-641` 里 `stable_rules` 一直为空，真正的 `stable_rule_candidates` 被塞进 `prompt_envelope.planner_runtime_instruction_block`。
   - 进一步依据：`astrmai/conversation/planning/prompt_refiner.py:104-121` 会把 runtime guidance cluster 截到 360 chars。

未实现/不完整项：
1. planner 真链路没有测试覆盖 post-compaction recovery 在同轮 prompt 中是否生效。
   - 依据：`test_prompt_refiner_focus_layout_ported` 只覆盖手工预置 `TurnContext` 的 refiner 分支。
2. follow-up 第二条消息的集成路径没有测试保护。
   - 依据：`tests/original_ported/test_planner_follow_up_ported.py:65` 只测 `_should_follow_up()`，没测第二条消息是否进入 trace/continuity。

高风险点：
1. `Planner` 已经过胖，多个共享收尾逻辑被分支绕开。
   - 依据：`astrmai/conversation/planning/planner.py:886` 同时承载 planning、budget、tool 装配、prompt 组装、执行、trace、continuity、follow-up。
2. `TurnContext` 已不是纯 trace 桶，而是“trace + runtime bus”。
   - 依据：`astrmai/conversation/planning/prompt_refiner.py:391` 和 `astrmai/memory/services/memory_injection_service.py:45` 都会反向读取它作为行为输入。

建议下一步：
1. 先补 planner 真链路测试，覆盖 post-compaction recovery 同轮生效、follow-up 第二条消息的 trace/continuity 更新，以及 `executed` 状态分类。
2. 再拆 executor 错误分类和 stable rules 分层，避免 tool/provider/fallback 混道，并把稳定规则重新放回 stable/semi-stable 层。
