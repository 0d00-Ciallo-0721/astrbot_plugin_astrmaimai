# Requirements Document

## Introduction

本 Spec 为「AstrMai 多Agent对话插件」中**深度审计发现的 5 个 CRITICAL 运行时缺陷**制定修复需求文档。范围覆盖：

- **R1**: Sys3 工作模式 Planner TOOL_CALL 路径下 SubAgent 轻量工具集崩溃
- **R2**: `database_review.py` 中无追踪的 fire-and-forget 异步任务导致数据静默丢失
- **R3**: `gateway_call.py` 中 `raw_completion_text` 变量未初始化导致的嵌套崩溃
- **R4**: `group_reply_wait_manager.py` 和 `private_chat_manager.py` 中 `monotonic()` 与 `time.time()` 时钟源混用
- **R5**: `chat_runtime_coordinator.py` 中 `executor_lock` 取消泄漏导致聊天永久阻塞

当前阶段产出物为 `specs/critical-bugfixes-v1/` 下的 `requirements.md` / `design.md` / `tasks.md`。**本轮仅产出 requirements.md，不进入 design 或 tasks 阶段**。

明确不在本 Spec 范围：HIGH/MEDIUM/LOW 级别的审计缺陷、安全漏洞、性能优化、功能新增、配置系统重构、AstrBot 框架升级、插件页面/WebUI 相关改造。

## Glossary

- **AstrMai**：`main.py` 中的 `AstrMaiPlugin` 类，继承 AstrBot `Star`，为插件唯一入口。
- **AstrBot**：聊天机器人框架，版本 >= v4.14.0，插件通过 `@filter` 装饰器注册钩子与命令。
- **Sys3 / Work Mode**：AstrMai 的多Agent工作模式，通过 `/work` 命令进入，采用 Router→SubAgent 架构。
- **SubAgent**：继承 `AstrMaiBaseSubAgent` → `FunctionTool[AstrAgentContext]` 的Agent-as-Tool，包括 `ComputerAgent` 和 `CronAgent`。
- **Router**：`Sys3Router`（`workmode/router.py`），编排静态Agent和动态Agent，提供轻量和全量工具集。
- **Planner / System2**：`conversation/planning/planner.py`，深度推理引擎，TOOL_CALL 模式下可调用 SubAgent 工具。
- **`get_light_tool_set()`**：AstrBot `ToolSet` 类的方法，创建参数为空、`handler=None` 的裸 `FunctionTool` 实例，用于向 Planner 暴露可用的 SubAgent 名称和描述。
- **`_execute_local()`**：AstrBot 核心中 `FunctionToolExecutor` 的方法，实际执行工具调用，检查 `handler`/`run`/`call` 覆盖。
- **Gateway / Lane**：`infrastructure/gateway/` 的模型网关和 `infrastructure/runtime/` 的通道管理，处理 LLM 调用路由、重试、冷却。
- **`executor_lock`**：`ChatRuntimeCoordinator` 中的 per-chat 锁，限制同一聊天同时进行的 System2 执行数。
- **`monotonic()`** vs **`time.time()`**：`time.monotonic()` 返回单调递增时钟（不受系统时间调整影响），`time.time()` 返回 Unix 时间戳。两者数值不在同一量级，不可混用比较。
- **EARS**：Easy Approach to Requirements Syntax，本文档所有 Acceptance Criteria 遵循的句式。
- **P0/P1/P2**：优先级分级 — P0 生产阻断级（本 Spec 全部为 P0）。
- **fire-and-forget**：通过 `asyncio.create_task()` 创建但未追踪、无 error callback 的后台任务。

## Requirements

本 Spec 5 条需求均为 **P0 生产阻断级**，按依赖关系和风险组织为单一 Wave。

### Wave 1：CRITICAL 运行时缺陷修复（5 项）

| # | 需求 | 影响 |
|---|------|------|
| R1 | Sys3 轻量工具集崩溃修复 | TOOL_CALL 路径 SubAgent 不可用 |
| R2 | Fire-and-forget 任务追踪 | 表达模式数据静默丢失 |
| R3 | `raw_completion_text` 未初始化修复 | LLM 调用链嵌套崩溃 |
| R4 | 时钟源统一 | 群聊回复等待和私聊会话管理永久失效 |
| R5 | `executor_lock` 取消泄漏修复 | 聊天永久阻塞无法进入 System2 |

