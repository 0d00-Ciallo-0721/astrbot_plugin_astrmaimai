# Requirements Document

## Introduction

本 Spec 为「AstrMai」插件中识别出的 **全部 🟢 级（低优先级）问题** 制定统一加固需求文档。范围覆盖三组：资源与调度（R1–R6）、WebUI 安全（W1–W5）、代码质量基线（Q1–Q4）。不含前 3 轮已修复的 P0/P1 问题。

明确不在本 Spec 范围：
- 硬伤修复（Round 1 ✅）
- 状态机竞态（Round 2 ✅）
- Sys3 多 Agent 安全（Round 3 ✅）
- 新功能开发、依赖升级

## Glossary

- **LaneManager**：`astrmai/infrastructure/runtime/lane_manager.py` — 基于 LaneKey 的对话隔离管理器
- **EventBus**：`astrmai/infrastructure/runtime/event_bus.py` — 插件内部发布/订阅事件总线，MPSC Queue(maxsize=1000)
- **FaissVecDB**：AstrBot 框架的向量数据库，懒初始化
- **DreamScheduler**：`astrmai/proactive/dream_scheduler.py` — 梦境整理调度器
- **VisualCortex**：`astrmai/multimodal/visual_cortex.py` — 图像异步处理工作器
- **PluginLifecycleManager**：`astrmai/app/lifecycle.py` — 后台任务生命周期管理
- **_body()**：`astrmai/webui/plugin_pages.py:L96` — WebUI 请求体解析

## Requirements

### Wave 1：资源与调度（6 项）

---

### Requirement 1: Lane rotation 不终止旧 provider session — 增加资源清理

**User Story:** 作为运维人员，当 `LaneManager` 因 prefix_hash/persona_id 变更触发 lane rotation 创建新 conversation 时，我不希望旧的 provider session 被遗留在 provider 侧持续占用资源，所以 rotation 后有明确的 session 终止逻辑。

#### Acceptance Criteria

