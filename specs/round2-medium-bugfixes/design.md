# Design Document — Round 2 MEDIUM Bugfixes

## Overview
20 个修复，~35 行变更，16 个文件。

| # | 文件 | 修复 |
|---|------|------|
| M1 | `proactive_task.py:761` | `if not should_skip_maintenance: ...` 分支化 |
| M2 | `wakeup_service.py:198` | except块 `self._callbacks.pop(intent.intent_id, None)` |
| M3 | `conversation_continuity.py:125` | `now = time.time()` |
| M4 | `judge.py:46,513` | 删除 FETCH_KNOWLEDGE/RETHINK_GOAL 相关代码 |
| M5 | `planner_side_inputs.py:394` | seen_names 预填充内置工具名 |
| M6 | `context_engine.py:558` | `isinstance(memory.emotion_tags, list)` → 直接用 |
| M7 | `reflector.py:183` | `pattern.id` 替代 `id(pattern)` |
| M8 | `review_service.py:114` | `"revision_needed"` → `review_status="pending"` |
| M9 | `expression_pattern_service.py:92` | 删除 `_source_requires_review` 覆盖逻辑 |
| M10 | `memory_index_projector.py:84` | build to temp → swap（或加注释声明已知风险） |
| M11 | `memory_retrieval_service.py:158` | `continue` → `break` |
| M12 | `reflector.py:141` | except块 `self._pending_reflections = self._pending_reflections[len(batch):]` |
| M13 | `react_retriever.py:135` | `await asyncio.wait_for(self.gateway.call_data_process_task(...), timeout=15.0)` |
| M14 | `lane_manager.py:89` | `if not self._lane_locks[oldest].locked(): self._lane_locks.pop(oldest, None)` |
| M15 | `center.py:105` | 添加 judge/mood/followup 等7条 subsystem 映射 |
| M16 | `admin_ui_service.py:88` | where 非空时仍用 `table` 参数查对应表 |
| M17 | `context_engine.py:550` | 删除 `_resolve_visual_memory_refs` 死方法 |
| M18 | `image_pipeline.py:35` | `try: handle.write(...) finally: os.close(fd)` |
| M19 | `gate.py:86` | 在 proactive 完成时 `_proactive_dispatching.pop(chat_id, None)` |
| M20 | `lane_storage.py:92` | 旋转时保留 `max_raw_turns*2` 条而非压缩为8行 |

## Verification
| 需求 | 验证 |
|------|------|
| M1–M20 | `lsp_diagnostics` + 源码检查 + `pytest tests/` |
