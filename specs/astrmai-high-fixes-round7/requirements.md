# Requirements Document — AstrMai 第二轮审查高危修复

> Spec ID: `astrmai-high-round7-20260630` | Type: `hardening`
> 基于第二轮深度审查（6 维度）中 9 项 🟠 HIGH 级问题（5 项 CRITICAL 已在上轮修复）。

---

## Introduction

第二轮深度审查共发现 28 项缺陷。5 项 🔴 CRITICAL 已在 `astrmai-critical-fixes-round7` 中修复完毕。本 Spec 覆盖剩余 **9 项 🟠 HIGH** 级问题。

### 范围覆盖

| # | 来源 | 问题 | 影响 |
|---|------|------|------|
| R1 | 事件流 | `message_entry.py:54` 权限守卫未调 `stop_event()` | 权限绕过 |
| R2 | 状态机 | `dispatcher.py:308` 主动分发时置空 `runtime_coordinator` | 并发消息丢失 |
| R3 | 状态机 | `proactive_task.py:204` stop() 后 crash-restart 重新激活 | 调度器泄漏 |
| R4 | 数据流 | `user_profile_service.py` 缓存永不失效 + 崩溃丢变更 | 数据丢失 |
| R5 | 数据流 | `database_review.py` 双写 ORM+v2_store | 分裂脑 |
| R6 | 数据流 | `v2_store.py:1123,1177` purge 不清理 FTS | 幻影检索 |
| R7 | 导入 | `plugin_pages.py:105` logger 未导入 | NameError |
| R8 | 生命周期 | `event_bus.py` singleton workers 永不停 | 资源泄漏 |
| R9 | 生命周期 | `persistence_manager.py` dispose 永不调用 | 连接泄漏 |

### 明确排除

| 排除项 | 理由 |
|--------|------|
| 🔴 CRITICAL（5 项） | 已在上轮 Spec 修复 |
| 🟡 MEDIUM（9 项） | 另行 Spec |
| 安全类/框架配置 | 不在插件范围 |
| 新功能/架构重构 | 纯修复 |

---

## Glossary

| 术语 | 定义 |
|------|------|
| **FTS** | Full-Text Search，SQLite 虚拟表，需手动同步 |
| **分裂脑** | 双写两个存储系统时一侧成功一侧失败，数据不一致 |
| **stop_event()** | AstrBot API，阻断事件下游处理 |
| **runtime_coordinator** | 运行时协调器，控制消息处理的并发 |

---

## Requirements

### Wave 1：P0 — 消息可靠性（R1–R3）

---

### Requirement 1: 权限守卫补 `event.stop_event()`

**User Story:** 当 AstrMai 判定某消息不应处理（权限拒绝）时，我不希望其他 AstrBot 插件或默认 LLM 仍能处理该消息。

#### Acceptance Criteria

1. WHEN `check_message_scope_access` 返回 `should_stop=True`，THE 系统 SHALL 调用 `event.stop_event()` 后再 `return`。
2. THE 行为 SHALL 与文件中其他 6 个提前返回路径一致（均调用了 `event.stop_event()`）。

#### Notes / Constraints

- 涉及文件：`astrmai/presentation/events/message_entry.py:54`
- 改动量：+1 行

---

### Requirement 2: 主动分发不阻塞并发消息

**User Story:** 当 AstrMai 主动向用户发送消息时，我不希望同时到达的用户消息被静默丢弃。

#### Acceptance Criteria

1. THE `ProactiveDispatcher._dispatch_locked` 方法 SHALL NOT 修改共享的 `attention_gate.runtime_coordinator` 属性。
2. THE 系统 SHALL 使用 per-chat 标志替代全局属性置空，避免影响并发消息。
3. WHEN 主动分发与用户消息同时到达同一 chat，THE 两条消息 SHALL 均被处理而非丢弃其一。

#### Notes / Constraints

