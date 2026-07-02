# Requirements Document — AstrMai 审计后加固

> Spec ID: `astrmai-post-audit-20260629` | Type: `hardening`
> 基于第六轮深度审查（6 维度 × ~119 项发现）中已完成的 5 个修复阶段，本 Spec 覆盖剩余未修复的系统性缺陷。
>
> **本轮仅产出 requirements.md，不进入后续阶段。**

---

## Introduction

本 Spec 为 AstrMai（AstrBot 聊天插件）第六轮审计后的残留缺陷制定加固需求文档。

### 背景

第六轮审查共发现 ~119 项缺陷，已完成以下修复（**不在本 Spec 范围**）：

| 已完成阶段 | 改动 | 状态 |
|-----------|------|:--:|
| Phase 1 | `main.py` 3 个事件 Hook 加 try/except 保护 | ✅ |
| Phase 2 | `message_entry.py` 7 处 `event.stop_event()` 缺失修复 | ✅ |
| Phase 3 | `safe_create_task()` 替换 16 处裸 `asyncio.create_task()` 调用 | ✅ |
| Phase 4 | `EventBus.stop()` + `persistence.dispose()` + 3 处无限 dict 清理 | ✅ |
| Phase 5 | `time.time()` → `time.monotonic()` Stage 1（30 处纯内存计时替换） | ✅ |

### 范围覆盖

本 Spec 聚焦**剩余未修复**的 5 类系统性缺陷：

1. **静默异常吞没**：~30 处 `except Exception:` 无日志记录，异常静默丢失
2. **配置模型/模式缺口**：4 个配置字段在 `_conf_schema.json` 中定义但在 `config.py` 模型中缺失
3. **时间源 DB 边界混用**：~18 处 `time.time()` 与数据库持久化时间戳交叉比较
4. **无限增长集合**：~7 处 dict/set 无内存上限或 TTL 清理
5. **测试基础设施**：~30 个测试 mock 需同步更新（`time.time` → `monotonic`）

### 明确排除

- **安全漏洞**：API 授权、硬编码密码 — 插件运行于 AstrBot 受信环境内，不在本 Spec 范围
- **AstrBot 框架配置**：`cmd_config.json` 中的密码哈希 — 属框架层，不在插件范围
- **新功能开发**：本 Spec 仅做修复加固，不引入新特性
- **架构重构**：不改动模块划分、接口契约或数据流拓扑
- **已完成修复的回归**：Phase 1-5 已修内容不在此重复

---

## Glossary

| 术语 | 定义 |
|------|------|
| **AstrBot** | 插件宿主框架，提供 Star 类、事件系统、LLM 调用等基础设施 |
| **Star** | AstrBot 插件基类，所有插件必须继承 |
| **Hook** | AstrBot 事件钩子（`on_llm_request`、`on_decorating_result` 等），在框架事件生命周期中触发 |
| **Handler** | AstrBot 指令/消息处理器，使用 `@filter.command` 等装饰器注册 |
| **`event.stop_event()`** | 阻断事件下游处理的 AstrBot API |
| **`safe_create_task()`** | 本插件新增的 `asyncio.create_task()` 封装，自动附加异常日志回调 |
| **`time.monotonic()`** | Python 单调时钟，不受 NTP 对时影响，适用于纯内存计时 |
| **`time.time()`** | Unix 墙上时钟，受 NTP 对时影响，适用于持久化/显示时间戳 |
| **DB 边界混用** | `time.time()` 值与数据库持久化的 `time.time()` 值交叉比较的场景 |
| **Pydantic Config** | `config.py` 中使用 pydantic `BaseModel` 定义的配置类 |
| **`_conf_schema.json`** | AstrBot 插件配置 UI 的 JSON Schema 描述文件 |
| **静默异常吞没** | `except Exception: pass` 或无日志记录的异常捕获，异常信息永久丢失 |
| **EARS** | Easy Approach to Requirements Syntax，本 Spec 全部 AC 遵循的句式 |
| **P0/P1/P2** | 优先级：P0 阻断级（聊天功能静默失败）、P1 高风险（数据完整性/可观测性）、P2 中优先级（长期稳定性/可维护性） |

