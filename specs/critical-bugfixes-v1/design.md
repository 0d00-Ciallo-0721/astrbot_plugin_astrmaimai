# Design Document

> 本文档对应 Spec `critical-bugfixes-v1`，描述 AstrMai 插件 5 个 CRITICAL 运行时缺陷的修复设计方案。
> 不包含 HIGH/MEDIUM/LOW 级别的审计缺陷修复。凡涉及 AstrBot 核心库（`astrbot.core.*`）的改动，本阶段一律采用插件层适配方案，不修改核心库。

## 1. Overview

### 1.1 整体策略

按「最小修改 → 单文件修复 → 跨文件修复 → 全量回归」推进：

| 阶段 | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|---------|
| ① R3 raw_completion_text 初始化 | 变量预赋值 | `gateway_call.py` | 单行修复 |
| ② R2/R4/R5 单文件修复 | 任务追踪 / 时钟统一 / CancelledError 处理 | `database_review.py`, `group_reply_wait_manager.py`, `private_chat_manager.py`, `chat_runtime_coordinator.py` | 局部修复 |
| ③ R1 Sys3 轻量工具集双轨制 | 新增 `_raw_agent_map` 兜底 | `router.py`, `planner_side_inputs.py` | 架构适配 |
| ④ 回归验证 | 全量测试 + LSP | 全部变更文件 | 验证 |

### 1.2 设计边界（重申）

- **不**修改 AstrBot 核心库 (`astrbot.core.agent.tool.ToolSet`, `astrbot.core.agent.tool_function_executor`)
- **不**重构嵌套 try/except 架构 (`gateway_call.py` 的 L185+L303)
- **不**修改 `ChatRuntimeState` 的 `executor_lock` 类型
- **不**新增外部依赖
- **不**修改配置系统或 `_conf_schema.json`

### 1.3 与 AstrBot 核心库的接口预留

| 预留点 | 位置 | 用途 |
|--------|------|------|
| `ToolSet.get_light_tool_set()` 行为假设 | `router.py:35` | 已知该方法创建裸 `FunctionTool`，返回的实例 `handler=None`。本 Spec 通过保留 `_raw_agent_map` 索引来规避该行为 |
| `FunctionToolExecutor._execute_local()` 的 MRO 检查 | AstrBot 核心 | 不可改；本 Spec 确保传递给 `tool_loop_agent` 的工具实例具有有效的 `call()` 覆盖 |

## 2. Architecture

### 2.1 修复涉及的系统模块

```
planner_side_inputs.py ──→ _build_execution_tools() ──→ Sys3Router ──→ AstrBot ToolSet
       │                         (R1)                   (R1)             (不可改)
       │
executor.py ──→ _acquire_chat_execution_lock() ──→ ChatRuntimeCoordinator
       │              (R5 调用方)                         (R5)
       │
gateway_call.py ──→ _call_with_lane_key() ──→ LLM API
       │               (R3)                    (超时/重试)
       │
database_review.py ──→ save_pattern() ──→ canonical_memories
       │                  (R2)               (SQLite)
       │
group_reply_wait_manager.py ──→ handle_incoming_message() ──→ asyncio.sleep
       │                           (R4)
private_chat_manager.py ──→ cleanup_stale_sessions()
                               (R4)
```

### 2.2 关键不变量（本 Spec 阶段冻结）

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| `time.monotonic()` 用于运行时超时/间隔比较 | `group_reply_wait_manager.py:140` | 不受系统时钟调整影响 |
| `asyncio.create_task` 创建的任务必须附加 `add_done_callback` 或加入追踪集合 | `database_review.py:77` | 防止静默任务崩溃 |
| `executor_pending` 必须在所有退出路径（含 `CancelledError`）中被正确递减 | `chat_runtime_coordinator.py:46` | 防止永久阻塞 |
| SubAgent 实例的 `call()` 方法必须在传递给 `tool_loop_agent` 时保持可用 | `router.py:35` | Sys3 TOOL_CALL 路径的必要条件 |
| `raw_completion_text` 在所有使用它的异常分支前必须已初始化 | `gateway_call.py:207-309` | 防止 `NameError` 嵌套崩溃 |

---

## 3. Wave 1 — CRITICAL 运行时缺陷修复（R1–R5）

