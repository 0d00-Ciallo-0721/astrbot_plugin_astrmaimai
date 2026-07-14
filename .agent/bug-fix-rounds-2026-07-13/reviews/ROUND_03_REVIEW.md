# Round 03 审查报告：决策、规划与实际发送状态

审查日期：2026-07-14
审查范围：R03-01 ~ R03-09（9 项修复）
审查方法论：逐项读取规划文档中列出的源文件，对照修复边界与回归目标进行静态源码审查。

## 总结表

| 修复ID | 优先级 | 状态 | 主文件关键行 | 说明 |
|--------|--------|------|-------------|------|
| R03-01 | P1 | ✅ 已修复 | gency_runtime.py:32,43,77 | monotonic() 统一时钟域 |
| R03-02 | P1 | ✅ 已修复 | planner.py:500-531 | cooldown从实际trace计算 |
| R03-03 | P1 | ✅ 已修复 | planner.py:133-153 plugin_facade.py:202-259 | 统一refresh链+回滚 |
| R03-04 | P1 | ✅ 已修复 | planner.py:1405-1458 | None结果区分路径settle |
| R03-05 | P2 | ✅ 已修复 | judge.py:48-53,411-414,511 | 单一action来源+降级 |
| R03-06 | P2 | ✅ 已修复 | xecutor.py:708-713 planner.py:1422-1436 | wait信号→typed outcome |
| R03-07 | P2 | ✅ 已修复 | goal_service.py:124-127,221-223 | []清除旧goals，None保留 |
| R03-08 | P1 | ✅ 已修复 | chat_runtime_coordinator.py:197-204 | failed→claimed重试 |
| R03-09 | P1 | ✅ 已修复 |
eply_service.py:107-117 planner.py:1422-1458 | stale提前截断所有写入 |

**结论：9/9 项修复均已实现且有效。** 所有修复边界的代码变更与被审查的源码一致，回归目标在静态审查层面均可达成。

## 逐项分析

### R03-01：Agency TTL/cooldown 混用 epoch 与 monotonic

**修复边界回顾：** 同一生命周期数据统一时钟域；持久化值与进程单调时钟不能直接相减。

**源码证据：**
- AgencyRuntimeStore.recent() (L32)：参数
ow 默认为 monotonic()。
- AgencyRuntimeStore.record() (L77)：	imestamp=monotonic()，写入值使用单调时钟。
- AgencyRuntimeStore.cooldown_tags() (L43)：
ow = monotonic()，与 item.timestamp 比较。
- AgencyRuntimeStore.recent() (L36)：
ow - float(item.timestamp) <= REFLECTION_TTL_SECONDS。

**判定：** ✅ 整个 AgencyRuntimeStore 生命周期内所有时间戳统一使用 	ime.monotonic()，不存在与 	ime.time() 混用的情况。10 分钟 cooldown 和 30 分钟 reflection TTL 均在同一时钟域内计算。

### R03-02：可用工具被记录为已执行动作

**修复边界回顾：** Agency cooldown/feedback 只能消费实际 tool execution trace，不得消费候选 ToolSet。

**源码证据：**
- Planner._executed_tool_names() (L500-513)：从 strmai_tool_execution_trace 读取，仅统计 status == "success" 的条目。
- Planner._cooldown_tags_from_execution() (L516-531)：调用 _executed_tool_names(event) 获取实际执行的工具名。
- pfc_tools.py _record_tool_execution() (L39-43)：仅在 call() 方法成功执行后写入 trace。
- Planner._record_agency_reflection() (L533-558)：调用 _executed_tool_names(event)，基于实际执行计算 cooldown tags。

**判定：** ✅ 所有 cooldown tag 的判定依据是 strmai_tool_execution_trace（实际执行记录），_tool_names(tools) 仅用于 trace 记录，不参与 cooldown 逻辑。普通文本回复不产生 meme/like/poke cooldown。

### R03-03：Planner-owned runtime components 不接收热配置

**修复边界回顾：** 建立 Planner 统一 refresh，原子更新 ContextEngine、PromptRefiner、Executor、CognitiveLoop、ActionModifier 等派生字段。

**源码证据：**
- PluginFacade._apply_hot_config_locked() (L189-259)：遍历组件列表（含 system2_planner L212），调用
efresh_config。
- Planner.refresh_config() (L133-153)：枚举 owned_components 元组（context_engine, prompt_refiner, cognitive_loop, goal_manager, action_modifier, expression_selector, executor），逐个调用
efresh(config)。
- 各组件
efresh_config() 均已实现：
  - context_engine.py:40-44
  - prompt_refiner.py:50-51
  - xecutor.py:62-63
  - judge.py:45-46
  - goal_service.py:54-55
