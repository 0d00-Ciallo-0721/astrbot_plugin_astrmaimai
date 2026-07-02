# Design Document — AstrMai 第二轮审查阻断级修复

> 本文档对应 Spec `astrmai-critical-round7-20260630`
> 基于 `requirements.md` 中 5 条需求（R1–R5），按 2 个 Wave 展开模块设计。
> **不包含**：非阻断级缺陷、架构重构、新功能开发。

---

## 1. Overview

### 1.1 整体策略

按「聊天修复 → 配置修复」两阶段推进：

| 阶段 | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|:--:|
| ① Wave 1 | R1–R4 四个独立修复，可并行 | 4 | 局部改动 |
| ② Wave 2 | R5 热重载传播 | 1 + ~13 | 批量加 `refresh_config()` |

### 1.2 设计边界

| 禁止项 | 原因 |
|--------|------|
| **不修改 Gateway 重试逻辑** | 仅修 bug，不重构 |
| **不重写 SubAgent 架构** | 仅接入路由层，不引入 lane 管理 |
| **不新增外部依赖** | 图片下载用 `aiohttp`（AstrBot 已依赖） |
| **不改变数据库 schema** | 纯运行时修复 |

### 1.3 与已完成修复的接口

| 已完成项 | 本 Spec 使用方式 |
|---------|----------------|
| `safe_create_task()` | Gateway 层已导入，无需改动 |
| `monotonic()` 导入 | 冷却修复不涉及时间源变更 |
| Hook try/except 保护 | R1 修复后 System2 正常 → Hook 能正常触发 |

---

## 2. Architecture

### 2.1 变更前后对比

```
变更前（Bug 状态）:
─────────────────────────────────────────────
System2    [attention_gate] → await sys2_process() → TypeError!
           _system2_entry 被 yield 污染为 async generator

Gateway    _cleanup_model_cooldowns() → NameError on 'cooldowns'
           模型冷却永久生效

SubAgent   ctx.tool_loop_agent() ─── 绕开 Gateway ───→ AstrBot provider
           无路由、无重试、无健康检查

Vision     extract_image_base64_from_url() → return ""
           所有远程图片被丢弃

HotReload  apply_hot_config() ─→ runtime.config ✅
                                proactive.config ✅
                                其余 13 个组件 ❌ 仍用旧配置


变更后（修复状态）:
─────────────────────────────────────────────
System2    [attention_gate] → await sys2_process() → reply_engine.handle_reply()
           _system2_entry 正常 async def，回退消息通过 await 发送

Gateway    _cleanup_model_cooldowns() → getattr(self, "_model_cooldowns", {})
           冷却正常过期

SubAgent   gateway.chat_in_lane() ─→ 正常路由 → LLM
           共享 Gateway 的健康/重试/冷却

Vision     aiohttp.get(url) → base64 → 正常到达视觉模型

HotReload  apply_hot_config() ─→ 遍历所有组件 → refresh_config()
           全部 15 个组件同步更新
```

### 2.2 关键不变量（本阶段冻结）

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| `_system2_entry` 签名不变 | `plugin_facade.py:427` | 调用方不修改 |
| SubAgent `ToolExecResult` 格式不变 | `base_agent.py` | LLM 工具契约 |
| `extract_image_base64_from_url` 签名不变 | `vision_binding.py:33` | 调用方不修改 |
| `apply_hot_config` 返回值 `bool` 不变 | `plugin_facade.py:80` | WebUI 调用方 |

---

## 3. Wave 1 — P0 聊天功能修复（R1–R4）

### 3.1 R1: `_system2_entry` yield → await 修复

**涉及文件**: `astrmai/app/plugin_facade.py`

#### 3.1.1 当前状态

```python
# plugin_facade.py:427-486 — 当前（BUG）
async def _system2_entry(self, main_event, events_to_process=None):
    if self.runtime.system2_runner:
        return await self.runtime.system2_runner.run(...)

    async with lock:
        try:
            ...
        except LLMCascadeFailureException:
            logger.exception(...)
            fallback = str(getattr(...))
            yield main_event.plain_result(fallback)  # ← BUG: yield makes this an async generator
        finally:
            ...
```

