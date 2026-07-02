# Requirements Document

## Introduction

本 Spec 为「AstrMai 多Agent对话插件」中**深度审计发现的 9 个 HIGH 严重等级运行时缺陷**制定修复需求文档。范围覆盖：

- **R6**: `on_llm_request` 每请求修改 `system_prompt` 破坏提供商缓存
- **R7**: EventBus `publish()` 和 `_worker_health_check()` 创建的后台任务未追踪
- **R8**: EventBus 健康检查误数分发任务为 worker，worker 死亡未被检测
- **R9**: EventBus 队列满时静默丢弃事件
- **R10**: `message_entry.py` 注意力分发失败时静默丢弃用户消息
- **R11**: `group_dialogue_store.py` 中 `str(None)` 变为键 `"None"` 导致数据损坏
- **R12**: `bootstrap.py` ProactiveTask 创建失败时主动功能静默禁用
- **R13**: `gate.py` 传感器过滤器 `except Exception: pass` 静默吞错
- **R14**: `main.py` 3 个钩子中 `except Exception: pass` 完全静默

明确不在本 Spec 范围：CRITICAL 级别缺陷（已由 `critical-bugfixes-v1` 覆盖）、MEDIUM/LOW 级别缺陷、功能新增。

## Glossary

- **EventBus**：`infrastructure/runtime/event_bus.py` 中的单例发布-订阅总线。含 3 个 MPSC worker、健康检查、有界队列(maxsize=1000)。
- **Provider Cache**：AstrBot 将 `system_prompt` 作为提供商（Gemini/OpenAI/Anthropic）服务端提示词缓存的关键输入。每次修改 `system_prompt` 破坏缓存的确定性哈希 → 成本激增 7-20 倍。
- **`on_llm_request` Hook**：AstrBot 每次 LLM 请求前触发。在该钩子中修改 `request.system_prompt` 每次都破坏缓存。
- **`_background_tasks`**：`EventBus._background_tasks` 集合，同时存储 worker 任务和分发任务。`stop()` 遍历该集合取消所有任务，健康检查也基于该集合计数。
- **Ghost Sentinel**：`[ASTRMAI_GHOST_LOCK]` 字符串，用于通知 AstrBot 框架抑制默认 LLM。
- **Focus Thread**：`group_dialogue_store.py` 中的 `_threads` 字典，key 为 `chat_id` 字符串。
- **EARS**：Easy Approach to Requirements Syntax。

## Requirements

本 Spec 9 条需求均为 **HIGH 严重等级**，组织为 2 个 Wave。

| Wave | # | 需求 | 影响 |
|------|---|------|------|
| Wave 1 | R6–R10 | 核心链路缺陷 | 提供商成本/事件丢失/消息丢弃 |
| Wave 2 | R11–R14 | 防御性修复 | 数据损坏/功能禁用/调试盲区 |

### Wave 1：核心链路缺陷（R6–R10）

---

### Requirement 6: `on_llm_request` 破坏提供商缓存修复

**User Story:** 作为使用 Gemini 反向代理的用户，我希望 AstrMai 在 `on_llm_request` 中注入 reverse-session 块时不破坏提供商的提示词缓存，所以每次 LLM 请求不会因为 `system_prompt` 被重新赋值而额外消耗 7-20 倍的输入 token。

#### Acceptance Criteria

1. WHEN `inject_gemini_reverse_session` 钩子被触发且 provider 为 gemini-reverse 类型，THE 注入 SHALL 使用 `request.extra_user_content_parts` 或等效机制（而非直接赋值 `request.system_prompt`）来附加 reverse-session 块。
2. WHEN provider 为**非** gemini-reverse 类型，THE `on_llm_request` 钩子 SHALL NOT 修改 `request.system_prompt`（包括重新赋值为相同值）。
3. THE 修复 SHALL 确保 reverse-session 块的内容仍然能被下游（Gemini 反向代理）正确解析。
4. WHERE `request` 对象不支持 `extra_user_content_parts`（AstrBot < v4.24.0），THE 修复 SHALL 回退到当前的 `system_prompt` 赋值行为，并记录 `logger.debug` 提示升级。
5. THE `post_hook_system_hash` 等 trace 字段 SHALL 继续正确计算（基于实际注入后的上下文）。

#### Notes / Constraints

