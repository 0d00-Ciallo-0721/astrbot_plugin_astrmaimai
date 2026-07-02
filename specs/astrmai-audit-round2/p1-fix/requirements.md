# Requirements Document — AstrMai P1 Fix Round-2

> Spec: `astrmai-audit-round2/p1-fix` | Type: hardening  
> Source: `specs/astrmai-audit-round1/bug-classification.md`  
> Target: 21 P1 bugs across 5 modules

## Introduction

本 Spec 对 AstrMai 插件 Round-1 审计中识别的 21 项 P1 (高优先级) 缺陷进行修复。范围覆盖：

- **生命周期/资源泄漏** (9 项): 无界 dicts、未清理 locks、僵尸 tasks、未释放连接
- **数据一致性/竞态** (5 项): TOCTOU races、无锁读取、任务创建竞态、session 阻塞
- **错误处理/LLM 韧性** (5 项): 缺 lane_key、返回值异常、闭包捕获、异常吞噬
- **事件流/Hook** (2 项): heartflow_is_command 未实现、缺 on_llm_response hook

明确不在本 Spec 范围：P0 (已修复)、P2/P3、架构重构、性能优化、新功能开发。

## Glossary

- **EARS**：Easy Approach to Requirements Syntax，本文档所有 Acceptance Criteria 遵循的句式。
- **P1**：高优先级 — 功能错误/状态异常/关键路径断裂/竞态条件，非立即崩溃但会逐渐恶化。
- **TOCTOU**：Time-of-Check to Time-of-Use race condition。
- **Unbounded dict**：字典随运行时间无限增长，无清理机制，导致内存泄漏。
- **Lane Key**：AstrMai 对话路由键，用于 LLM 调用的路由/限流控制。
- **lifecycle.py guard**：指 `lifecycle.py:22-25` 中 `track_task` 的 `RuntimeError` 保护模式。
- **done_callback**：`asyncio.Task.add_done_callback()` 用于捕获协程任务中的未处理异常。

## Requirements — Wave/Phase 划分

### Wave 1: 生命周期/资源泄漏修复 (R1–R9)

| Requirement | Bug ID | File | Description |
|-------------|--------|------|-------------|
| R1 | P1.1 | `persona_summarizer.py:196,271` | `create_task` 无 `done_callback` → 僵尸异常静默丢失 |
| R2 | P1.3 | `v2_store.py:60-61` | `_session_locks` dict 无界增长 |
| R3 | P1.4 | `memory_engine.py:87-88` | `_cognitive_feedback_cache` dict 无界增长 |
| R4 | P1.5 | `memory_turn_pipeline.py:38-44` | 4 个 dict 无界增长 |
| R5 | P1.9 | `reflector.py:33` | `_pending_reflections` list 治理停止时无界增长 |
| R6 | P1.10 | `event_bus.py:57-66` | `affection_changed` Event 未清除 → 永久过期 |
| R7 | P1.11 | `lane_manager.py:89-93` | `_lane_locks` dict 无界增长 |
| R8 | P1.12 | `chat_runtime_coordinator.py:26-35` | `_states` dict 从未清理 |
| R9 | P1.13 | `persistence_manager.py:54-55` | `dispose()` 从未被调用 |

### Wave 2: 数据一致性/竞态修复 (R10–R14)

| Requirement | Bug ID | File | Description |
|-------------|--------|------|-------------|
| R10 | P1.7 | `reflect_tracker.py:70-134` | `try_consume_feedback` TOCTOU 竞态 → 双重处理 |
| R11 | P1.8 | `reflect_tracker.py:55-58` | `get_unsent_requests` 标记 ALL 为 sent → 孤儿条目 |
| R12 | P1.14 | `database_service.py:189` | `get_chat_state` 无锁读取 vs 有锁写入 |
| R13 | P1.15 | `context_compaction.py:298-314` | 压缩任务创建竞态 → 丢失任务引用 |
| R14 | P1.16 | `gate.py:839-844` | Session worker 同步阻塞 System2 → 整个评估期锁定 |

### Wave 3: 错误处理/LLM 韧性修复 (R15–R19)