**根因**: `yield` 关键字使 Python 编译器将整个函数编译为 `async generator`。所有调用方（`attention_gate._debounce_and_judge:833`、`_engage_immediately:490`）使用 `await` 等待结果 → `TypeError`。

#### 3.1.2 设计决策

将 `yield` 替换为 `await reply_engine.handle_reply()`：

```python
# 修复后
async def _system2_entry(self, main_event, events_to_process=None):
    if self.runtime.system2_runner:
        return await self.runtime.system2_runner.run(...)

    async with lock:
        try:
            ...
        except LLMCascadeFailureException:
            logger.exception(...)
            fallback = str(getattr(getattr(self.runtime.config, "reply", None), "fallback_text", "")
                         or "（陷入了短暂的沉默...）")
            await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)
            # ← 修复: 用 await 发送回退消息，不再 yield
        finally:
            ...
```

**为什么安全**: `reply_engine.handle_reply` 内部调用 `event.send()` 或 `context.send_message()`，不回调 System2，无循环风险。

#### 3.1.3 影响范围

| 文件 | 改动 | 行数 |
|------|------|:--:|
| `plugin_facade.py:484` | `yield ...` → `await self.runtime.reply_engine.handle_reply(...)` | ±1 |

#### 3.1.4 禁止改动

- **不**修改 `system2_runner` 分支（line 428-429）
- **不**修改 `finally` 块

---

### 3.2 R2: `gateway_policy.py` cooldowns NameError

**涉及文件**: `astrmai/infrastructure/gateway/gateway_policy.py`

#### 3.2.1 当前状态

```python
# gateway_policy.py:15-19 — 当前（BUG）
def _cleanup_model_cooldowns(self) -> None:
    now = monotonic()
    for key, meta in list(cooldowns.items()):  # ← NameError: cooldowns 未定义
        if float(meta.get("until", 0.0) or 0.0) <= now:
            cooldowns.pop(key, None)
```

**对比同一类中正确写法**（`_model_cooldown_meta:22-24`）:
```python
def _model_cooldown_meta(self, pool_name, model_id):
    self._cleanup_model_cooldowns()  # ← 调用上述有 bug 的方法
    cooldowns = getattr(self, "_model_cooldowns", {})  # ← 正确获取
```

#### 3.2.2 设计决策

在 `_cleanup_model_cooldowns` 开头添加获取语句：

```python
# 修复后
def _cleanup_model_cooldowns(self) -> None:
    now = monotonic()
    cooldowns = getattr(self, "_model_cooldowns", {})  # ← 新增
    for key, meta in list(cooldowns.items()):
        if float(meta.get("until", 0.0) or 0.0) <= now:
            cooldowns.pop(key, None)
```

**`_model_cooldowns` 始终存在**: `GlobalModelGateway.__init__`（`gateway_call.py:36`）始终初始化 `self._model_cooldowns: dict = {}`。

#### 3.2.3 影响范围

| 文件 | 改动 | 行数 |
|------|------|:--:|
| `gateway_policy.py:16` | 加 `cooldowns = getattr(self, "_model_cooldowns", {})` | +1 |

#### 3.2.4 禁止改动

- **不**修改 `_model_cooldown_meta` 或 `_filter_cooldown_attempt_queue`（它们正确）

---

### 3.3 R3: SubAgent 接入 Gateway

**涉及文件**: `astrmai/workmode/subagents/base_agent.py`

#### 3.3.1 当前状态

```python
# base_agent.py:61,86-94 — 当前
async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
    ctx = context.context.context  # PluginRuntimeContext
    event = context.context.event   # AstrMessageEvent
    provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
    ...
    llm_resp = await ctx.tool_loop_agent(  # ← 裸 AstrBot provider，绕开 Gateway
        event=event,
        chat_provider_id=provider_id,
        prompt=query,
        ...
    )
```

