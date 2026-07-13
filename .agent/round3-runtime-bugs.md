# AstrMai 第三轮扫描报告 — 运行时逻辑异常

> 扫描日期: 2026-07-02
> 方法: 8 个并行 explore agent 分领域扫描
> 覆盖: 数值计算、工具执行、回复管线、状态机、会话流转、人设记忆注入、视觉多模态、配置边界
> 总计: ~55 bugs

---

## 一、数值计算 (8 bugs)

### N1 (HIGH) — 记忆融合权重和0.9，recency权重丢失
**文件**: `memory_scoring.py:11-16` → `memory_retrieval_service.py:307-320`
```python
canonical_weight=0.25, hybrid_weight=0.45, importance_weight=0.15, confidence_weight=0.05
# recency_weight=0.10 定义但从未在 _fuse_candidates 使用
```
- 4个使用中权重和=0.90，系统性下偏~10%
- `recency_weight=0.10` 是死代码
- **修复**: 加入 recency_weighted 或重分布权重

### N2 (HIGH) — Compaction count_score (0-70) 支配所有其他信号
**文件**: `context_compaction.py:1065,771-782`
```
count_score: 0-70    (消息量)
closure_score: 0-10
tail_activity: -14~0
topic_density: 0-8
stability: 0-11
benefit: 0-10
阈值: >= 75.0
```
- 80条消息 count=32，需要其他信号全满分才到75
- 120条消息 count=70，几乎任何信号都触发
- 消息量是唯一真实决策驱动
- **修复**: 归一化 count_score 到相同量级

### N3 (MEDIUM) — compute_hot_score 无界log项支配有界freshness
**文件**: `memory_scoring.py:109-113`
```python
beta * freshness + (1-beta) * math.log(access_count + 1)
```
- beta=0.7, freshness ≤ 0.7
- log(101) ≈ 4.62 → 0.3*4.62 ≈ 1.39 远超 freshness
- 高频访问项永远排第一，beta不控制权重
- **修复**: 归一化 log 项或提高 beta

### N4 (MEDIUM) — Recency bonus (0-90) 对 priority (800-1000) 无意义
**文件**: `focus_selector.py:14-46`
- reply-to-bot: +1000, 10秒前仍有 ~1040
- latest-message: +20, 刚收到 ~110
- recency 对同类事件无法有效排序
- **修复**: 改为乘法 `score * exp(-age/tau)`

### N5 (MEDIUM) — 硬编码 urgency=0.58，忽略静默时长和energy
**文件**: `wakeup_service.py:232`
```python
urgency=0.58  # 固定值，从不根据 minutes_silent 或 energy 推导
```
- 静默5小时 vs 静默6分钟 → 相同的 urgency
- think_level_policy 中 urgency >= 0.75 才能触发高级思考 → 永不可达
- **修复**: 从 `minutes_silent` 和 `energy` 动态推导

### N6 (LOW) — _heartflow_metric 键名不匹配
**文件**: `think_level_policy.py:346-360`
- 查找 `"talk_frequency_adjust"` 但 dict 中是 `"heartflow_talk_frequency_adjust"`
- 直接查找永远失败，仅回退到 regex 解析
- **修复**: 对齐键名

### N7 (LOW) — rhythm 频率因子范围过窄 (±12%)
**文件**: `rhythm.py:129`
```python
factor = _clamp(1.0 + (0.7 - base) * 0.25, 0.88, 1.12)
```
- base 0.1→0.9 仅影响 ~6% → 配置几乎无实际效果
- **修复**: 放宽 clamp 范围

### N8 (LOW) — CJK字符级Jaccard误判短中文话题
**文件**: `conversation_continuity.py:76-85`
- "你好" vs "好吗" → Jaccard=0.33 超阈值 0.28
- 语义无关短文本被误判为同话题
- **修复**: CJK最小字符集大小≥6或用 bigram overlap

---

## 二、工具执行 (8 bugs)

### T1 (HIGH) — SubAgent 失败返回 [SUBAGENT_ERROR] 被当作工具成功
**文件**: `base_agent.py:84-88,126-131`
```python
return f"[SUBAGENT_ERROR] 任务执行过程中发生错误：{exc}..."
```
- `FunctionTool.call()` 返回任何字符串都是成功
- LLM 收到错误文本当作正常结果，不重试不告警
- **修复**: 定义错误类型或抛异常