---

## Wave 划分框架

按影响面与修复难度将需求分为 5 个 Wave，严格串行推进：

```
Wave 1 (P0) ──→ Wave 2 (P1) ──→ Wave 3 (P1) ──→ Wave 4 (P2) ──→ Wave 5 (P2)
  异常日志         配置同步          时间源修复         集合清理          测试更新
  (R1-R4)         (R5-R8)         (R9-R11)         (R12-R15)         (R16-R18)
```

| Wave | 优先级 | 主题 | 需求数 | 影响文件 | 风险 |
|------|:--:|------|:--:|------|:--:|
| **Wave 1** | 🔴 P0 | 静默异常日志补全 | 4 | ~18 | 🟢 低 — 纯加日志 |
| **Wave 2** | 🟡 P1 | 配置模型/模式同步 | 4 | 2 | 🟡 中 — 涉及 pydantic 模型变更 |
| **Wave 3** | 🟡 P1 | 时间源 DB 边界修复 | 3 | ~10 | 🔴 高 — 需选策略，影响冷却/去重/召回 |
| **Wave 4** | 🟢 P2 | 无限集合清理 | 4 | ~5 | 🟢 低 — 加 TTL/上限即可 |
| **Wave 5** | 🟢 P2 | 测试 mock 同步 | 3 | ~5 | 🟢 低 — 仅测试代码 |

---

---

## Requirements

### Wave 1：🔴 P0 — 静默异常日志补全（R1–R4）

> **影响面**：聊天链路中异常静默吞没 → 故障排查无迹可循。  
> **策略**：在所有 `except Exception:` 无日志处补 `logger.exception()` 或 `logger.warning()`。

---

### Requirement 1: 批量补全静默异常日志

**User Story:** 作为运维者，当聊天链路出现异常时，我希望在日志中看到完整的异常堆栈，以便快速定位故障根因。

#### Acceptance Criteria

1. THE 系统 SHALL 在所有 `except Exception:` 块中输出 `logger.exception()` 或 `logger.warning()`（视严重程度）。
2. WHERE `except Exception:` 后紧跟 `pass` 或空块体，THE 系统 SHALL 至少输出 `logger.warning()`。
3. THE 系统 SHALL NOT 修改已有的 `logger.debug()` / `logger.error()` 块（仅补缺失）。
4. WHEN 异常块在 WebUI 路径（`webui/backend/`）中，THE 系统 SHALL 使用 `logger.exception()` 以确保管理员可见。

#### Notes / Constraints

- 涉及文件（按模块分组）：
  - **gate.py**: 4 处（`sensors.is_wakeup_signal`、`is_command`、`should_process_message`、`state_engine.get`）
  - **executor.py**: 3 处（`sanitized_event`、`failure_kind_classifier`、`temp_file_cleanup`）
  - **context_compaction.py**: 4 处（`bootstrap_snapshot`、`get_sender_id`、2× evaluate skip）
  - **vision_binding.py**: 2 处（`file_to_base64()`、`file_open`）
  - **gateway_lane.py**: 1 处（`json_serialization`）
  - **database_profile_relation.py**: 2 处（`UserProfile()` 构造）
  - **gateway_result.py**: 2 处（`json.loads` 提取）
  - **instant_memory_gate.py / memory_engine.py / memory_turn_pipeline.py / summarizer.py / topic_summarizer.py**: ~12 处
  - **chat_state_service.py**: 5 处
  - **private_chat_manager.py**: 2 处
  - **mood_manager.py**: 1 处
  - **event_utils.py**: 1 处
  - **cron_agent.py**: 1 处
  - **persona_summarizer.py**: 8 处
- 总计约 48 处，每处改动 1–2 行，机械性操作。
- **优先级依据**：`gate.py` 和 `persona_summarizer.py` 的静默异常直接阻断聊天响应，应在 Wave 1 首批修复。

