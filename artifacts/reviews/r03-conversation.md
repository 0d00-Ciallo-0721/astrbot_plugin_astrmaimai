# 审查报告：astrmai/conversation/planning/ + astrmai/conversation/execution/
> task_id: r12-conversation | 审查时间: 2025-07-16

## 概述
- 审查文件数: 12（cognitive_loop.py, planner.py, conversation_continuity.py, behavior_tuning.py, think_level_policy.py, planning_input_loader.py, executor.py, reply_freshness.py, agency_feedback_bridge.py, agency_runtime.py, expression_policy.py, goal_service.py）
- 发现总数: 20
- 严重: 3 | 中等: 10 | 建议: 7

---

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | **cognitive_loop.py:295-299** | `_safe_parse_json` 使用贪心正则 `r"\{.*\}"`（DOTALL）匹配 JSON。当 LLM 返回包含嵌套 `{}` 的文本（如含 json 代码块、多层字典、示例数据）时，会从第一个 `{` 匹配到最后一个 `}`，可能解析出错误的 JSON 结构或导致解析失败。应改用 `r"\{[^{}]*\}"` 非贪心模式或逐层解析。 |
| 2 | **executor.py:528-533, 589-594** | `_run_text_mode` 和 `_run_tool_mode` 中，**stale_drop 新鲜度检查在模型循环内部**：若模型 A 调用失败（耗时数秒），在尝试模型 B 之前重新检查新鲜度。路径正确，但**致命问题是**：当 `_check_pre_model_freshness` 返回 `False`（EXPIRED）时，仅设置 `astrmai_execution_status = "stale_drop"` 并返回 `None`。此时外层 `execute()` 的 `finally` 块会调用 `_release_chat_execution_lock` 释放锁，但 `plan_and_execute` 中 `reply_text is None` 的分支会调用 `_handle_fatal_fallback` 发送兜底回复（`fallback_text`）。**这意味着 stale_drop 的请求仍然会收到一条无意义的兜底回复**，用户会看到 "(temporary silence...)" 等文本——明明是因过时而丢弃，却仍然发送了消息，违背了 stale_drop 的设计意图。 |
| 3 | **conversation_continuity.py:170-171** | `record()` 中 `lightweight_event` / `wait` / `ignore` 路径**直接 `return item`，跳过 `state.turns.append`**。这意味着非实质性轮次完全不记录在轮次历史中。后续 `summary()` 的 `recent_turns` 切片仅来自 `state.turns`，因此 lightweight event 的上下文对未来的 `summary()` 完全不可见。如果连续多次 lightweight 事件（如心跳、状态同步）后紧跟一条正常消息，`turn_count` 不会反映真实对话轮次，可能导致 `continuity_weight` 计算偏差。设计注释引用了 `TECHNICAL-DEBT-INVENTORY D22`，说明此行为已知，但影响面需评估。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | **cognitive_loop.py:205-209** | `_build_finalize_prompt` 调用 `_build_initial_prompt(event, prompt_envelope)` 重新构建完整初始提示，然后再拼接 prior_data 和 observation。`_build_initial_prompt` 内部涉及多个 `get_extra` 调用和字符串拼接。这在 LLM 调用路径中造成**约 2 倍的无意义重复计算**。建议将初始提示结果缓存后复用。 |
| 5 | **cognitive_loop.py:223-237** | `gate_decision()` 在 `should_run()` 中被调用一次（line 99），随后 `decide()` 再次调用 `gate_decision()`（line 121）。这意味着对于每个需要 CognitiveLoop 决策的轮次，**门控逻辑被执行两次**。`should_run()` 的第二次 `_write_gate_state(event, gate, ran=False)` 在 `decide()` 开始时覆盖了 `should_run()` 中可能已设置的 ran=True 状态。 |
| 6 | **planner.py:1212-1213** | `stale_drop` 分支调用 `_record_conversation_continuity` 时传入 `tools=None`，导致 `action_taken="none"`。但此时 tools 实际已构建（line 1130-1135），只是执行器因过期而未使用。这会导致连续性状态中记录的 `last_action_taken` 为 "none"，后续轮次读到的历史不准确——本该是有工具但丢弃，却被记录为无操作。 |
| 7 | **conversation_continuity.py:112-126** | `_topic_similarity` 中 ASCII tokens 不足 2 个时退化为字符级 Jaccard。对 CJK 文本（如"今天天气真好啊" vs "今天心情真好呀"），字符级 Jaccard 噪声大。且若两者均为空集（无 ≥3 字符的 ASCII token），`left_chars` 和 `right_chars` 也可能在 `_normalize_topic_text` 去空格后产生非空字符集——但若均为空字符串则返回 0.0，此时 `_is_same_topic` 直接返回 False，**即两个空 topic 不会被视为相同话题**，这可能导致 `goal_status` 被误判为 "new"。 |
| 8 | **conversation_continuity.py:138-152** | `_expire_state_if_stale` 超时后**直接清空所有状态字段**（topic、goal、turns 等），相当于硬重置。而 `recent()`（line 157-164）是软过期（仅过滤超时的 turns）。两者对"过期"的定义一致（`TURN_TTL_SECONDS`），但硬重置的破坏性更大。如果某轮次被 `_expire_state_if_stale` 清空后，紧接着 `record()` 又判定为"continuing"（因无历史 topic），会出现状态矛盾。 |
| 9 | **executor.py:497-498** | `_execute` 方法中 `event._is_final_reply_phase = True` 使用了 `event` 对象的**私有属性直接赋值**。`_is_final_reply_phase` 以下划线开头，表示内部实现细节。其他模块可能未约定此属性名，且属性在 `finally` 中通过 `delattr` 删除——若在 try 块内发生异常导致 `finally` 未正确执行（理论上不会，但 with 嵌套复杂时存在风险），会残留脏属性。 |
| 10 | **executor.py:735-737** | `_handle_fatal_fallback` 在模型池耗尽时发送 `fallback_text` — 这是正确的。但该 fallback 也**被 stale_drop 路径触发**（`_run_text_mode` 和 `_run_tool_mode` 在模型池耗尽后也调用 `_handle_fatal_fallback`）。问题是 stale_drop 的期望行为是"静默丢弃"，而非发送 fallback。应在 stale_drop 路径中跳过 fallback。 |
| 11 | **planner.py:1222-1225** | `_record_conversation_continuity` 在 stale_drop 和 reply 成功两个路径中都调用了 `_apply_turn_continuity_context(event, chat_id)`，但此时 `ConversationContinuityStore.record()` 刚完成（或部分完成），紧接着又 snapshot 读取。这种"写后立即读"在单线程 asyncio 中没问题，但增加了无意义的 IO 开销。 |
| 12 | **executor.py:370-383** | `_execution_runtime_values` 中 `max_steps` 的计算逻辑：当 `tool_tier == "chat"` 时设为 2，否则为 `max(5, config_max_steps)`。若 `config_max_steps` 为 10，`max(5, 10)=10` 是正确的。但注释无说明为何 `chat` 模式下硬编码为 2——这可能限制某些需要多步推理的 chat-only 场景。 |
| 13 | **think_level_policy.py:98-101** | `decide()` 中 `continuity.get("goal_status") == "continuing"` 且 `turn_count > 0` 且文本 ≤18 字符且非疑问句时，直接返回 `level=0`（无需推理）。这**假设"continuing"状态的简短回复一定不需要推理**，但用户可能用简短命令（如"继续"、"然后呢"）触发需要记忆检索的后续回复。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 14 | **cognitive_loop.py:55-68** | `COMPLEXITY_HINTS` 包含单字符 `?` 和 `？`。任何包含问号的文本（包括 URL 参数、表情符号、标点符号）都会命中，导致 `gate_decision` 将简单对话误判为复杂。建议对单字符 hint 增加长度约束（如 `?` 单独出现不算，仅在文本其余部分 ≥3 字符时才匹配）。 |
| 15 | **executor.py:553-554, 614-615** | `_run_text_mode` 和 `_run_tool_mode` 中 `stale_drop` 后设置 `event.set_extra("astrmai_execution_status", "stale_drop")`，但 `_check_pre_model_freshness` 内部已通过 `event.get_extra` 读取新鲜度状态，外部代码（如 planner）通过 `reply_text is None` 判断 stale_drop。两处信息源应归一化，避免 `astrmai_execution_status` 成为无消费者死代码。 |
| 16 | **conversation_continuity.py:99-100** | `_normalize_topic_text` 中 `value.split(":", 1)[1]` 在 value 不含 `:` 时会引发 `IndexError`（虽然严格来说 `"xx:" in value` 的 if 判断已保护，但若冒号是 CJK 全角 `：` 则走另一个分支）。建议用 `value.split(":", 1)[-1]` 或加 try/except 防御。 |
| 17 | **executor.py:398-414** | `_inject_direct_vision_context` 中下载远程图片、创建临时文件、调用 vision API，**整个流程完全同步阻塞**（虽然是 async，但每个 URL 串行处理）。若图片较大或网络慢，会显著增加执行器延迟。建议增加并发下载（`asyncio.gather`）和超时控制。 |
| 18 | **planner.py:1245-1246** | `follow-up` 分支中 `await asyncio.sleep(random.uniform(1.0, 3.5))` 模拟人类追加消息的延迟。但此延迟**发生在持有 `executor` 锁期间**（虽然 follow-up 是用新的 `executor.execute` 调用，但 `plan_and_execute` 本身未持锁——仅 executor 内部持锁）。确认此 sleep 是否真的在锁外，否则会阻塞其他并发请求。 |
| 19 | **executor.py:749-751** | `_handle_fatal_fallback` 向管理员推送错误通知时，假设 `platform_id` 可从 `event.unified_msg_origin.split(":")[0]` 获取。但 `unified_msg_origin` 格式可能因平台而异（如 `qq:GroupMessage:12345` 或 `telegram:12345`），直接 split 取第一个元素可能得到 "qq" 或 "telegram" 等平台名——这在构造 `admin_umo` 时作为平台 ID 使用，若平台名与实际 UMO 前缀不匹配，消息将发送失败。 |
| 20 | **cognitive_loop.py:121-122** | `decide()` 中 `self._write_gate_state(event, gate, ran=False)` 重复调用——`should_run()` 已在 line 99-100 调用过一次。虽然第二次赋相同值无副作用，但这是多余的 IO（`set_extra` + turn_context 赋值），建议在 `decide()` 中复用 `should_run()` 的结果或移除 `decide()` 中的重复调用。 |

