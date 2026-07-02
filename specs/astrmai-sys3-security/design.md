# Design Document

> 本文档对应 Spec `astrmai-sys3-security`，描述 Sys3 多 Agent 子系统的安全审计与加固设计。
> M2/M3/M4 为审计确认（零改动），M5/M6 为代码加固。
> 不包含 M1（已在 Round 1 修复）、硬伤修复、状态机竞态。

## 1. Overview

### 1.1 整体策略

| 审查项 | 动作 | 改动文件 | 改动类型 |
|--------|------|---------|---------|
| M2 Router light_tool_set | 审计确认 ✅ | — | 只读 |
| M3 Sys2→Sys3 交接 | 审计确认 + 防御 | `planner_side_inputs.py` | 工具名去重 |
| M4 SubAgent provider_id | 审计确认 ✅ | — | 只读 |
| M5 Dynamic Agent 过滤 | 新增 active 检查 | `handoff_registry.py` | 加固 |
| M6 Cron session 隔离 | 确认架构 → 条件加固 | `heartbeat.py` | 加固 |

### 1.2 设计边界

- 不修改 SubAgent 的工具加载逻辑
- 不修改 Planner 的主流程
- 不修改 AstrBot 框架的 `cron_manager` 行为
- 不新增配置项

---

## 2. Architecture — 关键不变量

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| `Sys3Router.get_light_tools_for_planner()` 使用 `get_light_tool_set()` | `router.py:L35` | M2 审计结论 |
| `AstrMaiBaseSubAgent.call()` L61 重新获取 `provider_id` | `base_agent.py:L61` | M4 审计结论 |
| 静态 agents 优先于动态 agents | `router.py:L28` | M5 依赖此顺序 |
| `HandoffRegistry.discover()` 仅运行一次（`_loaded` 锁） | `handoff_registry.py:L16` | M5 缓存语义 |

---

## 3. Module Designs

### 3.1 M2: Router light_tool_set 审计

**涉及文件**: `astrmai/workmode/router.py`

#### 当前状态（审计确认 ✅）

```python
# L33-35: 正确使用 get_light_tool_set()
async def get_light_tools_for_planner(self) -> ToolSet:
    full_set = ToolSet(await self.get_all_agents())
    return full_set.get_light_tool_set()  # ← 压缩工具描述

# L37-38: 正确使用完整 ToolSet
async def get_full_tools_for_direct_entry(self) -> ToolSet:
    return ToolSet(await self.get_all_agents())
```

`get_all_agents()` (L25-28) 返回 `[*self._static_agents, *dynamic_agents]`，动态 agents 也包含在 full_set 中 → light_tool_set 也包含它们 ✅。

**结论：无需改动。**

---

### 3.2 M3: Sys2→Sys3 交接审计 + 工具名去重防御

**涉及文件**: `astrmai/conversation/planning/planner_side_inputs.py`

#### 当前状态（审计确认 ✅ + 防御加固）

```python
# L390-406: 工具合并
if is_tool_call_mode:
    sys3_light_tools = (await self.sys3_router.get_light_tools_for_planner()).tools
    tools = [
        WaitTool(),
        OmniPerceptionTool(...),
        SelfLoreQueryTool(...),
        *sys3_light_tools,  # ← Router 的 SubAgent 工具
    ]
```

#### 设计决策

**审计确认**：工具合并逻辑正确 ✅。**防御加固**：增加工具名去重检查。

```python
# ★ 新增去重：防止动态 Agent 名称与 WaitTool/OmniPerceptionTool 冲突
tool_names = set()
deduped_sys3_tools = []
for tool in sys3_light_tools:
    name = getattr(tool, "name", "")
    if name not in tool_names:
        tool_names.add(name)
        deduped_sys3_tools.append(tool)
    else:
        logger.warning(f"[Planner] duplicate tool name '{name}' in sys3 tools, skipped")

tools = [
    WaitTool(),
    OmniPerceptionTool(...),
    SelfLoreQueryTool(...),
    *deduped_sys3_tools,
]
```

#### 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `planner_side_inputs.py` | `_build_execution_tools()` 增加去重逻辑 | +8 |

#### 禁止改动

- **不**修改工具合并的业务逻辑
- **不**修改 `WaitTool`/`OmniPerceptionTool`/`SelfLoreQueryTool` 的构造参数

---

### 3.3 M4: SubAgent provider_id 审计

**涉及文件**: `astrmai/workmode/subagents/base_agent.py`

#### 当前状态（审计确认 ✅）

```python
# L61: 重新获取 provider_id
provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)

# L62-64: 获取失败返回错误
except Exception as exc:
    return f"[SUBAGENT_ERROR] 无法连接到语言模型服务：{exc}"

# L88: 使用独立的 provider_id
chat_provider_id=provider_id,
```

`ComputerAgent` 和 `CronAgent` 不重写 `call()` 方法 → 使用基类实现 ✅。

**结论：无需改动。**

---

### 3.4 M5: HandoffRegistry 动态 Agent active 过滤

