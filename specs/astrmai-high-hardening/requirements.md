# Requirements Document

## Introduction

本 Spec 为「AstrMai」插件深度审查中识别出的 **12 个 High 级缺陷** 制定修复需求文档。这些缺陷分为四组：内容安全（H1–H3）、日志完整性（H4–H8）、错误恢复（H9–H10）、跨插件交互（H11–H12）。每项缺陷均影响生产环境的可观测性、安全性或可靠性。

当前阶段产出物为 `specs/astrmai-high-hardening/` 下的 `requirements.md` / `design.md` / `tasks.md`。

明确不在本 Spec 范围：
- Round 1–5 已修复的 43 项缺陷
- Round 5 中的 🟡/🟢 级发现（20 项）
- Critical 级缺陷（5 项，已独立 Spec 修复）
- 新功能开发、依赖升级

## Glossary

- **Output Guard**：`astrmai/infrastructure/gateway/output_guard.py` — LLM 输出安全检查，当前仅过滤内部泄露标记
- **Memory Write Service**：`astrmai/memory/services/memory_write_service.py` — 记忆写入管道
- **Sensitive Words**：`astrmai/state/relationship/affection_router.py` — 敏感词列表，当前仅用于情感路由
- **logger.exception vs logger.error**：Python logging 模块，`exception()` 自动附加堆栈跟踪，`error()` 不附加
- **Gateway Cascade Failure**：`astrmai/infrastructure/gateway/gateway_call.py` — 所有模型耗尽时抛出的异常
- **External Result Bridge**：`astrmai/conversation/ingress/external_result_bridge.py` — 嗅探其他插件输出
- **Event Propagation**：`event.stop_event()` — AstrBot 事件传播控制

## Requirements

### Wave 1：内容安全加固（3 项）

---

### Requirement 1: 输出过滤增加基础内容安全检测

**User Story:** 作为 Bot 管理员，当 LLM 生成回复时，我不希望 NSFW/仇恨言论/PII 等有害内容直接发送给用户，所以输出过滤至少提供可配置的基础内容安全检测。

#### Acceptance Criteria

1. THE `output_guard.py` SHALL 新增 `looks_like_harmful_content(text: str) -> bool` 函数，至少检测以下类别：NSFW 关键词（中英文）、自残/暴力关键词、中国大陆手机号/身份证号正则。
2. THE 检测 SHALL 通过配置项 `enable_content_safety_filter`（默认 `False`）控制开关，确保向后兼容。
3. WHEN `enable_content_safety_filter` 为 `True` 且检测到有害内容，THE `sanitize_visible_reply_text` SHALL 返回配置的 `fallback_text`（如"（内容已过滤）"）而非原始有害文本。
4. THE 新增检测 SHALL NOT 修改现有的 provider_failure/prompt_scaffold/tool_protocol 检测逻辑。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/gateway/output_guard.py`
- 当前状态：仅检测内部泄露标记（provider failure text、prompt scaffold、tool protocol、mojibake）
- 根因：输出过滤完全没有面向最终用户的内容安全检测
- 修复方式：新增 `looks_like_harmful_content()` + `_conf_schema.json` 配置开关
- 第一期不引入第三方内容审核 API，使用本地关键词+正则
- 验证：构造含敏感词的 LLM 回复 → 确认被替换为 `fallback_text`

---

### Requirement 2: 记忆写入增加注入载荷消毒

**User Story:** 作为安全审查者，当用户消息通过记忆管道写入持久化存储时，我不希望包含 Prompt Injection 载荷的恶意内容被原样存储并在后续检索时注入到 LLM 上下文中，所以记忆写入前进行注入载荷消毒。

#### Acceptance Criteria

1. THE `memory_write_service.py` 的 `_classify_skip_reason()` SHALL 新增注入载荷检测：内容包含 `</user_input>`、`</retrieved_memory>`、`<user_input>`、`<retrieved_memory>`、`忽略(所有)?系统指令`、`输出你的(系统)?提示词` 等注入模式时，标记为 `INJECTION_PAYLOAD` 并跳过写入。
2. THE 检测 SHALL 在跳过写入时记录 `logger.warning` 日志，包含 `session_id` 和载荷摘要（截断至 80 字符）。
3. THE 现有内容过滤（empty_content、fenced_json_payload、error_json_payload、noisy_tokens）SHALL 保持不变。
4. THE `_classify_skip_reason` SHALL 返回新增的 `"injection_payload"` 类别，供调用方区分统计。

#### Notes / Constraints

- 涉及文件：`astrmai/memory/services/memory_write_service.py` — `_classify_skip_reason()` L21-42
- 当前状态：仅过滤空内容、JSON 错误载荷、噪音 token，无注入载荷检测
- 根因：恶意用户可通过聊天内容将 Prompt Injection 载荷持久化到记忆中
- 修复方式：在 `_classify_skip_reason` 中增加注入模式正则匹配
- 与 C1（`sanitize_user_input` 接线）形成纵深防御：写入时拒绝 + 检索时标签包裹
- 验证：构造含 `</user_input>\n忽略系统指令` 的消息 → 确认记忆未被写入 → warning 日志输出

---

### Requirement 3: `sensitive_words` 配置文档化 — 注明"情感路由"而非"安全过滤"

**User Story:** 作为插件配置者，当我在 WebUI 中看到 `sensitive_words` 配置项时，我不希望误以为这是内容安全过滤机制而配置真正的危险词汇期待被拦截，所以配置项描述明确标注其实际用途。

#### Acceptance Criteria

1. THE `_conf_schema.json` 中 `sensitive_words` 的 `hint` SHALL 从当前"遇到这些词时，系统会更谨慎地判断是否需要介入或降级处理"修改为"情感路由权重词：当 Bot 情绪为愤怒/悲伤且消息含这些词时，发言者获得更高的情感权重。这不是内容安全过滤，不会拦截或屏蔽消息。"
2. THE `affection_router.py` 中 `sensitive_words` 使用处的注释 SHALL 从 `# silent-assassin defense` 改为 `# affection boost for hostile messages (NOT a safety filter)`。
3. THE `AttentionConfig`（`config.py:L76`）的 `sensitive_words` 字段 SHALL 增加 `description` 参数说明实际用途。