> **注**：Wave 1 的五条需求相互独立（修改不同文件/模块），可并行实现，但需串行验证（Verification Strategy 中说明）。

---

### Requirement 1: Sys3 Planner TOOL_CALL 模式下 SubAgent 轻量工具集崩溃修复

**User Story:** 作为使用 AstrMai 工作模式的用户，我希望当 Sys2 Planner 判定需要进入 TOOL_CALL 模式时，能够成功调用 SubAgent（如 `transfer_to_computer`、`transfer_to_cron`），所以可以通过 Planner 的推理链路触发 SubAgent 执行，而不是在运行时抛出 `ValueError` 导致任务失败。

#### Acceptance Criteria

1. WHEN Sys2 Planner 判定 `judge_action == "TOOL_CALL"` 且 `is_tool_call_mode == True`，THE `_build_execution_tools()` SHALL 返回的 Sys3 工具列表中的 SubAgent 实例具备有效的 `call()` 方法覆盖（即保留 `AstrMaiBaseSubAgent` 或其子类实例，而非裸 `FunctionTool`）。
2. WHEN `_build_execution_tools()` 为 `is_tool_call_mode` 路径准备工具列表，THE 工具列表中的 Sys3 SubAgent SHALL 能够被 `tool_loop_agent` → `_handle_function_tools` → `_execute_local` 成功调用，不抛出 `ValueError("Tool must have a valid handler or override 'run' method.")`。
3. THE 修复 SHALL NOT 修改 AstrBot 核心库中的 `ToolSet.get_light_tool_set()` 方法签名或 `FunctionToolExecutor._execute_local()` 逻辑（不在本插件仓库范围内）。
4. THE `planner_side_inputs.py` 中 `_build_execution_tools()` 方法 SHALL 继续使用轻量工具集（压缩参数以减少 token 消耗），但同时保留实际可执行的 SubAgent 引用。
5. WHERE SubAgent 被 Planner 通过 tool_call_mode 调用，THE SubAgent 的 `call()` 方法 SHALL 正常执行其内部逻辑（Gateway 路径或 raw `tool_loop_agent` 路径），并返回有效的 `ToolExecResult`。

#### Notes / Constraints

- **根因**：`get_light_tool_set()`（AstrBot 核心 `tool.py`）创建裸 `FunctionTool(name=..., parameters={"type":"object","properties":{}}, handler=None)`。当 `_execute_local` 检查 `is_override_call` 时遍历 MRO，发现 `FunctionTool.call` 未被覆盖，且 `handler` 为 `None`，抛出 `ValueError`。
- **调用链**：`_build_execution_tools(is_tool_call_mode=True)` → `Sys3Router.get_light_tools_for_planner()` → `ToolSet.get_light_tool_set()` → 裸 `FunctionTool` → `_execute_local` → **ValueError**
- **影响文件（直接）**：`astrmai/workmode/router.py:35` → `get_light_tools_for_planner()`、`astrmai/conversation/planning/planner_side_inputs.py:392` → `_build_execution_tools()`、`astrmai/app/plugin_facade.py:410-463` → `enter_sys3_direct()`
- **影响文件（上游）**：`astrmai/conversation/execution/executor.py` → `_run_tool_mode()`、`astrmai/conversation/planning/planner.py` → `_invoke_planning_llm()`
- **设计约束**：AstrBot 核心的 `get_light_tool_set()` 不可修改；需在插件层建立「轻量 Schema + 真实 SubAgent 实例」的双轨制。

---

### Requirement 2: `database_review.py` 中 Fire-and-Forget 异步任务追踪与错误处理

**User Story:** 作为插件开发者，我希望表达模式 (`expression pattern`) 的异步持久化任务在失败时能够被检测并记录，所以可以防止表达模式数据静默丢失，并在 shutdown 时正确清理未完成的任务。

#### Acceptance Criteria

