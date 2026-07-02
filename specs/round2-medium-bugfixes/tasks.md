# Implementation Plan — Round 2 MEDIUM Bugfixes

## Overview
20 任务，2 Phase，全部可并行执行。

| Phase | 主题 | 任务 | 文件 |
|-------|------|:--:|------|
| Phase 1 | 单行/简单修复 | 1–15 | 13 files |
| Phase 2 | 结构修复 | 16–20 | 5 files |

---

### Phase 1: 单行/简单修复

- [ ] **1. M1+M3+M5+M11+M19: 五合一**
  - **Files**: `proactive_task.py:761`, `conversation_continuity.py:125`, `planner_side_inputs.py:394`, `memory_retrieval_service.py:158`, `gate.py:86`
  - **Steps**: M1分支化continue; M3改为time.time(); M5预填充seen_names; M11 break; M19 proactive完成时pop
  - _Requirements: M1,M3,M5,M11,M19_

- [ ] **2. M4: 删除judge死代码**
  - **Files**: `judge.py:46,513`
  - **Steps**: 从prompt移除FETCH_KNOWLEDGE/RETHINK_GOAL; 删除降级逻辑
  - _Requirements: M4_

- [ ] **3. M6: emotion_tags类型守卫**
  - **Files**: `context_engine.py:558`
  - **Steps**: `isinstance(memory.emotion_tags, list)` → 直接用
  - _Requirements: M6_

- [ ] **4. M7: id()→pattern.id**
  - **Files**: `reflector.py:183`
  - **Steps**: `pattern.id` 替代 `id(pattern)`
  - _Requirements: M7_

- [ ] **5. M8: 审核状态修正**
  - **Files**: `review_service.py:114`
  - **Steps**: revision_needed→pending (而非approved)
  - _Requirements: M8_

- [ ] **6. M9: 移除approved覆盖**
  - **Files**: `expression_pattern_service.py:92`
  - **Steps**: 删除`_source_requires_review`的覆盖逻辑
  - _Requirements: M9_

- [ ] **7. M12: 失败批次移除**
  - **Files**: `reflector.py:141`
  - **Steps**: except块移除batch
  - _Requirements: M12_

- [ ] **8. M13: ReAct超时**
  - **Files**: `react_retriever.py:135`
  - **Steps**: `asyncio.wait_for(..., timeout=15.0)`
  - _Requirements: M13_

- [ ] **9. M14: 活跃锁跳过**
  - **Files**: `lane_manager.py:89`
  - **Steps**: `.locked()`检查
  - _Requirements: M14_

- [ ] **10. M15: WorkloadFamily映射**
  - **Files**: `center.py:105`
  - **Steps**: 添加judge/mood等7条映射
  - _Requirements: M15_

- [ ] **11. M16: _safe_count修复**
  - **Files**: `admin_ui_service.py:85`
  - **Steps**: where非空时仍用table参数
  - _Requirements: M16_

- [ ] **12. M17: 删除死代码**
  - **Files**: `context_engine.py:550`
  - **Steps**: 删除`_resolve_visual_memory_refs`方法
  - _Requirements: M17_

- [ ] **13. M18: tempfile泄漏**
  - **Files**: `image_pipeline.py:35`
  - **Steps**: try/finally os.close(fd)
  - _Requirements: M18_

- [ ] **14. M2: callback泄漏**
  - **Files**: `wakeup_service.py:198`
  - **Steps**: dispatch异常时pop callback
  - _Requirements: M2_

- [ ] **15. M10: 索引原子重建**
  - **Files**: `memory_index_projector.py:84`
  - **Steps**: 注释标注已知风险；简单方案：加log说明不可中断
  - _Requirements: M10_

---

### Phase 2: 结构修复

- [ ] **16. M20: 旋转保留上下文**
  - **Files**: `lane_storage.py:92`
  - **Steps**: summary_mode改为保留更多轮次或增加摘要长度
  - _Requirements: M20_

- [ ] **17–20. 验证**
  - **Steps**: `lsp_diagnostics`全部16文件 + `pytest tests/ -q`
  - _Requirements: M1–M20_

---

## Summary
| 文件数 | 行数估计 |
|:--:|:--:|
| 16 files | ~35 lines |
