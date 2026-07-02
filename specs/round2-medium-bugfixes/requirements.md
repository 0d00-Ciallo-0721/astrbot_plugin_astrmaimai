# Requirements Document — Round 2 MEDIUM Bugfixes

## Introduction
本 Spec 覆盖第二轮审计中 20 个 MEDIUM 严重等级缺陷。全部为单文件局部修复，可并行执行。

## Requirements (单一 Wave，20 项可并行)

| # | 需求 | 文件 | 修复要点 |
|---|------|------|---------|
| M1 | continue→分支 | `proactive_task.py:761` | `continue` → 仅跳过维护块 |
| M2 | callback泄漏 | `wakeup_service.py` | dispatch异常时清理callback |
| M3 | 时钟统一 | `conversation_continuity.py:125` | `monotonic()` → `time.time()` |
| M4 | 死代码清理 | `judge.py:46,513` | 移除 FETCH_KNOWLEDGE/RETHINK_GOAL |
| M5 | 内置工具dedup | `planner_side_inputs.py:392` | seed seen_names |
| M6 | emotion_tags类型守卫 | `context_engine.py:558` | isinstance检查 |
| M7 | id()→pattern.id | `reflector.py:183` | 用DB ID去重 |
| M8 | 审核状态修正 | `review_service.py:114` | revision_needed→review_status="revision_needed" |
| M9 | 不覆盖approved | `expression_pattern_service.py:92` | 移除 source 强制覆盖 |
| M10 | 原子重建 | `memory_index_projector.py:84` | 先建临时索引再swap |
| M11 | continue→break | `memory_retrieval_service.py:158` | 达到limit后break |
| M12 | 失败批次跳过 | `reflector.py:141` | except块中移除batch |
| M13 | 超时保护 | `react_retriever.py` | asyncio.wait_for(timeout=15) |
| M14 | 活跃锁跳过 | `lane_manager.py:89` | .locked()检查 |
| M15 | 补全映射 | `center.py:97` | 添加7个子系统映射 |
| M16 | table参数修正 | `admin_ui_service.py:85` | where时仍用table |
| M17 | 删除死代码 | `context_engine.py:550` | 删除整个方法 |
| M18 | tempfile泄漏 | `image_pipeline.py:35` | try/finally清理 |
| M19 | dict定期清理 | `gate.py:86` | prune _proactive_dispatching |
| M20 | 旋转保留上下文 | `lane_storage.py:92` | 增加摘要长度或保留更多历史 |

## Verification
LSP诊断 + pytest 全量回归。
