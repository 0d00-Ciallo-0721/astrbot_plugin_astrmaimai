# Requirements Document — AstrMai P2 Fix Round-3

> Spec: `astrmai-audit-round3/p2-fix` | Type: hardening  
> Source: `specs/astrmai-audit-round1/bug-classification.md` lines 59-103  
> Target: 40 P2 bugs across 6 modules, 6 Waves

## Audit Reality (Pre-Fix Status)

Round-2 P1 fixes applied extensive ponytail guards that also covered many P2 bugs as side effects. After code audit:

| Status | Count | Description |
|--------|:-----:|-------------|
| Already code-fixed | 12 | Actual code changes existed before Round-3 |
| Ponytail-accepted | 17 | Comment documents risk, no code change needed |
| Correct behavior | 5 | Audit false-positive; existing code handles it |
| Feature already added | 5 | Missing hooks/features added in Round-2 |
| Fixed by P1.12 | 1 | P2.33 overlap with P1.12 fix |
| **Needs fix** | **1** | P2.18: ContextCompactionEngine no refresh_config |

**Only P2.18 requires a code change.** All others are verified resolved or accepted.

## Introduction

本 Spec 覆盖 Round-1 审计中识别的 40 项 P2 (中优先级) 缺陷。实际修复范围极窄，因为 39/40 已在 Round-2 副作用或 ponytail 注释中解决。

## Glossary

- **P2**: 中优先级 — 边缘情况 / 性能退化 / 错误处理不完善 / 日志缺失
- **Ponytail comment**: 文档化接受风险的注释 (`# ponytail: ...`)
- **Hot-apply**: AstrMai WebUI 配置热重载，无需重启插件
- **EARS**: Easy Approach to Requirements Syntax

## Requirements — Wave/Phase 划分

### Wave 1: 无界增长 (R1–R7)

| Requirement | Bug ID | File | Description | Status |
|-------------|--------|------|-------------|:------:|
| R1 | P2.3a | `expression_auto_check_task.py:34` | `_last_run_at` dict 无界 | ✅ ponytail prune |
| R2 | P2.3b | `jargon_auto_check_task.py:31` | `_last_run_at` dict 无界 | ✅ ponytail prune |
| R3 | P2.5 | `message_recorder.py:19` | `_windows` dict 从不清理 | ✅ ponytail prune |
| R4 | P2.7 | `model_router.py:97` | `_pools` 累积死模型 | ✅ ponytail prune |
| R5 | P2.11 | `context_compaction.py:195` | `_cooldown_by_chat` 无界 | ✅ ponytail prune |
| R6 | P2.13 | `sensors.py:18` | `foreign_commands` 不 dedup | ✅ set 天然去重 |
| R7 | P2.32 | `runtime_context.py:123` | `background_tasks` 无界 | ✅ track_task prune |

### Wave 2: 数据一致性 (R8–R13)

| Requirement | Bug ID | File | Description | Status |
|-------------|--------|------|-------------|:------:|
| R8 | P2.1 | `memory_engine.py:537` | `recall()` 硬编码 `exclude_kinds` | ✅ 参数已暴露 |
| R9 | P2.2 | `memory_processor.py:195` | Greedy regex `\{.*\}` | ✅ 平衡括号匹配 |
| R10 | P2.6 | `database_review.py:72` | `save_pattern` async 静默丢弃 | ✅ create_task |
| R11 | P2.8 | `lane_storage.py:11` | `ensure_lane` 重复创建 conversation | ✅ lane_creation_locks |
| R12 | P2.9 | `persona_cache.py:24` | 写入非原子 | ✅ tempfile+replace |
| R13 | P2.12 | `context_compaction.py:1393` | merge 失败无 rollback | ✅ ponytail 接受 |

### Wave 3: 状态/配置 (R14–R21)

| Requirement | Bug ID | File | Description | Status |
|-------------|--------|------|-------------|:------:|
| R14 | P2.10 | `context_compaction.py:1243` | 死代码 token 触发器 | ✅ ponytail 标记 |
| R15 | P2.16 | `chat_loop_kernel.py:1240` | `wait_arm` 优先级 | ✅ ponytail 接受 |
| R16 | P2.17 | `chat_loop_kernel.py:1283` | sync getter 无 await 安全 | ✅ ponytail 接受 |
| R17 | P2.18 | `bootstrap.py:232` | compaction `provider_id` 热重载后陈旧 | 🔴 **NEEDS FIX** |
| R18 | P2.22 | `lifecycle.py:47` | `is_running` 在服务确认前设置 | ✅ 已修正 (line 69) |
| R19 | P2.26 | `lifecycle.py:97` | ProactiveTask auto-restart 损坏 | ✅ `_restart_if_still_running` |
| R20 | P2.30 | `plugin_facade.py:80` | `apply_hot_config` 部分失败 | ✅ ponytail 日志 |
| R21 | P2.38 | `main.py:79` | reverse session 可能被覆盖 | ✅ ponytail 注释 |