### 3.1 R1: Sys3 Planner TOOL_CALL 模式下 SubAgent 轻量工具集崩溃修复

**涉及文件**: `astrmai/workmode/router.py`, `astrmai/conversation/planning/planner_side_inputs.py`

#### 3.1.1 当前状态

**`router.py:33-35`** — `get_light_tools_for_planner()` 直接调用 AstrBot 核心的 `get_light_tool_set()`：
```python
# router.py:33-35 (CURRENT)
async def get_light_tools_for_planner(self) -> ToolSet:
    full_set = ToolSet(await self.get_all_agents())
    return full_set.get_light_tool_set()  # ⚠ 返回裸 FunctionTool，handler=None
```

**`planner_side_inputs.py:391-392`** — `_build_execution_tools()` 获取轻量工具并直接注入执行工具列表：
```python
# planner_side_inputs.py:391-392 (CURRENT)
if is_tool_call_mode:
    sys3_light_tools = (await self.sys3_router.get_light_tools_for_planner()).tools
    # ... 这些裸 FunctionTool 被传入 tool_loop_agent ...
```

**调用链**：`_build_execution_tools` → `executor._run_tool_mode` → `gateway.tool_chat_in_lane_result` → `AstrBot tool_loop_agent` → `_handle_function_tools` → `_execute_local` → 检测到 `handler=None` + `call()` 未覆盖 → **`ValueError`**

#### 3.1.2 设计决策

**方案：`Sys3Router` 内部维护 `_raw_agent_map`，`get_light_tools_for_planner()` 返回轻量 Schema 的同时保留真实 SubAgent 索引**

在 `Sys3Router` 中新增 `_raw_agent_map: dict[str, object]` 字段，存储 `name → 真实 SubAgent 实例` 的映射。`get_light_tools_for_planner()` 仍然返回轻量工具集（供 Planner 的 prompt 使用），但同时在返回的轻量工具上通过 monkey-patch 注入可用的 `handler` 引用。

**修复后代码（router.py）**：

```python
# router.py — 修复后
class Sys3Router:
    def __init__(self, plugin_config, context, db_service=None):
        # ... existing init ...
        self._raw_agent_map: dict[str, object] = {}  # ← 新增

    async def get_all_agents(self) -> list:
        static_names = {getattr(agent, "name", "") for agent in self._static_agents}
        dynamic_agents = await self._handoff_registry.discover(static_names)
        agents = [*self._static_agents, *dynamic_agents]
        # 更新索引
        self._raw_agent_map = {getattr(a, "name", ""): a for a in agents if getattr(a, "name", "")}
        return agents

    async def get_light_tools_for_planner(self) -> ToolSet:
        full_set = ToolSet(await self.get_all_agents())
        light_set = full_set.get_light_tool_set()
        # ponytail: inject handler refs so _execute_local won't raise ValueError
        for light_tool in light_set.tools:
            name = getattr(light_tool, "name", "")
            raw_agent = self._raw_agent_map.get(name)
            if raw_agent is not None:
                light_tool.handler = raw_agent.call  # ← 绑定真实 call()
        return light_set

    async def get_full_tools_for_direct_entry(self) -> ToolSet:
        return ToolSet(await self.get_all_agents())
```

**关键设计点**：
- `light_tool.handler = raw_agent.call` 利用了 AstrBot `_execute_local` 的 handler 优先逻辑：`_execute_local` 在 L419 检查 `func_tool.handler`，若不为 None 则通过 `handler(context, *args, **kwargs)` 调用。
- Monkey-patch 的是 `handler` 字段（而非 `call` 方法），避免了与 MRO 检查的冲突。
- `_raw_agent_map` 在每次 `get_all_agents()` 时更新，确保动态 Agent 也被索引。

#### 3.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `astrmai/workmode/router.py` | 新增 `_raw_agent_map` 字段（L15），在 `get_all_agents()` 中更新（L30），在 `get_light_tools_for_planner()` 中注入 handler（L36-38） | +5 |
| `astrmai/conversation/planning/planner_side_inputs.py` | 无需修改（`_build_execution_tools` 调用方不变） | 0 |
| **Total** | **1 个文件** | **~5 行** |

#### 3.1.4 禁止改动

- **不**修改 AstrBot 核心的 `ToolSet.get_light_tool_set()` 方法
- **不**修改 `planner_side_inputs.py` 中的 `_build_execution_tools()` 逻辑
- **不**修改 `_execute_local()` 的 MRO 检查逻辑