---

## 亮点

1. **ConversationContinuityStore 的 `record()` 设计清晰**：`lightweight_event` / `wait` / `ignore` 的跳过逻辑有明确设计文档（TECHNICAL-DEBT-INVENTORY D22），状态机各分支（new/continuing/redirected/guarded/observing）覆盖了主要场景。
2. **CognitiveLoop 的 readonly tool gate**：`_readonly_tool_gate` 和 `ALLOWED_READONLY_TOOLS` 的白名单机制设计严谨，有效防止 CognitiveLoop 越权调用副作用工具。`_allow_pushback` 的 flag 组合守卫逻辑细致，区分了直接攻击与第三方冲突。
3. **Executor 的 `_check_pre_model_freshness`**：在每次模型尝试前都检查新鲜度，对长尾推理场景（模型反复失败、重试）提供了及时的过期退出，避免浪费 token。
4. **ThinkLevelPolicy 的 Heartflow 集成**：与 Heartflow 状态（insert_pressure、talk_willingness、candidate_score 等）的联动决策缜密，覆盖了频率守卫、姿态信号、直接问句等多种场景，体现了领域知识深度。
5. **规划-执行分离架构**：Planner（策略层）与 ConcurrentExecutor（执行层）职责清晰。Planner 处理 prompt 构建、认知决策、连续性跟踪；Executor 专注模型调用、重试、新鲜度检查，分工明确。

