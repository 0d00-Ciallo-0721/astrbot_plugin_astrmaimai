# Requirements Document

## Introduction

本 Spec 为「AstrMai」插件 Round 5 深度审查中识别出的 **剩余 🟡/🟢 级发现** 制定统一完善需求文档。范围覆盖配置完善、韧性缺口、人设打磨、可观测性四组，共 13 项。这些发现非生产阻断级，但补齐后可显著提升插件的健壮性和可维护性。

明确不在本 Spec 范围：
- Round 1–6 已修复的 53 项缺陷
- 新功能开发、依赖升级

## Glossary（仅新增术语）

- **EventBus Worker**：`event_bus.py` 中 3 个 `_worker_loop` 协程，消费事件队列
- **ProactiveTask Loop**：`proactive_task.py` 中 `_loop()` 主循环
- **Persona Staleness**：人设缓存过期检测，当前人设仅首次构建，永不更新
- **FrequencyController Dead Code**：`frequency_controller.py` 存在于代码中但从未被调用

## Requirements

### Wave 1：配置完善（3 项）

---

### Requirement 1: `_conf_schema.json` 增加数字范围提示

**User Story:** 作为通过 WebUI 配置插件的用户，当我在文本框中输入数值时，我不希望不知道有效范围（如 `base_frequency` 应该是 0~1），所以 WebUI 能显示数值边界。

**Acceptance Criteria:**
1. THE `_conf_schema.json` 中所有概率 0–1 字段 SHALL 增加 `"minimum": 0, "maximum": 1`。
2. THE `_conf_schema.json` 中所有百分比 0–100 字段 SHALL 增加 `"minimum": 0, "maximum": 100`。
3. THE `_conf_schema.json` 中所有正整数 ≥1 字段 SHALL 增加 `"minimum": 1`。

**Notes:** 涉及 `_conf_schema.json` 全文，与 config.py 的 Pydantic 约束互补。WebUI 端显示范围提示，Pydantic 端做强校验。

---

### Requirement 2: `emotion_mapping` 格式校验

**User Story:** 作为配置者，当我在 `emotion_mapping` 中填写格式错误的值时（如缺少冒号分隔符），我不希望运行时静默失败，所以启动时有格式校验 warning。

**Acceptance Criteria:**
1. THE `AstrMaiConfig.__init__` SHALL 在加载后遍历 `reply.emotion_mapping`，检查每个条目是否包含 `":"` 分隔符，否则 `logger.warning`。
2. THE 校验 SHALL 不阻止启动（仅 warning）。

**Notes:** 涉及 `config.py` `AstrMaiConfig.__init__`。当前依赖运行时 `split(":")` 容错。

---

### Requirement 3: 模型池名称校验 warning

**User Story:** 作为配置者，当我在 `agent_models` 中填写了拼写错误的模型名时，我不希望等到第一次调用失败才知道，所以启动时有基本的存在性校验。

**Acceptance Criteria:**
1. THE `AstrMaiConfig.__init__` SHALL 检查 `provider` 的各模型池是否为空（不为空时不校验具体名称，因为模型名格式依赖 provider）。
2. THE 已通过 C3 实现了空池检测 ✅，本项仅扩展为：当模型池非空但所有条目格式异常（如不含 `/` 分隔符的标准模型名格式）时 `logger.warning`。

**Notes:** 涉及 `config.py`。简单启发式：标准模型名格式为 `provider/model` 含斜杠。

---

### Wave 2：韧性缺口（3 项）

---

### Requirement 4: EventBus Worker 自动重启

**User Story:** 作为运维人员，当 EventBus 的 3 个 worker 协程因意外异常全部崩溃时，我不希望事件永久丢失，所以有 worker 健康检查和自动重启。

**Acceptance Criteria:**
1. THE `EventBus` SHALL 新增 `_worker_health_check()` 方法，每 30 秒检查活跃 worker 数量。
2. WHEN 活跃 worker 数 < 3，THE 方法 SHALL 启动新 worker 补足至 3 个。
3. THE 重启 SHALL 记录 `logger.warning`，包含重启原因。

**Notes:** 涉及 `event_bus.py`。当前 worker 单次异常自愈但无重启机制。

---

### Requirement 5: ProactiveTask 主循环自动重启

**User Story:** 作为运维人员，当 ProactiveTask 的 `_loop()` 因外部取消（非正常 shutdown）而意外终止时，我不希望主动行为永久停止，所以有重启机制。