1. THE `save_pattern()` 方法中创建的 `asyncio.create_task(self._save_pattern_to_canonical_async(pattern))` 任务 SHALL 附加 `add_done_callback`，在任务失败时记录错误日志并携带异常堆栈。
2. THE 异步保存任务 SHALL 被追踪（例如加入 `PluginLifecycleManager` 的后台任务集合），以便在 `terminate()` 时能够等待或取消未完成的任务。
3. WHEN `_save_pattern_to_canonical_async(pattern)` 因数据库连接断开等原因失败，THE 错误 SHALL 被 `logger.exception()` 记录，且不影响调用方的主流程。
4. THE `save_pattern()` 方法的调用方（同步上下文）SHALL 不因异步保存失败而收到异常——保存是 best-effort 的后台操作。
5. IF 插件 shutdown 时仍有未完成的保存任务，THEN THE 系统 SHALL 等待任务完成（最多 5 秒超时），超时后 cancel 并记录警告。

#### Notes / Constraints

- **根因**：`database_review.py:77` 中 `asyncio.create_task(...)` 创建的任务无 `add_done_callback`、无 `_background_tasks` 追踪。任务失败时 Python 仅在 stderr 输出 `Task exception was never retrieved`，生产环境难以察觉。
- **影响文件**：`astrmai/infrastructure/persistence/database_review.py:71-79`
- **上下文**：`save_pattern()` 被 `ExpressionPatternService` 调用，后者又被 `EvolutionManager._save_patterns()` 调用。失败会导致学到的表达模式永久丢失。
- **不修改**：`asyncio.create_task` → `safe_create_task` 的全局替换（`safe_create_task` 本身有 bug，见审计报告 MEDIUM #5）。

---

### Requirement 3: `gateway_call.py` 中 `raw_completion_text` 变量未初始化修复

**User Story:** 作为插件开发者，我希望 LLM 网关调用在响应处理失败时不会因为未初始化的变量引发二次 `NameError` 而崩溃，所以可以确保所有 LLM 调用失败路径都能安全地触发模型冷却机制，而非因嵌套异常导致整个调用链中断。

#### Acceptance Criteria

1. THE `raw_completion_text` 变量 SHALL 在 LLM 调用 try 块**之前**被初始化为空字符串 `""`，确保所有异常分支中该变量均可安全使用。
2. WHEN LLM API 调用在 `getattr(response, "completion_text")` 阶段失败（`response` 对象不包含该属性），THE 内部 `except Exception` 块 SHALL 能够安全地使用 `raw_completion_text` 进行冷却处理，不抛出 `NameError`。
3. THE 嵌套 try/except 结构（外层 `except Exception` L185 + 内层 `except Exception` L303）SHALL 保持功能等价，仅修复变量初始化问题。
4. THE `logger.exception()` 调用在 L197 和 L309 SHALL 输出正确的异常上下文（当前 L197 在 `except` 块内正确获取，L309 在内层 `except` 块内正确获取）。

#### Notes / Constraints

- **根因**：`raw_completion_text` 在 L209 才通过 `getattr(response, "completion_text", "")` 赋值。但 L207 的 `getattr(response, "completion_text")`（无默认值）若失败，控制流跳到内层 `except Exception` L303，此时 `raw_completion_text` 未绑定 → `NameError`。
- **影响文件**：`astrmai/infrastructure/gateway/gateway_call.py:185-321`
- **调用上下文**：`_call_with_lane_key()` 和 `_call_raw()` 方法均使用此 try/except 结构。
- **不修改**：嵌套 try/except 的架构重构（仅做最小修复）。

---

### Requirement 4: 群聊回复等待与私聊会话管理时钟源统一

**User Story:** 作为使用 AstrMai 群聊回复等待和私聊会话功能的用户，我希望超时判断使用一致的时钟源，所以 Bot 的「等待对方继续说话」和「私聊会话管理」功能能够正常工作，而不是因为时钟源混用导致所有等待状态立即过期。

#### Acceptance Criteria