---

### 3.2 R2: `database_review.py` Fire-and-Forget 任务追踪

**涉及文件**: `astrmai/infrastructure/persistence/database_review.py`

#### 3.2.1 当前状态

```python
# database_review.py:71-79 (CURRENT)
def save_pattern(self, pattern: ExpressionPattern):
    service = getattr(getattr(self, "memory_engine", None), "expression_pattern_service", None)
    if service and hasattr(service, "write_pattern"):
        try:
            asyncio.get_running_loop()
            # ponytail: fire-and-forget canonical write from async context
            asyncio.create_task(self._save_pattern_to_canonical_async(pattern))  # ⚠ 无追踪
        except RuntimeError:
            asyncio.run(self._save_pattern_to_canonical_async(pattern))
```

**问题**：`asyncio.create_task(...)` 创建的任务没有被追踪（无 `add_done_callback`，无 `_background_tasks.add()`）。任务失败时 Python 仅在 stderr 输出 `Task exception was never retrieved`。

#### 3.2.2 设计决策

**方案：为 fire-and-forget 任务附加 `add_done_callback` 进行错误日志记录**

由于 `save_pattern()` 是一个同步方法（被 ORM 层调用），无法直接 `await`。保留 fire-and-forget 模式，但附加错误处理：

```python
# database_review.py:71-79 — 修复后
def save_pattern(self, pattern: ExpressionPattern):
    service = getattr(getattr(self, "memory_engine", None), "expression_pattern_service", None)
    if service and hasattr(service, "write_pattern"):
        try:
            asyncio.get_running_loop()
            task = asyncio.create_task(self._save_pattern_to_canonical_async(pattern))
            task.add_done_callback(
                lambda t, p=pattern: (
                    logger.exception(f"[DatabaseReview] canonical save failed for pattern {getattr(p, 'id', '?')}")
                    if t.exception() else None
                )
            )
        except RuntimeError:
            asyncio.run(self._save_pattern_to_canonical_async(pattern))
```

**关键设计点**：
- `add_done_callback` 在任务完成时被调用（无论成功或失败），通过 `t.exception()` 检查是否有异常。
- Lambda 使用默认参数 `p=pattern` 捕获 pattern 对象的引用，避免闭包变量延迟绑定的问题。
- 只记录错误日志，不影响主流程（best-effort 保存）。
- 不需要将任务加入 `_background_tasks`，因为 shutdown 时任务的取消由 asyncio 的 TaskGC 处理。

#### 3.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `astrmai/infrastructure/persistence/database_review.py` | L77: 拆分 `create_task` 为变量，附加 `add_done_callback` | +4/-1 |
| **Total** | **1 个文件** | **~3 行** |

#### 3.2.4 禁止改动

- **不**修改 `_save_pattern_to_canonical_async()` 方法本身
- **不**将 `save_pattern()` 改为异步方法（破坏 ORM 层调用约定）

---

### 3.3 R3: `gateway_call.py` `raw_completion_text` 未初始化修复

**涉及文件**: `astrmai/infrastructure/gateway/gateway_call.py`

#### 3.3.1 当前状态

```python
# gateway_call.py:185-309 (CURRENT, 简化)
try:
    response = await ...  # LLM API 调用 (L180-204)
except Exception as exc:
    # ... 重试逻辑 (L185-204)，raw_completion_text 尚未赋值

try:
    content = getattr(response, "completion_text", "") or ""  # L207
    raw_completion_text = content                               # L209 ⚠ 仅在此赋值
    # ... 正常处理 (L210-302)
except Exception as exc:
    # L303-321: 使用 raw_completion_text 进行冷却处理
    self._open_model_cooldown(report_pool, model_id, f"{last_error} {raw_completion_text}")  # L313 ⚠ NameError!
```

**根因**：L207 的 `getattr(response, "completion_text")` 若失败 → 跳到内层 `except Exception` L303 → `raw_completion_text` 未绑定 → L313 使用时报 `NameError`。

#### 3.3.2 设计决策

**方案：在 try 块之前将 `raw_completion_text` 初始化为空字符串**

最小修复，一行变更：