#### Notes / Constraints

- 涉及文件：`_conf_schema.json`、`affection_router.py`、`config.py`
- 当前状态：`sensitive_words` 的命名和 hint 容易误导为安全过滤机制
- 根因：该配置仅用于情感路由（affection distribution），不拦截消息
- 修复方式：纯文档/注释修改，零行为变更
- 验证：WebUI 中 `sensitive_words` hint 明确标注"这不是内容安全过滤"

---

### Wave 2：日志完整性修复（5 项）

---

### Requirement 4: 消息处理决策提升至 INFO 级别

**User Story:** 作为运维人员，当生产环境中 Bot 对某条消息"不回复"时，我不希望排查"为什么不回复"需要开启 DEBUG 日志（会产生海量噪音），所以关键决策点在生产日志级别可见。

#### Acceptance Criteria

1. THE `gate.py:process_event()` 中所有早期返回路径（`"DUPLICATED"`、`"FILTERED"`、`"IGNORED_IMAGE"`、`"PRIVATE_WAIT"`、`"THROTTLED"`、`"repeater_echo"`）SHALL 使用 `logger.info()` 记录决策原因、`chat_id`、`trace_id`。
2. THE `gate.py:_debounce_and_judge()` 中 `judge_action` 决策（PASS/WAIT/IGNORE）SHALL 在 `debug_trace` 之外增加 `logger.info()` 记录，包含 `judge_action`、`reason`、`chat_id`。
3. THE `decision_router.py` 中 Judge 返回的 `AttentionDecision.reason` SHALL 被传递到日志中（当前仅在 `debug_trace` 中记录）。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/attention/gate.py`、`astrmai/conversation/attention/decision_router.py`
- 当前状态：所有决策日志均为 DEBUG 级别
- 根因：生产环境默认日志级别为 INFO，所有决策日志不可见
- 修复方式：在关键决策点增加 `logger.info()`，保持原有 `debug_trace` 不变
- 注意：INFO 日志应精简（一行），避免刷屏
- 验证：生产日志级别 INFO → 每条消息处理决策可见

---

### Requirement 5: 状态变更增加 INFO 日志

**User Story:** 作为调试人员，当 Bot 的情绪/精力/好感度发生变化时，我不希望完全无法追踪状态演变轨迹，所以核心状态变更在生产日志中可见。

#### Acceptance Criteria

1. THE `StateEngine.update_mood()`（`chat_state_service.py:L260`）SHALL 在 CAS 写入后使用 `logger.info()` 记录：`chat_id`、`mood_tag`、`new_value`、`delta`。
2. THE `StateEngine.consume_energy()`（`chat_state_service.py:L446`）SHALL 在非 FriendMessage 路径使用 `logger.info()` 记录：`chat_id`、消耗量、消耗后 energy。
3. THE `StateEngine.calculate_and_update_affection()` SHALL 在好感度变更时使用 `logger.info()` 记录：`user_id`、事件类型、新旧 `social_score`。
4. THE `EnergyManager.should_drop_by_energy()` SHALL 在决定 drop 时使用 `logger.info()` 记录：`chat_id`、当前 energy、drop 原因。

#### Notes / Constraints

- 涉及文件：`astrmai/state/chat_state_service.py`、`astrmai/state/energy/energy_manager.py`
- 当前状态：状态变更仅有 DEBUG 日志或完全无日志
- 根因：情绪/精力/好感度变更在生产环境不可见
- 修复方式：在状态写入点增加 `logger.info()`
- 验证：生产日志中可见 mood/energy/affection 变更轨迹

---

### Requirement 6: `logger.exception` 替换 `logger.error` — 7 处关键错误路径增加堆栈

**User Story:** 作为排查生产故障的开发者，当 Gateway 级联失败或后台任务崩溃时，我不希望只有一行 `logger.error(str)` 而无堆栈跟踪，所以所有关键错误路径提供完整堆栈以便根因分析。

#### Acceptance Criteria

1. THE `gateway_call.py` L196 和 L313 中 `logger.error(f"[Gateway] fatal model failure {model_id}: {last_error[:120]}")` SHALL 替换为 `logger.exception(f"[Gateway] fatal model failure {model_id}")`（自动附加堆栈）。
2. THE `gate.py` L285 和 L316 中 `logger.error(..., exc_info=exc)` SHALL 替换为 `logger.exception(...)`。
3. THE `lifecycle.py` L33、L145、L162 中 `logger.error(...)` SHALL 替换为 `logger.exception(...)`。
4. THE `judge.py` L548 中 Judge 失败时的 `logger.warning` SHALL 替换为 `logger.exception`（Judge 失败是异常路径，需要堆栈）。
5. THE `mood_manager.py` L242 中 mood 分析失败时的 `logger.warning` SHALL 替换为 `logger.exception`。

#### Notes / Constraints

- 涉及文件：`gateway_call.py`、`gate.py`、`lifecycle.py`、`judge.py`、`mood_manager.py`
- 当前状态：**零** `logger.exception` 使用，7 处关键错误使用 `logger.error` 无堆栈
- 根因：`logger.exception()` 与 `logger.error()` 功能相近但自动附加 `exc_info=True`
- 修复方式：逐处替换，保持日志消息文本不变
- 验证：触发各错误路径 → 日志含完整堆栈跟踪

---

### Requirement 7: LLM 调用增加延迟计时

**User Story:** 作为性能调优人员，当 Bot 响应变慢时，我不希望无法判断是 LLM API 慢还是插件内部处理慢，所以每次 LLM 调用记录端到端延迟。

#### Acceptance Criteria

1. THE `gateway_call.py:_elastic_call_result()` SHALL 在每次 LLM 调用前后使用 `time.perf_counter()` 记录延迟，并将延迟值传入 `_log_usage()`。
2. THE `_log_usage()`（`gateway_result.py`）SHALL 在 INFO 日志中新增 `latency_ms` 字段。
3. THE 延迟 SHALL 区分首 token 时间（TTFT）和总时间（如 AstrBot 框架支持流式）。若框架不支持流式延迟分离，仅记录总延迟。

#### Notes / Constraints

- 涉及文件：`gateway_call.py`、`gateway_result.py`
- 当前状态：`_log_usage()` 记录 token 数/model/provider，但无延迟
- 根因：无延迟数据 → 无法判断慢查询根因
- 修复方式：`t0 = time.perf_counter()` → LLM 调用 → `latency = (time.perf_counter() - t0) * 1000`
- 验证：INFO 日志含 `latency_ms` 字段

---

### Requirement 8: Judge prompt 从 INFO 降级为 DEBUG

**User Story:** 作为运维人员，当启用 `debug_mode` 时，我不希望 Judge 的完整 System1 prompt（1000+ 字符）在 INFO 日志中刷屏淹没真正的决策日志，所以 Judge prompt 移至 DEBUG 级别。

#### Acceptance Criteria

1. THE `judge.py` L455-461 中 `logger.info(f"[Judge] System1 prompt:\n{prompt}")` SHALL 改为 `logger.debug(...)`。
2. THE 如果用户需要查看 Judge prompt，可通过临时调整日志级别为 DEBUG 实现，不影响生产环境。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/decision/judge.py`
- 当前状态：Judge prompt 全文在 `debug_mode=True` 时以 INFO 级别输出
- 根因：INFO 日志被 Judge prompt 洪水淹没，真正的决策日志不可见
- 修复方式：`logger.info` → `logger.debug`
- 验证：`debug_mode=True` 时 Judge prompt 仅 DEBUG 级别可见