`ctx.tool_loop_agent()` 是 AstrBot 框架的原始 API，不走 `GlobalModelGateway` 的路由/重试/冷却。

#### 3.3.2 设计决策

优先使用 Gateway，回退时保留裸调用：

```python
# 修复后
async def call(self, context, **kwargs):
    ctx = context.context.context
    event = context.context.event
    chat_id = event.unified_msg_origin

    gateway = getattr(ctx, "gateway", None)
    if gateway is not None:
        # 优先走 Gateway（享受路由/重试/冷却）
        try:
            result = await gateway.tool_chat_in_lane_result(
                lane_key=LaneKey(subsystem="sys3", task_family=self.name, scope_id=chat_id),
                base_origin=chat_id,
                event=event,
                prompt=query,
                system_prompt=self.system_prompt,
                tools=self.get_tool_set() if hasattr(self, "get_tool_set") else None,
                models=gateway.get_agent_models(),
                max_steps=getattr(self, "max_steps", 10),
                timeout=getattr(self, "tool_call_timeout", 30),
            )
            return result.text
        except Exception:
            logger.warning(f"[AstrMai-SubAgent] Gateway failed for {self.name}, falling back to raw provider")
    
    # 回退：裸 AstrBot provider
    provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
    llm_resp = await ctx.tool_loop_agent(...)
    return llm_resp.completion_text
```

**需要新增导入**: `from ...infrastructure.runtime.lane_manager import LaneKey`

#### 3.3.3 影响范围

| 文件 | 改动 | 行数 |
|------|------|:--:|
| `base_agent.py` | 加 Gateway 优先路径 + 回退 | +15 / -3 |

#### 3.3.4 禁止改动

- **不**修改 `ToolExecResult` 返回值格式
- **不**修改子类 `ComputerAgent`、`CronAgent` 的 `call()` 覆盖（如无覆盖则自动继承修复）
- **不**移除现有裸 `tool_loop_agent` 作为回退

---

### 3.4 R4: 远程图片 URL 下载

**涉及文件**: `astrmai/conversation/attention/vision_binding.py`

#### 3.4.1 当前状态

```python
# vision_binding.py:33-35 — 当前（BUG）
async def extract_image_base64_from_url(gate, url: str):
    logger.debug(f"[{gate.__class__.__name__}] remote image URLs are disabled: {url}")
    return ""  # ← 直接丢弃所有远程 URL
```

**根因**: 该函数被设计为"暂不支持"，但实际聊天平台（QQ/微信）的图片绝大多数以 URL 形式传递。

#### 3.4.2 设计决策

使用 `aiohttp`（AstrBot 已依赖）异步下载并转 base64：

```python
# 修复后
import base64
import aiohttp

async def extract_image_base64_from_url(gate, url: str) -> str:
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        logger.debug(f"[{gate.__class__.__name__}] unsafe image URL ignored: {url[:80]}")
        return ""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[{gate.__class__.__name__}] image download failed: HTTP {resp.status} for {url[:80]}")
                    return ""
                data = await resp.read()
                if len(data) > 10 * 1024 * 1024:  # 10MB limit
                    logger.warning(f"[{gate.__class__.__name__}] image too large: {len(data)} bytes")
                    return ""
                return base64.b64encode(data).decode("ascii")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning(f"[{gate.__class__.__name__}] image download failed: {exc}")
        return ""
```

**安全约束**:
- 仅允许 `http://` 和 `https://` 协议
- 10 秒超时防止阻塞
- 10MB 上限防止内存溢出

#### 3.4.3 影响范围

| 文件 | 改动 | 行数 |
|------|------|:--:|
| `vision_binding.py` | 函数体重写 | +20 / -3 |

#### 3.4.4 禁止改动

