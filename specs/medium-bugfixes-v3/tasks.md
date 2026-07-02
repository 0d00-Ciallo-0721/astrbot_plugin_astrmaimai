# Implementation Plan

## Overview

| Phase | 主题 | 任务 | 改动文件 |
|-------|------|------|---------|
| Phase 1 | 防御性修复 | Task 1–4 | `plugin_helpers.py`, `memory_write_service.py`, `lifecycle.py`, `memory_scoring.py` |
| Phase 2 | 竞态与缓存 | Task 5–7 | `v2_store.py`, `handoff_registry.py`, `user_profile_service.py` |
| Phase 3 | 验证 | Task 8 | 全部 |

---

## Tasks

### Phase 1: 防御性修复

- [ ] **1. R15: `safe_create_task` → `create_task` + 守卫 `get_name`**
  - **Goal**: 替换 `ensure_future` 为 `create_task`，保护 `get_name` 调用
  - **Files**: ✏️ `astrmai/shared/helpers/plugin_helpers.py` (L36-43)
  - **Steps**: L36 `ensure_future` → `create_task`；L39 `t.get_name()` → `t.get_name() if hasattr(t, 'get_name') else name`
  - **AC**: `create_task` 存在；`hasattr(t, 'get_name')` 守卫存在
  - **Check**: `lsp_diagnostics("astrmai/shared/helpers/plugin_helpers.py")`
  - _Requirements: R15_

- [ ] **2. R16: 向量投影失败 warning**
  - **Goal**: `project()` 失败时记录日志
  - **Files**: ✏️ `astrmai/memory/services/memory_write_service.py` (L102-103)
  - **Steps**: 将 `await self.index_projector.project(...)` 包裹在 try/except 中，except 内调用 `logger.warning`
  - **AC**: try/except 包裹 project 调用；`logger.warning` 含 memory_id 和 exc
  - **Check**: `lsp_diagnostics("astrmai/memory/services/memory_write_service.py")`
  - _Requirements: R16_

- [ ] **3. R19: shutdown flush 日志**
  - **Goal**: `except Exception: pass` → `logger.warning`
  - **Files**: ✏️ `astrmai/app/lifecycle.py` (L173-174)
  - **Steps**: 替换为 `logger.warning("[AstrMai] shutdown flush failed", exc_info=True)`
  - **AC**: `logger.warning` 存在
  - **Check**: `lsp_diagnostics("astrmai/app/lifecycle.py")`
  - _Requirements: R19_

- [ ] **4. R21: `compute_hot_score` log(0) 防御**
  - **Goal**: 使用 `max(0.0, ...)` 确保 log 参数 ≥ 1
  - **Files**: ✏️ `astrmai/memory/services/memory_scoring.py` (L113)
  - **Steps**: `float(candidate.access_count or 0)` → `max(0.0, float(candidate.access_count or 0))`
  - **AC**: `max(0.0` 存在
  - **Check**: `lsp_diagnostics("astrmai/memory/services/memory_scoring.py")`
  - _Requirements: R21_

---

### Phase 2: 竞态与缓存

- [ ] **5. R17: 会话锁 LRU 驱逐跳过活跃锁**
  - **Goal**: pop 前检查 `lock.locked()`，跳过被持有的锁
  - **Files**: ✏️ `astrmai/memory/services/v2_store.py` (L75-77)
  - **Steps**: `if not self._session_locks[oldest].locked(): self._session_locks.pop(oldest, None)`
  - **AC**: `.locked()` 检查存在
  - **Check**: `lsp_diagnostics("astrmai/memory/services/v2_store.py")`
  - _Requirements: R17_

- [ ] **6. R18: HandoffRegistry 每次重新扫描**
  - **Goal**: 移除 `_loaded` 一次性缓存，每次 discover 都扫描
  - **Files**: ✏️ `astrmai/workmode/tools/handoff_registry.py` (L15-18)
  - **Steps**: 移除 `if self._loaded: return` 逻辑；扫描前记录已存在名称去重
  - **AC**: `_loaded` 检查不再存在（或仅用于日志）
  - **Check**: `lsp_diagnostics("astrmai/workmode/tools/handoff_registry.py")`
  - _Requirements: R18_

- [ ] **7. R20: user_profile TOCTOU 锁内重读**
  - **Goal**: 在第二次获取锁后重新读取 profile
  - **Files**: ✏️ `astrmai/state/user_profile_service.py` (L254-255)
  - **Steps**: `async with self._get_user_lock(user_id):` 后调用 `await self._get_profile_inner(user_id)` 重读
  - **AC**: 锁内重读 profile 的代码存在
  - **Check**: `lsp_diagnostics("astrmai/state/user_profile_service.py")`
  - _Requirements: R20_

---

### Phase 3: 验证

- [ ] **8. LSP + pytest 回归**
  - **Goal**: 所有变更文件 LSP clean + pytest
  - **Steps**: 1) `lsp_diagnostics` 全部 7 个文件; 2) `pytest tests/ -q`
  - **Check**: `lsp_diagnostics(...)`, `pytest tests/`
  - _Requirements: R15–R21_

---

## Summary

| # | 文件 | 改动 |
|---|------|:--:|
| 1 | `plugin_helpers.py` | +2/-1 |
| 2 | `memory_write_service.py` | +4/-1 |
| 3 | `lifecycle.py` | +1/-1 |
| 4 | `memory_scoring.py` | +1/-1 |
| 5 | `v2_store.py` | +2/-1 |
| 6 | `handoff_registry.py` | +8/-4 |
| 7 | `user_profile_service.py` | +1 |
| **Total** | **7 Files** | **~20 lines** |