---

### Wave 3：错误恢复加固（2 项）

---

### Requirement 9: Sys2 对话入口捕获 Gateway 级联失败

**User Story:** 作为终端用户，当 LLM Gateway 所有模型耗尽时，我不希望收到 AstrBot 框架的通用异常消息（"插件执行出错"），所以 Sys2 对话入口优雅降级为兜底回复。

#### Acceptance Criteria

1. THE `plugin_facade.py:_system2_entry()`（L435-481）SHALL 在 `try/finally` 块之外增加 `except LLMCascadeFailureException` 捕获，捕获后通过 `yield event.plain_result(fallback_text)` 返回兜底消息。
2. THE 兜底消息 SHALL 使用配置的 `reply.fallback_text`（默认 `"（陷入了短暂的沉默...）"`）。
3. THE 捕获 SHALL 同时记录 `logger.exception` 以便运维排查。
4. THE 修复 SHALL NOT 改变 `_system2_entry` 的现有 `try/finally` 资源清理逻辑。

#### Notes / Constraints

- 涉及文件：`astrmai/app/plugin_facade.py` — `_system2_entry()`
- 当前状态：`_system2_entry` 使用 `try/finally` 但无 `except` — 级联失败直接抛给 AstrBot 框架
- 根因：Gateway 级联失败是预期内的降级场景，不应暴露为框架异常
- 修复方式：增加 `except LLMCascadeFailureException` 分支
- 依赖：`from ..infrastructure.gateway.gateway_exceptions import LLMCascadeFailureException`
- 验证：模拟所有模型不可用 → 用户收到兜底消息而非框架异常