---

### Requirement 2: Gateway 层异常日志补全

**User Story:** 作为插件开发者，当 Gateway 层（gate.py）的传感器检查静默失败时，我希望知道是哪个检查、为什么失败，以避免错误地允许/拒绝消息。

#### Acceptance Criteria

1. WHEN `gate.py:155` 的 `sensors.is_wakeup_signal()` 抛出异常，THE 系统 SHALL 记录 `logger.warning()` 含 `chat_id`。
2. WHEN `gate.py:517` 的 `sensors.is_command()` 抛出异常，THE 系统 SHALL 记录 `logger.warning()` 含消息文本截断。
3. WHEN `gate.py:522` 的 `sensors.should_process_message()` 抛出异常，THE 系统 SHALL 记录 `logger.warning()` 并说明降级为 `True`。
4. WHEN `gate.py:682` 的 `state_engine.get()` 抛出异常，THE 系统 SHALL 记录 `logger.warning()` 含 `chat_id`。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/attention/gate.py`
- 上述 4 处均为消息准入/唤醒决策链路，静默失败会导致消息误放行或误拒绝。

---

### Requirement 3: Persona Summarizer 8 处异常日志补全

**User Story:** 作为调试者，当人格摘要生成链路（persona_summarizer.py）中任意切片失败时，我希望看到具体是哪个切片步骤失败，而不是无声退化。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `persona_summarizer.py` 中 8 处 `except Exception:` 块各补 `logger.exception()`。
2. WHEN 补日志后，THE 日志消息 SHALL 包含当前处理的 `chat_id` 和切片步骤标识（如 `"expressiveness_ratio"`、`"response_style"` 等）。
3. THE 系统 SHALL NOT 改变现有降级逻辑（返回默认值/跳过该切片）。

#### Notes / Constraints

- 涉及文件：`astrmai/memory/persona/persona_summarizer.py`
- 8 处分布在 lines ~457, 494, 526, 558, 590, 620, 651, 683
- 人格摘要影响聊天质量（角色一致性），但非阻断性 — 降级后仍可继续聊天。

---

### Requirement 4: 内存/状态管线异常日志补全

**User Story:** 作为运维者，当记忆检索、状态管理、定时任务中发生异常时，我希望日志中能追溯到具体步骤，而非看到"功能突然不工作"。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `chat_state_service.py` 5 处 `except Exception:` 块各补 `logger.exception()` 含 `chat_id`。
2. THE 系统 SHALL 在 `memory_engine.py` / `memory_turn_pipeline.py` / `summarizer.py` / `topic_summarizer.py` 等记忆管线文件中 ~12 处各补 `logger.exception()`。
3. THE 系统 SHALL 在 `cron_agent.py:86` 补 `logger.exception()` 含 `job_id`。
4. THE 系统 SHALL 在 `database_profile_relation.py` 2 处补 `logger.warning()` 含原始 `profile_data` 截断（≤ 200 字符，避免日志膨胀）。

#### Notes / Constraints

- 涉及文件：~10 个，分布较广
- 记忆管线异常降级后不影响聊天基本功能，但会导致记忆质量退化。

---

### Wave 2：🟡 P1 — 配置模型/模式同步（R5–R8）

> **影响面**：用户 WebUI 中可见的配置项无法生效 → 功能退化但用户不知。  
> **策略**：对齐 `_conf_schema.json` ↔ `config.py` Pydantic 模型，补缺失字段。

---

### Requirement 5: `enable_token_estimator` 加入 ConversationConfig

**User Story:** 作为插件管理员，当我在 WebUI 中开启 Token 估算时，我希望该配置能实际传递到运行时，使上下文压缩基于 Token 估算值而非字符数。

#### Acceptance Criteria

1. THE `ConversationConfig` 模型（`config.py`）SHALL 新增 `enable_token_estimator: bool = False` 字段。
2. WHEN 配置通过 WebUI 保存后，THE 系统 SHALL 通过 `runtime.config.conversation.enable_token_estimator` 读取到用户设置的值。
3. THE 现有代码中的 `getattr(..., "enable_token_estimator", False)` 回退路径 SHALL 保持不变作为兼容层。
4. THE `_conf_schema.json` 中的 `enable_token_estimator` 字段定义 SHALL 保持不变（已存在）。

#### Notes / Constraints

- 涉及文件：`config.py`（+1 字段），`_conf_schema.json`（已存在，不修改）
- 当前状态：`_conf_schema.json:176-181` 定义了该字段，但 `config.py:149` 的 `ConversationConfig` 无此字段
- 消费方：`shared/constants/defaults.py:100` 通过 `getattr` 回退读取

---

### Requirement 6: `review_runner_interval_sec` + `review_runner_min_interval_sec` 加入 EvolutionConfig

**User Story:** 作为插件管理员，我希望调整表情治理审查的运行间隔，以平衡审查及时性与 LLM 调用成本。

#### Acceptance Criteria

1. THE `EvolutionConfig` 模型（`config.py`）SHALL 新增 `review_runner_interval_sec: int = 60` 字段。
2. THE `EvolutionConfig` 模型 SHALL 新增 `review_runner_min_interval_sec: int = 45` 字段。
3. THE `_conf_schema.json` 中 `evolution.items` 块 SHALL 新增这两个字段的定义（含 `hint`、`default`）。
4. WHEN 配置保存后，THE 消费方（`bootstrap.py:446`、`expression_auto_check_task.py:38`、`jargon_auto_check_task.py:79`）SHALL 能通过 `runtime.config.evolution.review_runner_interval_sec` 读取。

#### Notes / Constraints

- 涉及文件：`config.py`（+2 字段），`_conf_schema.json`（+2 字段定义）
- 当前消费方使用 `getattr(..., "review_runner_interval_sec", 60)` 回退 → 加入模型后回退永不触发
- 字段类型：`int`，范围建议 30–600 秒

---

### Requirement 7: `auto_recall_probability` 加入 MemoryConfig

**User Story:** 作为插件管理员，我希望调整自动记忆召回的概率，以控制记忆注入聊天上下文的频率。

#### Acceptance Criteria

1. THE `MemoryConfig` 模型（`config.py`）SHALL 新增 `auto_recall_probability: float = 0.0` 字段。
2. THE `_conf_schema.json` 中 `memory.items` 块 SHALL 新增该字段定义（含 `hint`、`default`）。
3. WHEN 配置保存后，THE 消费方（`context_engine.py:517`）SHALL 能通过 `runtime.config.memory.auto_recall_probability` 读取。

#### Notes / Constraints

- 涉及文件：`config.py`（+1 字段），`_conf_schema.json`（+1 字段定义）
- 当前消费方使用 `getattr(..., "auto_recall_probability", 0.0)` → 永久回退为 0（禁用）
- 字段类型：`float`，范围 0.0–1.0

---

### Requirement 8: `_conf_schema.json` 与 `config.py` 全局对齐验证

**User Story:** 作为 Spec 执行者，在完成 R5–R7 后，我希望确认两个配置定义文件之间不再有遗漏字段。

#### Acceptance Criteria

1. THE 系统 SHALL 遍历 `_conf_schema.json` 中所有 `items` 下的字段名，逐一确认在 `config.py` 对应 Pydantic 模型中有同名字段。
2. THE 系统 SHALL 遍历 `config.py` 中所有 Pydantic 模型字段，逐一确认在 `_conf_schema.json` 中有对应定义。
3. IF 发现任何不匹配，THEN THE 系统 SHALL 在验证报告中列出差异项。

#### Notes / Constraints

- 手动对齐流程，生成差异清单
- 不涉及代码修改 — 验证产出物为差异报告

---

### Wave 3：🟡 P1 — 时间源 DB 边界修复（R9–R11）

> **影响面**：`time.time()` 与 DB 持久化时间戳交叉比较 → NTP 对时导致计时跳变。  
> **策略**：采用 **max-guard 策略**（Option A）— 在计算 delta 时用 `max(0, delta)` 钳制，防御负值。

---

### Requirement 9: DB 查询截止时间保护

**User Story:** 作为插件开发者，当系统用 `time.time()` 计算 DB 查询的 `cutoff_timestamp` 时，若发生 NTP 回拨，查询截止时间可能早于数据写入时间，导致数据丢失。

#### Acceptance Criteria

1. WHEN 计算 DB 查询截止时间（`cutoff = time.time() - max_age_seconds`），THE 系统 SHALL 在查询前记录 `max(cutoff, 0)` 钳制。
2. THE 系统 SHALL 在以下 4 个站点应用此保护：
   - `database_service.py:146` — `cutoff_timestamp = time.time() - max_age_seconds`
   - `memory_retrieval_service.py:353` — `time.time() - item.created_at`
   - `session_memory_summarizer.py:43` — `cutoff_time = time.time() - days * 86400`
   - `v2_store.py:1086,1135` — `cutoff = self._now() - older_than_seconds`
3. WHERE `cutoff` 计算结果为负值（NTP 回拨），THE 系统 SHALL 输出 `logger.warning()` 一次并钳制为 `0`。

#### Notes / Constraints

- 涉及文件：4 个
- **不替换** `time.time()` → `time.monotonic()`（DB 时间戳使用 Unix epoch，epoch 不同）
- 采用 max-guard 策略：`delta = max(0, now - stored_ts)`

---

### Requirement 10: 聊天链路时间比较保护

**User Story:** 当 Judge、CognitiveLoop、ReplyFreshness 等聊天决策模块用 `time.time()` 与事件/状态时间戳比较时，NTP 回拨可能导致消息误判为"过期"或"新鲜"。

#### Acceptance Criteria

1. WHEN 计算历史消息新鲜度（`judge.py:191`），THE 系统 SHALL 用 `max(0, now - timestamp)` 钳制 delta。
2. WHEN 计算认知循环空闲时间（`cognitive_loop.py:682`），THE 系统 SHALL 用 `max(0, time.time() - last_reply_time)` 钳制。
3. WHEN 计算回复新鲜度（`reply_freshness.py:55`），THE 系统 SHALL 用 `max(0, time.time() - event_ts)` 钳制。
4. THE 系统 SHALL NOT 替换上述站点中的 `time.time()` 为 `time.monotonic()`。

#### Notes / Constraints

- 涉及文件：`judge.py`、`cognitive_loop.py`、`reply_freshness.py`
- 这些站点的时间戳来源为事件时间（`event.timestamp`）或状态持久化时间（`last_reply_time`）— 均为 Unix epoch

---

### Requirement 11: 状态存储时间源一致性

**User Story:** 当关系引擎（relationship_engine）、心情衰减（mood_decay）、聊天状态（chat_state_service）等模块在写入和读取状态时间戳时使用不同的时间源，我希望能明确标注这些混合站点，防止未来维护者错误替换。

#### Acceptance Criteria

1. THE 系统 SHALL 在以下站点添加 `# ponytail: wall-clock timestamp, mixed with DB values — do NOT replace with monotonic` 注释：
   - `relationship_engine.py:86-88` — `first_seen` / `last_interaction` / `last_decay_time`
   - `mood_decay.py:8` — `now = time.time()` 与 `state.last_reply_time` 比较
   - `chat_state_service.py:90,116` — `now = time.time()` 用于 state TTL
   - `user_profile_service.py:109,116` — `now = time.time()` 用于 profile touch
   - `promotion_engine.py:79` — `now_ts = float(now or time.time())`
   - `hybrid_retriever.py:79` — 时间衰减与 DB `create_time` 比较
   - `memory_scoring.py:64` — 时间评分与 DB 时间戳比较