- 涉及文件：`astrmai/proactive/dispatcher.py:304-311`
- 当前：`setattr(self.attention_gate, "runtime_coordinator", None)` 全局副作用
- 替代方案：使用 `chat_id` 维度的标志位

---

### Requirement 3: stop() 后不再重新激活调度器

**User Story:** 当我通过 WebUI 停止主动调度器后，crash-restart 逻辑不应在 5 秒后重新激活它。

#### Acceptance Criteria

1. WHEN `stop()` 已将 `_is_running = False`，THE `_on_loop_done` 回调 SHALL NOT 调度重启。
2. THE 重启逻辑 SHALL 在延迟回调中再次检查 `_is_running`，而非仅在回调注册时检查。
3. THE 系统 SHALL 使用 `asyncio.get_running_loop()` 替代废弃的 `asyncio.get_event_loop()`。

#### Notes / Constraints

- 涉及文件：`astrmai/proactive/proactive_task.py:204-215`
- 改动：在 lambda 内二次检查 `_is_running` + 替换 `get_event_loop`

---

### Wave 2：P1 — 数据完整性（R4–R6）

---

### Requirement 4: UserProfile 缓存失效 + 即时持久化

**User Story:** 当 WebUI 修改了用户画像后，聊天中应能立即读取到新画像；bot 崩溃时不应丢失最近 15 秒的画像变更。

#### Acceptance Criteria

1. THE `UserProfileService` SHALL 提供 `invalidate_cache(user_id)` 方法，供 WebUI 修改路径调用。
2. THE `observe_user_activity` 和 `record_profile_learning_touch` SHALL 在修改后立即持久化，而非依赖 15 秒定时 flush。
3. WHEN 持久化失败时，THE 系统 SHALL 记录 `logger.warning` 并保留 `is_dirty=True` 以待下次重试。

#### Notes / Constraints

- 涉及文件：`astrmai/state/user_profile_service.py`
- 当前：依赖 `lifecycle.py` 中 15 秒周期的 `_db_sync_task`

---

### Requirement 5: 双写统一为单写路径

**User Story:** 当表情模式被保存时，我不希望出现 ORM 表有数据但 v2_store 没有（或相反）的分裂脑状态。

#### Acceptance Criteria