**Acceptance Criteria:**
1. THE `ProactiveTask.start()` SHALL 在 `asyncio.create_task(self._loop())` 后增加 `task.add_done_callback(self._on_loop_done)`。
2. THE `_on_loop_done()` SHALL 检查 `self._is_running` 标志，若仍为 `True`（意外终止），则 `logger.error` + 延迟 5 秒后重新 `start()`。

**Notes:** 涉及 `proactive_task.py`。正常 shutdown 通过 `stop()` 设 `_is_running=False`，不触发重启。

---

### Requirement 6: 记忆写入增加幻觉/投毒防御

**User Story:** 作为系统设计者，当 LLM 幻觉或恶意用户诱导产生虚假记忆时，我不希望这些内容被无条件写入持久化存储，所以记忆写入有基本的置信度门控。

**Acceptance Criteria:**
1. THE `MemoryWriteService.write()` SHALL 在写入前检查 `request.confidence`，若低于配置阈值 `min_memory_confidence`（默认 0.3）则跳过并记录 `logger.debug`。
2. THE `min_memory_confidence` SHALL 通过 `MemoryConfig` 配置（`config.py` + `_conf_schema.json`）。

**Notes:** 涉及 `memory_write_service.py`、`config.py`、`_conf_schema.json`。`confidence` 字段已存在于 `MemoryWriteRequest` 中。

### Wave 3：人设打磨（4 项）

---

### Requirement 7: Persona 摘要 fallback 改进

**User Story:** 作为依赖 Bot 角色一致性的用户，当 Persona 摘要的 LLM 调用全部失败时，我不希望 fallback 产生无意义的截断文本，所以有更健壮的降级策略。

**Acceptance Criteria:**
1. THE `persona_summarizer.py` 的 `_summarize_core_identity_with_retry()` 在 3 次 LLM 重试全部失败后，SHALL 使用 `original_prompt[:150]` 之外增加简单的关键句提取：优先取含"你是"、"角色"、"身份"的句子，其次取前 3 个非空行。
2. THE 降级文本 SHALL 以 `[系统降级提取]` 前缀标识（已存在 ✅），并在前缀后附加降级原因（如 `"LLM 3次重试均失败"`）。

**Notes:** 涉及 `persona_summarizer.py`。当前降级为 `original_prompt[:150]` 裸截断。

---

### Requirement 8: self_lore 自动注入选项

**User Story:** 作为期望 Bot 记住自身设定的用户，当 self_lore 已通过 PersonaSummarizer 构建但仅能通过 `self_lore_query` 工具手动调用时，我不希望这些知识被闲置，所以有自动注入到系统提示词的选项。

**Acceptance Criteria:**
1. THE `context_engine.py:_load_persona_payload()` SHALL 新增可选的 self_lore 自动注入：当 `include_self_lore_in_prompt=True` 时，调用 `SelfLoreService.recall_persona_lore()` 并将结果附加到 persona 块的末尾。
2. THE `include_self_lore_in_prompt` SHALL 通过 `_conf_schema.json` 的 `persona` 分组配置，默认 `False`（保持当前行为）。

**Notes:** 涉及 `context_engine.py`、`_conf_schema.json`、`config.py`。

---

### Requirement 9: Persona 缓存过期检测

**User Story:** 作为通过 AstrBot WebUI 更新人设的用户，当我修改了人设文本后重启 Bot，我不希望插件仍使用旧的摘要缓存，所以能检测到人设文本变更并自动重建。

**Acceptance Criteria:**
1. THE `PersonaSummarizer.get_summary()` SHALL 在读取缓存时比较 `original_prompt` 的 SHA-256 哈希与缓存中的 `source_hash`。
2. WHEN 哈希不匹配，THE 方法 SHALL 清除旧缓存并触发重新摘要。
3. THE 首次构建时 SHALL 存储 `source_hash` 到缓存 JSON 中。

**Notes:** 涉及 `persona_summarizer.py`。当前缓存永不过期，手动改人设后需手动清缓存。

---

### Requirement 10: FrequencyController 死代码清理或 Rewire

**User Story:** 作为代码维护者，当 `FrequencyController` 在 `gate.py` 中被注入但从未被调用时，我不希望死代码混淆代码阅读，所以要么清理要么 Rewire。

**Acceptance Criteria:**
1. THE 项目 SHALL 执行二选一：
   - **方案 A（清理）**：从 `bootstrap.py`、`gate.py`、`runtime_context.py` 中移除 `FrequencyController` 的注入代码，保留 `frequency_controller.py` 文件但标注 `# deprecated, unused`。
   - **方案 B（Rewire）**：在 `gate.py:process_event()` 中调用 `frequency_controller.should_reply()` 作为快速门控（在 attention 处理之前），并增加配置开关控制。

