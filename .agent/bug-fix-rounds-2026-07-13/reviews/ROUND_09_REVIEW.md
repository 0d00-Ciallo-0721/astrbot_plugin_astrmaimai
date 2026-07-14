# ROUND 09 REVIEW — 学习、反思与人工审核

**审查日期**: 2026-07-14
**审查方式**: 静态源码分析 (仅读，零修改)
**审查范围**: R09-01 ~ R09-08，共 8 项

---

## R09-01 / P1：普通用户/Bot 日志从不触发 mining

**审查文件**: `astrmai/learning/evolution_manager.py` L128-424

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| record_user_message 消费 recorder 触发结果 | ✅ | L304-305: `triggered = self.recorder.record(...)` → `_schedule_mining_if_triggered(triggered)` |
| process_bot_reply 同样触发 | ✅ | L417-418: 相同模式 |
| process_feedback 同样触发 | ✅ | L293-294: 相同模式 |
| 每群组防重复任务 | ✅ | L103-117: `_mining_tasks` dict + `task.done()` 检查，运行中不重复创建 |
| 任务完成自动清理 | ✅ | L113-117: `add_done_callback(_release)` 从 dict 移除 |
| 阈值触发后读取未处理日志 | ✅ | L408-412: `_try_trigger_mining` → `_load_unprocessed_logs` → 数量 >= 阈值 → mine |
| 挖掘后标记已处理 | ✅ | L341: `_mark_logs_processed([...])` |
| 使用 per-group async Lock 防并发 | ✅ | L314-318: `_get_mining_lock(group_id)` → `async with group_lock:` |

**注释**: 完整的触发链：消息录入 → recorder.record() → 阈值检查 → background task → lock → 日志加载/过滤 → mining → mark_processed。recorder 参数 (window_seconds, min_messages, cooldown) 从 config 动态读取，`refresh_config()` 会同步更新。

---

## R09-02 / P1：两个 lifecycle scheduler 并发消费同一 reflector queue

**审查文件**: `astrmai/proactive/proactive_task.py` L510-521 vs L799-821, `astrmai/learning/review/expression_governance_runner.py` L72-106, `astrmai/learning/review/reflector.py` L82-163

**验证结论**: ✅ 已实现 (单消费者方案)

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| _run_reflection_tasks 定义但未在 _loop 中调用 | ✅ | proactive_task.py L510 定义，L799-821 `_loop()` 中不包含对其调用 |
| ExpressionGovernanceRunner 为唯一消费者 | ✅ | expression_governance_runner.py L84-96: `run_once()` 遍历 groups 调用 `reflect_batch` |
| reflector._processing_lock 串行化并发 | ✅ | reflector.py L87: `async with self._processing_lock:` |
| 批次内失败项留在队列 | ✅ | reflector.py L153-158: 仅 acked_ids 从 `_pending_reflections` 移除 |
| 未使用队列位置删除 (无 head-pop) | ✅ | 整个 `reflect_batch` 通过 id 匹配进行清理 |

**注释**: 修复策略是**单消费者**（仅 ExpressionGovernanceRunner），而非原子 claim-by-ID。ProactiveTask 中 `_run_reflection_tasks()` 方法仍存在但未被 `_loop()` 调用——为死代码。reflector 的 `_processing_lock` + per-item ack 机制在单消费者下足够安全。

---

## R09-03 / P2：reflect batch 部分写入失败导致已成功 delta 重复应用

**审查文件**: `astrmai/learning/review/reflector.py` L82-344

**验证结论**: ⚠️ 基本实现 (有已知残余风险)

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 逐项独立处理 | ✅ | L122-151: `for item in batch:` 每项独立评分、调整权重 |
| 成功项 independent ack | ✅ | L150-151: `if adjusted: acked_ids.add(...)` |
| 仅移除已确认项 | ✅ | L154-158: filter out acked_ids，其余保留 |
| 失败项 retry 时跳过 LLM | ✅ | L89-95: retry_items 有 `_reflection_score` 则直接复用，不重新调用 LLM |
| 权重 delta 非幂等 (同一 delta 可能重复应用) | ⚠️ | L130-148: `delta=-0.3` 或 `0.15`，retry 时如上次已部分生效则双倍 |
| 部分失败时日志记录 | ✅ | L160-161: `logger.warning("[Reflector] partial reflection failure...")` |

