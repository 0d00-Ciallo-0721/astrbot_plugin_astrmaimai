# Requirements Document — AstrMai 第二轮审查阻断级修复

> Spec ID: `astrmai-critical-round7-20260630` | Type: `hardening`
> 基于第二轮深度审查（6 维度 × 28 项发现）中 5 项 🔴 阻断级问题。

---

## Introduction

第二轮深度审查覆盖 6 个维度（事件流链路、状态机逻辑、数据流/DB、LLM 调用链、导入依赖图、插件生命周期），共发现 28 项缺陷。其中 **5 项为阻断级**，直接影响聊天功能和配置管理。

本 Spec 覆盖这 5 项的修复需求。

### 背景

| 审查轮 | 范围 | 发现 | 状态 |
|--------|------|:--:|:--:|
| 第一轮 | 异步/异常/资源/逻辑/API/安全 | ~119 | ✅ 5 Phase 已修 |
| 第二轮 | 功能连接/状态机/数据流/LLM链/导入/生命周期 | 28 | 🔴 5 项阻断待修 |

### 范围覆盖

1. **R1**: `plugin_facade.py:484` — `yield` 在 `async def` 中使函数变为 async generator，System2 回复链断裂
2. **R2**: `gateway_policy.py:17` — `cooldowns` 变量未定义，模型冷却永不过期
3. **R3**: `base_agent.py:61,86` — SubAgent 绕开 Gateway 直接用裸 AstrBot provider
4. **R4**: `vision_binding.py:33` — 远程图片 URL 直接返回空字符串
5. **R5**: `plugin_facade.py:80-95` — 热重载只更新 3/15 组件

### 明确排除

| 排除项 | 理由 |
|--------|------|
| 🟠 高危漏洞（9 项） | 另行 Spec 处理 |
| 🟡 中危漏洞（9 项） | 另行 Spec 处理 |
| 安全类/框架配置 | 不在插件范围 |
| 新功能开发 | 纯修复 |
| 架构重构 | 最小改动原则 |

---

## Glossary

| 术语 | 定义 |
|------|------|
| **async generator** | Python 中 `async def` + `yield` 编译为异步生成器，不可 `await`，须 `async for` |
| **System2** | 插件的主 LLM 调用路径：attention_gate → sys2_process → planner → executor → reply_engine |
| **Gateway** | `GlobalModelGateway` — 模型路由、重试、冷却、健康评分 |
| **SubAgent** | Agent-as-Tool 模式，内部再拉起 `tool_loop_agent()` |
| **热重载** | WebUI 保存配置后调用 `apply_hot_config()` 更新运行时配置 |
| **冷却 (cooldown)** | 模型调用失败后进入冷却期，期间不参与路由 |

---

## Requirements

### Wave 1：🔴 P0 — 聊天功能修复（R1–R4）

---

### Requirement 1: 修复 `_system2_entry` 的 `yield` → System2 回复链恢复

**User Story:** 作为用户，当我在聊天中发送消息时，我希望 AstrMai 能正常调用 LLM 并返回回复，而不是静默崩溃。

#### Acceptance Criteria

1. THE `_system2_entry` 方法 SHALL 不再包含 `yield` 语句，恢复为普通 `async def`。
2. WHEN `LLMCascadeFailureException` 被捕获时，THE 系统 SHALL 通过 `await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)` 发送回退消息，而非 `yield`。
3. THE `_bridge` 调用方（`attention_gate._debounce_and_judge:833`、`_engage_immediately:490`）SHALL 能正常 `await` 返回值而不抛出 `TypeError`。

#### Notes / Constraints

- 涉及文件：`astrmai/app/plugin_facade.py:427-486`
- 当前 `yield` 位于 line 484 的 `except LLMCascadeFailureException` 块
- 替换方案：`yield main_event.plain_result(fallback)` → `await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)`
- 影响面：修复后所有 System2 调用路径恢复正常

---

### Requirement 2: 修复 `gateway_policy.py` `cooldowns` NameError

**User Story:** 作为插件运营者，当模型因限流进入冷却后，我希望能在一段时间后自动恢复，而不是被永久拉黑。

#### Acceptance Criteria