**Notes:** 涉及 `bootstrap.py`、`gate.py`、`runtime_context.py`。建议方案 A（清理），因为当前频控逻辑已由 EnergyManager + AttentionGate 的 throttle 替代。

---

### Wave 4：可观测性（3 项）

---

### Requirement 11: Lane rotation 资源泄漏指标

**User Story:** 作为运维人员，当 Lane rotation 频繁触发时，我不希望 provider 侧 session 泄漏不可见，所以有指标暴露。

**Acceptance Criteria:**
1. THE `LaneManager` SHALL 新增 `_rotation_count` 计数器，每次 rotation 时递增。
2. THE `PluginRuntimeContext.build_diagnostics()` SHALL 包含 `lane_rotation_count` 指标。
3. THE 指标 SHALL 通过 WebUI `/runtime/status` API 暴露。

**Notes:** 涉及 `lane_manager.py`、`runtime_context.py`。

---

### Requirement 12: 对话膨胀监控

**User Story:** 作为运维人员，当 AstrMai 的 Lane 机制产生大量 AstrBot conversation 时，我不希望对话存储膨胀不可见，所以有数量监控。

**Acceptance Criteria:**
1. THE `LaneManager` SHALL 在 `ensure_lane()` 中统计当前活跃 lane 数量。
2. THE 统计 SHALL 通过 `build_diagnostics()` 暴露为 `active_lane_count`。

**Notes:** 涉及 `lane_manager.py`、`runtime_context.py`。帮助监控 `conversation_manager` 的共享资源使用。

---

### Requirement 13: 启动阶段日志完善

**User Story:** 作为调试启动问题的开发者，当 `PluginLifecycleManager.on_program_start()` 执行各阶段时，我不希望只看到"Starting"和"Running"两条日志，所以每个阶段有明确的时间戳日志。

**Acceptance Criteria:**
1. THE `on_program_start()` SHALL 在每个 `set_boot_phase()` 调用后增加 `logger.info(f"[AstrMai] boot phase: {phase}")`，记录阶段名称和耗时。
2. THE 阶段包括：`lifecycle.memory`、`lifecycle.commands`、`lifecycle.proactive`、`lifecycle.visual`、`lifecycle.workmode`、`runtime.running`。

**Notes:** 涉及 `lifecycle.py`。当前 `set_boot_phase()` 仅设内部状态，不记录日志。

---

## Out of Scope

- Round 1–6 已修复的 53 项缺陷
- 新功能开发
- 依赖升级
- Persona 压缩质量调优（LLM prompt 优化）
- Meme sender 文件内容校验（本地文件，风险低）
- 命令冲突解决（概率低）

## High-Risk Confirmation

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| HK1 | R4 EventBus worker 重启可能产生重复消费 | 🟢 | worker 重启后从队列继续消费，不重复 |
| HK2 | R5 ProactiveTask 重启可能导致双重 dream | 🟡 | 增加 `_restart_lock` 防止并发重启 |
| HK3 | R10 清理 FrequencyController 可能影响未知调用方 | 🟡 | 先搜索全项目引用，确认零调用后再清理 |
| HK4 | R9 Persona 哈希检测可能因 LLM 非确定性输出频繁重建 | 🟢 | 仅检测 `original_prompt` 文本变更，非 LLM 输出 |

## Dependency Map

```
全部 13 项涉及不同文件，可全并行执行。
```

## Verification Strategy

| 验证层 | 方式 | 覆盖 |
|--------|-----|:--:|
| 单元 | `event_bus.py` worker 重启测试 | R4 |
| 单元 | `proactive_task.py` loop 重启测试 | R5 |
| 单元 | `memory_write_service.py` confidence 门控 | R6 |
| 单元 | `persona_summarizer.py` fallback + staleness | R7, R9 |
| 集成 | `context_engine.py` self_lore 自动注入 | R8 |
| 审计 | FrequencyController 引用搜索 | R10 |
| 手工 | WebUI `/runtime/status` 指标验证 | R11, R12 |
| 手工 | 启动日志阶段验证 | R13 |
| LSP | 全部变更文件 | ALL |

---

> **写入 3 完成。** `requirements.md` 全部 13 条需求已写入。可进入 Phase 2（设计文档）。