### Wave 4: 异步/错误恢复 (R22–R31)

| Requirement | Bug ID | File | Description | Status |
|-------------|--------|------|-------------|:------:|
| R22 | P2.4 | `evolution_manager.py:69` | CancelledError 当 ERROR 打 | ✅ 已有处理 |
| R23 | P2.14 | `judge.py:331` | `time.time()` NTP 脆弱 | ✅ ponytail 接受 |
| R24 | P2.15 | `decision_router.py:67` | judge timeout 2.0s 太短 | ✅ 可配置 3.0s |
| R25 | P2.21 | `lifecycle.py:144` | `_db_sync_task` 二次 flush 被取消 | ✅ ponytail pass |
| R26 | P2.23 | `lifecycle.py:131` | `cron_guard` reload 残留心跳 | ✅ 正确行为 |
| R27 | P2.24 | `lifecycle.py:144` | 15s flush 崩溃丢数据 | ✅ 已改 5s |
| R28 | P2.25 | `lifecycle.py:227` | `SHUTDOWN_TASK_TIMEOUT` 3.0s 太短 | ✅ 已改 8.0s |
| R29 | P2.28 | `plugin_facade.py:151` | `track_incoming` 静默不同步 | ✅ track_task |
| R30 | P2.29 | `plugin_facade.py:183` | `suppress_default` 无 stop_event | ✅ ponytail 文档 |
| R31 | P2.31 | `plugin_facade.py:393` | `enter_sys3_direct` async generator 误用 | ✅ async generator 正确 |

### Wave 5: Hooks/Bootstrap (R32–R37)

| Requirement | Bug ID | File | Description | Status |
|-------------|--------|------|-------------|:------:|
| R32 | P2.19 | `bootstrap.py:258` | VisualCortex 部分初始化泄漏 | ✅ 异常已捕获 |
| R33 | P2.20 | `bootstrap.py:265` | CronHeartbeatGuard 部分构造未清理 | ✅ 异常已捕获 |
| R34 | P2.27 | `plugin_facade.py:430` | `max_steps`/`timeout` 硬编码 | ✅ config 读取 |
| R35 | P2.37 | `main.py:75` | 缺 `on_agent_begin`/`done` hooks | ✅ 已添加 |
| R36 | P2.39 | `main.py:125` | `on_decorating_result` 流式时静默跳过 | ✅ ponytail 注释 |
| R37 | P2.40 | `main.py:81` | `inject_gemini_reverse_session` 吞异常 | ✅ logger.exception |

### Wave 6: 跨模块标注 (R38–R40)

| Requirement | Bug ID | File | Description | Status |
|-------------|--------|------|-------------|:------:|
| R38 | P2.34 | cross-cutting | 无统一 `session_waiter` 抽象 | ✅ ponytail main.py:35 |
| R39 | P2.35 | cross-cutting | persona 热切换无上下文清理 | ✅ ponytail main.py:37 |
| R40 | P2.36 | cross-cutting | 无 compaction 错误恢复机制 | ✅ ponytail compaction.py:176 |

---

## R17: P2.18 — ContextCompactionEngine 缺 refresh_config

### User Story
用户通过 WebUI 热重载配置后，`compaction_provider_id` 变更未生效，继续使用旧的 provider_id。

#### Acceptance Criteria
1. THE `ContextCompactionEngine` SHALL 添加 `refresh_config` 方法。
2. WHEN `apply_hot_config` 调用 `comp.refresh_config(parsed_config)`，THE `provider_id` SHALL 更新为新值。
3. THE 方法 SHALL 更新 `compaction_trigger_segments`、`compaction_trigger_tokens`、`compaction_keep_recent_segments`、`compaction_summary_max_tokens`（保持与其他组件一致）。

#### Notes / Constraints
- 涉及文件: `astrmai/conversation/attention/context_compaction.py` (write)
- 最小改动: +10 行 `refresh_config` 方法
- 参照其他组件的 `refresh_config` 模式

## Verification Strategy

| 验证层 | 命令 | 覆盖需求 |
|--------|------|---------|
| 导入检查 | `python -c "import astrmai; print('OK')"` | R17 |
| 测试回归 | `$env:PYTHONPATH='.'; pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py` | R1–R40 |
| LSP 诊断 | `lsp_diagnostics` on changed file | R17 |

## Out of Scope

- P0/P1 修复 (Round-1/2 已完成)
- P3 修复 (32 项，另行安排)
- 架构重构或新功能开发