- **根因**：`main.py:97-105` 每次 `on_llm_request` 都无条件执行 `request.system_prompt = maybe_attach_reverse_session_block(...)`。即使 `maybe_attach_reverse_session_block` 对非 Gemini 提供者返回原字符串不变，**赋值操作本身**已破坏 AstrBot 的缓存哈希。
- **AstrBot Skill §7.1.1 警告**：「不要用 `req.system_prompt += ...` 追加每轮变化的内容...会破坏模型服务端的提示词缓存，显著增加请求成本（约 7-20 倍）。system_prompt 只适合追加长期稳定的角色设定或全局规则。」
- **影响文件**：`main.py:88-125`

---

### Requirement 7: EventBus 未追踪的后台任务修复

**User Story:** 作为插件开发者，我希望 EventBus 的 `publish()` 和健康检查创建的任务在 `stop()` 时能被正确取消，所以 shutdown 时不会遗留僵尸任务继续运行。

#### Acceptance Criteria

1. THE `trigger_knowledge_update()` 中 L69 创建的 `safe_create_task` 任务 SHALL 被加入 `_background_tasks` 集合。
2. THE `publish()` 中 L203 创建的 `_worker_health_check` 任务 SHALL 被加入 `_background_tasks` 集合。
3. WHEN `stop()` 被调用，THE 所有通过 `_background_tasks` 追踪的任务 SHALL 被取消并等待完成。
4. THE 分发任务的生命周期管理（L151-159：`add_done_callback` 中 `self._background_tasks.discard(t)`）SHALL 保持不变。

#### Notes / Constraints

- **根因**：L69 `safe_create_task(self.publish(...))` 未加入 `_background_tasks` → `stop()` 不取消。L203 `safe_create_task(self._worker_health_check())` 同样未加入。
- **影响文件**：`astrmai/infrastructure/runtime/event_bus.py:69,203`

---

### Requirement 8: EventBus 健康检查误数修复

**User Story:** 作为插件开发者，我希望健康检查只统计 worker 任务数量，而非混合统计分发任务，所以 worker 全部死亡时健康检查能正确检测并恢复。

#### Acceptance Criteria

1. THE `_worker_health_check()` (L172-184) SHALL 仅统计 worker 任务（`_worker_loop` 创建的），不统计分发任务（`callback(data)` 创建的）。
2. AFTER 3 个 worker 全部死亡（因异常退出），THE 健康检查 SHALL 在 30 秒内检测到 `active < 3` 并重新创建 worker。
3. THE `append_dispatch_task`（分发任务）SHALL 继续被追踪在 `_background_tasks` 中，但通过独立的计数方式避免干扰健康检查。

#### Notes / Constraints

- **根因**：`_background_tasks` 混合了 worker 任务（L152-153 的 `_worker_loop`）和分发任务（L153 的 `safe_create_task(callback(data))`）。健康检查 L178 统计所有 `_background_tasks` 中的非完成任务。若 3 个 worker 死亡但有多个分发任务活跃，`active` 仍 ≥3，不触发恢复。
- **方案**：新增 `_worker_tasks: set[asyncio.Task]` 专门追踪 worker，健康检查只统计该集合。
- **影响文件**：`astrmai/infrastructure/runtime/event_bus.py:172-184`

---

### Requirement 9: EventBus QueueFull 事件丢弃修复

**User Story:** 作为插件开发者，我希望 EventBus 队列满时能以合理的频率记录丢弃事件，并在可能时通知调用方，所以生产环境能快速发现事件处理瓶颈。

#### Acceptance Criteria

1. THE `publish()` 中 L207-212 的丢弃处理 SHALL 记录每次丢弃事件的 `topic`（而非仅每 100 次记录一次）。
2. THE 丢弃日志 SHALL 包含当前队列大小 (`_event_queue.qsize()`) 以辅助诊断。
3. THE `_dropped_count` 计数器 SHALL 在 `stop()` 或定期通过可观测接口暴露（如 admin API 的 `/runtime/health`）。

#### Notes / Constraints

- **根因**：`QueueFull` 时仅 `self._dropped_count % 100 == 1` 记录日志，丢失的 99% 事件 topic 不可见。无背压/重试机制。
- **方案**：每次丢弃记录 `logger.warning`（含 topic 和 qsize），仅在连续大量丢弃时考虑降频。将 `_dropped_count` 暴露到 admin API。
- **影响文件**：`astrmai/infrastructure/runtime/event_bus.py:207-212`

