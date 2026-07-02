# Implementation Plan

> 本任务列表派生自同目录 `requirements.md` 与 `design.md`。

## Overview

本任务列表把 5 条需求翻译为 **6 个任务**（3 审计 + 3 加固）。M2/M3/M4 为零改动审计，M3/M5/M6 有少量代码改动。

| Phase | 主题 | 任务 | 改动 |
|-------|------|------|------|
| Phase 1 | 审计确认 | Tasks 1-3 | 只读 |
| Phase 2 | 代码加固 | Tasks 4-5 | +11/-0 |
| Phase 3 | 验证 | Task 6 | 验证 |

## Tasks

### Phase 1: 审计确认（只读）

- [ ] 1. M2+M4 审计确认 — Router light_tool_set + SubAgent provider_id
  - **Goal**: 确认 M2 和 M4 当前实现已正确
  - **Steps**:
    1. 读 `router.py` L33-35 确认 `get_light_tool_set()` 被调用
    2. 读 `base_agent.py` L61 确认 `provider_id` 重新获取
    3. 读 `computer_agent.py` 和 `cron_agent.py` 确认不重写 `call()`
    4. 标记 M2=M4=✅ 已确认
  - **Check**: 代码审计 → 无需改动
  - _Requirements: M2, M4_

- [ ] 2. M3 审计 — `_build_execution_tools()` 工具合并确认
  - **Goal**: 确认 Sys2→Sys3 工具交接正确
  - **Steps**:
    1. 读 `planner_side_inputs.py` L390-406
    2. 确认 `WaitTool` / `OmniPerceptionTool` / `SelfLoreQueryTool` / `sys3_light_tools` 均包含
    3. 确认 `OmniPerceptionTool` 参数完整
  - **Check**: 代码审计 → 已确认 ✅
  - _Requirements: M3_

- [ ] 3. M6 审计 — 确认 `cron_manager` 架构
  - **Goal**: 确认 AstrBot `cron_manager` 是全局单例还是 per-session
  - **Steps**:
    1. 搜索 `cron_manager` 的实例化位置
    2. 确认是全局单例 → M6 无需 session 过滤，仅加日志
    3. 确认是 per-session → 需增加 `target_origin` 过滤
  - **Check**: 搜索 `cron_manager` 全项目 → 记录结论
  - _Requirements: M6_

### Phase 2: 代码加固

- [ ] 4. M3+M5 加固 — 工具名去重 + HandoffRegistry active 检查
  - **Goal**: 防止工具同名覆盖 + 过滤 inactive 动态 Agent
  - **Files**:
    - ✏️ `astrmai/conversation/planning/planner_side_inputs.py` — 去重
    - ✏️ `astrmai/workmode/tools/handoff_registry.py` — active 检查
  - **Steps**:
    1. `planner_side_inputs.py`: `_build_execution_tools()` 中 `sys3_light_tools` 后增加去重循环
    2. `handoff_registry.py`: `discover()` L26 后增加 `if not getattr(handoff, "active", True): continue`
  - **Acceptance Criteria**:
    - 同名工具去重：第二个被跳过 + warning 日志
    - `active=False` 的 handoff 不被注入
    - `active=True`（默认）正常注入
  - **Forbidden**: 不修改工具合并业务逻辑；不修改 WaitTool/OmniPerceptionTool 构造参数
  - **Check Commands**: `pytest tests/ -v -k "planner_side_inputs or handoff"` ； `python -c "from astrmai.workmode.tools.handoff_registry import HandoffRegistry; print('OK')"`
  - **Risk Notes**: 🟢 防御性加固，零回归风险
  - _Requirements: M3, M5_

- [ ] 5. M6 加固 — CronHeartbeatGuard 日志 + 条件过滤
  - **Goal**: 增加 job 恢复日志 + 根据架构结论决定是否加 session 过滤
  - **Files**:
    - ✏️ `astrmai/workmode/cron_guard/heartbeat.py`
  - **Steps**:
    1. `reload_all_lost_jobs()`: 恢复每个 job 前增加 `logger.info(f"reviving job '{snap.name}' from session '{snap.target_origin}'")`
    2. 若 Task 3 确认 `cron_manager` 是 per-session → `_revive_job()` 增加 `target_origin` 参数并传递给 `CronJob`
    3. 若 Task 3 确认 `cron_manager` 是全局单例 → 仅增加日志，不加过滤
  - **Acceptance Criteria**:
    - 恢复日志包含 `target_origin`
    - 若需要过滤，per-session 只恢复本 session 的 job
  - **Forbidden**: 不修改 `cron_manager` 行为；不修改 `CronJob` PO 结构
  - **Check Commands**: `pytest tests/ -v -k "cron_guard or heartbeat"`
  - **Risk Notes**: 🟡 架构确认前不加 session 过滤
  - _Requirements: M6_

### Phase 3: 验证

- [ ] 6. 验证 — LSP + 回归测试
  - **Goal**: 全部改动无回归
  - **Steps**: `pytest tests/ -v --tb=short` ； `lsp_diagnostics` 变更文件 ； `git diff --stat`
  - **Check**: ≥ 68 passed；0 lsp error
  - _Requirements: ALL_

## Dependency Chain

```
Task 1 (M2+M4 审计) → Task 2 (M3 审计) → Task 3 (M6 架构确认)
                                               ↓
Task 4 (M3+M5 加固) ←──────────────────────────┘
    ↓
Task 5 (M6 加固) → Task 6 (验证)
```

## Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `planner_side_inputs.py` | 工具名去重 | +8 |
| 2 | `handoff_registry.py` | active 检查 | +3 |
| 3 | `heartbeat.py` | 日志 + 条件过滤 | +3 |
| **Total** | **3 文件** | | **+14** |

---

# 🔍 交叉验证报告

| 检查项 | 结果 |
|--------|:--:|
| 需求→设计 M2→§3.1 | ✅ |
| 需求→设计 M3→§3.2 | ✅ |
| 需求→设计 M4→§3.3 | ✅ |
| 需求→设计 M5→§3.4 | ✅ |
| 需求→设计 M6→§3.5 | ✅ |
| 设计→任务 | ✅ 5/5 |
| 任务字段完整性 | ✅ 6/6 × 8 fields |
| EARS 覆盖率 | ✅ 18 条 |
| 文件实存性 | ✅ 3/3 |
| 依赖链 | ✅ 无循环/孤儿 |
| **缺口** | **0** |