| Requirement | Bug ID | File | Description |
|-------------|--------|------|-------------|
| R15 | P1.2 | `memory_retrieval_service.py:383` | LLM 调用缺 `lane_key` → 限流绕过 |
| R16 | P1.6 | `hybrid_retriever.py:27-31` | `add_memory` 返回 `None` 时调用者误作有效 doc_id |
| R17 | P1.17 | `bootstrap.py:504-510` | 闭包捕获 `runtime.system2_callback` pre-binding |
| R18 | P1.18 | `lifecycle.py:22-25` | `track_task` 无 `RuntimeError` guard → shutdown 竞态崩溃 |
| R19 | P1.19 | `plugin_facade.py:451-510` | 仅捕获 `LLMCascadeFailureException`；其他异常传播未处理 |

### Wave 4: 事件流/Hook 修复 (R20–R21)

| Requirement | Bug ID | File | Description |
|-------------|--------|------|-------------|
| R20 | P1.20 | `main.py` | `heartflow_is_command` 标记完全未实现 |
| R21 | P1.21 | `main.py:80-115` | 缺 `on_llm_response` hook |

---

## Wave 1 — 生命周期/资源泄漏修复 (R1–R9)

### Requirement R1: `persona_summarizer.py` — create_task 无 done_callback

**User Story:** 作为运维人员，我希望后台任务异常能被捕获并记录，以便排查 PersonaSummarizer 切片生成失败时不会静默丢失。

#### Acceptance Criteria
1. THE `asyncio.create_task()` 调用 (lines 196, 271) SHALL 添加 `add_done_callback()` 以捕获不触发异常。
2. WHEN 后台任务抛出异常，THE 系统 SHALL 通过 logger 记录该异常。
3. THE 修复 SHALL 参照 `lifecycle.py:25` 和 `memory_turn_pipeline.py:55` 已有的 done_callback 模式。

#### Notes / Constraints
- 涉及文件: `astrmai/memory/persona/persona_summarizer.py:196,271`
- 最小改动: 每处 +1 行 `task.add_done_callback(self._handle_background_task_result)`
- 需在类中添加 ~3 行的 `_handle_background_task_result` 方法

### Requirement R2: `v2_store.py` — _session_locks 无界增长

**User Story:** 作为系统维护者，我希望 MemoryV2Store 的 session locks 不会随会话数无限增长，以防长期运行后内存耗尽。

#### Acceptance Criteria
1. THE `_session_locks` dict SHALL 有上限 (max 200 个 session)。
2. WHEN 超过上限，THE 系统 SHALL 删除最旧的未使用 lock。
3. THE 清理机制 SHALL 在 `_get_session_lock` 中惰性触发，不需额外定时器。

#### Notes / Constraints
- 涉及文件: `astrmai/memory/services/v2_store.py:60-61,68-75`
- 最小改动: `_get_session_lock` 中添加 ~5 行 LRU 淘汰逻辑
- 使用 `OrderedDict` 或简单的 `len()` + `popitem()` 模式

### Requirement R3: `memory_engine.py` — _cognitive_feedback_cache 无界增长

**User Story:** 作为系统维护者，我希望 `_cognitive_feedback_cache` 不会随 chat_id 数量无限增长。

#### Acceptance Criteria
1. THE `_cognitive_feedback_cache` dict SHALL 有上限 (max 100 个 chat_id)。
2. WHEN 超过上限，THE 系统 SHALL 清除最旧条目。
3. THE 限制 SHALL 在每次写入缓存时检查。

#### Notes / Constraints
- 涉及文件: `astrmai/memory/services/memory_engine.py:87`
- 最小改动: ~3 行长度检查在 `_cognitive_feedback_cache[chat_id] = ...` 处
- 搜索代码找到所有写入点

### Requirement R4: `memory_turn_pipeline.py` — 4 个 dict 无界增长

**User Story:** 作为系统维护者，我希望 MemoryTurnPipeline 的 4 个 per-chat dicts 在 chat 不活跃后被清理。