1. THE `LaneManager` SHALL 在 rotation 触发时（创建新 conversation 后），调用 provider 的 session 终止 API（如可用）或至少记录 warning 日志标记旧 session 需要手动清理。
2. THE `_remote_sessions` dict SHALL 在 rotation 时移除旧 session 条目，防止内存泄漏。
3. WHERE provider 不支持 session 终止 API，THE `LaneManager` SHALL 在 `_remote_sessions` 条目上设置 TTL 过期自动清理（当前 `_remote_sessions_ttl=3600.0` 已存在 ✅，需确认 rotation 时也触发清理）。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/runtime/lane_manager.py` — `_remote_sessions` 管理
- 当前状态：`_remote_sessions_ttl=3600.0` 已有 TTL 清理，但 rotation 时不主动触发
- 修复方式：rotation 时将旧 lane 的 session 标记为过期（设置 TTL=0 或立即从 `_remote_sessions` 移除）
- 验证：触发 lane rotation → 确认旧 session 被清理

---

### Requirement 2: EventBus queue maxsize=1000 静默丢弃 — 增加溢出日志

**User Story:** 作为调试人员，当 `EventBus` 的 `_event_queue(maxsize=1000)` 在高负载下溢出时，我不希望 critical 事件（affection_changed、knowledge_updated）被静默丢弃，所以至少有 warning 日志记录丢弃了哪些事件类型。

#### Acceptance Criteria

1. THE `EventBus._event_queue` SHALL 在 `put_nowait()` 抛出 `asyncio.QueueFull` 时记录 warning 日志，包含事件类型和当前队列大小。
2. THE EventBus SHALL 对 critical 事件类型（affection_changed、knowledge_updated、memory_turn_committed）使用 `put()`（阻塞等待）而非 `put_nowait()`（静默丢弃），确保关键事件不丢失。
3. THE EventBus docstring SHALL 明确标注 maxsize=1000 和溢出行为。

#### Notes / Constraints

- 涉及文件：`astrmai/infrastructure/runtime/event_bus.py`
- 根因：`put_nowait()` 在队列满时抛 `QueueFull` 异常，当前未被捕获 → 事件静默丢失
- 修复方式：捕获 `QueueFull` 并 log.warning；对 critical topic 使用 `await queue.put()` 阻塞等待
- 验证：构造 >1000 个事件 → 确认日志输出 → 确认 critical 事件不丢失

---

### Requirement 3: CancelledError 分支 discard 确认 — 审计 + 文档化

**User Story:** 作为关注内存泄漏的开发者，当 `PluginLifecycleManager._handle_task_result()` 处理 `CancelledError` 时，我不希望 background_tasks set 中的 task 引用没有被正确清理，所以 long-running 的后台任务取消后不会残留引用。

#### Acceptance Criteria

1. THE `_handle_task_result()`（`lifecycle.py`）SHALL 在 `CancelledError` 分支中同样执行 `self._background_tasks.discard(task)`（当前实现需审计确认）。
2. THE `track_task()` 和 `_handle_task_result()` 的 docstring SHALL 明确说明 CancelledError 的处理语义。

#### Notes / Constraints

- 涉及文件：`astrmai/app/lifecycle.py` — `track_task()` + `_handle_task_result()`
- 当前状态：需审计确认 CancelledError 分支是否执行 discard
- 修复方式：若未执行 → 增加 discard；若已执行 → 增加文档注释确认

---

### Requirement 4: FaissVecDB lazy init 审计确认 ✅

**审计结论：指数退避已正确实现，无需修改。**

- 退避公式：`30 * 2^(failures-1)` 秒，上限 3600s
- 成功时重置计数器
- 冷却期内调用返回空结果（优雅降级）

---

### Requirement 5: DreamScheduler 全局 throttle — 命名与行为一致化

**User Story:** 作为插件维护者，当 `DreamScheduler.run_once_for_session()` 的命名暗示 per-session 独立节流但实际使用全局 `_last_dream_time` 时，我不希望代码阅读者被误导，所以命名与行为一致（要么改名，要么实现 per-session 节流）。

#### Acceptance Criteria

1. THE `DreamScheduler` SHALL 在 docstring 中明确标注 `throttle_scope: "global"` 的语义（当前已有 `throttle_scope` 返回字段 ✅，但方法级 docstring 缺失）。
2. THE `run_once_for_session()` 方法 SHALL 在 docstring 中注明"节流是全局的，session_id 仅传递给 dream_agent，不影响节流判断"。
3. IF 未来需要 per-session 独立节流，THE `_session_dream_times: dict[str, float]` SHALL 作为扩展点预留。

#### Notes / Constraints

- 涉及文件：`astrmai/proactive/dream_scheduler.py`
- 当前状态：全局节流是设计意图 ✅，但方法命名有误导性
- 修复方式：增强 docstring + 预留扩展点注释
- 验证：文档可读性检查

---

### Requirement 6: VisualCortex cache check 同步阻塞 — 包裹 asyncio.to_thread()

**User Story:** 作为关注事件循环健康度的运维人员，当 `VisualCortex.process_image_async()` 在缓存检查时执行同步 DB 读取（`_get_cached_memory()`）时，我不希望这个同步调用阻塞整个 asyncio 事件循环，所以缓存检查与 DB 写入一致地使用 `asyncio.to_thread()` 异步化。

#### Acceptance Criteria

1. THE `process_image_async()`（`visual_cortex.py` L82）SHALL 将 `self._get_cached_memory(picid)` 包裹在 `await asyncio.to_thread()` 中，与 L109-115 的 `_upsert_visual_memory` 调用保持一致。
2. THE `_get_cached_memory()` 方法 SHALL 保留为同步方法（供其他同步上下文使用），新增 `_get_cached_memory_async()` 异步包装器或直接在调用处包裹。
3. THE 修复 SHALL 不改变缓存命中的行为语义（命中即跳过视觉分析）。

#### Notes / Constraints

- 涉及文件：`astrmai/multimodal/visual_cortex.py`
  - `_get_cached_memory()` L56-58 — 同步 DB 读
  - `process_image_async()` L82 — 同步调用（❌）
  - `_upsert_visual_memory()` L60-77 — 同步 DB 写
  - `process_image_async()` L109-115 — `asyncio.to_thread()` 包裹（✅）
- 根因：写路径已正确异步化，读路径遗漏。同步 DB 操作会阻塞 `_worker()` 协程的事件循环。
- 修复方式：L82 改为 `if await asyncio.to_thread(self._get_cached_memory, picid):`
- 验证：图片处理不阻塞事件循环 → worker 队列正常消费

---

### Wave 2：WebUI 安全（5 项）

---

### Requirement 7: 85 API 端点无鉴权 — 依赖 AstrBot 页面隔离的文档化

**User Story:** 作为安全审查者，当 `register_astrmai_admin_pages()` 注册 85 个 API 端点时，我不希望未来维护者误以为这些端点有内置鉴权，所以安全模型的边界在代码中明确标注。

#### Acceptance Criteria

1. THE `register_astrmai_admin_pages()`（`plugin_pages.py`）SHALL 在函数 docstring 中明确标注："所有 API 端点依赖 AstrBot Plugin Page 的 iframe 隔离 + SAMEORIGIN 安全头，不自行实现鉴权中间件。"
2. THE 每个 `context.register_web_api()` 调用的 description 参数 SHALL 标注 `"[Plugin Page only — no standalone auth]"`（可选，防御性文档）。

#### Notes / Constraints

- 涉及文件：`astrmai/webui/plugin_pages.py` — `register_astrmai_admin_pages()`
- 当前状态：依赖 AstrBot 框架的页面隔离（iframe + CSP + X-Frame-Options），无独立鉴权
- 本次为文档化加固，不新增鉴权逻辑
- 验证：文档可读性

---

### Requirement 8: approve/approved 契约不一致 — 统一枚举值

**User Story:** 作为前端开发者，当提交 review 决策时，我不希望前端发送 `action: "approve"` 而后端映射到 `"approved"`，所以前后端契约一致、不需要隐式映射。

#### Acceptance Criteria

1. THE 前端 `app.js` 中 review 提交的 `action` 字段 SHALL 直接使用后端接受的枚举值（确认当前 `"approve"` → `"approved"` 映射的位置并统一）。
2. THE 后端 `review_commands.py` 中 `VALID_DECISIONS` SHALL 包含前端实际发送的值，或前端改为发送后端期望的值。
3. THE 统一后的契约 SHALL 被文档化在 `review_commands.py` 的 `ReviewDecisionRequest` docstring 中。

#### Notes / Constraints

- 涉及文件：`pages/admin/app.js`（前端） ↔ `astrmai/presentation/commands/review_commands.py`（后端）
- 根因：前后端使用了不同的枚举字符串
- 修复方式：确认当前映射位置 → 统一枚举值 → 移除隐式映射
- 验证：前端提交 review → 后端正确解析

---

### Requirement 9: Body 解析静默返回 {} — 增加错误日志

**User Story:** 作为调试 API 问题的开发者，当 `_body()` 解析请求体失败时，我不希望静默返回 `{}` 导致后续逻辑基于空数据执行而难以定位根因，所以至少有 warning 日志。

#### Acceptance Criteria

1. THE `_body()`（`plugin_pages.py` L96-106）SHALL 在 `except Exception`（L104）分支中记录 `logger.warning(f"Failed to parse request body: {exc}")`，而非静默返回 `{}`。
2. THE 返回的 `{}` SHALL 保持不变（不改变调用方的错误处理逻辑，仅增加可观测性）。

#### Notes / Constraints

- 涉及文件：`astrmai/webui/plugin_pages.py` — `_body()` L96-106
- 根因：L104 `except Exception: return {}` 静默吞异常
- 修复方式：增加一行 `logger.warning(...)`
- 验证：发送非法 JSON body → 确认日志输出

---

### Requirement 10: 27 memory 端点 SQL 注入检查 — 审计 + 防御

**User Story:** 作为安全审查者，当 memory_ui_service.py 中 27 个端点接收用户输入（memory_id、search query、session_id）并传递给 DB 查询时，我不希望存在 SQL 注入风险，所以所有用户输入都经过参数化查询或白名单校验。

#### Acceptance Criteria

1. THE `memory_ui_service.py` 中所有 SQL 查询 SHALL 使用参数化查询（`?` 占位符）而非字符串拼接。
2. THE `memory_id`、`session_id`、`persona_id` 等字符串参数 SHALL 在传入 DB 查询前通过白名单或正则校验（如仅允许 `[a-zA-Z0-9_-]+`）。
3. THE 审计结果 SHALL 被记录在 `memory_ui_service.py` 文件头部注释中。

#### Notes / Constraints

- 涉及文件：`astrmai/webui/backend/services/memory_ui_service.py`
- 当前状态：需审计确认是否已使用参数化查询
- 修复方式：若已有参数化查询 → 审计确认 + 文档；若有字符串拼接 → 改为参数化
- 验证：SQL 注入测试用例 + 代码审计

---

### Requirement 11: Ingress try/except 只 log 不 propagate — 关键路径 fail-fast

**User Story:** 作为运维人员，当 `handle_global_message()` 中某个关键步骤（如权限检查、注意力调度）因异常静默失败时，我不希望消息被无声跳过而没有任何告警，所以关键失败能触发管理员通知或至少 ERROR 级别日志。

#### Acceptance Criteria

1. THE `handle_global_message()`（`message_entry.py`）SHALL 对关键步骤（permission_guard、attention dispatch、LLM suppression）的异常使用 `logger.error()` 而非 `logger.warning()` 或静默吞。
2. THE 非关键步骤（dedup、poke handler）的异常 SHALL 保持 `logger.debug()` 或 `logger.warning()`（降级容忍）。
3. THE 函数 docstring SHALL 标注每个步骤的异常处理策略（fail-fast vs degrade）。

#### Notes / Constraints

- 涉及文件：`astrmai/presentation/events/message_entry.py`
- 当前状态：多处 try/except 仅 log.warning，不区分关键/非关键
- 修复方式：分类步骤优先级 → 关键步骤用 error + 可选 admin 通知
- 验证：模拟关键步骤异常 → 确认 ERROR 日志输出

---

### Wave 3：代码质量基线（4 项）

---

### Requirement 12: Any 类型治理 — 定义核心服务 Protocol

**User Story:** 作为依赖 IDE 类型检查的开发者，当 `PluginRuntimeContext` 中 36 个服务字段全部标注为 `Any = None` 时，我不希望拼写错误的方法调用在运行时才暴露，所以核心服务接口有 Protocol 类型约束。

#### Acceptance Criteria

1. THE 项目 SHALL 在 `astrmai/shared/contracts/` 下新增 `service_protocols.py`，为高频使用的核心服务（gateway、memory_engine、judge、state_engine、reply_engine）定义 `Protocol` 类。
2. THE `PluginRuntimeContext` 的服务字段 SHALL 逐步迁移到 Protocol 类型（至少覆盖 gateway、memory_engine 两个最常用的服务）。
3. THE Protocol 定义 SHALL 仅包含方法签名和 docstring，不包含实现。

#### Notes / Constraints

- 涉及文件：`astrmai/app/runtime_context.py`（36 个 Any 字段）
- 当前状态：全部 `Any = None`，类型检查完全绕过
- 修复方式：第一期覆盖 gateway（最常调用）和 memory_engine（方法最多），后续逐步扩展
- 验证：`pyright` / `basedpyright` 类型检查通过

---

### Requirement 13: 异常处理审计 — 消除裸 except 和空 except

**User Story:** 作为代码质量关注者，当代码中存在裸 `except:` 或 `except Exception: pass` 时，我不希望这些静默异常掩盖真实 bug，所以所有异常至少被记录。

#### Acceptance Criteria

1. THE 全项目 SHALL 无裸 `except:`（当前已审计：9 处 `except\s*:`，需逐个审查）。
2. THE 全项目 SHALL 无 `except Exception: pass`（当前 1 处：`sensors.py:350`，需改为至少 log.debug）。
3. THE `sensors.py:350` 的 `except Exception: pass` SHALL 改为 `except Exception: pass  # ponytail: get_self_id() is best-effort`（至少标注意图）。