---

### Requirement 10: `message_entry.py` 注意力分发失败时用户消息静默丢弃修复

**User Story:** 作为用户，我希望当 AstrMai 内部处理出错时能收到明确的错误提示，所以我的消息不会无声无息地消失。

#### Acceptance Criteria

1. WHEN `record_and_dispatch_attention()` (L93) 抛出异常，THE 系统 SHALL 向用户发送错误提示（如 `yield event.plain_result("处理出错，请稍后重试")`）。
2. THE 错误提示 SHALL 使用可配置的 fallback 文本（复用 `config.reply.fallback_text`）。
3. THE `status == "error"` 路径 SHALL 仍然阻止默认 LLM 响应（与当前 ghost sentinel 逻辑兼容）。

#### Notes / Constraints

- **根因**：`message_entry.py:93-97` 中 `record_and_dispatch_attention` 崩溃后 `status="error"`、`is_direct_call=False`。但 L100-111 的后续代码不检查 `status`——如果 `status` 不是 `"engaged"` 等特定值，`suppress_default_llm_if_engaged` 返回 `None`，ghost_message 为空 → 无任何输出给用户。
- **影响文件**：`astrmai/presentation/events/message_entry.py:93-111`

---

### Wave 2：防御性修复（R11–R14）

---

### Requirement 11: `str(None)` 变为键 `"None"` 修复

**User Story:** 作为插件开发者，我希望 `GroupDialogueStore` 在 `chat_id` 为 `None` 时使用 sentinel 键或直接拒绝操作，所以不会因为不同的 `None` chat_id 共享同一线程数据导致对话存储混乱。

#### Acceptance Criteria

1. THE `_threads` 字典的 `get()` 和 `_get_thread()` 操作 SHALL 在 `chat_id` 为 `None`（或空字符串）时返回 `None`/创建独立 sentinel，而非将 `str(None)` = `"None"` 作为合法键。
2. THE 修复 SHALL 应用于所有 `str(chat_id or "")` 出现的模式（至少 L126, L134, L139 等）。
3. WHERE `chat_id` 为合法值（非空字符串），THE 行为 SHALL 不变。

#### Notes / Constraints

- **根因**：`group_dialogue_store.py` 多处使用 `str(chat_id or "")`。当 `chat_id=None`，表达式 `chat_id or ""` 得 `""`，`str("")` = `""`。实际是 `str(chat_id or "")` = `str("")` = `""`. Wait — `chat_id or ""` when `chat_id` is `None` evaluates to `""`. Then `str("")` = `""`. So the key is actually `""`, not `"None"`. Let me re-check...

Actually, the expression is `str(chat_id or "")`. If `chat_id` is `None`: `None or ""` evaluates to `""`, then `str("")` = `""`. So the key is empty string `""`, not `"None"`.

But if `chat_id` is the actual string `"None"` or if the `str()` wraps the whole thing differently... Let me check: `str(chat_id or "")`. If `chat_id = None`: `(None or "") = ""`, `str("") = ""`. Correct, key is `""`.

The bug is actually that all calls where `chat_id` is somehow empty/falsy (empty string, None) share the same key `""`. This is still a data corruption issue.

- **影响文件**：`astrmai/conversation/attention/group_dialogue_store.py`（所有 `str(chat_id or "")` 出现处）

---

### Requirement 12: ProactiveTask 创建失败静默禁用修复

**User Story:** 作为插件用户，我希望当主动发言功能因配置或初始化问题失败时能被告知，所以 Bot 不会悄无声息地变为完全被动。

#### Acceptance Criteria

1. WHEN `ProactiveTask` 创建失败 (`bootstrap.py:483-485`)，THE `_record_optional_failure` 记录之外 SHALL 输出 `logger.warning` 级别的日志，明确说明「主动发言功能已禁用」。
2. THE admin API (`/runtime/health`) SHALL 反映主动功能的降级状态。
3. THE 日志消息 SHALL 包含具体的异常类型和消息，便于用户排查。

#### Notes / Constraints

- **影响文件**：`astrmai/app/bootstrap.py:475-485`

---

### Requirement 13: `gate.py` 传感器过滤器 `except Exception: pass` 修复

**User Story:** 作为插件开发者，我希望传感器的 `is_command` 和 `should_process_message` 失败时能记录异常信息，所以生产环境下能诊断为何消息被错误过滤或放行。