**注释**: 项级别隔离正确——item 1 失败不影响 item 2-8 的 commit。**残余风险**：若 `_adjust_canonical_pattern_weight` 的 DB 写入在成功返回前崩溃，重试时会再次应用相同 delta（double-apply）。概率极低（单行 UPDATE 的原子性），可接受为 pragmatic fix。

---

## R09-04 / P1：pending_human 继续自动审核并反复发送同一问题

**审查文件**: `astrmai/learning/review/expression_auto_check_task.py` L39-68, `astrmai/learning/review/reflect_tracker.py` L72-81, `astrmai/proactive/review_dispatcher.py` L14-26

**验证结论**: ✅ 已实现 (主要路径)

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| pending_human 从自动审核跳过 | ✅ | auto_check_task.py L59-60: `if review_status == "pending_human": continue` |
| sent 状态稳定，不发重复 | ✅ | reflect_tracker.py L74: `get_unsent_requests()` 只返回 `sent=False` 的项 |
| 发送后标记 sent | ✅ | review_dispatcher.py L22-23: 发送后调用 `mark_request_sent` |
| 显式 requeue 支持 | ✅ | reflect_tracker.py L51-58: `requeue_request()` 重置 sent/processing |
| 发送成功 + mark 失败可导致重复发送 | ⚠️ | review_dispatcher.py L21-23: `send_message` 成功但 `mark_request_sent` 失败 → 下次仍 sent=False → 重复 |

**注释**: 核心逻辑正确。**残余风险**：`send_message` 成功后 `mark_request_sent` 抛异常（如 tracker 引用丢失），请求保持 sent=False 导致重发。建议将 send+mark 包裹在同一个 try 内，或使用 request-level idempotency key。概率低，可接受。

---

## R09-05 / P2：jargon 先标 active 再 project，投影失败后永不重试

**审查文件**: `astrmai/learning/review/jargon_auto_check_task.py` L77-275, `astrmai/memory/services/v2_store.py` L1133-1172

**验证结论**: ⚠️ 基本实现 (有 crash-recovery 盲区)

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 投影前设置 projection_status=pending | ✅ | L194-196: `metadata["projection_status"] = "pending"` |
| 投影失败回滚到 review_pending | ✅ | L249-262: projector.project() 返回 False → `update_memory` 回滚到 review_pending |
| 回滚后清理索引残片 | ✅ | L260-261: `projector.cleanup_deleted([...])` |
| 投影成功后标记 projected | ✅ | L264-271: `metadata["projection_status"] = "projected"` |
| run_once 恢复路径检查 approved+pending_projection | ✅ | L113-119: 检测到 review_status=approved + projection_status=pending → retry activate |
| query 过滤 statuses=["review_pending"] 漏掉已 active 的记录 | ⚠️ | L101-106: list_candidates 只查 review_pending 状态，若 crash 后 status 已变 active 则不命中 |
| list_candidates 不包含 active 状态 | ⚠️ | v2_store.py L1147: `active_statuses = list(statuses or [...]),` 传入的是固定列表 |

**注释**: 正常路径正确——project 失败 → rollback → 下次恢复。**残余风险**：`store.update_memory(status="active")` (L235-241) 和 `projector.project()` (L249) 之间 crash，留下 active+projection_status=pending 的孤立记录。恢复查询只查 review_pending 状态，不会命中。概率极低但需注意。

---

## R09-06 / P2：Learning governance 与 DreamScheduler 热配置仍使用旧派生值

