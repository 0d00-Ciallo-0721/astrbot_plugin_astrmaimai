# Implementation Plan — Round 2 HIGH Bugfixes

## Overview

| Phase | 主题 | 任务 | 文件 |
|-------|------|:--:|------|
| Phase 1 | 单行修复 | 1–6 | 5 files |
| Phase 2 | 结构修复 | 7–10 | 4 files |
| Phase 3 | 验证 | 11 | all |

---

## Tasks

### Phase 1: 单行修复

- [ ] **1. R1+R4+R5+R9: 四合一单行修复**
  - **Files**: `config.py:122`, `executor.py:211`, `think_level_policy.py:202`, `gateway_policy.py:51`
  - **Steps**: 按设计文档修改4个单行
  - **Check**: `lsp_diagnostics` ×4
  - _Requirements: R1,R4,R5,R9_

- [ ] **2. R3: bootstrap attention_gate 警告**
  - **Files**: `bootstrap.py:357-358`
  - **Steps**: 添加 `logger.warning`
  - **Check**: `lsp_diagnostics`
  - _Requirements: R3_

- [ ] **3. R10: BM25 score_range 修正**
  - **Files**: `bm25.py:108-112`
  - **Steps**: `else max(abs(max_score), 1.0)`
  - **Check**: `lsp_diagnostics`
  - _Requirements: R10_

- [ ] **4. R11: VisualCortex queue maxsize**
  - **Files**: `visual_cortex.py:21`
  - **Steps**: `maxsize=100`
  - **Check**: `lsp_diagnostics`
  - _Requirements: R11_

- [ ] **5. R13: startup_hooks raise**
  - **Files**: `startup_hooks.py:13-16`
  - **Steps**: 添加 `raise`
  - **Check**: `lsp_diagnostics`
  - _Requirements: R13_

- [ ] **6. R8: gateway_call TimeoutError 不重新抛出**
  - **Files**: `gateway_call.py:183-185`
  - **Steps**: 替换为 `is_fatal=False; continue`
  - **Check**: `lsp_diagnostics`
  - _Requirements: R8_

---

### Phase 2: 结构修复

- [ ] **7. R2: dream 节流移入信号量**
  - **Files**: `dream_scheduler.py:59,79`
  - **Steps**: 将 `should_run` 检查移入 `async with self._bg_semaphore:` 块内
  - **Check**: `lsp_diagnostics`
  - _Requirements: R2_

- [ ] **8. R6: ExpressionPattern UNIQUE 约束**
  - **Files**: `orm_models.py:22` + `database_review.py:88`
  - **Steps**: 添加约束 → 现有重复行清理 → `IntegrityError` 捕获
  - **Check**: `lsp_diagnostics` ×2
  - **Risk**: 🟡 先清理重复行
  - _Requirements: R6_

- [ ] **9. R7: 异步 init 更新 user_version**
  - **Files**: `persistence_schema.py:251`
  - **Steps**: `_init_db` 末尾调用 `await self._run_migrations()`
  - **Check**: `lsp_diagnostics`
  - _Requirements: R7_

- [ ] **10. R12: PROACTIVE_BLOCKED 延迟队列**
  - **Files**: `gate.py:86,649`
  - **Steps**: 添加 `_deferred_messages: dict[str,list]`，阻塞时入队(max 5)，完成时重放
  - **Check**: `lsp_diagnostics`
  - _Requirements: R12_

---

### Phase 3: 验证

- [ ] **11. 全量回归**
  - **Steps**: `lsp_diagnostics` 全部10文件 + `pytest tests/ -q`
  - **AC**: 无新增error；pass数不减少
  - _Requirements: R1–R13_

---

## Summary

| # | 文件 | 改动 |
|---|------|:--:|
| 1 | `config.py` | +1/-1 |
| 2 | `executor.py` | +1/-1 |
| 3 | `think_level_policy.py` | +1/-1 |
| 4 | `gateway_policy.py` | +1/-1 |
| 5 | `bootstrap.py` | +2 |
| 6 | `bm25.py` | +1/-1 |
| 7 | `visual_cortex.py` | +1/-1 |
| 8 | `startup_hooks.py` | +1 |
| 9 | `gateway_call.py` | +2/-2 |
| 10 | `dream_scheduler.py` | +3/-2 |
| 11 | `orm_models.py` | +3 |
| 12 | `database_review.py` | +4 |
| 13 | `persistence_schema.py` | +1 |
| 14 | `gate.py` | +8 |
| **Total** | **14 files** | **~30 lines** |