---

### Requirement 10: DB 运行时查询增加异常保护

**User Story:** 作为运维人员，当 SQLite 数据库在运行时因磁盘满/锁竞争等原因抛出 `OperationalError` 时，我不希望整个消息处理链路崩溃，所以数据库操作在关键路径上有 try/except 保护。

#### Acceptance Criteria

1. THE `ChatStateService._get_state_inner()` 中的 `await self.persistence.load_chat_state(chat_id)` SHALL 增加 `except Exception` 捕获，失败时返回默认 `ChatState` 并记录 `logger.exception`。
2. THE `ChatStateService` 中所有 `save_chat_state()` 调用 SHALL 增加 `except Exception` 捕获，失败时记录 `logger.exception` 并继续（降级为内存状态）。
3. THE `UserProfileService` 中所有 `save_user_profile()` 调用 SHALL 同样增加异常保护。
4. THE 修复 SHALL NOT 在重试逻辑中吞异常（不静默丢弃），每次失败都需记录日志。

#### Notes / Constraints

- 涉及文件：`astrmai/state/chat_state_service.py`、`astrmai/state/user_profile_service.py`
- 当前状态：DB 初始化失败被吞（已修复），但运行时查询无保护
- 根因：SQLite 在运行中可能因磁盘满/锁超时抛异常，当前直接传播到调用方
- 修复方式：在 DAO 层增加 try/except + 降级逻辑
- 验证：模拟 DB 只读 → 消息处理不崩溃 → 日志含异常信息

---

### Wave 4：跨插件交互优化（2 项）

---

### Requirement 11: 外部结果嗅探增加来源白名单

**User Story:** 作为多插件环境的管理员，当 AstrMai 嗅探所有其他插件的输出并将其记录为"Bot 说过"时，我不希望第三方插件（如天气查询、翻译插件）的输出被误认为 AstrMai 的发言而污染记忆，所以外部结果嗅探仅处理已知来源。

#### Acceptance Criteria