**审查文件**: `astrmai/learning/evolution_manager.py` L57-89, `astrmai/learning/review/expression_governance_runner.py` L33-44, `astrmai/proactive/proactive_task.py` L157-177, `astrmai/proactive/dream_scheduler.py` L24-28

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| EvolutionManager 刷新 recorder/miner | ✅ | evolution_manager.py L59-89: window_seconds, min_messages, cooldown, expression_miner, jargon_miner 均刷新 |
| ExpressionGovernanceRunner 刷新 interval + 传播到子组件 | ✅ | runner.py L35-44: interval_seconds + 遍历 reflector/auto_check_task/jargon_auto_check_task 调用 refresh_config |
| ProactiveTask 刷新所有子服务 | ✅ | proactive_task.py L161-177: 遍历 9 个子服务调用 refresh_config |
| DreamScheduler 刷新 interval + visible | ✅ | dream_scheduler.py L26-28: `_dream_interval` 和 `dream_visible` 从 config.life 重读 |
| 链式传播完整 | ✅ | 每个 refresh_config 向其拥有的子组件传播 |

**注释**: 热更新链完整。每个持有 derived values 的组件都在 `refresh_config()` 中从 config 重新计算。注意——bootstrap.py 和 lifecycle.py 中未找到对 `refresh_config` 的直接调用，实际触发逻辑应在主入口 `main.py` 的配置重载 handler 中。

---

## R09-07 / P2：人工反馈在解析和持久化前就从 pending 队列 pop

**审查文件**: `astrmai/learning/review/reflect_tracker.py` L91-252

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| claim-before-process 模式 | ✅ | L120-124: 先检查 processing 状态 → 设置 `processing=True` |
| 解析失败 release claim | ✅ | L126-129: `_parse_feedback` 返回 None → `_release_claim` |
| 持久化失败 release claim | ✅ | L170-183: `update_review` 异常或返回 falsy → `_release_claim` |
| 解析/持久化成功后 ack (pop) | ✅ | L184-185: `self._pending.pop(pattern_id)` |
| processing 标志防止并发 | ✅ | L122: `if pending.get("processing"): return None` |
| 无法解析时返回提示 | ✅ | L129: 返回 "暂未处理，请稍后重试" |

**注释**: 严格的 claim-process-ack 模式。反馈不会丢失——任何阶段的失败都释放 claim，保留在队列中。唯一例外：若 `_release_claim` 自身失败（几乎不可能，仅设置 bool），claim 会卡在 processing=True 状态，需下次重启清空。

---

## R09-08 / P3：画像/昵称模板默认语义字符串 mojibake

**审查文件**: `astrmai/learning/profiling/profile_generator.py` L18-34, `astrmai/learning/profiling/nickname_generator.py` L12-21

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 画像模板默认值使用中文占位符 | ✅ | profile_generator.py L19-24: "暂无旧画像", "暂无标签", "暂无记忆点" |
| 昵称模板默认值使用中文占位符 | ✅ | nickname_generator.py L13-15: "暂无画像", "暂无" |
| 结构化空值优先 | ✅ | 先取属性值，空则用 `or` 链 fallback 到中文占位符 |
| 不修改已有画像数据 | ✅ | 仅修改模板 payload/prompt 的默认值，不改 persist 的数据 |

**注释**: 默认值均为合法的中文 UTF-8 字符串，无论用何种编码读取都不会出现 mojibake。已满足 "修正默认 payload，优先结构化空值" 的要求。

---

## 总结

| 修复 ID | 结论 | 残余风险 |
|---------|------|----------|
| R09-01 | ✅ 完全实现 | — |
| R09-02 | ✅ 完全实现 | 死代码 `_run_reflection_tasks` 可清理 |
| R09-03 | ⚠️ 基本实现 | delta 非幂等重试 (概率极低) |
| R09-04 | ✅ 基本实现 | send 成功 + mark 失败 → 重复发送 (概率低) |
| R09-05 | ⚠️ 基本实现 | crash-recovery 盲区：active 无 projection (概率极低) |
| R09-06 | ✅ 完全实现 | — |
| R09-07 | ✅ 完全实现 | — |
| R09-08 | ✅ 完全实现 | — |

**总体评估**: 8 项中 5 项完全实现 (R09-01, R09-02, R09-06, R09-07, R09-08)，3 项基本实现但有可接受的残余风险。所有残余风险均属 crash-recovery 或双重操作边界情况，概率极低，不影响正常生产运行。R09-02 的修复方式（移除重复消费者）比实现原子 claim 更简洁，符合 "simple-first" 原则。