---

## 总结

规划与执行两个模块构成了 AstrMai System 2 的主干管道，整体质量较高。**CognitiveLoop** 的决策链（gate → decide → readonly tool → finalize）完整且安全，但 `_safe_parse_json` 的贪心正则和 `gate_decision` 的重复调用是明显的瑕疵。**ConcurrentExecutor** 的 `text_mode`/`tool_mode` 模型重试 + 新鲜度检查路径设计合理，但 stale_drop 后仍触发 fallback 回复是严重的行为缺陷——用户会在本应静默丢弃的场景下收到无意义回复。**ConversationContinuityStore** 的状态机设计清晰，`lightweight_event` 的跳过策略有明确文档，但完全不记录轻量事件的轮次历史可能导致 `turn_count` 偏差。`_expire_state_if_stale` 的硬重置与 `recent()` 的软过期存在不一致性。

**最需优先修复的三项**：
1. **(🔴#2)** Stale_drop 后不应触发 fallback 回复——在 `_run_text_mode`/`_run_tool_mode` 中区分"模型池耗尽"与"stale_drop"，后者直接返回 None 不调用 `_handle_fatal_fallback`。
2. **(🔴#1)** 修复 `_safe_parse_json` 的贪心正则，改用非贪心或逐层解析。
3. **(🟡#6)** stale_drop 分支的 `_record_conversation_continuity` 应传入真实的 tools 列表，而非 None，以保持 `action_taken` 记录的准确性。