```python
# gateway_call.py — 修复后 (在 L185 的 except 之前或在 L206 的 try 之前插入)
                    except Exception as exc:
                        # ... existing code ...
                        continue

                    raw_completion_text = ""  # ← 新增：在所有使用该变量的异常分支前初始化
                    try:
                        content = getattr(response, "completion_text", "") or ""
                        latency_ms = (time.perf_counter() - t0) * 1000
                        raw_completion_text = content
                        # ... rest unchanged ...
```

**插入位置**：L205 和 L206 之间（外层 `except Exception` 的 `continue` 之后，内层 `try` 之前）。该位置确保所有到达内层 `try` 块的执行路径都先经过 `raw_completion_text = ""` 的初始化。

#### 3.3.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `astrmai/infrastructure/gateway/gateway_call.py` | L205 后插入 `raw_completion_text = ""` | +1 |
| **Total** | **1 个文件** | **~1 行** |

#### 3.3.4 禁止改动

- **不**重构嵌套 try/except 结构
- **不**修改 `_open_model_cooldown()` 的调用逻辑

---

### 3.4 R4: 时钟源统一（monotonic vs time.time）

**涉及文件**: `astrmai/state/group_wait/group_reply_wait_manager.py`, `astrmai/state/private_chat/private_chat_manager.py`

#### 3.4.1 当前状态

**`group_reply_wait_manager.py`**：
```python
# L140 — 使用 monotonic() 设置超时
expires_at=monotonic() + self.timeout_sec,

# L171 — 使用 time.time() 获取当前时间（错误混用）
now = time.time()

# L178 — 比较不同时钟源（永远为 True）
if now >= state.expires_at:
```

**`private_chat_manager.py`**：
```python
# L89 — 使用 monotonic() 设置最后消息时间
session.last_message_time = monotonic()

# L144 — 使用 time.time() 计算静默时长（错误混用）
"silence_sec": time.time() - session.last_message_time,

# L171/174 — cleanup_stale_sessions 使用 time.time()
now = time.time()
silence_min = (now - session.last_message_time) / 60.0
```

#### 3.4.2 设计决策

**方案：统一使用 `time.monotonic()`，并为仅用于日志/展示的字段保留 `time.time()`**

| 文件 | 行号 | 当前 | 修复 |
|------|------|------|------|
| `group_reply_wait_manager.py` | L171 | `now = time.time()` | `now = monotonic()` |
| `private_chat_manager.py` | L144 | `time.time() - session.last_message_time` | `monotonic() - session.last_message_time` |
| `private_chat_manager.py` | L171 | `now = time.time()` | `now = monotonic()` |

**`private_chat_manager.py` 修复后（L144/L171）**：

```python
# L144 — 修复后
"silence_sec": monotonic() - session.last_message_time,

# L171 — 修复后
now = monotonic()
```

**`group_reply_wait_manager.py` 修复后（L171）**：

```python
# L171 — 修复后（需要确保文件顶部已 import monotonic）
now = monotonic()
```

**验证点**：检查 `group_reply_wait_manager.py` 是否已导入 `from time import monotonic`（L140 已使用 `monotonic()`，确认已导入）。检查 `private_chat_manager.py` 是否已导入（L89 已使用 `monotonic()`，确认已导入）。

#### 3.4.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `astrmai/state/group_wait/group_reply_wait_manager.py` | L171: `time.time()` → `monotonic()` | +1/-1 |
| `astrmai/state/private_chat/private_chat_manager.py` | L144: `time.time()` → `monotonic()`; L171: `time.time()` → `monotonic()` | +2/-2 |
| **Total** | **2 个文件** | **~3 行** |

#### 3.4.4 禁止改动

- **不**修改其他模块中对 `time.time()` 的正确使用（日志时间戳、记忆衰减、情绪衰减等）
- **不**将 `last_message_time` 改为同时存储两种时钟（最小修复原则）

---

### 3.5 R5: `executor_lock` 取消泄漏修复

**涉及文件**: `astrmai/infrastructure/runtime/chat_runtime_coordinator.py`

#### 3.5.1 当前状态

```python
# chat_runtime_coordinator.py:41-49 (CURRENT)
async def try_acquire_executor(self, chat_id: str, max_pending: int = 2) -> Optional[asyncio.Lock]:
    async with self._lock:
        state = self._states.setdefault(chat_id, ChatRuntimeState())
        if state.executor_pending >= max_pending:
            return None
        state.executor_pending += 1          # ← L46: 递增
        executor_lock = state.executor_lock
    await executor_lock.acquire()             # ← L48: 可取消点 ⚠
    return executor_lock                      # ← 取消后永远不到达
```

