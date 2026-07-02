# Implementation Plan — AstrMai 第二轮审查高危修复

> 本任务列表派生自同目录 `requirements.md`（9 条需求）与 `design.md`（9 个模块设计）。
> **执行原则**：Wave 1/2/3 内任务可并行，Wave 间无强制串行依赖。

---

## Overview

| Phase | Wave | 主题 | 任务 | 改动 |
|-------|------|------|:--:|------|
| Phase 1 | Wave 1 | 消息可靠性 | T1–T3 | 3 文件 |
| Phase 2 | Wave 2 | 数据完整性 | T4–T6 | 3 文件 |
| Phase 3 | Wave 3 | 基础设施 | T7–T9 | 3 文件 |
| Phase 4 | — | 回归验证 | T10 | 全量测试 |

---

## Tasks

### Phase 1: Wave 1 — 消息可靠性（T1–T3）可并行

- [ ] **T1. 权限守卫补 stop_event()**
  - **Goal**: `message_entry.py:54` 在 `return` 前加 `event.stop_event()`
  - **Files**: `astrmai/presentation/events/message_entry.py` (写)
  - **Steps**:
    1. 读 line 52-58
    2. 在 `return` 前插入 `event.stop_event()`
  - **Acceptance Criteria**: `grep "stop_event" message_entry.py` 返回 7 处（原 6 + 新增 1）
  - **Forbidden**: 不修改其他 stop_event 调用
  - **Check Commands**: `grep -c "stop_event" astrmai/presentation/events/message_entry.py`
  - **Risk Notes**: 🟢 +1 行
  - _Requirements: R1_

- [ ] **T2. 主动分发改用 per-chat 标志**
  - **Goal**: 移除 `setattr(runtime_coordinator, None)` 全局副作用
  - **Files**: `astrmai/proactive/dispatcher.py`, `astrmai/conversation/attention/gate.py` (写)
  - **Steps**:
    1. gate.py `__init__` 加 `self._proactive_dispatching: dict[str, bool] = {}`
    2. gate.py:643 改为检查 `self._proactive_dispatching.get(chat_id, False)`
    3. dispatcher.py:304-311 替换为 `self.attention_gate._proactive_dispatching[chat_id] = True` + finally pop
  - **Acceptance Criteria**:
    - `grep "runtime_coordinator.*None" dispatcher.py` 返回 0
    - `grep "_proactive_dispatching" gate.py` 有结果
  - **Forbidden**: 不改变 `inject_external_event` 的业务逻辑
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/proactive/dispatcher.py',encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟡 涉及两文件联动
  - _Requirements: R2_

- [ ] **T3. stop() 后不重新激活调度器**
  - **Goal**: lambda 内二次检查 `_is_running` + 替换 `get_event_loop`
  - **Files**: `astrmai/proactive/proactive_task.py` (写)
  - **Steps**:
    1. 提取 `_restart_if_still_running()` 方法（二次检查 `_is_running`）
    2. `_on_loop_done` 中 lambda 改为调用 `_restart_if_still_running`
    3. `get_event_loop()` → `get_running_loop()`
  - **Acceptance Criteria**:
    - `grep "get_event_loop" proactive_task.py` 返回 0
    - `grep "_restart_if_still_running" proactive_task.py` 存在
  - **Forbidden**: 不修改 `start()`/`stop()` 核心逻辑
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/proactive/proactive_task.py',encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟡 调度器生命周期变更
  - _Requirements: R3_

---

### Phase 2: Wave 2 — 数据完整性（T4–T6）可并行

- [ ] **T4. UserProfile 缓存失效 + 即时持久化**
  - **Goal**: 加 `invalidate_cache()` + `_flush_profile()` 即时写入
  - **Files**: `astrmai/state/user_profile_service.py` (写)
  - **Steps**:
    1. 加 `invalidate_cache(user_id=None)` 方法
    2. 加 `_flush_profile(user_id, profile)` 方法
    3. `observe_user_activity` 末尾调用 `await self._flush_profile(user_id, profile)`
  - **Acceptance Criteria**:
    - `grep "invalidate_cache\|_flush_profile" user_profile_service.py` 各 1 处
    - `is_dirty` 在成功持久化后为 False
  - **Forbidden**: 不移除 `flush_message_counters` 定时任务（保留作为兜底）
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/state/user_profile_service.py',encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟡 增加 DB 写入频率
  - _Requirements: R4_

- [ ] **T5. 双写统一为 v2_store 主路径**
  - **Goal**: `save_pattern` 以 v2_store 为主写入，ORM 降级为 deprecated 读兼容
  - **Files**: `astrmai/infrastructure/persistence/database_review.py` (写)
  - **Steps**:
    1. 调换写入顺序：v2_store 先 → ORM 后
    2. ORM 写入加 try/except + `# deprecated: ORM write for read-compat only`
  - **Acceptance Criteria**:
    - `save_pattern` 中 v2_store 写入在 ORM 之前
    - ORM 写入有 try/except 保护
  - **Forbidden**: 不移除 ORM 读取路径
  - **Check Commands**: `grep "deprecated.*ORM\|v2_store" astrmai/infrastructure/persistence/database_review.py`
  - **Risk Notes**: 🟡 写入顺序变更
  - _Requirements: R5_