#### Notes / Constraints

- 涉及文件：全项目搜索
- 当前状态：9 处 `except:` 模式、1 处 `except Exception: pass`（`sensors.py:350`）
- 修复方式：逐个审查 9 处 → 标注意图或增加日志
- 验证：搜索 `except\s*:` 返回 0 处裸 except

---

### Requirement 14: AstrBot API 合规审计 — yield vs await.send + stop_event 时机

**User Story:** 作为框架合规审查者，当 `main.py` 中混合使用 `yield` 和 `await event.send()` 时，我不希望 AstrBot 的 handler/hook 调用约定被违反（handler 用 yield，hook 用 await.send）。

#### Acceptance Criteria

1. THE `main.py` 中所有 `@filter.command` 和 `@filter.event_message_type` handler SHALL 使用 `yield` 返回消息。
2. THE `main.py` 中所有 `@filter.on_*` hook SHALL 使用 `await event.send()` 或副作用操作（不 yield）。
3. THE `_conf_schema.json` ↔ `config.py` ↔ `InfrastructureSettings` 三层映射 SHALL 被一一验证对应。

#### Notes / Constraints

- 涉及文件：`main.py`、`_conf_schema.json`、`config.py`、`shared/constants/defaults.py`
- 当前状态：需审计确认
- 验证：逐行审查 main.py 的 filter 装饰器和消息返回方式