- 回滚逻辑 (L239-258)：失败时恢复 old_raw_config/old_config 到所有组件。

**判定：** ✅ 热配置通过 PluginFacade → Planner → owned_components 链路完整传递。回滚路径使失败场景下每个 child 的配置版本保持一致性。

### R03-04：Executor 返回 None 时 proactive completion 丢失

**修复边界回顾：** 引入 typed execution outcome，区分 fallback 已发送、wait、stale、fatal/no-send；每条路径 exactly-once 调 completion。

**源码证据：**
- xecutor.py._run_tool_mode() (L708-713)：[SYSTEM_WAIT_SIGNAL] 返回 None 并设置 strmai_execution_signal="wait"。
- xecutor.py._check_pre_model_freshness() (L581,675-677)：stale 时设置 strmai_execution_status="stale_drop" 返回 None。
- xecutor._handle_fatal_fallback() (L914-921)：stale_drop 时跳过 fallback，返回 None。
- Planner._finalize_plan_result() (L1405-1458)：
  -
eply_text is None 时 (L1422)，从 event extra 读取 xecution_status/xecution_signal，区分为 skipped_wait/stale_drop/其他。
  - 所有无回复路径统一调用 _finalize_proactive_event(event, None) (L1456)，
eply_sent=False。
  - _finalize_proactive_event() (L900-928)：设置
eply_sent=False，执行 callback 后移除。
- 有回复路径 (L1474)：_finalize_proactive_event(event, reply_text)，
eply_sent=True。

**判定：** ✅ 已发送 fallback 用
eply_sent=True；wait/stale/failure 用 False。Callback 在所有路径上被正确执行并移除。energy/cooldown 语义通过 _settle_no_send_relationship_event (L1449) 正确结算。

### R03-05：Judge 宣告已移除 action，随后把合法输出改成 IGNORE

**修复边界回顾：** prompt enum、parser 与 runtime valid actions 使用单一来源；不支持 action 不得出现在提示中。

**源码证据：**
- Judge._available_action_names() (L48-53)：基于 BASE_ACTIONS + 条件 TOOL_ACTION，返回 	uple[str, ...]。
- Judge._build_dynamic_actions() (L58-66)：仅从 _available_action_names 构建提示文本。
- valuate() prompt (L431)：{available_actions} 来自 _build_dynamic_actions()。
- vailable_action_names = self._available_action_names(message) (L413)：与解析后的校验使用**同一调用**。
- 降级处理 (L511-513)：if plan.action not in available_action_names: plan.action = "IGNORE"。
- L513 注释：FETCH_KNOWLEDGE/RETHINK_GOAL downgrade removed; actions no longer offered to LLM。

**判定：** ✅ _available_action_names() 是 prompt enum、parser 校验和降级映射的唯一数据源。已移除的 FETCH_KNOWLEDGE/RETHINK_GOAL 既不出现在 prompt 中，也不在校验集合中。LLM 返回未知 action → IGNORE，而非静默通过。

### R03-06：WaitTool 结果只写 event extra，Planner 当 stale drop

**修复边界回顾：** wait 必须作为 typed outcome 返回并执行 no-send settlement、trace 和 proactive completion。

**源码证据：**
- WaitTool.call() (pfc_tools.py:116-118)：返回 "[SYSTEM_WAIT_SIGNAL]"，使用 _record_tool_execution 写入 trace。
- ConcurrentExecutor._run_tool_mode() (L708-713)：检测 "[SYSTEM_WAIT_SIGNAL]"，设置 strmai_execution_signal="wait" 和 strmai_execution_status="skipped_wait"，返回 None。
- Planner._finalize_plan_result() (L1424-1427)：xecution_signal == "wait" → skipped_reason="wait"。
- 无文本发送 (L1437-1458)：_record_agency_reflection(chat_id, None, ...) → action_taken="none"。_finalize_proactive_event(event, None) → reply_sent=False，callback 被移除。
- 不写入 dialogue segment (仅在 L1460+ 才写入，此时
eply_text is None 不经过该路径)。

**判定：** ✅ 工具 wait 不发文本，记录 skipped_wait，callback 被正确移除。wait 被作为 typed outcome (xecution_signal="wait") 传递而非被当作 stale drop 处理。

### R03-07：合法空 goal list 无法清除旧 goals

**修复边界回顾：** 区分 parse failure 与成功解析 []；空列表执行清理/衰减规则。

**源码证据：**
- GoalManager._parse_goals() (L197-223)：
  - items 为合法 list []（L200-201 或 L206）。
  - L210-211：isinstance(items, list) → True，不返回 None。
  - L214：or item in items: → 空循环。
  - L221-222：if items and not goals: → [] 为 falsy → 不执行