**调用方（executor.py:324）**不处理取消后的清理：
```python
# executor.py:321-338
async def _acquire_chat_execution_lock(self, chat_id: str):
    using_runtime_coordinator = self.runtime_coordinator is not None
    if using_runtime_coordinator:
        chat_lock = await self.runtime_coordinator.try_acquire_executor(chat_id, max_pending=2)
        # ⚠ 如果此处被取消，release_executor 不会被调用
```

#### 3.5.2 设计决策

**方案：在 `try_acquire_executor()` 中捕获 `CancelledError` 并递减计数器**

```python
# chat_runtime_coordinator.py:41-49 — 修复后
async def try_acquire_executor(self, chat_id: str, max_pending: int = 2) -> Optional[asyncio.Lock]:
    async with self._lock:
        state = self._states.setdefault(chat_id, ChatRuntimeState())
        if state.executor_pending >= max_pending:
            return None
        state.executor_pending += 1
        executor_lock = state.executor_lock
    try:
        await executor_lock.acquire()
    except asyncio.CancelledError:
        # ponytail: decrement on cancel to prevent permanent blockage
        async with self._lock:
            if chat_id in self._states:
                self._states[chat_id].executor_pending = max(0, self._states[chat_id].executor_pending - 1)
        raise
    return executor_lock
```

**关键设计点**：
- 在 `CancelledError` handler 中获取 `_lock` 并递减 `executor_pending`。使用 `self._states[chat_id]` 而非缓存 `state`，因为 `_states` dict 可能被并发修改。
- 显式 `raise` 确保 `CancelledError` 向上传播，不被静默吞掉。
- `max(0, ...)` 防止计数器变为负数（防御性编程）。

#### 3.5.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` | L48 改为 `try/except CancelledError`，添加递减逻辑 | +6/-1 |
| **Total** | **1 个文件** | **~5 行** |

#### 3.5.4 禁止改动

- **不**修改 `executor.py` 中的 `_acquire_chat_execution_lock()` 或 `_release_chat_execution_lock()` 方法
- **不**修改 `ChatRuntimeState` 的数据结构
- **不**添加新的 `release_executor` 重载签名

---

## 4. Risk Assessment

| # | 风险 | 等级 | 触发条件 | 缓解措施 |
|---|------|------|---------|---------|
| RSK1 | R1 的 `handler` monkey-patch 可能与 AstrBot 版本升级后 `_execute_local` 的 handler 调用约定不兼容 | 🟡 | AstrBot 核心 `_handle_function_tools` 的 `func_tool.handler` 检查逻辑变更（例如从检查 `handler` 改为检查 `call` 的 MRO） | 在 `get_light_tools_for_planner()` 中添加注释说明依赖；core 升级时运行 Sys3 冒烟测试 |
| RSK2 | R1 的 `_raw_agent_map` 与 `get_all_agents()` 中的动态 Agent 发现可能存在时序问题——若 `HandoffRegistry._loaded` 为 False 时首次调用 `get_all_agents()`，后续注册的 Agent 不会被索引 | 🟢 | 本 Spec 不修复 `HandoffRegistry._loaded` 缓存问题 | 当前 `_raw_agent_map` 在每次 `get_all_agents()` 时全量重建，不受缓存影响 |
| RSK3 | R4 的 `monotonic()` 替换在 `get_session_info()` L144 中用于计算 `silence_sec`，该字段可能被 WebUI admin API 消费并展示给用户。`monotonic()` 值对用户无意义 | 🟡 | WebUI 展示了 `silence_sec` 字段 | `monotonic()` 差值（`monotonic() - last_message_time`）在秒级别上是正确的相对时间，展示值与 `time.time()` 差值语义一致 |
| RSK4 | R5 的 `CancelledError` handler 在 `async with self._lock:` 中再次获取了 `_lock`，而调用方 `try_acquire_executor` 已经释放了该锁。若 `CancelledError` 在 `executor_lock.acquire()` 返回后被触发（即已获取锁但未 `return`），handler 中再次获取 `_lock` 不会死锁 | 🟢 | 无 | `executor_lock.acquire()` 返回后立即 `return executor_lock`，中间无 `await` 点，`CancelledError` 不会在该窗口发生 |
| RSK5 | 五条修复在同一个代码库中，但修改不同文件。若 AstrBot 框架在修复期间升级，可能引入新的不兼容 | 🟡 | AstrBot 版本升级（如 v4.26 → v4.27） | 所有修复使用现有 API 约定（`asyncio.create_task`, `time.monotonic`, `FunctionTool.handler`），不依赖未发布的 API |