#### Acceptance Criteria
1. THE 系统 SHALL 在 `_sweep_loop` 中定期清理不活跃的 chat 条目。
2. WHEN chat 超过 30 分钟无活动，THE 系统 SHALL 移除其 `_session_history_buffer`、`_memory_locks`、`_worker_tasks`、`_worker_queues` 条目。
3. THE 清理 SHALL 保留 `_instant_llm_last_check` dict 的活跃检查。

#### Notes / Constraints
- 涉及文件: `astrmai/memory/services/memory_turn_pipeline.py:38-44,52`
- `_sweep_loop` 已存在，在其中添加 ~10 行清理逻辑
- 需要获取最后一次活动时间（可从 `_instant_llm_last_check` 或其他来源）

### Requirement R5: `reflector.py` — _pending_reflections 治理停止时无界增长

**User Story:** 作为系统维护者，当治理循环(ProactiveTask)停止时，`_pending_reflections` 不应无限增长。

#### Acceptance Criteria
1. THE `_pending_reflections` list SHALL 有最大长度限制 (max 200)。
2. WHEN 超过限制，THE 系统 SHALL 丢弃最旧条目并记录 warning。
3. THE 限制 SHALL 在 `record_usage` 追加时检查。

#### Notes / Constraints
- 涉及文件: `astrmai/learning/review/reflector.py:33,60-63`
- 最小改动: `record_usage` 末尾 +3 行检查

### Requirement R6: `event_bus.py` — affection_changed Event 从未清除

**User Story:** 作为插件开发者，每次好感度变更事件应正确触发 await wait() 唤醒，而非首次触发后永久跳过。

#### Acceptance Criteria
1. THE `trigger_affection_change()` SHALL 在每次事件后清除 `affection_changed` Event。
2. THE 清除 SHALL 发生在 publish 完成之后，避免订阅者错过事件。
3. WHEN `affection_changed.set()` 被调用，THE Event SHALL 被消费后重置。

#### Notes / Constraints
- 涉及文件: `astrmai/infrastructure/runtime/event_bus.py:57-66`
- 最小改动: 在 `trigger_affection_change()` 末尾 +1 行 `self.affection_changed.clear()`
- 注意：`.clear()` 必须在 publish 之后执行

### Requirement R7: `lane_manager.py` — _lane_locks 无界增长

**User Story:** 作为系统维护者，lane locks 不应随每次 prompt 版本变更新增而永久积累。

#### Acceptance Criteria
1. THE `_lane_locks` dict SHALL 有上限 (max 100 个 lane)。
2. WHEN 超过上限，THE 系统 SHALL 惰性清理未使用的 lock。

#### Notes / Constraints
- 涉及文件: `astrmai/infrastructure/runtime/lane_manager.py:89-93`
- 最小改动: `_get_lane_lock` 中添加 ~5 行 LRU 淘汰
- 使用 `OrderedDict` 或将 `_lane_locks` 改为 `OrderedDict`

### Requirement R8: `chat_runtime_coordinator.py` — _states dict 从未清理

**User Story:** 作为系统维护者，chat runtime states 不应随数百个群聊永久积累。

#### Acceptance Criteria
1. THE `ChatRuntimeCoordinator` SHALL 提供 `prune_inactive()` 方法清理超过 30 分钟无活动的 chat state。
2. THE `prune_inactive()` SHALL 在 shutdown 或定期调用时被触发。
3. WHEN chat_id 超过 30 分钟无活动，THE 系统 SHALL 移除其 ChatRuntimeState。