### T2 (HIGH) — 整个 tool_loop 超时 → 所有中间结果丢失
**文件**: `gateway_lane.py:492-506`
```python
response = await asyncio.wait_for(
    self.context.tool_loop_agent(...),
    timeout=self._api_timeout(),  # 包裹整个循环
)
```
- 5步中4步成功 → 第5步慢 → 全部取消 → model重试 → 从头开始
- **修复**: 保存中间结果，重试时恢复

### T3 (HIGH) — max_steps耗尽→空回复→model重试→结果全部丢失
**文件**: `gateway_lane.py:507-510`
```python
if not reply_text.strip():
    raise ValueError("empty_response")  # 触发 model 重试
```
- 已执行的多步工具结果被丢弃
- **修复**: 返回已完成的部分结果而非空回复

### T4 (MEDIUM) — [SYSTEM_WAIT_SIGNAL]/[TERMINAL_YIELD] 字符串污染
**文件**: `executor.py:681-685`
```python
if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
    return None  # 终止整个回合
```
- SubAgent 正常输出中包含此字面量 → 被误判 → 回复丢失
- **修复**: 使用专用信号通道而非魔术字符串

### T5 (MEDIUM) — 工具结果在 model 重试时静默丢弃
**文件**: `executor.py:708-724`
- model失败 → 重试 → `tool_chat_in_lane_result` 从头调 → 旧结果全丢
- 工具副作用(cron创建/文件写入)已执行但 LLM 不可见
- **修复**: 保存执行历史

### T6 (MEDIUM) — _safe_parse_json 吞所有异常返回 {}

**文件**: `cognitive_loop.py:425-440`
```python
except Exception:
    return {}  # HTML错误页/连接错误静默当作"无决策"
```
- **修复**: 至少记录非JSON错误

### T7 (MEDIUM) — Light tool handler 注入静默失败
**文件**: `router.py:42-46`
- tool.name 与 raw_agent.name 不匹配 → handler 不注入 → 静默失败
- LLM 收到不透明的 `ValueError`
- **修复**: handler 未注入时记录 warning

### T8 (LOW) — tool_chat_in_lane 只返回 .text

**文件**: `gateway_lane.py:378-411`
- 调用方只看到最终回复文本
- 工具调用历史不可见
- **修复**: 可传递完整 LLMCallResult

---

## 三、回复管线 (8 bugs)

### R1 (CRITICAL) — terminal_yield 后的内容未验证就直接发送
**文件**: `executor.py:686-696`
```python
terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:"):].strip()
return await self._finalize_reply(..., terminal_content, ...)  # 未验证!
```
- `validate_visible_output_text()` 未调用
- LLM输出垃圾文本直接发给用户
- **修复**: 在 `_finalize_reply` 前加验证

### R2 (HIGH) — Fast mode 15s 超时 → 第一个 model 超时 → 第二个 model 已过期
**文件**: `executor.py:362-363`
- 3个 model 池中，第一个超时后第二个机会窗口已过
- 静默失效：空回退文本 `"(temporary silence...)"`
- **修复**: fast mode 加快速重试逻辑

### R3 (HIGH) — Follow-up 复用相同 system_prompt → 幻觉/重复回复
**文件**: `planner.py:1430-1436`
```python
await self.executor.execute(
    system_prompt=final_system_prompt,  # 复用完整system_prompt!
    prompt=follow_prompt,
)
```
- LLM 看到相同上下文两次 → 重复回复
- 时间锚点过期 → 上下文矛盾
- follow-up 结果不记录到 dialogue segments
- **修复**: follow-up 使用最小 system_prompt

### R4 (HIGH) — 外部回复在私聊中卡在 PRIVATE_WAIT
**文件**: `gate.py:724-729` + `external_result_bridge.py:50`
- _SyntheticExternalEvent 无 message_obj → 所有唤醒标志为 False
- 私聊中落入 PRIVATE_WAIT → 永不被处理
- **修复**: 外部事件应设置 `is_external_bot_reply` 避开 PRIVATE_WAIT

### R5 (MEDIUM) — Stance 限制截断回复 → 与认知意图矛盾
**文件**: `reply_artifact_builder.py:143-186`
- stance="cool" + intent="answer" → 截断到 2句/60字
- 认知可能后续 override intent 但 stance 限制不检查
- **修复**: stance 限制前检查最新 social_intent