- **不**修改 `extract_image_base64_from_file`（本地文件路径处理）
- **不**新增外部依赖（`aiohttp` 已是 AstrBot 传递依赖）

---

## 4. Wave 2 — P1 热重载修复（R5）

### 4.1 R5: 热重载传播到全部子组件

**涉及文件**: `astrmai/app/plugin_facade.py` + 各子组件

#### 4.1.1 当前状态

```python
# plugin_facade.py:80-95 — 当前（不完整）
def apply_hot_config(self, config_dict, parsed_config) -> bool:
    self.runtime.raw_config = dict(config_dict)
    self.runtime.config = parsed_config
    if hasattr(self.runtime, "rebuild_infrastructure_settings"):
        self.runtime.rebuild_infrastructure_settings()
    proactive_task = getattr(self.runtime, "proactive_task", None)
    if proactive_task is not None and hasattr(proactive_task, "refresh_config"):
        proactive_task.refresh_config(parsed_config)
    if hasattr(self.runtime, "sync_host_compat_attrs"):
        self.runtime.sync_host_compat_attrs()
    return True
```

**失效的 13 个组件**（持有旧 `runtime.config` 引用）:

| 组件 | 配置文件 | 持有的旧引用 | 影响 |
|------|---------|------------|------|
| `GlobalModelGateway` | `model_gateway.py:30` | `self.config`, `self.settings` | 模型池、重试次数 |
| `LaneManager` | `lane_manager.py` | `self.config` | 会话管理 |
| `StateEngine` | `chat_state_service.py:197` | `self.config` | 状态管理 |
| `PreFilters` | `sensors.py` | `self.config` | 过滤器 |
| `FrequencyController` | `frequency_controller.py` | `self.config` | 频率控制 |
| `PrivateChatManager` | `private_chat_manager.py` | `self.config` | 私聊管理 |
| `AttentionGate` | `gate.py` | `self.config` | 注意力门 |
| `Judge` | `judge.py` | `self.gateway`（间接） | 判官 |
| `EvolutionManager` | `evolution_manager.py` | 间接 | 演化管理 |
| `MemoryEngine` | `memory_engine.py` | `embedding_models` | 记忆引擎 |
| `MoodManager` | `mood_manager.py` | `self.config` | 心情管理 |
| `EnergyManager` | `chat_state_service.py:205` | `self.config` | 能量管理 |
| `RelationshipEngine` | `chat_state_service.py:203` | `self.config` | 关系引擎 |

#### 4.1.2 设计决策

**最小改动策略**: 给每个持有 config 引用的组件加 `refresh_config(new_config)` 方法，`apply_hot_config` 中遍历调用。

```python
# plugin_facade.py — 修复后
def apply_hot_config(self, config_dict, parsed_config) -> bool:
    self.runtime.raw_config = dict(config_dict)
    self.runtime.config = parsed_config

    # Rebuild infrastructure settings first (gateway depends on it)
    if hasattr(self.runtime, "rebuild_infrastructure_settings"):
        self.runtime.rebuild_infrastructure_settings()

    # Refresh all components that hold cached config
    components = [
        ("gateway", getattr(self.runtime, "gateway", None)),
        ("lane_manager", getattr(self.runtime, "lane_manager", None)),
        ("state_engine", getattr(self.runtime, "state_engine", None)),
        ("sensors", getattr(self.runtime, "sensors", None)),
        ("frequency_controller", getattr(self.runtime, "frequency_controller", None)),
        ("private_chat_manager", getattr(self.runtime, "private_chat_manager", None)),
        ("attention_gate", getattr(self.runtime, "attention_gate", None)),
        ("evolution", getattr(self.runtime, "evolution", None)),
        ("memory_engine", getattr(self.runtime, "memory_engine", None)),
        ("proactive_task", getattr(self.runtime, "proactive_task", None)),
    ]
    for name, comp in components:
        if comp is not None and hasattr(comp, "refresh_config"):
            try:
                comp.refresh_config(parsed_config)
            except Exception as exc:
                logger.warning(f"[AstrMai] refresh_config failed for {name}: {exc}")

    # Host plugin compat
    if hasattr(self.runtime, "sync_host_compat_attrs"):
        self.runtime.sync_host_compat_attrs()
    return True
```