#### Notes / Constraints
- 涉及文件: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:26-35`
- `ChatRuntimeState` 已有 `latest_activity_ts` 字段可用于判断
- 最小改动: +8 行 `prune_inactive()` 方法

### Requirement R9: `persistence_manager.py` — dispose() 从未被调用

**User Story:** 作为运维人员，插件重载时 SQLAlchemy engine pool 应被正确释放，避免连接泄漏。

#### Acceptance Criteria
1. THE `dispose()` 方法 SHALL 在插件 `terminate()` 时被调用。
2. THE `plugin_facade.py` 的 `terminate()` SHALL 调用 `self.runtime.persistence.dispose()`。

#### Notes / Constraints
- 涉及文件: `astrmai/infrastructure/persistence/persistence_manager.py:54-55`, `astrmai/app/plugin_facade.py`
- `dispose()` 已存在，只需在 terminate 链路中调用
- 最小改动: plugin_facade.py terminate +1 行

---

## Wave 2 — 数据一致性/竞态修复 (R10–R14)

### Requirement R10: `reflect_tracker.py` — try_consume_feedback TOCTOU 竞态

**User Story:** 作为系统维护者，同一审核请求不应被两个并发调用重复处理。

#### Acceptance Criteria
1. THE `try_consume_feedback` SHALL 在 LLM 调用和 DB 更新期间持有 `_lock`，防止同一 candidate 被两次处理。
2. WHEN 两个并发调用对同一 candidate 执行，THE 第二个调用 SHALL 发现 candidate 已被移除。
3. THE 修复 SHALL NOT 在 LLM 调用期间持有 lock（避免阻塞其他操作）。

#### Notes / Constraints
- 涉及文件: `astrmai/learning/review/reflect_tracker.py:70-134`
- 关键改动: 在 lock 保护下用 atomically pop 替代 read-then-pop
- 最小改动: lines 70-134 重构为 ~15 行改动

### Requirement R11: `reflect_tracker.py` — get_unsent_requests 标记 ALL 为 sent

**User Story:** 作为系统维护者，`get_unsent_requests` 应只标记返回的条目为 sent，而非全部。

#### Acceptance Criteria
1. THE `get_unsent_requests` SHALL 仅标记返回给调用者的条目为 `sent=True`。
2. WHEN 条目未被返回（因为不在候选中），THE 条目 SHALL 保持 `sent=False`。

#### Notes / Constraints
- 涉及文件: `astrmai/learning/review/reflect_tracker.py:55-58`
- 修复: line 56 的 `for` 循环移到 line 55 的 requests 列表上
- 最小改动: 1 行修改

### Requirement R12: `database_service.py` — get_chat_state 无锁读取

**User Story:** 作为系统维护者，在非 WAL 模式下读写并发不会导致 SQLITE_BUSY 或脏读。

#### Acceptance Criteria
1. THE `get_chat_state` SHALL 在读取时启用 WAL 模式，或使用 `_db_lock`。
2. IF WAL 模式未默认启用，THEN THE 系统 SHALL 在 `get_chat_state` 前确保 WAL journal mode。

#### Notes / Constraints
- 涉及文件: `astrmai/infrastructure/persistence/database_service.py:189-209`
- 最小改动: +1 行 `PRAGMA journal_mode=WAL` 在 connect 之后
- 不可简单加 `_db_lock`（该方法为 sync，lock 为 async）

### Requirement R13: `context_compaction.py` — 压缩任务创建竞态

**User Story:** 作为系统维护者，同一 chat_id 的并发评估请求不应创建多个重复压缩任务。

#### Acceptance Criteria
1. THE `schedule_compaction_evaluation` SHALL 对 `_pending_tasks` 的检查和写入使用原子操作。
2. WHEN 任务已存在且未完成，THE 系统 SHALL 返回 `skipped_reason="evaluation_already_scheduled"`。
3. THE 修复 SHALL 消除 check-then-create 竞态窗口。

#### Notes / Constraints
- 涉及文件: `astrmai/conversation/attention/context_compaction.py:297-314`
- 当前已有 check (line 298)，但 check 和 write 之间非原子
- 最小改动: +1 行 lock 保护或使用 `asyncio.Lock`

### Requirement R14: `gate.py` — Session worker 阻塞

**User Story:** 作为系统维护者，System2 处理期间不应阻塞整个 session evaluation 循环。

#### Acceptance Criteria
1. THE `sys2_process` 调用 SHALL 不阻塞 session worker 循环。
2. WHEN sys2_process 执行期间，THE `is_evaluating` 标志 SHALL 在 sys2 开始前重置。
3. THE 修复 SHALL 使用 `asyncio.create_task()` 包装 sys2_process 以不阻塞。

#### Notes / Constraints
- 涉及文件: `astrmai/conversation/attention/gate.py:839-844`
- 改动: line 840 的 `await` 改为 `asyncio.create_task()`
- 注意维护任务引用防止僵尸 task

---

## Wave 3 — 错误处理/LLM 韧性修复 (R15–R19)

### Requirement R15: `memory_retrieval_service.py` — LLM 调用缺 lane_key

**User Story:** 作为系统维护者，记忆检索的 LLM 查询改写应遵守 lane 限流控制，避免绕过并发限制。

#### Acceptance Criteria
1. THE `_rewrite_queries` SHALL 向 `call_data_process_task` 传递 `lane_key` 参数。
2. THE `lane_key` SHALL 使用 `LaneKey(subsystem="bg", task_family="query_rewrite", scope_id=chat_id)` 模式。

#### Notes / Constraints
- 涉及文件: `astrmai/memory/services/memory_retrieval_service.py:383`
- 最小改动: +2 行 lane_key 参数
- 需从调用链获取 chat_id（可能需要添加参数）

### Requirement R16: `hybrid_retriever.py` — add_memory 返回 None

**User Story:** 作为系统维护者，当向量存储离线时，调用者应明确获知写入失败，而非误将 None 当作有效 doc_id。

#### Acceptance Criteria
1. WHEN vector 为 None，THE `add_memory` SHALL 抛出 `RuntimeError` 而非返回 `None`。
2. THE 调用者 (`memory_index_projector.py:55`) SHALL 正确处理异常。

#### Notes / Constraints
- 涉及文件: `astrmai/memory/retrieval/hybrid_retriever.py:27-31`
- 最小改动: `return None` → `raise RuntimeError("Vector store offline")`

### Requirement R17: `bootstrap.py` — 闭包捕获 pre-binding

**User Story:** 作为系统维护者，System2 bridge 不应在 callback 尚未绑定时被提前调用导致 RuntimeError。

#### Acceptance Criteria
1. THE `_build_system2_bridge` SHALL 在每次调用时检查 `runtime.system2_callback` 而非构造时捕获。
2. WHEN `system2_callback` 为 None，THE bridge SHALL 抛出明确的 RuntimeError。

#### Notes / Constraints
- 涉及文件: `astrmai/app/bootstrap.py:504-510`
- 当前代码已检查 `if runtime.system2_callback is None: raise RuntimeError`，但闭包在构造时已绑定 `runtime`
- 修复: 在 `_bridge` 内部每次调用时重新检查

### Requirement R18: `lifecycle.py` — track_task 无 RuntimeError guard

**User Story:** 作为运维人员，shutdown 期间不应因 `asyncio.create_task()` 抛出 RuntimeError 而崩溃。

#### Acceptance Criteria
1. THE `track_task` SHALL 使用 `safe_create_task` 或捕获 `RuntimeError`。
2. WHEN event loop 已关闭，THE 方法 SHALL 返回一个已取消的 mock Task 而非崩溃。

#### Notes / Constraints
- 涉及文件: `astrmai/app/lifecycle.py:22-25`
- 改动: `asyncio.create_task(coro)` → `safe_create_task(coro)` (已导入)

### Requirement R19: `plugin_facade.py` — 异常仅捕获 LLMCascade

**User Story:** 作为系统维护者，System2 入口应能优雅处理所有异常类型，而非让未捕获异常传播导致崩溃。

#### Acceptance Criteria
1. THE `_system2_entry` SHALL 在 `except LLMCascadeFailureException` 之后追加 `except Exception` 处理。
2. WHEN 非 LLMCascade 异常发生，THE 系统 SHALL 记录错误并发送 fallback 回复。
3. THE fallback SHALL 与 LLMCascade 路径一致。

#### Notes / Constraints
- 涉及文件: `astrmai/app/plugin_facade.py:505-508`
- 最小改动: +2 行 `except Exception as e: logger.error(...); send_fallback(...)`

---

## Wave 4 — 事件流/Hook 修复 (R20–R21)

### Requirement R20: `main.py` — heartflow_is_command 未实现

**User Story:** 作为系统维护者，当 AstrBot HeartCore 将消息标记为命令时，AstrMai 应识别并跳过处理，避免命令消息被误解为对话。

#### Acceptance Criteria
1. THE `on_global_message` handler SHALL 在入口处检查 `event.get_extra("heartflow_is_command")`。
2. WHEN `heartflow_is_command` 为 True，THE handler SHALL 立即返回（不 yield）。

#### Notes / Constraints
- 涉及文件: `main.py:134-137`
- 最小改动: +2 行检查在 `on_global_message` 开头

### Requirement R21: `main.py` — 缺 on_llm_response hook

**User Story:** 作为插件开发者，我需要一个 hook 在 LLM 响应完成后检查和修改响应内容（如注入状态栏、替换敏感词）。

#### Acceptance Criteria
1. THE `main.py` SHALL 注册 `@filter.on_llm_response()` handler。
2. THE handler SHALL 记录包含 `completion_text`、`provider_id`、`chat_id` 的响应摘要。
3. THE handler SHALL 捕获所有异常防止 hook 链中断。

#### Notes / Constraints
- 涉及文件: `main.py`
- 最小实现：~8 行 handler，记录响应元数据并 trace
- 参照 AstrBot SDK: `on_llm_response` 接收 `(self, event, response: LLMResponse)`

---

## Out of Scope (不在本 Spec 范围内)

- P0 修复 (Round-1 已完成 9/9)
- P2/P3 修复 (40 P2 + 32 P3，另行安排)
- 架构重构或模块拆分
- 性能优化（如异步池化、连接复用增强）
- 新增功能或业务逻辑变更
- 文档/注释改进
- 测试新增（仅保证现有测试无回归）

## High-Risk Confirmation List (高风险确认清单)

- **R11 (get_unsent_requests)**: 修改后可能影响现有调用者 (Plugin Pages 审核 API)，需确认 `plugin_facade.py` 中的 consumers
- **R14 (gate.py session worker)**: `create_task` fire-and-forget 可能引入孤儿任务，需确保 task 被追踪或最终被 cancel
- **R20 (heartflow_is_command)**: 检查太早可能跳过合法消息，需确认 AstrBot HeartCore 的标记语义
- **R10 (TOCTOU)**: 修改锁范围可能影响 LLM 调用耗时，需确保不会导致审核请求排队
- **R16 (add_memory None)**: 抛出 RuntimeError 可能需要在调用者处添加 try/except，检查所有调用路径

## Dependency Map (需求依赖关系)

```
Wave 1 (R1–R9)  ──独立──→  可并行修复
Wave 2 (R10–R14) ──独立──→  可并行修复
Wave 3 (R15–R19) ──独立──→  可并行修复
Wave 4 (R20–R21) ──独立──→  可并行修复
     ↓
  全量回归测试
```

四个 Wave 之间无交叉依赖，但每个 Wave 内部按文件维度有隐含依赖：
- R1 仅涉及 `persona_summarizer.py`
- R10+R11 均涉及 `reflect_tracker.py`，建议串行（先 R11 再 R10）
- R6 仅涉及 `event_bus.py`
- R17+R18+R19 涉及多个 app 文件，互不冲突

## Verification Strategy (验证策略)

| 验证层 | 命令/方式 | 覆盖需求 |
|--------|----------|---------|
| 单元测试回归 | `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py` | R1–R21 |
| LSP 诊断 | `lsp_diagnostics` on each changed file | R1–R21 |
| 导入检查 | `python -c "import astrmai; print('OK')"` | R1–R21 |
| Git diff 审查 | `git diff --stat` 确认仅改动目标文件 | R1–R21 |
| 手工验证 | 加载插件确认无 import/structure 错误 | R20–R21 |

### 验证通过标准

1. 全量测试 ≥ 现有 passed 数量 (无回归)
2. 全部修改文件 lsp_diagnostics 无 error 级别
3. `import astrmai` 成功无异常
4. 每个修改文件的 diff ≤ 30 行 (最小改动原则)