1. THE `_cleanup_model_cooldowns` 方法 SHALL 通过 `getattr(self, "_model_cooldowns", {})` 获取冷却字典，而非引用未定义的 `cooldowns` 变量。
2. WHEN `_cleanup_model_cooldowns` 被调用时，THE 系统 SHALL 正常遍历冷却条目并移除过期的。
3. THE `_model_cooldown_meta` 和 `_filter_cooldown_attempt_queue` SHALL 不再因调用 `_cleanup_model_cooldowns` 而抛出 `NameError`。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/gateway/gateway_policy.py:15-19`
- 当前代码：`for key, meta in list(cooldowns.items()):` → `cooldowns` 未定义
- 修复：`cooldowns = getattr(self, "_model_cooldowns", {})` 前置一行
- `_model_cooldowns` 在 `GlobalModelGateway.__init__` 中始终初始化（`gateway_call.py:36`）

---

### Requirement 3: SubAgent LLM 调用接入 Gateway

**User Story:** 作为插件开发者，当 SubAgent（计算机代理、定时任务代理）需要调用 LLM 时，我希望它享受与主聊天相同的模型路由、重试和健康检查保护。

#### Acceptance Criteria

1. THE `BaseAgent.call()` 方法 SHALL 通过 `self.context.context.context.gateway`（或等效方式）获取 Gateway 实例。
2. WHEN SubAgent 需要调用 LLM 时，THE 系统 SHALL 使用 Gateway 的 `chat_in_lane` 或等效方法，而非裸 `ctx.tool_loop_agent()`。
3. IF Gateway 不可用（降级场景），THEN THE 系统 SHALL 回退到裸 `tool_loop_agent()` 并记录 `logger.warning`。
4. THE 修改 SHALL 不影响 SubAgent 的 `ToolExecResult` 返回值格式。

#### Notes / Constraints

- 涉及文件：`astrmai/workmode/subagents/base_agent.py:61,86-94`
- 当前：`ctx.tool_loop_agent(event=event, chat_provider_id=provider_id, ...)` 直接走 AstrBot
- 目标：通过 `context.context.context` → `PluginRuntimeContext` → `gateway` 获取 `GlobalModelGateway`
- 子类 `ComputerAgent`、`CronAgent` 继承 `BaseAgent`，修复一处即可覆盖全部

---

### Requirement 4: 恢复远程图片 URL 下载功能

**User Story:** 作为用户，当我在 QQ/微信中发送图片时，希望 bot 能"看到"图片内容并据此回复。

#### Acceptance Criteria

1. THE `extract_image_base64_from_url` 函数 SHALL 通过 HTTP 下载远程图片 URL，而非直接返回空字符串。
2. WHEN 下载成功时，THE 系统 SHALL 返回 base64 编码的图片数据。
3. WHEN 下载失败（超时、404、网络错误）时，THE 系统 SHALL 记录 `logger.warning` 并返回空字符串（保持兼容）。
4. THE 下载 SHALL 设置超时限制（≤ 10 秒）防止阻塞。
5. THE 系统 SHALL NOT 下载非 HTTP/HTTPS 协议的 URL。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/attention/vision_binding.py:33-35`
- 当前：直接 `return ""`
- 可选方案：使用 `aiohttp` 异步下载，或使用 AstrBot 内置的 HTTP 工具
- 安全约束：需验证 URL 协议，仅允许 `http://` 和 `https://`

---

### Wave 2：🟡 P1 — 配置热重载修复（R5）

---

### Requirement 5: 热重载传播到全部子组件

**User Story:** 作为插件管理员，当我在 WebUI 中修改配置（模型池、功能开关等）并保存后，我希望所有组件都能读取到新配置，而不需要重启插件。

#### Acceptance Criteria

1. THE `apply_hot_config()` 方法 SHALL 调用 `gateway.refresh_config(new_config)` 更新 Gateway 的模型池和设置。
2. THE `apply_hot_config()` 方法 SHALL 更新以下组件的配置引用：`state_engine`、`sensors`、`attention_gate`、`frequency_controller`、`private_chat_manager`、`lane_manager`。
3. IF 某组件不支持热刷新，THEN THE 系统 SHALL 记录 `logger.warning` 列出该组件名称。
4. THE 热重载 SHALL NOT 破坏当前活跃的聊天会话（不重置 state_engine）。
5. THE `StateEngine` 的 `self.config` SHALL 在热重载时更新，且其子组件（`mood_manager`、`energy_manager`、`relationship_engine`）SHALL 同步更新。

#### Notes / Constraints

- 涉及文件：`astrmai/app/plugin_facade.py:80-95`、各子组件 `__init__`
- 当前仅更新 `runtime.config` + `proactive_task`
- 最小方案：给每个组件加 `refresh_config(new_config)` 方法（可空实现）
- Gateway 的 `refresh_config` 需重建 `InfrastructureSettings` 并更新 `self.settings`

---

## Out of Scope

| 排除项 | 理由 |
|--------|------|
| 非阻断级缺陷（🟠 9 项 + 🟡 9 项） | 另行 Spec |
| 完整 Gateway 重试逻辑重写 | 架构变更，超出本 Spec |
| 视觉模型切换/优化 | 新功能 |
| SubAgent 全部接入 Gateway lane 管理 | 可后续迭代，本 Spec 仅接入路由层 |

---

## High-Risk Confirmation List

| 风险 | 等级 | 缓解 |
|------|:--:|------|
| R1 `yield`→`await reply_engine` 引入循环调用 | 🔴 | `reply_engine.handle_reply` 不回调 System2，无循环 |
| R3 Gateway 上下文穿透路径断裂 | 🟡 | `context.context.context` 三层穿透已在上轮加固过 |
| R4 HTTP 下载引入新依赖 | 🟡 | 使用 `aiohttp`（AstrBot 已依赖）或内置 `urllib` + `asyncio.to_thread` |
| R5 热重载期间竞态（并发请求读配置） | 🟡 | 加 `asyncio.Lock` 保护配置替换原子性 |

---

## Dependency Map

```
R1 (System2修复) ←────────────────────────┐
R2 (冷却修复) ───── 无依赖，可并行 ────────┤
R3 (SubAgent接入) ─ 无依赖，可并行 ────────┼──→ R5 (热重载)
R4 (图片下载) ───── 无依赖，可并行 ────────┤
                                          │
R5 依赖 R1 (需要 System2 路径已修复后验证) ─┘
```

---

## Verification Strategy

| 需求 | 验证方式 | 通过标准 |
|------|---------|---------|
| R1 | 发送消息 → 应有回复 | bot 正常返回 LLM 回复 |
| R2 | 模拟限流 → 等待冷却 → 模型应恢复可用 | 模型冷却后重新参与路由 |
| R3 | SubAgent 执行任务 → 应走 Gateway 路由 | 日志中出现 Gateway lane 调用 |
| R4 | 发送图片 URL → bot 应识别图片内容 | `extract_image_base64_from_url` 返回非空 base64 |
| R5 | WebUI 改配置 → 新对话使用新配置 | 模型池/功能开关生效 |

### 回归基线

- `python -m pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py`
- 目标：≥ 810 passed（不引入新失败）