1. THE `save_pattern` 方法 SHALL 统一使用 v2_store 作为唯一写入路径。
2. THE 系统 SHALL 将 ORM `ExpressionPattern` 表标记为 deprecated，仅保留读取兼容。
3. IF 任一写入失败，THEN THE 系统 SHALL 回滚另一侧（或至少记录 error 并阻止后续读取到不一致数据）。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/persistence/database_review.py:70-95`
- 当前：`save_pattern` → SQLModel ORM + `_save_pattern_to_canonical_async` → v2_store

---

### Requirement 6: purge 操作同步清理 FTS

**User Story:** 当过期 jargon/expression 被清理后，FTS 全文搜索不应返回已删除的记录。

#### Acceptance Criteria

1. WHEN `purge_jargon_candidates` 删除 `canonical_memories` 行，THE 系统 SHALL 同步删除 `canonical_fts` 中对应 `memory_id`。
2. WHEN `purge_kind_candidates` 执行，THE 系统 SHALL 同样同步 FTS。
3. THE 删除操作 SHALL 在同一事务内完成（SQLite BEGIN/COMMIT）。

#### Notes / Constraints

- 涉及文件：`astrmai/memory/services/v2_store.py:1123,1177`
- 改动：在 DELETE 后追加 `DELETE FROM canonical_fts WHERE memory_id = ?`

---

### Wave 3：P2 — 基础设施（R7–R9）

---

### Requirement 7: plugin_pages.py 补 logger 导入

**User Story:** 当 WebUI 请求体 JSON 解析失败时，我不希望整个请求崩溃，而是希望看到日志记录。

#### Acceptance Criteria

1. THE `plugin_pages.py` SHALL 在文件顶部导入 `from astrbot.api import logger`。
2. THE `_body` 方法中的 `logger.warning(...)` SHALL 正常执行而不抛出 `NameError`。

#### Notes / Constraints

- 涉及文件：`astrmai/webui/plugin_pages.py:105`
- 改动：+1 行导入

---

### Requirement 8: EventBus 在 terminate 中停止

**User Story:** 当插件关闭或热重载时，我不希望 EventBus 的 4 个后台 worker 继续运行消耗资源。

#### Acceptance Criteria

1. THE `PluginLifecycleManager.terminate()` SHALL 调用 `runtime.event_bus.stop()`。
2. THE EventBus SHALL 在 `stop()` 中取消所有 `_background_tasks` 并等待完成。
3. IF `event_bus` 为 None，THEN THE 系统 SHALL 跳过（不崩溃）。

#### Notes / Constraints

- 涉及文件：`astrmai/app/lifecycle.py`（调用方），`astrmai/infrastructure/runtime/event_bus.py`（stop 方法已存在）
- EventBus 是 Singleton，需确保 stop 后 `_workers_started` 重置

---

### Requirement 9: PersistenceManager dispose 在 terminate 中调用

**User Story:** 当插件关闭时，SQLAlchemy 连接池应被释放，避免数据库文件锁残留。

#### Acceptance Criteria

1. THE `PluginLifecycleManager.terminate()` SHALL 调用 `runtime.persistence.dispose()`。
2. THE `dispose()` 调用 SHALL 在 background tasks 取消之后执行（避免任务仍在访问 DB）。
3. IF `persistence` 为 None，THEN THE 系统 SHALL 跳过。

#### Notes / Constraints

- 涉及文件：`astrmai/app/lifecycle.py`（调用方），`astrmai/infrastructure/persistence/persistence_manager.py`（dispose 方法已存在）

---

## Out of Scope

| 排除项 | 理由 |
|--------|------|
| 🟡 MEDIUM（9 项） | 另行 Spec |
| 🔴 CRITICAL（5 项） | 已修复 |
| ORM 外键/级联删除 | 需 schema 迁移，单独处理 |
| 完整 FTS 一致性修复 | 超出本 Spec，仅修 purge 路径 |

---

## High-Risk Confirmation List

| 风险 | 等级 | 缓解 |
|------|:--:|------|
| R2 改成 per-chat 标志需线程安全 | 🟡 | 使用 `dict[str, bool]` + `asyncio.Lock` 替代全局 setattr |
| R4 即时持久化增加 DB 写入频率 | 🟡 | 批量写入仍保留，仅 profile 变更即时写 |
| R5 单写迁移需兼容旧 ORM 读取路径 | 🟡 | deprecated 标记，不删除读取代码 |

---

## Dependency Map

```
R7 (import) → 无依赖，先修
R1 (stop_event) ─┐
R2 (coordinator) ─┤
R3 (restart)    ──┤ 无互相依赖，可并行
R6 (FTS purge)  ──┤
R8 (eventbus)   ──┤
R9 (dispose)    ──┘
R4 (profile)    → 独立
R5 (dual-write) → 独立，需 code review
```

---

## Verification Strategy

| 需求 | 验证方式 | 通过标准 |
|------|---------|---------|
| R1 | `grep "stop_event" message_entry.py` | 7 处 stop_event 调用 |
| R2 | `grep "runtime_coordinator" dispatcher.py` | 无 setattr 到 None |
| R3 | 单元测试 | stop() 后 5s 内不重新创建任务 |
| R4 | `grep "invalidate_cache" user_profile_service.py` | 方法存在 |
| R5 | `grep "save_pattern" database_review.py` | 无双写路径 |
| R6 | `grep "canonical_fts" v2_store.py` | purge 函数中有 DELETE FTS |
| R7 | `grep "from astrbot.api import logger" plugin_pages.py` | 存在 |
| R8 | `grep "event_bus.stop" lifecycle.py` | 存在 |
| R9 | `grep "persistence.dispose" lifecycle.py` | 存在 |
| 全量 | `pytest tests/ -q` | ≥ 836 passed |
