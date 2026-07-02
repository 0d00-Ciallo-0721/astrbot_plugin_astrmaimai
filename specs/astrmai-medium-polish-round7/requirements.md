# Requirements Document — AstrMai 第二轮审查中危修复

> Spec ID: `astrmai-medium-round7-20260630` | Type: `hardening`
> 基于第二轮深度审查中 9 项 🟡 MEDIUM 级问题（🔴5 + 🟠9 已在前两轮修复）

---

## Introduction

第二轮深度审查共 28 项缺陷。🔴5 + 🟠9 已修复。本 Spec 覆盖剩余 **9 项 🟡 MEDIUM**。

### 范围覆盖

| # | 来源 | 问题 | 影响 |
|---|------|------|------|
| R1 | 状态机 | `chat_loop_kernel.py:1881` PROACTIVE_WAKEUP/HEARTFLOW 落到 IDLE | 调度优先级错误 |
| R2 | 状态机 | `chat_state_service.py:306` CAS 心情更新丢弃并发变更 | 心情不准确 |
| R3 | 状态机 | `chat_loop_kernel.py:1866` 消息重置 fairness 计数器 | fairness 绕过 |
| R4 | LLM链 | `context_compaction.py` 无 Token 阈值 | 长消息溢出 |
| R5 | LLM链 | `context_compaction.py:1040` `_stability_analysis_v2` 传参错误 | "natural_pause" 信号丢失 |
| R6 | 数据流 | `v2_store.py:805` mark_accessed 复活 stale | 过期记忆误复活 |
| R7 | 导入 | `plugin_helpers.py:10` 模块级 import_module | 测试时崩溃 |
| R8 | 生命周期 | `safe_create_task` 无跟踪 | 关闭时残留 |
| R9 | 生命周期 | `dedupe.py` 全局状态挂 `sys` | 热重载污染 |

### 明确排除

| 排除项 | 理由 |
|--------|------|
| 🔴 CRITICAL（5 项） | 已修复 |
| 🟠 HIGH（9 项） | 已修复 |
| 架构重构 | 最小改动 |

---

## Glossary

| 术语 | 定义 |
|------|------|
| **CAS** | Compare-And-Set，乐观并发控制 |
| **fairness** | 调度公平性，防止某聊天过度占用资源 |
| **stale** | 标记为过期的记忆条目 |
| **FTS** | Full-Text Search |

---

## Requirements

### Wave 1：P1 — 调度/状态修正（R1–R3）

### Requirement 1: PROACTIVE_WAKEUP/HEARTFLOW 正确映射到 ACTIVE 阶段

**User Story:** 当主动唤醒或心流评估触发时，聊天的调度优先级应反映其"活跃"状态。

#### Acceptance Criteria
1. THE `_derive_phase` SHALL 将 `PROACTIVE_WAKEUP` 和 `HEARTFLOW_EVALUATE` 映射到 `"ACTIVE"` 阶段。
2. WHEN 上述 action 出现时，THE `_plan_next_tick` SHALL 赋予 ACTIVE 优先级（60 分），而非 IDLE（20 分）。

#### Notes: `chat_loop_kernel.py:1881-1897`，+2 行

---

### Requirement 2: CAS 心情更新失败时重新计算

**User Story:** 当并发心情更新导致 CAS 失败时，不应丢弃并发变更。

#### Acceptance Criteria
1. WHEN CAS 失败时（`abs(current - snapshot) >= 0.0001`），THE 系统 SHALL 记录 `logger.debug` 并使用当前值重新调用 LLM 计算，而非应用过时 delta。
2. IF 重新计算不可行，THEN THE 系统 SHALL 保留 `current_mood` 不变并跳过本次更新。

#### Notes: `chat_state_service.py:306-310`，+5 行

---

### Requirement 3: 消息到达不重置 fairness 计数器

**User Story:** 一个被调度器连续选中的聊天，不应因收到一条消息就清零 fairness 惩罚。

#### Acceptance Criteria
1. THE `_update_state` SHALL NOT 在非 heartbeat 触发时将 `consecutive_selected_count` 重置为 0。
2. THE `consecutive_selected_count` SHALL 仅在 heartbeat 触发的 `_update_state` 中维护。

#### Notes: `chat_loop_kernel.py:1866-1867`，-2 行

---

### Wave 2：P2 — LLM/数据精度（R4–R6）

### Requirement 4: 添加 Token 阈值触发压缩

**User Story:** 当上下文接近模型窗口上限时，应触发压缩，避免 API 错误。