### R6 (MEDIUM) — _clean_reply_content 回退文本覆盖 provider 失败
**文件**: `reply_artifact_builder.py:60-67`
- executor 路径正确抛出 → 但 handle_reply 直接路径不检查
- provider 失败文本被替换为中文占位符 → 用户看到无意义回复
- **修复**: handle_reply 增加 provider 失败检测

### R7 (LOW) — is_self_reply 标记在发送事件上设置但因命名混乱
**文件**: `reply_artifact_builder.py:353`
- 在原始事件上设置 `is_self_reply=True` 用于发送方追踪
- 非传感器路径可能误判为自回复
- **修复**: 重命名为 `reply_sent_by_self` 或用独立 key

### R8 (LOW) — gentle_two_step 合并为单段后仍用 gentle 延迟
**文件**: `reply_artifact_builder.py:226-231`
- 合并为1段后仍加 +0.25s 延迟
- 多余的延迟
- **修复**: 单段时用 "instant" 延迟

---

## 四、状态机 (6 bugs)

### S1 (HIGH) — last_decay_time 不入序列化 → 跨重启衰减计时器重置
**文件**: `relationship_engine.py:62-74,89`
- `to_dict()` 未包含 `last_decay_time`
- `from_dict()` 读取 `data.get("last_decay_time", time.time())` → 永远命中默认值
- 每次重启 → 所有关系衰减从当前时间重新计算 → 衰减永不触发(若重启频繁)
- **修复**: `to_dict()` 增加 `"last_decay_time": self.last_decay_time`

### S2 (HIGH) — 新ChatState energy recovery锚点为epoch 0
**文件**: `mood_decay.py:21-28`
```python
recovery_anchor = raw_last_recovery if raw_last_recovery > 0.0 else \
    float(getattr(state, "last_reply_time", 0.0) or 0.0)  # → 0.0
# (now - 0.0) >= recovery_window → 永远 True
```
- 新状态立即获得 `energy + 0.1`
- **修复**: `recovery_anchor <= 0.0` 时跳过恢复

### S3 (MEDIUM) — msg_count=0 → energy卡死在 min_threshold 下
**文件**: `energy_manager.py:41-49`
```python
drop_prob = 1.0  # 永远丢弃
recover_amount = msg_count * cost  # 0 * 0.05 = 0
```
- energy 不恢复 → 所有消息永远被丢弃
- **修复**: recover_amount 加 `max(cost, msg_count * cost)`

### S4 (MEDIUM) — min_threshold >= 0.5 时 drop_prob 爆炸
**文件**: `energy_manager.py:44`
```python
drop_prob = (0.5 - energy) / max(0.001, (0.5 - min_threshold))
# min_threshold ≥ 0.5 → 分母=0.001 → drop_prob >> 1.0
```
- **修复**: 加 `drop_prob = clamp(drop_prob, 0.0, 1.0)`

### S5 (MEDIUM) — 每日重置将 mood 强制归零（非衰减）
**文件**: `chat_state_service.py:141`
```python
state.mood = 0.0  # 23:59 mood=0.9 → 00:00 mood=0.0
```
- **修复**: 改为渐进衰减或保持 mood

### S6 (LOW) — CAS mood 更新的 epsilon 过于宽松
**文件**: `chat_state_service.py:317-321`
- epsilon=0.0001 意味着 mood 变化 <0.0001 才走 CAS 成功路径
- 任何微小自然衰减都触发 delta 回退
- **修复**: 适当放宽或改用版本号

---

## 五、会话流转 (8 bugs)

### F1 (HIGH) — 外部事件永远返回 INGRESS_EXTERNAL，绕过 wait/cooldown
**文件**: `chat_loop_kernel.py:1471-1472`
```python
if snapshot.trigger_type == "external":
    return self._make_decision(snapshot, "INGRESS_EXTERNAL", ...)
```
- bot 处于 WAIT 状态或 cooldown 中 → 外部事件仍被调度
- 信息洪泛风险
- **修复**: 外部事件检查 wait/cooldown 状态