## 5. Verification Matrix

| 需求 | 验证方式 | 文件 | 通过标准 |
|------|---------|------|---------|
| R1 | 构造 MockEvent 设置 `judge_action="TOOL_CALL"`、`extra["astrmai_action_tier"]="sys3"`，触发 `_build_execution_tools(is_tool_call_mode=True)`，检查返回的 `deduped_sys3_tools` 中每个元素的 `handler` 不为 None | `router.py`, `planner_side_inputs.py` | 所有 SubAgent 工具 `handler is not None` |
| R1 | 手动启动 AstrBot，发送 `/work 帮我计算 1+1`，验证 ComputerAgent 被成功调用 | 全链路 | Bot 返回计算结果，无 `ValueError` |
| R2 | 检查 `database_review.py:77` 中 `create_task` 返回的 task 是否有 `add_done_callback` 调用 | `database_review.py` | `task.add_done_callback` 存在 |
| R2 | 模拟 `_save_pattern_to_canonical_async` 抛出异常，检查日志中是否有 `[DatabaseReview] canonical save failed` 错误记录 | `database_review.py` | `logger.exception()` 被调用 |
| R3 | 检查 `gateway_call.py` L205 和 L206 之间是否存在 `raw_completion_text = ""` 初始化 | `gateway_call.py` | 变量在 `try` 块之前初始化 |
| R3 | 在 `getattr(response, "completion_text")` 调用处注入 mock（模拟缺失属性），验证不再抛出 `NameError` | `gateway_call.py` | 正常进入 `_open_model_cooldown`，无 `NameError` |
| R4 | 检查 `group_reply_wait_manager.py:171` 使用 `monotonic()` 而非 `time.time()` | `group_reply_wait_manager.py` | `now = monotonic()` |
| R4 | 检查 `private_chat_manager.py:144,171` 使用 `monotonic()` 而非 `time.time()` | `private_chat_manager.py` | `monotonic()` 替换 `time.time()` |
| R4 | 手动触发群聊 @某人 → 等待 → 验证等待状态不会立即过期 | 全链路 | Bot 等待指定秒数后超时，而非立即过期 |
| R5 | 检查 `try_acquire_executor` 中 `await executor_lock.acquire()` 被 `try/except CancelledError` 包裹 | `chat_runtime_coordinator.py` | `except asyncio.CancelledError:` 存在 |
| R5 | 模拟连续 2 次在 `try_acquire_executor` 中取消任务，验证第 3 次调用仍能成功获取锁 | `chat_runtime_coordinator.py` | `executor_pending` 被正确递减 |
| 全量回归 | `pytest` 运行 `tests/` 目录 | 全部测试文件 | 所有测试通过（或预存在的失败数量不增加） |
| 全量回归 | `lsp_diagnostics` 对全部变更文件 | 全部变更文件 | 无 error 级别诊断 |

## 6. 变更文件汇总

| # | 文件 | 改动类型 | 行数估计 |
|---|------|------|:------:|
| 1 | `astrmai/workmode/router.py` | 新增 `_raw_agent_map` + handler 注入 | +5 |
| 2 | `astrmai/infrastructure/persistence/database_review.py` | `create_task` 附加 `add_done_callback` | +4/-1 |
| 3 | `astrmai/infrastructure/gateway/gateway_call.py` | 变量预初始化 | +1 |
| 4 | `astrmai/state/group_wait/group_reply_wait_manager.py` | `time.time()` → `monotonic()` | +1/-1 |
| 5 | `astrmai/state/private_chat/private_chat_manager.py` | `time.time()` → `monotonic()` ×2 | +2/-2 |
| 6 | `astrmai/infrastructure/runtime/chat_runtime_coordinator.py` | `CancelledError` handler | +6/-1 |
| **Total** | **6 个文件** | | **~17 行** |