1. THE `group_reply_wait_manager.py` 中 `_create_wait_state()` 的 `expires_at` 字段与 `handle_incoming_message()` 的过期检查 SHALL 使用相同的时钟源（统一使用 `time.monotonic()`）。
2. THE `private_chat_manager.py` 中 `signal_new_message()` 的 `last_message_time` 字段与 `cleanup_stale_sessions()` 的静默时长计算 SHALL 使用相同的时钟源（统一使用 `time.monotonic()`）。
3. THE `private_chat_manager.py` 中 `get_session_info()` 的 `silence_sec` 计算 SHALL 使用与 `last_message_time` 一致的时钟源。
4. WHEN `handle_incoming_message()` 检查 `now >= state.expires_at` 判断等待是否过期，THE `now` SHALL 通过 `time.monotonic()` 获取，与 `expires_at` 的取值方式一致。
5. THE 修复 SHALL NOT 影响其他模块中对 `time.time()` 的正确使用（如日志时间戳、记忆衰减、情绪衰减等）。
6. THE 群聊回复等待功能 SHALL 在修复后恢复正常行为：Bot @mention 某人后等待指定秒数，期间若对方回复则捕捉，超时则自动取消。

#### Notes / Constraints

- **根因**：`_create_wait_state()` 使用 `monotonic()` 设置 `expires_at`（如 `monotonic() + 30` ≈ `172000.0 + 30 = 172030.0`），但 `handle_incoming_message()` 使用 `time.time()` 获取 `now`（约 `1750000000.0`）。比较 `1750000000.0 >= 172030.0` 永远为 `True`，所有等待状态立即过期。
- **影响文件**：
  - `astrmai/state/group_wait/group_reply_wait_manager.py:140`（设置 `expires_at`）、`:171,178`（过期检查）
  - `astrmai/state/private_chat/private_chat_manager.py:89`（设置 `last_message_time`）、`:144,171-174`（读取比较）
- **`time.monotonic()` vs `time.time()` 选择**：`monotonic()` 不受系统时钟调整（NTP、夏令时）影响，更适合超时/间隔计算。本 Spec 选择统一为 `monotonic()`。

---

### Requirement 5: `executor_lock` 取消泄漏导致聊天永久阻塞修复

**User Story:** 作为插件开发者，我希望 System2 执行器的 per-chat 并发控制能够在任务取消时正确释放资源，所以聊天不会因为偶然的任务取消而永久无法进入 System2 处理流程。

#### Acceptance Criteria

1. WHEN `try_acquire_executor()` 中的 `await executor_lock.acquire()` 因任务取消而抛出 `asyncio.CancelledError`，THE `executor_pending` 计数器 SHALL 被正确递减（通过 `release_executor()` 或内联递减）。
2. AFTER 两次任务取消，THE 同一 `chat_id` 的 `executor_pending` SHALL NOT 保持为 `max_pending`（默认 2），后续 `try_acquire_executor()` 调用 SHALL 能够成功获取执行锁。
3. THE 正常路径（无取消）的 `executor_pending` 递增/递减行为 SHALL 保持不变。
4. THE `release_executor()` 方法 SHALL 继续正确释放锁并递减计数器。
5. IF `CancelledError` 发生在锁获取之后（即已成功获取锁，但在后续 `await` 中被取消），THEN `release_executor()` SHALL 仍能被 `finally` 块调用。

#### Notes / Constraints

- **根因**：`try_acquire_executor()` 在 L46 递增 `executor_pending`，在 L48 `await executor_lock.acquire()`（可取消点）。任务取消时跳过 L49 之后的 `return executor_lock`，计数器未递减。L57 的 `release_executor()` 仅在正常路径被调用。
- **影响文件**：`astrmai/infrastructure/runtime/chat_runtime_coordinator.py:41-61`
- **调用上下文**：被 `executor.py` 中的 `ConcurrentExecutor` 调用，每次 System2 推理前获取。
- **不修改**：`ChatRuntimeState` 的 `executor_lock` 实现类型（`asyncio.Lock`）。

---

## Out of Scope（不在本 Spec 范围内）

以下审计发现虽然具有 HIGH 或 MEDIUM 严重等级，但不属于本 Spec 的 CRITICAL 修复范围：