**涉及文件**: `astrmai/workmode/tools/handoff_registry.py`

#### 当前状态

```python
# L24-28: 仅过滤同名，未检查 active 状态
for handoff in getattr(orchestrator, "handoffs", []) or []:
    agent_name = getattr(handoff, "name", "")
    if not agent_name or agent_name in static_names:
        continue
    self._dynamic_agents.append(handoff)  # ← 无 active 检查
```

#### 设计决策

**增加 `active` 状态检查。** 默认 `True` 保持向后兼容。

```python
for handoff in getattr(orchestrator, "handoffs", []) or []:
    agent_name = getattr(handoff, "name", "")
    if not agent_name or agent_name in static_names:
        continue
    if not getattr(handoff, "active", True):  # ★ 新增
        logger.info(f"[Sys3Router] skip inactive dynamic agent: {agent_name}")
        continue
    self._dynamic_agents.append(handoff)
```

#### 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `handoff_registry.py` | `discover()` 增加 `active` 检查 | +3 |

#### 禁止改动

- **不**修改 `static_names` 过滤逻辑
- **不**修改 `_find_orchestrator()` 实现
- **不**修改 `_loaded` 缓存语义

---

### 3.5 M6: CronHeartbeatGuard session 隔离

**涉及文件**: `astrmai/workmode/cron_guard/heartbeat.py`

#### 当前状态

```python
# L20-44: 恢复所有 active snapshot，不检查 session
async def reload_all_lost_jobs(self) -> int:
    snapshots = await self.db_service.get_all_active_cron_snapshots()
    for snap in snapshots:
        if snap.job_id not in active_job_ids:
            if await self._revive_job(cron_mgr, snap):  # ← 无 session 过滤
                revived += 1
```

`_revive_job()` (L97-111) 构造 `CronJob` 时未包含 `target_origin`。

#### 设计决策

**步骤 1（审计）**：先确认 AstrBot `cron_manager` 是全局单例还是 per-session。搜索 `cron_manager` 的实例化方式。

**步骤 2（条件加固）**：
- 若 `cron_manager` 是**全局单例** → 所有 session 共享 → 当前行为正确，增加 docstring 说明全局语义 ✅
- 若 `cron_manager` 是**per-session** → 增加 `target_origin` 过滤 + `_revive_job()` 传递 `target_origin`

**防御性加固（无论架构如何）**：增加 warning 日志标注恢复的 job 来源 session。

```python
# reload_all_lost_jobs() 增加日志：
logger.info(
    f"[CronGuard] reviving job '{snap.name}' "
    f"from session '{snap.target_origin}' (id={snap.job_id})"
)
```

#### 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:--:|
| `heartbeat.py` | `reload_all_lost_jobs()` 增加日志 | +3 |
| `heartbeat.py` | `_revive_job()` 传递 `target_origin`（条件） | +3 |

#### 禁止改动

- **不**修改 `cron_manager` 的行为
- **不**修改 `CronJob` PO 的结构
- **不**删除现有 snapshot 恢复逻辑

---

## 4. Risk Assessment

| # | 风险 | 等级 | 触发条件 | 缓解 |
|---|------|:--:|---------|------|
| RSK1 | M3 新增去重可能过滤掉同名但不同实现的有效工具 | 🟢 | 动态 Agent 名称与内置工具同名 | 仅过滤同名（极少发生）+ warning 日志 |
| RSK2 | M5 `active=False` 的 Agent 被过滤 → 如果 WebUI 未设置 `active` 字段，默认 `True` 不受影响 | 🟢 | `getattr(handoff, "active", True)` 默认值 | 向后兼容 |
| RSK3 | M6 `cron_manager` 架构确认前不应加 session 过滤 | 🟡 | cron_manager 是全局单例 | 先审计再加固 |

## 5. Verification Matrix

| # | 需求 | 验证方式 | 通过标准 |
|---|------|---------|---------|
| V1 | M2 | 代码审计：`router.py` L35 确认 `get_light_tool_set()` | 已确认 ✅ |
| V2 | M3 | 单元：`_build_execution_tools(is_tool_call_mode=True)` 工具列表含 4 类 | WaitTool/OmniPerception/SelfLoreQuery/sys3_light 均存在 |
| V3 | M3 | 单元：同名工具去重 → 仅保留第一个 | 第二个被跳过 + warning 日志 |
| V4 | M4 | 代码审计：`base_agent.py` L61 确认重新获取 `provider_id` | 已确认 ✅ |
| V5 | M5 | 单元：`active=False` 的 handoff 不被注入 | `_dynamic_agents` 不含该 agent |
| V6 | M5 | 单元：`active=True`（默认）的 handoff 正常注入 | 向后兼容 |
| V7 | M6 | 代码审计：确认 `cron_manager` 架构 | 结论记录 |
| V8 | M6 | 集成：多群 cron job → 重启 → session 隔离 | 各群恢复各自的 job |
| V9 | ALL | `lsp_diagnostics` 变更文件 | 0 error |
