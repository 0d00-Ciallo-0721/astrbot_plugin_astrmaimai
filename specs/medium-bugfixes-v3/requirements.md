# Requirements Document

## Introduction

本 Spec 为「AstrMai」中 7 个 **MEDIUM 严重等级**运行时缺陷制定修复需求。范围覆盖：

- R15: `safe_create_task` 用 `ensure_future` 而非 `create_task`，done callback 可能抛 `AttributeError`
- R16: 向量索引投影失败静默，SQL-vs-向量漂移
- R17: 会话锁 LRU 驱逐竞争，活跃会话可能被踢出
- R18: HandoffRegistry 缓存永不过期，新注册 SubAgent 不被发现
- R19: shutdown flush `except Exception: pass`，最终状态丢失
- R20: `user_profile_service` TOCTOU 竞态
- R21: `memory_scoring.py` 中 `math.log(0)` → `-inf`，所有记录被标记 stale

明确不在本 Spec 范围：CRITICAL/HIGH 级别缺陷、功能新增。

## Glossary

- **`ensure_future` vs `create_task`**：`ensure_future` 可返回裸 `Future`（无 `.get_name()`），`create_task` 始终返回 `Task`。
- **LRU Eviction**：`v2_store.py` 中 `_session_locks` pop 最旧条目。若有协程持有被驱逐的锁，新访问创建新锁，破坏互斥。
- **HandoffRegistry**：`workmode/tools/handoff_registry.py`，发现 WebUI 注册的动态 SubAgent。`_loaded=True` 后不再扫描。
- **TOCTOU**：Time-of-check-to-time-of-use 竞态，指 `get_user_profile` 和 `_get_user_lock` 之间数据可能被其他协程修改。

## Requirements

### Requirement 15: `safe_create_task` 使用 `create_task`

**User Story:** 作为开发者，我希望 `safe_create_task` 使用 `asyncio.create_task` 确保返回 `Task` 对象，所以 `t.get_name()` 不会因返回 `Future` 而抛 `AttributeError`。

#### Acceptance Criteria
1. THE `safe_create_task` SHALL 使用 `asyncio.create_task(coro)` 替代 `asyncio.ensure_future(coro)`。
2. THE done callback 中 `t.get_name()` 调用 SHALL 被 `hasattr(t, 'get_name')` 守卫或移除。

**涉及文件**: `astrmai/shared/helpers/plugin_helpers.py:36`

---

### Requirement 16: 向量索引投影失败不静默

**User Story:** 作为开发者，我希望 `MemoryWriteService.write()` 在向量索引投影失败时通知调用方，所以 SQL-vs-向量漂移可以被检测。

#### Acceptance Criteria
1. THE `write()` 方法 SHALL 在 `index_projector.project()` 失败时记录 `logger.warning`（含异常详情和 `memory_id`）。
2. THE `project()` 失败 SHALL NOT 阻止 `memory_id` 的返回（保持 best-effort 语义）。

**涉及文件**: `astrmai/memory/services/memory_write_service.py:100-103`

---

### Requirement 17: 会话锁 LRU 驱逐竞争修复

**User Story:** 作为开发者，我希望活跃会话的锁不被 LRU 驱逐踢出，所以并发访问不会绕过互斥保护。

#### Acceptance Criteria
1. THE `_get_session_lock` SHALL 在驱逐时跳过已被其他协程持有的锁（通过 `lock.locked()` 检查）。
2. THE `_session_locks` 上限 SHALL 从 200 提升至 500 或移除上限，依赖 Python GC 管理。

**涉及文件**: `astrmai/memory/services/v2_store.py:75-77`

---

### Requirement 18: HandoffRegistry 缓存过期

**User Story:** 作为用户，我希望通过 WebUI 新注册的 SubAgent 能被 Sys3Router 发现而无需重启插件。

#### Acceptance Criteria
1. THE `discover()` 方法 SHALL 在每次 `Sys3Router.get_all_agents()` 调用时重新扫描（移除 `_loaded` 一次性缓存逻辑），或使用 TTL 缓存（如 60 秒刷新一次）。
2. THE 重新发现 SHALL 保持幂等：已存在的 Agent 不被重复添加。

**涉及文件**: `astrmai/workmode/tools/handoff_registry.py:15-18`

---

### Requirement 19: shutdown flush 记录异常

**User Story:** 作为开发者，我希望 shutdown 时 `flush_message_counters` 的失败被记录，所以能发现关机时的数据丢失。

#### Acceptance Criteria
1. THE `except Exception: pass` SHALL 替换为 `except Exception: logger.warning(..., exc_info=True)`。

**涉及文件**: `astrmai/app/lifecycle.py:173-174`

---

### Requirement 20: user_profile TOCTOU 竞态修复

**User Story:** 作为开发者，我希望 `observe_user_activity` 在读-改-写之间保持数据一致性。

#### Acceptance Criteria
1. THE `observe_user_activity` SHALL 在 `get_user_profile` 获取的锁内完成所有修改，而非释放后重新获取。
2. OR THE 方法 SHALL 在第二次获取锁后重新读取 profile 以检测中间修改。

**涉及文件**: `astrmai/state/user_profile_service.py:254-274`

---

### Requirement 21: `compute_hot_score` log(0) 防御

**User Story:** 作为开发者，我希望 `compute_hot_score` 在 `access_count` 异常为负数时不会产生 `-inf` 导致所有记录被标记 stale。

#### Acceptance Criteria
1. THE `compute_hot_score` SHALL 使用 `max(0, access_count)` 确保 `log()` 参数 ≥ 1。

**涉及文件**: `astrmai/memory/services/memory_scoring.py:113`

---

## Out of Scope / Dependency Map / Verification

7 条需求修改不同文件/方法，可并行实现。验证方式：LSP 诊断 + 源代码断言 + pytest。

| 验证层 | 覆盖需求 |
|--------|---------|
| LSP 诊断 | R15–R21 |
| 源代码检查 | `create_task` 存在、`_loaded` 逻辑变更、`logger.warning` 存在、`max(0, ...)` 守卫 |
| pytest | 全量回归 |