### F2 (HIGH) — Worker 崩溃后 session.is_evaluating 永不重置
**文件**: `gate.py:351-358`
```python
def _handle_session_worker_result(self, task):
    try:
        task.result()
    except Exception:
        logger.error(...)
    # 忘记录! is_evaluating 未重置为 False
```
- 后续事件入 accumulation_pool 但没有新 worker → 消息永久堆积
- **修复**: 异常时设置 `session.is_evaluating = False`

### F3 (HIGH) — 所有事件都是 self-message 时 focus 返回 self-message
**文件**: `focus_selector.py:72,57`
- 回退到 `events[-1]` → 可能是 bot 自己的消息
- bot 对自己的消息生成回复 → 重复/奇怪行为
- **修复**: 回退时过滤 self-message

### F4 (MEDIUM) — PROACTIVE_BLOCKED 后消息永远不恢复
**文件**: `gate.py:650,652-654`
- `_proactive_dispatching` 永不重置为 False → 所有后续正常消息被队列化
- 队列满 5 条后静默丢弃
- **修复**: proactive 结束后重置标记 + drain 队列

### F5 (MEDIUM) — WAIT action 的 events 在下次 re-judge 时造成循环
**文件**: `gate.py:858-862`
- WAIT 的events 保留在 attention window
- 下次同批 events → 同样 WAIT → 死循环
- **修复**: re-judge 时给已 WAIT 过的 events 降低超时或去重

### F6 (MEDIUM) — thread root 找不到时返回 None 但 reason="explicit_reply_target"
**文件**: `thread_builder.py:16`
```python
return None, "explicit_reply_target"  # 实际未找到!
```
- 下游 consumer 看到 reason 误判
- **修复**: reason 改为 `"explicit_reply_target_not_found"`

### F7 (MEDIUM) — Heartbeat 中 heartflow_signal 与 pending_heartflow 同时存在时后处理丢失
**文件**: `chat_loop_kernel.py:1487-1512`
- proactive_signal 先处理 → pending_heartflow 延后
- 同一 tick 的 heartflow_signal 被 pending_heartflow 覆盖
- **修复**: 累积而非覆盖

### F8 (LOW) — 维护预算中非选中chat的 budget 为 1/1 → 始终通过预算检查
**文件**: `chat_loop_kernel.py:1482-1483`
- `_maintenance_budget_state` 对非选中 chat 返回 `{total:1, remaining:1}`
- 意味着所有非选中 chat 都通过预算检查
- 仅在实际有信号时才调度 → 无实际危害

---

## 六、人设/记忆注入 (9 bugs)

### M1 (CRITICAL) — 记忆直接注入 prompt 无 `<retrieved_memory>` 标签隔离
**文件**: `prompt_refiner.py:883-884,944-952`
```python
sanitize_memory_content(text)  # 调用但结果存入死字段
# 实际注入使用原始文本:
memory_parts.append(await self._resolve_visual_memory(injection))
sections.append("---记忆闪回---\n" + ...)
```
- `sanitize_memory_content()` 的 `<retrieved_memory>` 包装结果存入 `prompt_envelope.memory_block`（死字段）
- 实际注入使用原始未包装文本
- system prompt 中说要尊重 `<retrieved_memory>` 标签，但标签从未出现
- **安全漏洞**: 恶意存储的记忆可直接注入指令
- **修复**: 在注入点 `prompt_refiner.py:944-952` 使用已 sanitize 的内容

### M2 (HIGH) — LLM 生成的 deep_guidance 直接追加到 prompt 块
**文件**: `memory_context_builder.py:60-68`
- `_compress_guidance()` 调用 LLM 生成指导文本
- 结果直接追加到记忆块，无 sanitize
- 如果压缩LLM产生对抗性输出 → 注入到主LLM上下文
- **修复**: deep_guidance 用独立标签包装

### M3 (HIGH) — Epoch-0 时间戳获得最高 recency_score
**文件**: `v2_store.py:667-668`
```python
age_days = ... if created_at else 0.0  # created_at=0 → 0.0
recency_score = 1.0 / (1.0 + 0.0) = 1.0  # 最大值!
```
- 损坏的/零时间戳记忆排在最前面
- **修复**: `if created_at else 0.0` → `if created_at > 0 else 1e9`

### M4 (HIGH) — Hybrid 搜索结果全部 recency_score=1.0