#### Acceptance Criteria
1. THE `maybe_compact` SHALL 在消息数阈值之外，额外检查 token 估算值。
2. WHEN `token_estimate > context_window * 0.82`，THE 系统 SHALL 触发压缩。
3. IF `token_estimator` 不可用，THEN SHALL 回退到消息数阈值。

#### Notes: `context_compaction.py`，+10 行，复用 `token_estimator.py`

---

### Requirement 5: 修正 `_stability_analysis_v2` 传参

**User Story:** `detect_safe_window` 返回的 `safe_window_reason` 应正确传递给稳定性分析。

#### Acceptance Criteria
1. THE `build_decision_snapshot` SHALL 将 `detect_safe_window` 返回的第二个元素（`safe_window_reason`）作为 `_stability_analysis_v2` 的第四参数。
2. THE `"natural_pause"` 信号 SHALL 能正常被触发。

#### Notes: `context_compaction.py:1040-1047`，±1 行

---

### Requirement 6: mark_accessed 不复活 stale 记录

**User Story:** 搜索时访问的 stale 记录不应被自动复活为 active。

#### Acceptance Criteria
1. THE `mark_accessed` SHALL NOT 将 `status = 'stale'` 的记录改为 `'active'`。
2. THE `mark_accessed` SHALL 仅更新 `last_access_time` 和 `access_count`。

#### Notes: `v2_store.py:1006`，-1 行（移除 CASE WHEN status='stale'）

---

### Wave 3：P3 — 基础设施卫生（R7–R9）

### Requirement 7: plugin_helpers 模块级 import 改为惰性

**User Story:** 在测试环境中导入 `plugin_helpers` 不应因缺少 AstrBot 而崩溃。

#### Acceptance Criteria
1. THE `Comp = import_module("astrbot.api.message_components")` SHALL 移入函数体内惰性加载。
2. THE `_message_component_class` SHALL 在首次调用时加载模块并缓存。

#### Notes: `plugin_helpers.py:10`，±3 行

---

### Requirement 8: safe_create_task 结果纳入跟踪

**User Story:** 插件关闭时，所有由 `safe_create_task` 创建的后台任务应被取消。

#### Acceptance Criteria
1. THE `safe_create_task` SHALL 将任务加入 `runtime.background_tasks`（如可访问）。
2. IF `runtime` 不可访问，THEN SHALL 仅附加 error callback（保持现状）。

#### Notes: `plugin_helpers.py:23-37`，+3 行

---

### Requirement 9: dedupe 全局状态从 sys 移入模块级变量

**User Story:** 热重载插件后，旧的去重缓存不应残留。

#### Acceptance Criteria
1. THE `sys._astrmai_debounce_cache` 和 `sys._astrmai_debounce_lock` SHALL 移入模块级变量。
2. THE `threading.Lock` SHALL 替换为 `asyncio.Lock`（在 asyncio 上下文使用）。

#### Notes: `dedupe.py:22-24`，±3 行

---

## Out of Scope

| 排除项 | 理由 |
|--------|------|
| 完整 token 感知架构 | 超出 MEDIUM 范围 |
| fairness 机制重设计 | 仅修 bug，不重构 |

---

## Dependency Map

```
R1→R3→R7 (Wave1, 可并行)
R4→R5→R6 (Wave2, 可并行)
R8→R9    (Wave3, 可并行)
```

---

## Verification Strategy

| 需求 | 验证 | 标准 |
|------|------|------|
| R1 | `grep "PROACTIVE_WAKEUP\|HEARTFLOW" chat_loop_kernel.py` | 在 `_derive_phase` 中有 case |
| R2 | `grep "CAS\|recompute\|skip" chat_state_service.py` | 失败路径不再丢数据 |
| R3 | `grep "consecutive_selected_count" chat_loop_kernel.py` | 仅 heartbeat 分支赋值 |
| R4 | `grep "token_estim\|0.82" context_compaction.py` | 新增阈值检查 |
| R5 | `grep "natural_pause\|safe_window_reason" context_compaction.py` | 参数正确 |
| R6 | `grep "stale.*active\|CASE WHEN" v2_store.py` | 不再复活 |
| R7 | `grep "import_module.*message_components" plugin_helpers.py` | 在函数体内 |
| R8 | `grep "background_tasks" plugin_helpers.py` | 有 add 调用 |
| R9 | `grep "sys\._astrmai" dedupe.py` | 0 匹配 |
| 全量 | `pytest tests/ -q` | ≥ 847 passed |