**各组件 `refresh_config` 最小实现**:

```python
# GlobalModelGateway (model_gateway.py) — 需要重建 settings
def refresh_config(self, config):
    self.config = config
    from ...shared.constants.defaults import build_infrastructure_settings
    self.settings = build_infrastructure_settings(config)

# StateEngine (chat_state_service.py) — 传播到子组件
def refresh_config(self, config):
    self.config = config
    if hasattr(self, "mood_manager"):
        self.mood_manager.config = config
    if hasattr(self, "energy_manager"):
        self.energy_manager.config = config
    if hasattr(self, "relationship_engine"):
        self.relationship_engine.config = config

# 其余组件 — 简单赋值
def refresh_config(self, config):
    self.config = config
```

#### 4.1.3 影响范围

| 文件 | 改动 | 行数 |
|------|------|:--:|
| `plugin_facade.py:80-95` | 重写，加遍历刷新 | +20 / -5 |
| `model_gateway.py` | 加 `refresh_config()` | +8 |
| `chat_state_service.py` | 加 `StateEngine.refresh_config()` | +8 |
| 其余 ~8 组件 | 各加 `refresh_config()`（简单赋值） | +1 × 8 |
| **合计** | **~11 文件** | **~+45** |

#### 4.1.4 禁止改动

- **不**重建设组件实例（避免破坏活跃会话）
- **不**修改 `config` 对象的内部结构
- **不**在 `refresh_config` 中做 IO 操作

---

## 5. Risk Assessment

| 风险 | 等级 | 触发条件 | 缓解措施 |
|------|:--:|------|---------|
| R1 `await reply_engine` 引入循环调用 | 🔴 | `handle_reply` 回调 System2 | 验证 `reply_engine` 不回调 System2；人工 code review |
| R3 Gateway 上下文穿透断裂 | 🟡 | `context.context.context` 链中某层为 None | `getattr(ctx, "gateway", None)` 防御 + 回退裸调用 |
| R4 HTTP 下载阻塞事件循环 | 🟡 | 图片 URL 响应慢 | `aiohttp` 异步 + 10s 超时 |
| R5 热重载锁竞争 | 🟡 | 重载期间并发请求读旧配置 | 当前重载为同步方法，asyncio 单线程无竞态 |
| R5 某组件 `refresh_config` 抛异常中断后续 | 🟡 | 某组件实现有 bug | try/except 包围每次调用，单个失败不阻断 |

---

## 6. Verification Matrix

| 需求 | 验证方式 | 通过标准 |
|------|---------|---------|
| R1 | ① `python -c "import ast; ast.parse(open('astrmai/app/plugin_facade.py').read())"` 无 `yield` 在 `_system2_entry` 中 | AST 解析无 async generator 标记 |
| R1 | ② `grep "yield" plugin_facade.py` | `_system2_entry` 方法中 0 匹配 |
| R2 | `python -c "from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin; print('ok')"` | Import 无 NameError |
| R3 | `grep "tool_loop_agent" base_agent.py` | 仅在回退分支中出现 |
| R4 | `grep 'return ""' vision_binding.py` | 仅在错误分支中出现（非函数开头） |
| R5 | `grep "refresh_config" plugin_facade.py` | ≥ 10 次（遍历列表 + 各组件方法） |
| 全量 | `pytest tests/ -q --ignore=tests/integration/runtime/test_runtime_contracts_migrated.py` | ≥ 810 passed |

### 变更汇总

| 维度 | 文件数 | 行数变化 |
|------|:--:|:--:|
| Wave 1 (R1–R4) | 4 | +40 / -10 |
| Wave 2 (R5) | ~11 | +45 / -5 |
| **合计** | **~15** | **~+85 / -15** |