**文件**: `memory_retrieval_service.py:81`
```python
recency_score=1.0  # 硬编码，忽略实际年龄
```
- 3个月前的向量匹配与昨天的同分
- **修复**: 从 v2 store 获取实际 created_at

### M5 (MEDIUM) — sanitize_memory_content 是死代码
**文件**: `prompt_envelope.py:24-36` + `prompt_refiner.py:883-884`
- 正确实现 `<retrieved_memory>` 包装
- 但结果存入 `prompt_envelope.memory_block`（永不被读）
- system rules 中的 `<retrieved_memory>` 防护完全无效
- **修复**: 在注入点复用 sanitized 结果

### M6 (MEDIUM) — 记忆指导标签与内容在同一文本块
**文件**: `memory_context_builder.py:65-69`
- "(internal memory reference; do not quote...)" 与记忆内容以换行分隔
- 恶意记忆可包含伪装的指导行
- **修复**: 用 XML 标签区分

### M7 (LOW) — Persona 切换可能返回缓存的旧 persona 数据
**文件**: `persona_summarizer.py:193-221`
- 缓存 self-healing 使用 `cached_data.get("raw", original_prompt)`
- 如果 cache 存在但属于旧 persona → 返回错误 shard
- **修复**: 缓存 key 包含 persona_id

### M8 (LOW) — 上下文满时记忆被截断优先于最近对话
**文件**: `prompt_refiner.py:288-398`
- `_apply_flexible_context_budget` 修剪顺序：background → warm → 记忆 → 最近对话
- 关键记忆先被截断，不重要的对话尾巴保留
- **修复**: 调整修剪优先级

### M9 (LOW) — self_lore_service 回退文本可能编码损坏
**文件**: `self_lore_service.py:45`
- GBK/UTF-8 混用可能导致回退文本显示乱码
- LLM 可能产生编码异常的回复
- **修复**: 确保 UTF-8 一致性

---

## 七、视觉/多模态 (7 bugs)

### V1 (HIGH) — GIF base64 decode errors="ignore" 静默损坏数据
**文件**: `image_pipeline.py:65`
```python
gif_data = base64.b64decode(gif_base64.encode("ascii", errors="ignore").decode("ascii"))
```
- `errors="ignore"` 静默丢弃非ASCII字节 → base64损坏 → GIF解码失败
- **修复**: 直接 `base64.b64decode(gif_base64)` + try/except

### V2 (HIGH) — VisualCortex worker 从不接收任务（死代码）
**文件**: `visual_cortex.py:33-34`
- `submit_task()` 定义但**从未被任何代码调用**
- `_worker()` 无限循环等待空队列
- 视觉处理实际在 `executor.py` 中同步阻塞执行
- **修复**: 连接 submit_task 或删除 VisualCortex

### V3 (HIGH) — put_nowait 未捕获 QueueFull → 任务丢失
**文件**: `visual_cortex.py:34`
```python
self.queue.put_nowait((picid, base64_data))  # 无 try/except
```
- 队列满(maxsize=100) → QueueFull 异常 → 未处理
- **修复**: try+log+drop 或改用 await put()

### V4 (MEDIUM) — 视觉缓存忽略 scope/chat 上下文
**文件**: `visual_cortex.py:86-89` + `orm_models.py:146-153`
- `_get_cached_memory(picid)` 只用 `picid` 做键
- `VisualMemory` 表无 `chat_id`/`scope_id` 列
- 同图在不同聊天 → 返回第一个聊天的分析结果
- **修复**: 缓存 key 包含 scope_id

### V5 (MEDIUM) — vision_binding.py 整个模块是死代码
**文件**: `vision_binding.py`, `gate.py:137-141`
- `extract_image_base64` 和 wrapper 定义但从不被调用
- 不支持 `data:` URI 格式
- **修复**: 连接或删除

### V6 (LOW) — _is_image_only 双重回退模式中断
**文件**: `gate.py:289-290`
```python
event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls"))
```
- `extracted_image_refs` 为空列表 `[]`(truthy) → 不回退
- 但 `extracted_image_urls` 可能有数据
- **修复**: 显式 `or` 而非 `get_extra(default)`

### V7 (INFO) — VL 模式无 text-only 优雅回退
**文件**: `executor.py:383-512`
- 所有视觉调用失败 → 无提示 → LLM 不知道有图片
- 无 "图片分析失败，以下是纯文字回复" 的提示
- **修复**: 分析失败时注入提示