2. THE 系统 SHALL NOT 替换上述站点中的 `time.time()`。

#### Notes / Constraints

- 涉及文件：~7 个
- 纯文档/注释改动，不影响运行逻辑
- 注释模板：`# ponytail: wall-clock, mixed with DB — keep time.time()`

---

### Wave 4：🟢 P2 — 无限集合清理（R12–R15）

> **影响面**：长期运行后内存持续增长 → 最终 OOM。  
> **策略**：添加 TTL 清理或 max-size 上限。

---

### Requirement 12: `gate._proactive_injection_lock` 清理

**User Story:** 作为运维者，当 bot 运行数周后与成千上万个唯一 chat_id 交互时，我希望不再使用的 chat_id 的 `asyncio.Lock` 能被释放。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `gate.py` 的 `_prune_stale_focus_pools()` 方法中同步清理 `_proactive_injection_lock` 的对应键。
2. WHEN `focus_pools` 中的 `chat_id` 因超时被移除，THE 系统 SHALL 同时 `pop` 对应的 `_proactive_injection_lock[chat_id]`。
3. THE 清理逻辑 SHALL 与 `_prune_stale_focus_pools()` 使用相同的 TTL（24h）和相同的调度间隔（300s）。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/attention/gate.py`
- 参考已实现的 `_last_focus_pool_prune` 守卫模式
- 改动量：~4 行

---

### Requirement 13: `chat_state_service._chat_locks` 清理

**User Story:** 当 bot 长期运行后，我希望 `chat_state_service` 中的 `asyncio.Lock` 字典能被周期性清理。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `chat_state_service.py` 中添加 `_prune_stale_locks()` 方法，遍历 `_chat_locks` 并移除引用计数为 0（当前无等待者）的锁。
2. WHERE `_chat_locks` 字典大小超过 500，THE 系统 SHALL 触发清理。
3. THE 清理 SHALL 在每次 `_get_lock()` 调用时以 `time.monotonic()` 为守卫（每 300s 最多执行一次）。

#### Notes / Constraints

- 涉及文件：`astrmai/state/chat_state_service.py`
- `asyncio.Lock` 无内置"是否有等待者"API — 需要用 `try: lock.acquire() + lock.release()` 探测，或用弱引用

---

### Requirement 14: `memory_engine._disabled_cognitive_feedback_keys` TTL

**User Story:** 当 bot 长期运行后禁用大量认知反馈信号时，我希望能自动清理不再相关的禁用记录。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `memory_engine.py` 中将 `_disabled_cognitive_feedback_keys` 从 `set` 重构为 `dict[str, float]`（key → 禁用时间戳）。
2. THE 系统 SHALL 在每次 `disable_cognitive_feedback()` 调用时清理超过 7 天的条目。
3. THE 成员检查逻辑 SHALL 保持 O(1)（`key in dict`）。

#### Notes / Constraints

- 涉及文件：`astrmai/memory/services/memory_engine.py`
- 当前结构：`set[tuple[str, str, str, str]]`
- 需同步更新 `_cognitive_feedback_key()` 的返回值类型

---

### Requirement 15: `private_chat_manager._chat_to_user` 清理

**User Story:** 当私聊会话关闭后，我希望 `_chat_to_user` 映射能被同步清理。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `private_chat_manager.py` 的 `cleanup_stale_sessions()` 方法中同步清理 `_chat_to_user` 中对应的键。
2. WHEN `close_session(user_id)` 被调用，THE 系统 SHALL 同步清理 `_chat_to_user` 中的反向映射。

#### Notes / Constraints

- 涉及文件：`astrmai/state/private_chat/private_chat_manager.py`
- 改动量：~3 行
- 当前 `cleanup_stale_sessions()` 只清理 `_sessions` 不清理 `_chat_to_user`

---

### Wave 5：🟢 P2 — 测试基础设施（R16–R18）

> **影响面**：Phase 5 的 `time.time` → `monotonic` 替换导致 ~30 个测试因 mock 失效而失败。  
> **策略**：同步更新测试 mock。

---

### Requirement 16: 测试 mock 同步 `time.monotonic`

**User Story:** 当代码中使用 `time.monotonic()` 替代 `time.time()` 后，我希望对应的测试也能正确 mock 新函数。

#### Acceptance Criteria

1. THE 系统 SHALL 排查所有使用 `@patch("time.time")` 或 `mock.patch("time.time")` 的测试文件。
2. WHEN 被 mock 的代码已改为使用 `time.monotonic()`，THE 对应的 `@patch` 装饰器 SHALL 同步改为 `@patch("time.monotonic")`。
3. THE 同步后的测试 SHALL 全部通过（≥ 前值 836 passed）。
4. THE 系统 SHALL NOT 修改测试的业务逻辑（仅更新 mock target）。

#### Notes / Constraints

- 涉及文件：约 5–8 个测试文件（含 `test_group_dialogue_store_and_compaction.py`、`test_proactive_scheduler_refactor.py` 等）
- 预估 30 处 `@patch` 需更新
- 可在 CI 中用 `grep -r "time\.time" tests/` 定位全部 mock 点

---

### Requirement 17: `safe_create_task` 单元测试

**User Story:** 当 `safe_create_task()` 被广泛使用后，我希望能有测试覆盖其异常日志行为，确保 fire-and-forget 任务失败时能正确记录。

#### Acceptance Criteria

1. THE 系统 SHALL 添加测试验证：WHEN `safe_create_task()` 包装的协程抛出异常，THEN `logger.error` 被调用一次。
2. THE 系统 SHALL 添加测试验证：WHEN `safe_create_task()` 包装的协程正常完成，THEN `logger.error` 不被调用。
3. THE 系统 SHALL 添加测试验证：`safe_create_task()` 返回的是 `asyncio.Task` 对象。

#### Notes / Constraints

- 涉及文件：新建 `tests/unit/shared/test_safe_create_task.py`
- 使用 `mock.patch("astrmai.shared.helpers.plugin_helpers._astrbot_logger")` 验证日志行为

---

### Requirement 18: 修复后 Hook 测试回归

**User Story:** 当 `main.py` 中 3 个事件 Hook 加了 try/except 后，我希望确认 Hook 仍能正确执行其核心逻辑（注入 session、嗅探外部结果、拦截错误）。

#### Acceptance Criteria

1. THE `test_main_reverse_session_hook_refactor.py` 中的测试 SHALL 确认 `inject_gemini_reverse_session` Hook 在正常输入下仍能正确注入 system_prompt。
2. IF Hook 内部逻辑抛出异常，THEN THE Hook SHALL NOT 向框架层传播异常（try/except 生效）。
3. THE 测试 SHALL 覆盖 `sniff_external_plugin_results` 和 `intercept_and_notify_errors` 两个 Hook 的异常路径。

#### Notes / Constraints

- 涉及文件：`tests/test_main_reverse_session_hook_refactor.py`（可能需修正导入路径）、新建 `tests/unit/test_hook_error_resilience.py`
- `test_main_reverse_session_hook_refactor.py` 当前因相对导入问题失败 — 需修正测试的导入路径

---

---

## Out of Scope

以下内容明确不在本 Spec 范围内：

| 排除项 | 理由 |
|--------|------|
| **安全漏洞修复**（API 授权绕过、硬编码密码） | 插件运行于 AstrBot 受信环境，安全边界由宿主框架保证 |
| **`cmd_config.json` 中的 MD5 密码哈希** | 属 AstrBot 框架层配置，不在插件范围内 |
| **新功能开发** | 本 Spec 为纯加固，不引入新特性 |
| **架构重构** | 不改变模块划分、接口契约、数据流拓扑 |
| **DB schema 变更** | 不新增/删除数据库表或列 |
| **`proactive/rhythm.py` 的时间源替换** | 使用 `time.localtime()` 必须保留 `time.time()` |
| **Phase 1-5 已修复内容的回归修复** | 已修内容不在此重复 |
| **AstrBot 框架 API 版本兼容性** | librarian agent 超时未完成，独立跟进 |

---

## High-Risk Confirmation List

| # | 风险项 | 等级 | 缓解措施 |
|---|--------|:--:|------|
| HR-1 | Wave 2 配置模型变更后，旧的 `getattr` 回退路径与新的模型属性路径同时存在 → 双重读取逻辑需验证不冲突 | 🔴 | R8 全局对齐验证 |
| HR-2 | Wave 3 `max(0, delta)` 钳制可能掩盖真实的时钟回拨问题（NTP 回拨 > 几秒时，数据新鲜度判断全失效） | 🔴 | 在 `logger.warning()` 中加入 `delta` 值，运维可监控 |
| HR-3 | Wave 3 涉及的 4 个 DB 查询站点如果漏改一个，可能导致该站点在 NTP 回拨时数据不一致 | 🟡 | 用 `grep "time.time()"` 遍历全量确认无遗漏 |
| HR-4 | Wave 4 的 `chat_state_service._chat_locks` 清理可能误删正在使用的锁（探测方法不当） | 🟡 | 清理前检查锁的 `_waiters` 属性（`asyncio.Lock` 内部字段，非公开 API）→ 备选方案：不做主动清理，改为 LRU 上限 |
| HR-5 | Wave 5 测试 mock 同步可能遗漏 `unittest.mock.patch` 以外的 mock 方式（如 `monkeypatch.setattr`、手动替换） | 🟡 | `grep -r "time\.time\|monotonic" tests/` 全量扫描 |

---

## Dependency Map

```
                    ┌─────────────┐
                    │  Wave 1 (P0) │  异常日志补全 (R1–R4)
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
       ┌──────────┐ ┌──────────┐ ┌──────────┐
       │ Wave 2   │ │ Wave 3   │ │ Wave 5   │
       │ (P1)     │ │ (P1)     │ │ (P2)     │
       │ R5–R8    │ │ R9–R11   │ │ R16–R18  │
       │ 配置同步  │ │ 时间源    │ │ 测试更新  │
       └────┬─────┘ └────┬─────┘ └──────────┘
            │            │
            └─────┬──────┘
                  ▼
           ┌──────────┐
           │ Wave 4   │
           │ (P2)     │
           │ R12–R15  │
           │ 集合清理  │
           └──────────┘
