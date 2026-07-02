# Requirements Document

## Introduction

本 Spec 为 AstrMai 插件深度自检后确认的 **4 项最终缺口** 制定修复需求。其余 4 项经分析判定为"已满足"或"可跳过"（见自检报告）。这 4 项涉及：LLM 延迟观测、DB 运行时保护、self_lore 注入、Persona 缓存过期。

## Requirements

### Requirement 1: LLM 调用增加延迟计时（H7）

**User Story:** 作为性能调优人员，当 Bot 响应变慢时，我需要区分 LLM API 延迟 vs 插件内部处理延迟。

**Acceptance Criteria:**
1. `_elastic_call_result()` 在 LLM 调用前后使用 `time.perf_counter()` 记录延迟
2. `_log_usage()` 新增 `latency_ms` 字段
3. 延迟值传入 `_record_benchmark_sample()` 样本

**Notes:** 涉及 `gateway_call.py` L173 + `gateway_result.py` L116。当前 `time` 已导入但仅用于时间戳。

---

### Requirement 2: DB 运行时操作增加异常保护（H10）

**User Story:** 当 SQLite 运行时出错，消息处理不应崩溃。

**Acceptance Criteria:**
1. `_get_state_inner()` L95 `load_chat_state()` 包裹 try/except → 返回默认 state + `logger.exception`
2. `mark_energy_consumed()` L150 + `atomic_update_mood()` L138 `save_chat_state()` 包裹 try/except
3. 异常时降级为仅内存状态，记录 `logger.exception`

**Notes:** 涉及 `chat_state_service.py`。persistence 层（`state_profile_persistence.py`）无内部保护，需在调用层加。

---

### Requirement 3: self_lore 注入到系统提示词（R8）

**User Story:** Persona 背景知识（self_lore）已构建但从未注入 prompt，应可选注入。

**Acceptance Criteria:**
1. `context_engine.py` 接收 `memory_engine` 引用（或通过已有属性访问）
2. `_load_persona_payload()` 中调用 `memory_engine.recall_persona_lore()` 获取 self_lore 文本
3. 将 self_lore 附加到 `persona_payload["self_lore"]`
4. `_build_role_block()` 在角色块末尾注入 self_lore（如有）

**Notes:** `PersonaConfig.include_self_lore_in_prompt` 已在 Round ⑦ 添加（config.py + _conf_schema.json），默认 False。仅需接线。

---

### Requirement 4: Persona 缓存过期检测（R9）

**User Story:** 修改人设文本后不需手动清缓存即可生效。

**Acceptance Criteria:**
1. `get_summary()` 在返回缓存前比对 `original_prompt` 的 SHA-256 哈希与缓存中的 `raw_hash`
2. 哈希不匹配时清除旧缓存并触发重新摘要
3. 首次构建时存储 `raw_hash` 到缓存

**Notes:** `_compute_hash()` 已存在于 `persona_summarizer.py:L30`。仅需在 L184-L211 缓存返回路径增加比对。

---

## Out of Scope

- H4（决策 INFO 日志）、H5（状态 INFO 日志）— DEBUG + debug_trace 已满足需求
- R6（置信度门控）— 所有调用方已传合理置信度
- R7（Persona fallback）— 仅在 3x LLM 失败时触发，已满足

## Verification

| # | 验证方式 | 通过标准 |
|---|---------|---------|
| R1 | `_log_usage()` 日志含 `latency_ms` | 字段存在 |
| R2 | 模拟 DB 只读 → 消息处理不崩溃 | 日志含 exception + 使用默认 state |
| R3 | `include_self_lore_in_prompt=True` → payload 含 self_lore | 字段非空 |
| R4 | 修改人设文本 → 缓存重建 | 哈希不匹配触发重建 |