eturn None。
  - L223：
eturn goals → 返回 []（空列表）。
  - 如果 JSON 非法（L207 抛出异常）：返回 None。
- nalyze_and_update() (L124-127)：if new_goals is not None: → [] is not None → 进入。if not new_goals: → [] is falsy → self._goals.pop(chat_id, None) 清除所有旧目标。
- None 路径 (L123-124)：
ew_goals is None → 跳过整个更新块，旧状态保留。

**判定：** ✅ [] → 清除旧 goals。非法 JSON → None → 保留旧状态并记录失败（LLM 调用异常被 catch，返回 fallback 字符串 "陪伴用户..."）。

### R03-08：发送失败后的 claim 永久阻止 fallback model 重试

**修复边界回顾：** failed claim 可由同 turn 安全重试；committed/in-flight claim 才拒绝 duplicate。

**源码证据：**
- ChatRuntimeCoordinator.claim_send() (L189-212)：
  - L197-204：xisting.status == "failed" → 重置为 "claimed"，清空 outbound_message_ids 和 rror，返回 True。
  - L205-207：xisting is not None (且 status 不是 "failed"，即 claimed/committed) → 拒绝，返回 False。
-
eply_artifact_builder._send_segments() (L467-475)：发送异常且
ot artifact.sent 时调用 mark_send_failed() 设置 status="failed"。
- mark_send_failed() (L227-237)：设置 status = "failed"。
- 效果：首模型发送异常 → claim 标记为 failed；次模型重试时 claim_send() 检测到 failed → 重置为 claimed 并允许重试。已成功发送 → committed → 拒绝。

**判定：** ✅ 三态语义（claimed → committed/failed，failed → claimed 可重试）完整实现。首模型异常、次模型成功时用户收到一次；已成功发送仍拒绝重复。

### R03-09：stale 回复未发送却写入 dialogue/learning/proactive sent 状态

**修复边界回顾：** ReplyService 返回 sent/blocked/partial artifact；上层只对真实可见内容提交历史和 sent 状态。

**源码证据：**
- ReplyService.handle_reply() (L74-154)：
  - L107-117：FreshnessState.EXPIRED → 打印日志，调用 _settle_no_send_affection，**直接返回 artifact**（不经过发送、历史、记忆写入路径）。
  - L98-106：rtifact.blocked (e.g., outbound_policy blocked) → 同样返回，不写入。
  - 只有 L123 的 _send_segments 成功后才进入 L133-153 的 _sync_native_history_mirror、_ingest_memory_turn、_settle_post_send 写入路径。
- ReplyFreshnessMixin._build_outbound_policy() (L124-129)：FreshnessState.EXPIRED → OutboundPolicy(should_send=False)。
- ReplyArtifactMixin._build_visible_reply_artifact() (L256-263)：
ot policy.should_send → 返回 blocked artifact（无 visible_text，无 segments）。
- Planner._finalize_plan_result() (L1422-1458)：
eply_text is None 路径不写入 dialogue（L1460+ 在 if reply_text is None: return None 之后）。stale_drop 的 _finalize_proactive_event(event, None) 设置
eply_sent=False。

**判定：** ✅ freshness 过期时，ReplyService 在 handle_reply 中提前返回 blocked artifact，不经过任何写入路径。Planner 侧 _finalize_plan_result 对
eply_text is None 路径也不写入 dialogue/expression/proactive-sent。平台、dialogue、learning、memory 均无 phantom assistant turn。

## 代码质量观察

- **R03-05** 中的注释 # ponytail: M4 — FETCH_KNOWLEDGE/RETHINK_GOAL downgrade removed; actions no longer offered to LLM 清晰记录了刻意简化。
- **R03-08** 中的三态 claim 设计（claimed/committed/failed）语义清晰，mark_send_failed 和 claim_send 中的 failed→claimed 重置逻辑紧凑无冗余。
- **R03-09** 在 ReplyService 中尽早截断 (L107-117) 而非传播到下游再由各组件各自判断，符合收束原则。

## 潜在风险

- **R03-01**：monotonic() 的值在进程重启后从零开始，若 AgencyRuntimeStore 在重启后保留了旧的反射数据（tombstone/replay），TTL 会误判为过期。当前 _by_chat 是纯内存 dict，重启即清空，因此无此风险。
- **R03-08**：failed → claimed 重置清空了 outbound_message_ids 和 rror——若 failed claim 的原始 outbound_message_ids 需要保留用于审计/排查，此重置会丢失信息。可考虑在 _history 或单独的 audit log 中保留。

---

9 项全部通过。无阻塞项。