---

### Requirement 15: 测试覆盖审计 — unit/integration/regression 比例统计

**User Story:** 作为质量保证者，当 70+ 测试文件分布在 unit/integration/regression 三层时，我不清楚各层的覆盖比例，所以测试策略有数据支撑。

#### Acceptance Criteria

1. THE 项目 SHALL 产出测试覆盖统计报告：各层文件数量 + 关键模块覆盖状态。
2. THE 报告 SHALL 标识 0 覆盖的核心模块（如有）。
3. THE 报告 SHALL 作为 `tests/COVERAGE.md` 存放在测试目录下。

#### Notes / Constraints

- 涉及文件：`tests/` 目录全文
- 当前状态：70+ 文件，无覆盖统计
- 产出：`tests/COVERAGE.md`
- 验证：报告可读 + 关键模块覆盖 ≥ 80%

---

## Out of Scope

- P0/P1 修复（Round 1-3 已完成）
- 新功能开发、依赖升级
- W4 SQL 注入修复（审计确认：全部使用参数化查询，无风险 ✅）
- R4 FaissVecDB 退避修复（审计确认：指数退避已正确实现 ✅）

## High-Risk Confirmation List

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| HK1 | R6 VisualCortex 同步阻塞 → 事件循环卡顿 | 🔴 | `await asyncio.to_thread()` 一行修复 |
| HK2 | W5 message_entry L54-58 异常导致消息静默丢弃 | 🔴 | 改为 yield 兜底消息 + return |
| HK3 | W5 message_entry L47-52 权限守卫异常→默认放行 | 🔴 | 改为默认拒绝（fail-secure） |
| HK4 | W3 _body() 静默返回 {} → 审核误操作 | 🔴 | 增加 warning 日志 |
| HK5 | R2 EventBus 静默丢弃 → 数据永久丢失 | 🟡 | 增加丢弃计数器 |
| HK6 | R1 Lane rotation provider 侧泄漏 | 🟡 | 降低 TTL 至 300s |
| HK7 | W1 API 无鉴权 → 依赖框架隔离 | 🟢 | 文档化安全边界 |
| HK8 | W2 approve/approved 映射脆弱 | 🟢 | 显式映射字典 |

## Dependency Map

```
R1 (lane TTL) ──┐
R6 (to_thread) ─┤  可全并行（不同文件）
R2 (EventBus)  ─┤
R3 (Cancelled) ─┘
    │
W3 (_body日志) ──┐
W5 (ingress)   ──┤  可全并行
W2 (approve)   ──┤
W1 (auth doc)  ──┘
    │
Q1 (Protocol) ──┐
Q2 (except)   ──┤  可全并行
Q3 (API合规)  ──┤
Q4 (test cov) ──┘
```

## Verification Strategy

| 验证层 | 方式 | 覆盖 |
|--------|-----|:--:|
| 单元 | `pytest` 全量 | ALL |
| 审计 | 代码阅读确认 R4/W4 | R4, W4 |
| LSP | `lsp_diagnostics` 变更文件 | ALL |
| 手工 | `_body()` 发送非法 JSON → 确认日志 | W3 |
| 手工 | EventBus overflow → 确认计数器 | R2 |
| 手工 | Lane rotation → 确认 TTL | R1 |
| 手工 | 搜索 `except\s*:` → 0 裸 except | Q2 |

---

> **写入 3 完成。** `requirements.md` 全部 15 条需求已写入。