1. THE `external_result_bridge.py:bridge_external_plugin_result()` SHALL 在注入 attention gate 之前检查结果的 `loop_source` 或 `plugin_name` 是否在可配置的白名单中。
2. THE 白名单 SHALL 通过配置项 `external_result_sources`（默认 `["astrbot_builtin"]`）控制，仅处理 AstrBot 内置管道的输出。
3. WHEN 结果的 `loop_source` 不在白名单中，THE 函数 SHALL 跳过处理并记录 `logger.debug`。
4. THE 白名单 SHALL 支持 `"*"` 通配符表示处理所有来源（保持旧行为）。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/ingress/external_result_bridge.py`、`_conf_schema.json`
- 当前状态：无来源过滤，所有非自回复的结果都被嗅探
- 根因：第三方插件的输出被 AstrMai 当作"Bot 说过"存储
- 修复方式：新增 `external_result_sources` 配置项 + 检查逻辑
- 验证：翻译插件输出 → AstrMai 不记录；AstrBot 内置管道输出 → AstrMai 记录

---

### Requirement 12: `stop_event()` 增加可配置的降级策略

**User Story:** 作为多插件环境的管理员，当 AstrMai 的 `intercept_and_notify_errors` 在检测到错误关键词时调用 `event.stop_event()`，我不希望这个操作杀死其他插件的正常消息，所以 `stop_event()` 行为可配置。

#### Acceptance Criteria

1. THE `outbound_error_policy.py:intercept_outbound_error()` SHALL 在调用 `event.stop_event()` 之前检查配置项 `error_interception_mode`。
2. THE `error_interception_mode` SHALL 支持三个值：`"block_and_stop"`（当前行为：设 None + stop_event）、`"block_only"`（仅设 None，不 stop_event）、`"log_only"`（仅记录日志，不修改结果）。
3. THE 默认值 SHALL 为 `"block_only"`（降低对其他插件的影响，仅阻止错误消息发送但不杀死事件）。
4. THE 配置项 SHALL 同步在 `_conf_schema.json` 中增加 `error_interception_mode` 的 `options` 枚举。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/execution/outbound_error_policy.py`、`_conf_schema.json`、`config.py`
- 当前状态：错误拦截始终调用 `event.stop_event()`，无配置余地
- 根因：`stop_event()` 阻止所有下游插件处理该事件
- 修复方式：增加 `error_interception_mode` 配置项 + 条件逻辑
- 验证：`log_only` 模式 → 错误消息仅记录日志，不阻止其他插件

---

## Out of Scope

- Round 1–5 已修复的 43 项缺陷
- Critical 级缺陷（5 项，已独立 Spec）
- 🟡/🟢 级发现（20 项）
- 第三方内容审核 API 集成（H1 第一期仅本地关键词）
- 新功能开发、依赖升级

## High-Risk Confirmation List

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| HK1 | H1 关键词检测可能误杀 → 默认 `False`（用户主动开启） | 🟡 | 关键词列表保守 |
| HK2 | H2 注入检测可能误拒 XML 标签合法内容 | 🟡 | 仅检测明确注入模式 |
| HK3 | H4 INFO 日志量增加 10-50x | 🟡 | 每条精简为一行 |
| HK4 | H11 白名单默认仅 `astrbot_builtin` → 行为改变 | 🟡 | 提供 `"*"` 通配符 |
| HK5 | H12 默认 `block_only` → 下游插件行为改变 | 🟡 | 提供 `block_and_stop` 选项 |

## Dependency Map

```
全部 12 项涉及不同文件集，可全并行执行。
H1-H3 (内容安全) || H4-H8 (日志) || H9-H10 (错误恢复) || H11-H12 (跨插件)
```

## Verification Strategy

| 验证层 | 方式 | 覆盖 |
|--------|-----|:--:|
| 单元 | `output_guard.py` / `memory_write_service.py` / `gate.py` / `judge.py` 测试 | H1-H8 |
| 集成 | 模拟级联失败 → 兜底消息 / DB 只读 → 不崩溃 | H9-H10 |
| 集成 | 翻译插件输出 → 不记录 / log_only 模式验证 | H11-H12 |
| LSP | `lsp_diagnostics` 全部变更文件 | ALL |
| 全量 | `pytest tests/ -q --tb=short` | ALL |

---

> **写入 4 完成。** `requirements.md` 全部 12 条需求已写入。可进入 Phase 2（设计文档）。