#### Acceptance Criteria

1. THE `_passes_sensor_filters()` 中的两个 `except Exception` 块 SHALL 使用 `logger.exception()` 替代 `logger.warning(exc_info=True)` 加 `pass`/默认返回，确保完整堆栈被记录。
2. WHERE `is_command` 检查失败，THE 日志 SHALL 包含消息摘要（截断至 100 字符），便于关联具体消息。

#### Notes / Constraints

- **根因**：`gate.py:540-542` 和 `546-548` 使用 `except Exception: logger.warning(..., exc_info=True); pass` 和 `return True`。虽然已记录 warning，但 `exc_info=True` 的输出不如 `logger.exception()` 直观。
- **影响文件**：`astrmai/conversation/attention/gate.py:535-549`

---

### Requirement 14: `main.py` 钩子 `except Exception: pass` 修复

**User Story:** 作为插件开发者，我希望 `on_llm_response`、`on_agent_begin`、`on_agent_done` 钩子中的异常能被记录，所以生产环境不会因完全静默的 `pass` 而丢失调试信息。

#### Acceptance Criteria

1. THE `on_llm_response` (L133-134) 中的 `except Exception: pass` SHALL 替换为 `except Exception: logger.debug(..., exc_info=True)`（debug 级别，避免刷屏）。
2. THE `on_agent_begin` (L141-142) 中的 `except Exception: pass` SHALL 替换为 `except Exception: logger.debug(..., exc_info=True)`。
3. THE `on_agent_done` (L148-149) 中的 `except Exception: pass` SHALL 替换为 `except Exception: logger.debug(..., exc_info=True)`。

#### Notes / Constraints

- **根因**：三处 `except Exception: pass` 完全静默。如果钩子中添加了更多逻辑（当前仅 debug logging），任何 bug 都不可见。
- **影响文件**：`main.py:133-134, 141-142, 148-149`

---

## Out of Scope

- CRITICAL 级别缺陷（已由 `critical-bugfixes-v1` 覆盖）
- MEDIUM/LOW 级别审计缺陷
- `on_llm_request` 架构重构（仅做最小修复：条件保护 system_prompt 赋值）
- EventBus 背压/重试机制（仅改进日志和可观测性）
- Ghost Sentinel 协议变更（仅修复消息丢弃问题）

## High-Risk Confirmation List

| # | 高风险事项 | 等级 | 缓解措施 |
|---|-----------|------|---------|
| HR1 | R6 改用 `extra_user_content_parts` 可能影响 reverse-session 块的解析位置（原本在 system_prompt 中，改后在 user content 中） | 🔴 | 验证 Gemini 反向代理能正确解析 user content 中的 reverse-session 块；若不行则回退为条件赋值（仅在 gemini-reverse 时才修改 system_prompt） |
| HR2 | R10 在 message_entry 中添加 yield 可能影响后续 on_global_message 的 async generator 流 | 🟡 | 仅在 status=="error" 时 yield，确保不干扰正常路径 |
| HR3 | R11 `str(chat_id or "")` 修复可能影响大量调用点（整个 group_dialogue_store 遍布此模式） | 🟡 | 优先修复最关键的 3 个方法；其他调用点通过提高调用方 chat_id 保证非 None |

## Dependency Map

```
Wave 1 (R6–R10): 核心链路 — 并行实现
  R6 (main.py: on_llm_request)       ──┐
  R7 (event_bus.py: task tracking)   ──┤
  R8 (event_bus.py: health check)    ──┼── 可并行（不同文件/不同方法）
  R9 (event_bus.py: queue drop)      ──┤
  R10 (message_entry.py: silent msg) ──┘
    ↓
Wave 2 (R11–R14): 防御性修复 — 并行实现
  R11 (group_dialogue_store.py) ──┐
  R12 (bootstrap.py)            ──┼── 可并行
  R13 (gate.py)                 ──┤
  R14 (main.py: hooks)          ──┘
    ↓
全量回归验证
```

## Verification Strategy

| 验证层 | 命令/方式 | 覆盖需求 |
|--------|----------|---------|
| LSP 诊断 | `lsp_diagnostics` 对所有变更文件 | R6–R14 |
| 源代码断言 | 检查修改后的代码中 `except Exception: pass` 被替换、任务被追踪 | R7, R8, R13, R14 |
| pytest 回归 | 运行现有测试套件 | R6–R14 |