- HIGH: `main.py` 中 `on_llm_request` 修改 `system_prompt` 破坏提供商缓存
- HIGH: `event_bus.py` 中未追踪的后台任务和健康检查误数
- HIGH: `event_bus.py` 中 QueueFull 静默丢弃事件
- HIGH: `message_entry.py` 中注意力分发失败静默丢弃消息
- HIGH: `group_dialogue_store.py` 中 `str(None)` 变为键 `"None"`
- HIGH: `bootstrap.py` 中 ProactiveTask 创建失败静默禁用
- HIGH: `gate.py` 中传感器过滤器 `except Exception: pass`
- HIGH: `main.py` 中 3 个钩子的 `except Exception: pass` 完全静默
- MEDIUM: `plugin_helpers.py` 中 `safe_create_task` 用 `ensure_future` 而非 `create_task`
- MEDIUM: `memory_write_service.py` 中向量索引投影失败静默
- MEDIUM: `v2_store.py` 中会话锁 LRU 驱逐竞争
- MEDIUM: `handoff_registry.py` 中缓存永不过期
- MEDIUM: `lifecycle.py` 中 shutdown flush 静默失败
- MEDIUM: `user_profile_service.py` 中 TOCTOU 竞态
- MEDIUM: `memory_scoring.py` 中 `math.log(0)` → `-inf`

## High-Risk Confirmation List（高风险确认清单）

| # | 高风险事项 | 风险等级 | 缓解措施 |
|---|-----------|---------|---------|
| HR1 | R1 的双轨制方案可能引入新的工具集不一致 | 🔴 | 严格验证 Planner TOOL_CALL 路径下 SubAgent 调用能成功执行，不回归 full_tools 路径 |
| HR2 | R1 的修复涉及 AstrBot 核心 `ToolSet` 行为假设，核心库版本更新可能冲突 | 🟡 | 在 `get_light_tools_for_planner()` 中注释说明对 `get_light_tool_set()` 的依赖；考虑添加运行时检测 |
| HR3 | R4 的 `monotonic()` 替换可能影响持久化到 DB 的时间戳语义（若 `last_message_time` 被持久化） | 🟡 | 检查 `last_message_time` 是否被序列化到 DB；若是则用互补方案（`time.time()` 用于持久化，`monotonic()` 仅用于运行时比较） |
| HR4 | R5 的 `CancelledError` 处理可能被上游的 `except Exception` 捕获而失效 | 🔴 | 在 `CancelledError` handler 中显式 `raise`，确保该异常不被上游的 `except Exception` 吞掉 |
| HR5 | 五条修复的验证依赖于 AstrBot 运行环境，本地单元测试可能无法完全覆盖真实场景 | 🟡 | 每条修复提供 MockEvent 最小验证脚本；关键路径（R1/R4/R5）编写集成测试 |

## Dependency Map（需求依赖关系）

五条需求修改**不同文件**，无代码级依赖，可并行实现：

```
R1 (router.py + planner_side_inputs.py) ──┐
R2 (database_review.py)                 ──┤
R3 (gateway_call.py)                    ──┼── 并行实现
R4 (group_reply_wait_manager.py        ──┤
     + private_chat_manager.py)         ──┤
R5 (chat_runtime_coordinator.py)       ──┘
                                          │
                                          ▼
                                    全量回归验证 (Phase 5)
```

> **验证依赖**：R1 和 R5 都影响 System2 执行路径，需在回归验证阶段联合测试。

## Verification Strategy（验证策略）

| 验证层 | 命令/方式 | 覆盖需求 |
|--------|----------|---------|
| LSP 诊断 | `lsp_diagnostics` 对所有变更文件 | R1–R5 |
| 单元测试 | 为 R2/R3/R5 编写最小 `assert` 验证函数 | R2, R3, R5 |
| Mock 集成 | 构造 `MockEvent` + 模拟 Planner TOOL_CALL 路径（R1）；模拟时钟混用场景（R4） | R1, R4 |
| 全量回归 | `pytest` 运行现有测试套件（`tests/` 目录） | R1–R5 |
| 手动验证 | 启动 AstrBot + `/work` 命令触发 Sys3（R1）；@某人触发群聊等待（R4）；连续取消 System2 任务验证计数器恢复（R5） | R1, R4, R5 |