---

## 八、配置边界 (9 bugs)

### C1 (HIGH) — asyncio.Semaphore(0) 崩溃
**文件**: `config.py:192` → `model_gateway.py:38`
- `max_concurrent_llm_calls: ge=0` → 0 可达
- `asyncio.Semaphore(0)` 在 Python < 3.12 抛 ValueError
- **修复**: 改 `ge=1` 或 `max(1, value)`

### C2 (HIGH) — api_timeout=0 立即杀死所有 LLM 调用
**文件**: `config.py:191` → `gateway_call.py:174`
- `api_timeout: ge=0.0` → 0 可达
- `asyncio.wait_for(..., timeout=0)` 立即 TimeoutError
- **修复**: 改 `ge=1.0`

### C3 (MEDIUM) — FrequencyController base_frequency 热重载失效
**文件**: `frequency_controller.py:64,68`
- `__init__` 缓存 `base_frequency` → `self.BASE_FREQ`
- `refresh_config()` 只设 `self.config`，不重读
- 热重载后使用旧 base_frequency
- **修复**: `refresh_config` 中重读

### C4 (MEDIUM) — energy drop_prob 上界未 clamp
**文件**: `energy_manager.py:44`
- `min_reply_threshold >= 0.5` → 分母 0.001 → drop_prob >> 1.0
- `random.random() < drop_prob` → 永远 True
- **修复**: 加 `min(1.0, ...)` clamp

### C5 (MEDIUM) — 孤儿配置字段（定义但从未使用）
**文件**: `config.py:65-70,185,148,163`
- `debounce_window`, `bg_pool_size`, `max_message_length` — 从未引用
- `throttle_probability`, `throttle_min_entropy`, `repeater_threshold` — 从未引用
- `auto_recall_probability` — 从未引用
- `enable_content_safety_filter` — 从未引用
- `enable_token_estimator` — 仅设标志但不门控
- `include_self_lore_in_prompt` — 从未引用
- **修复**: 连接或从 config 移除

### C6 (MEDIUM) — 关键字段缺少 validator
**文件**: `config.py:107-108,185`
- `review_runner_interval_sec`: 无 `ge` → 可设 0
- `review_runner_min_interval_sec`: 无 `ge`
- `auto_recall_probability`: 无 `ge=0.0, le=1.0`
- **修复**: 添加 `ge`/`le` 约束

### C7 (MEDIUM) — wakeup_cooldown=0 + silence_threshold=0 可导致 spam
**文件**: `config.py:117,120`
- 两者都 `ge=0`
- 组合 → 无冷却、无静默要求 → 每次 tick 发消息
- **修复**: 加下限

### C8 (LOW) — proactive_quiet_hours 默认值与 schema 描述矛盾
**文件**: `config.py:114` vs `_conf_schema.json`
- `_conf_schema.json`: "留空表示关闭 quiet hours"
- Pydantic default: `["23:30-07:30"]` (已启用)
- **修复**: 对齐默认值或描述

### C9 (LOW) — expression_governance_runner 不在热重载列表中
**文件**: `plugin_facade.py:98-110`
- `review_runner_interval_sec` 改变后 `expression_governance_runner` 不更新
- **修复**: 加入热重载列表

---

## 修复建议优先级

### 第一批 (安全/崩溃)
1. **M1** — 记忆注入无标签隔离（CRITICAL 安全）
2. **C1** — Semaphore(0) 崩溃
3. **C2** — api_timeout=0 全杀 LLM

### 第二批 (数据完整性)
4. **S1** — last_decay_time 不持久化
5. **N1** — 记忆融合权重不完整
6. **T2/T3** — 工具循环超时丢弃结果
7. **F2** — Worker 崩溃后 pool 永不 drain

### 第三批 (逻辑正确性)
8. **R1** — terminal_yield 未验证
9. **R3** — Follow-up 重复回复
10. **F1** — 外部事件绕过 wait/cooldown
11. **V1** — GIF base64 损坏
12. **N2** — Compaction count_score 霸权

### 第四批 (配置/边界)
13. **C3** — 热重载 base_frequency 失效
14. **C5** — 孤儿配置字段清理
15. **C6** — 缺失 validator

---
*扫描完成。所有发现均附带文件:行号引用。*