```

- **Wave 1** 是基础 — 补日志后，Wave 2/3 的变更效果可观测。
- **Wave 2 和 Wave 3 独立并行** — 配置同步不碰运行时逻辑，时间源修复不碰配置。
- **Wave 5** 可与 Wave 2/3 并行（仅碰测试文件）。
- **Wave 4** 依赖 Wave 3（`_prune_stale_focus_pools` 已在 Phase 4 实现，Wave 4 在其上扩展清理其他 dict）。

---

## Verification Strategy

| 验证层 | 命令/方式 | 覆盖需求 |
|--------|----------|---------|
| **LSP 诊断** | `lsp_diagnostics` 对每个变更文件 | R1–R15（全量） |
| **语法检查** | `python -c "import astrmai"` 无异常 | R1–R15 |
| **单元测试** | `pytest tests/ -q` ≥ 836 passed | R16–R18 |
| **配置一致性** | 人工对比 `_conf_schema.json` ↔ `config.py` | R8 |
| **日志输出** | `grep -c "logger.exception\|logger.warning"` 变更前后对比 | R1–R4 |
| **导入验证** | `python -c "from astrmai.config import AstrMaiConfig; c = AstrMaiConfig()"` | R5–R7 |
| **时间钳制** | 人工注入负 delta 验证 `max(0, delta)` 生效 | R9–R11 |
| **内存增长** | 长期运行后 `sys.getsizeof()` 对比清理前后 | R12–R15 |

### 回归基线

- 当前：**805/864 passed, 59 failed**（含测试 mock 失效）
- Wave 5 完成后目标：**≥ 836/864 passed**
- 预存 `SyntaxError`（`test_runtime_contracts_migrated.py`）不阻塞 — 独立修复

---
_（requirements.md — 写入 3/3 完成。18 条需求 × 5 Wave，Spec Phase 1 结束。）_