- [ ] **T6. purge 同步清理 FTS**
  - **Goal**: `purge_jargon_candidates` 和 `purge_kind_candidates` 后追加 FTS DELETE
  - **Files**: `astrmai/memory/services/v2_store.py` (写)
  - **Steps**:
    1. `purge_jargon_candidates` (line ~1123): 在 DELETE 后加 `DELETE FROM canonical_fts WHERE memory_id IN (...)`
    2. `purge_kind_candidates` (line ~1177): 同上
  - **Acceptance Criteria**:
    - `grep "canonical_fts" v2_store.py` 在 purge 函数中有匹配
  - **Forbidden**: 不修改其他 FTS 同步逻辑
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/memory/services/v2_store.py',encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟢 纯增量
  - _Requirements: R6_

---

### Phase 3: Wave 3 — 基础设施（T7–T9）可并行

- [ ] **T7. plugin_pages.py 补 logger 导入**
  - **Goal**: 文件顶部加 `from astrbot.api import logger`
  - **Files**: `astrmai/webui/plugin_pages.py` (写)
  - **Steps**: 在现有导入后追加一行
  - **Acceptance Criteria**: `grep "from astrbot.api import logger" plugin_pages.py` 有结果
  - **Forbidden**: 不修改其他导入
  - **Check Commands**: `python -c "import ast; ast.parse(open('astrmai/webui/plugin_pages.py',encoding='utf-8').read()); print('ok')"`
  - **Risk Notes**: 🟢 +1 行
  - _Requirements: R7_

- [ ] **T8. terminate 中停止 EventBus**
  - **Goal**: `_terminate_impl` 末尾调用 `runtime.event_bus.stop()`
  - **Files**: `astrmai/app/lifecycle.py` (写)
  - **Steps**: 在 background tasks 取消后、dispose 之前插入
  - **Acceptance Criteria**: `grep "event_bus.stop" lifecycle.py` 有结果
  - **Forbidden**: 不修改 `event_bus.stop()` 实现
  - **Check Commands**: `grep "event_bus.stop" astrmai/app/lifecycle.py`
  - **Risk Notes**: 🟢 +3 行
  - _Requirements: R8_

- [ ] **T9. terminate 中 dispose PersistenceManager**
  - **Goal**: `_terminate_impl` 末尾调用 `runtime.persistence.dispose()`
  - **Files**: `astrmai/app/lifecycle.py` (写)
  - **Steps**: 在 event_bus.stop 之后插入
  - **Acceptance Criteria**: `grep "persistence.dispose" lifecycle.py` 有结果
  - **Forbidden**: 不修改 `dispose()` 实现
  - **Check Commands**: `grep "persistence.dispose" astrmai/app/lifecycle.py`
  - **Risk Notes**: 🟢 +3 行
  - _Requirements: R9_

---

### Phase 4: 回归验证（T10）

- [ ] **T10. 全量回归**
  - **Goal**: 确认无新增回归
  - **Steps**:
    1. AST parse 全部变更文件
    2. `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Acceptance Criteria**: ≥ 836 passed, 0 新增 SyntaxError
  - **Check Commands**: `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py 2>&1 | tail -3`

---

## Dependency Chain

```
T1 ─┬─→ (可并行)
T2 ─┤
T3 ─┘
T4 ─┬─→ (可并行)
T5 ─┤
T6 ─┘
T7 ─┬─→ (可并行)
T8 ─┤
T9 ─┘
      ↓
     T10 (回归)
```

---

## Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| T1 | `message_entry.py` | +1 | +1 |
| T2 | `dispatcher.py` + `gate.py` | 替换 setattr → dict | +8 / -6 |
| T3 | `proactive_task.py` | 替换 restart 逻辑 | +12 / -8 |
| T4 | `user_profile_service.py` | +cache invalidation + flush | +20 |
| T5 | `database_review.py` | 调换写入顺序 | +5 / -3 |
| T6 | `v2_store.py` | +2 FTS DELETE | +4 |
| T7 | `plugin_pages.py` | +1 import | +1 |
| T8 | `lifecycle.py` | +event_bus.stop | +3 |
| T9 | `lifecycle.py` | +persistence.dispose | +3 |
| **Total** | **9 文件** | | **~+57 / -17** |

---

## 执行检查清单

- [ ] T1–T9 全部完成
- [ ] 全部变更文件 AST parse 通过
- [ ] `grep "stop_event" message_entry.py` = 7
- [ ] `grep "get_event_loop" proactive_task.py` = 0
- [ ] `grep "from astrbot.api import logger" plugin_pages.py` 存在
- [ ] `grep "event_bus.stop\|persistence.dispose" lifecycle.py` 各 1 处
- [ ] T10 passed ≥ 836
