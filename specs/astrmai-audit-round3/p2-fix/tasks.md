# Implementation Plan — AstrMai P2 Fix Round-3

> 派生自同目录 `requirements.md` 与 `design.md`。  
> **执行原则**：仅 1 个代码修改任务 + 1 个验证任务，严格串行。

## Overview

39/40 P2 bugs already resolved. Only P2.18 needs a code fix.

| Phase | 主题 | 任务 | 改动类型 |
|-------|------|------|---------|
| Phase 1 | P2.18 refresh_config | Task 1 | 补丁 (patch) |
| Phase 2 | 最终验证 + 总结 | Task 2 | 验证 |

## Tasks

### Phase 1: P2.18 — ContextCompactionEngine 缺 refresh_config

- [ ] 1. R17: context_compaction.py — 添加 refresh_config 方法
  - **Goal**: `ContextCompactionEngine` 支持热重载更新 `provider_id` 及 compaction 参数
  - **Files**: `astrmai/conversation/attention/context_compaction.py` (write)
  - **Steps**:
    1. 在 `__init__` 之后 (~line 207) 添加 `refresh_config(self, config)` 方法
    2. 从 `config.conversation` 读取 5 个 compaction 参数
    3. 更新 `self.provider_id` 及各参数
  - **Acceptance Criteria**:
    - `refresh_config` 方法存在
    - 方法签名与 `apply_hot_config` 调用兼容
    - 所有 `getattr` 有 default fallback
    - `provider_id` 字符串化处理
  - **Forbidden**: 不修改 `__init__` 签名，不修改 `bootstrap.py`
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - **Risk Notes**: 🟢 低风险，纯增量代码
  - _Requirements: R17

### Phase 2: 最终验证

- [ ] 2. Verify — 全量回归 + 40 项状态审计
  - **Goal**: 确认无新增回归，40 项 P2 全部有迹可查
  - **Files**: 无 (read-only + 命令执行)
  - **Steps**:
    1. 运行 pytest 确认无回归
    2. 运行 `python -c "import astrmai; print('OK')"` 确认导入
    3. 运行 `lsp_diagnostics` 检查修改文件
    4. 验证 P2.18 fix: grep `def refresh_config` in context_compaction.py
    5. 审计 40 项 P2 状态：对照 bug-classification.md 逐项标记
  - **Acceptance Criteria**:
    - pytest passed count >= 818
    - import astrmai OK
    - lsp_diagnostics 无 error
    - context_compaction.py 中有 `def refresh_config`
    - round3-summary.md 创建完成，40/40 全标记
  - **Check Commands**: `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
  - _Requirements: R1–R40

## 已解决 39/40 项验证清单

无需代码变更，源码验证即可：

| Bug | 证据位置 | 证据类型 |
|-----|---------|---------|
| P2.1 | `memory_engine.py:540` | `exclude_kinds` 参数已暴露 |
| P2.2 | `memory_processor.py:195` | 平衡括号正则 |
| P2.3a | `expression_auto_check_task.py:43-46` | ponytail prune |
| P2.3b | `jargon_auto_check_task.py:84-87` | ponytail prune |
| P2.4 | `evolution_manager.py:70-77` | CancelledError 处理 |
| P2.5 | `message_recorder.py:22-25` | ponytail prune |
| P2.6 | `database_review.py:77` | asyncio.create_task |
| P2.7 | `model_router.py:219-224` | ponytail prune stale pools |
| P2.8 | `lane_storage.py:13-22` | `_lane_creation_locks` |
| P2.9 | `persona_cache.py:26-29` | tempfile+replace |
| P2.10 | `context_compaction.py:1251` | ponytail dead code |
| P2.11 | `context_compaction.py:1244-1248` | cooldown prune |
| P2.12 | `context_compaction.py:176-178` | ponytail accept rollback risk |
| P2.13 | `sensors.py:18` | set 天然去重 |
| P2.14 | `judge.py:332` | ponytail accept NTP risk |
| P2.15 | `decision_router.py:66-67` | 可配置 timeout 3.0s |
| P2.16 | `chat_loop_kernel.py:1255` | ponytail accept priority design |
| P2.17 | `chat_loop_kernel.py:1283-1284` | ponytail accept sync getter |
| P2.19 | `bootstrap.py:263-264` | exception caught |
| P2.20 | `bootstrap.py:278-281` | exception caught, work_mode_enabled=False |
| P2.21 | `lifecycle.py:173-174` | ponytail pass on secondary flush |
| P2.22 | `lifecycle.py:69` | is_running after start_background_services |
| P2.23 | `lifecycle.py:151-156` | reload failure → return without heartbeat |
| P2.24 | `lifecycle.py:165` | 5s flush interval (was 15s) |
| P2.25 | `lifecycle.py:15` | SHUTDOWN_TASK_TIMEOUT = 8.0 (was 3.0) |
| P2.26 | `proactive_task.py:219-223` | _restart_if_still_running resets _is_running |
| P2.27 | `plugin_facade.py:452-453` | config.sys3.max_steps, config.sys3.tool_timeout |
| P2.28 | `plugin_facade.py:166` | track_task wraps update_user_stats |
| P2.29 | `plugin_facade.py:197-198` | ponytail docstring for caller |
| P2.30 | `plugin_facade.py:97-119` | ponytail log + per-component error catch |
| P2.31 | `plugin_facade.py:410-463` | async generator with docstring warning |
| P2.32 | `lifecycle.py:23-25` | track_task prunes done tasks |
| P2.33 | `lifecycle.py:203-206` | _states.clear() in terminate |
| P2.34 | `main.py:35-36` | ponytail session_waiter |
| P2.35 | `main.py:37` | ponytail persona context cleanup |
| P2.36 | `context_compaction.py:176-178` | ponytail compaction recovery |
| P2.37 | `main.py:137-149` | on_agent_begin/done hooks |
| P2.38 | `main.py:87` | ponytail reverse session overwrite |
| P2.39 | `main.py:159-160` | ponytail streaming skip |
| P2.40 | `main.py:122-125` | except Exception + logger.exception |
