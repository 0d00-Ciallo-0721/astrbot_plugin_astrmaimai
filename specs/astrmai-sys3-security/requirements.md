# Requirements Document

## Introduction

本 Spec 为「AstrMai」插件 Sys3 多 Agent 子系统中识别出的 **5 个安全审查项** 制定审计/加固需求文档。M1（ComputerAgent 沙箱）已在 Round 1 的 H1 中修复，不在本 Spec 范围。本 Spec 聚焦 M2–M6：Router 工具集验证、Sys2→Sys3 交接正确性、SubAgent provider_id 检查、动态 Agent 注入安全、Cron 恢复的 session 隔离。

当前阶段产出物为 `specs/astrmai-sys3-security/` 下的 `requirements.md` / `design.md` / `tasks.md`。

明确不在本 Spec 范围：
- ComputerAgent 沙箱（已修复）
- 硬伤修复（Round 1 已完成）
- 状态机竞态（Round 2 已完成）
- 新功能开发、依赖升级

## Glossary

- **Sys3Router**：`astrmai/workmode/router.py` — 多 Agent 调度中枢
- **HandoffRegistry**：`astrmai/workmode/tools/handoff_registry.py` — 从 AstrBot WebUI 发现动态 SubAgent
- **CronHeartbeatGuard**：`astrmai/workmode/cron_guard/heartbeat.py` — 定时任务 snapshot 恢复守护
- **AstrMaiBaseSubAgent**：`astrmai/workmode/subagents/base_agent.py` — 所有 SubAgent 的抽象基类
- **_build_execution_tools()**：`astrmai/conversation/planning/planner_side_inputs.py:L377` — Sys2→Sys3 工具交接点
- **get_light_tool_set()**：AstrBot 框架方法，压缩工具描述以降低 Planner token 消耗

## Requirements

### Requirement 2: 验证 Router 的 `get_light_tool_set()` 使用正确性

**User Story:** 作为关注 LLM token 消耗的运维人员，我希望 Sys3Router 在 Planner 使用的工具集是轻量压缩版，所以 Router 的上下文消耗不会随着 SubAgent 数量增长而线性膨胀。

#### Acceptance Criteria

1. THE `Sys3Router.get_light_tools_for_planner()`（L33-35）SHALL 使用 `ToolSet.get_light_tool_set()` 压缩工具描述，确认当前实现已正确。
2. THE `Sys3Router.get_full_tools_for_direct_entry()`（L37-38）SHALL 返回完整 ToolSet（用于 `/work` 命令专属 lane），确认与 light 版本的行为差异符合设计意图。
3. THE `HandoffRegistry.discover()` 返回的动态 agents SHALL 同样经过 `get_light_tool_set()` 压缩（确认 `get_all_agents()` 返回的列表中包含动态 agents 且 light 版本也包含它们）。

#### Notes / Constraints

- 涉及文件：`astrmai/workmode/router.py` L33-38
- 当前状态：L33-35 已使用 `get_light_tool_set()` ✅，L37-38 使用完整 ToolSet ✅
- 本次为**审计确认**，非修复。需验证动态 agents 也包含在 light set 中。
- 验证：检查 `get_all_agents()` 的返回结果在 `get_light_tools_for_planner()` 和 `get_full_tools_for_direct_entry()` 中是否一致。

---

### Requirement 3: Sys2→Sys3 交接 — `_build_execution_tools()` 工具合并正确性

**User Story:** 作为依赖 Sys3 工作模式执行任务的用户，当 Planner 的 `is_tool_call_mode=True` 时，我不希望 LLM 可用的工具集中遗漏 `WaitTool`、`OmniPerceptionTool` 或 `SelfLoreQueryTool`，所以 Planner→Sys3 的交接是完整且正确的。

#### Acceptance Criteria

1. THE `_build_execution_tools()`（L377-406）SHALL 在 `is_tool_call_mode=True` 时包含以下工具：`WaitTool`、`OmniPerceptionTool`、`SelfLoreQueryTool`、以及 `sys3_light_tools`（来自 Router 的轻量 SubAgent 集）。
2. THE `OmniPerceptionTool` SHALL 正确传入 `memory_engine`、`db_service`、`chat_id`、`sender_id`、`sender_name` 参数（L398-405）。
3. THE `sys3_light_tools` 列表 SHALL 与 `WaitTool`/`OmniPerceptionTool`/`SelfLoreQueryTool` 合并后去重——确认不存在同名工具覆盖（如动态 Agent 名称与 WaitTool 同名）。
4. THE `SelfLoreQueryTool` 的构造参数 SHALL 被验证为非 None（确认 `persona_lore_service`、`memory_context_builder` 等依赖已正确初始化）。

#### Notes / Constraints

- 涉及文件：`astrmai/conversation/planning/planner_side_inputs.py` L377-406
- 当前状态：L391-406 已包含 WaitTool + OmniPerceptionTool + SelfLoreQueryTool + sys3_light_tools ✅
- 本次为**审计确认** + 防御性加固：增加工具名去重检查 + 参数非空断言。
- 验证：构造 `is_tool_call_mode=True` 的 Planner 调用 → 确认工具列表包含 4 类工具 → 确认无同名覆盖。

---

### Requirement 4: SubAgent 内部重新获取 `provider_id` 正确性

**User Story:** 作为依赖 SubAgent 独立推理能力的用户，当 SubAgent 从 Planner 的 tool_call_mode 或 `/work` 命令被调用时，我不希望 SubAgent 使用错误的 `provider_id`（例如继承了调用方的 provider 而非自己的独立 provider），所以 SubAgent 的 LLM 调用是隔离的。

#### Acceptance Criteria

1. THE `AstrMaiBaseSubAgent.call()`（L53-89）SHALL 在 L61 通过 `ctx.get_current_chat_provider_id(event.unified_msg_origin)` 重新获取 `provider_id`，确认当前实现已正确。
2. THE `provider_id` 获取失败时 SHALL 返回 `[SUBAGENT_ERROR]` 而非静默使用默认 provider（L62-64 已实现 ✅）。
3. THE `ComputerAgent` 和 `CronAgent` 的 `call()` 方法 SHALL NOT 重写 `provider_id` 获取逻辑（应使用基类实现）。

#### Notes / Constraints

- 涉及文件：`astrmai/workmode/subagents/base_agent.py` L61-64
- 当前状态：L61 已重新获取 `provider_id` ✅，异常处理正确 ✅
- 本次为**审计确认**，非修复。
- 验证：检查 `ComputerAgent.call()` 和 `CronAgent.call()` 是否重写了 provider_id 获取逻辑。

---

### Requirement 5: Dynamic Agents 注入安全 — HandoffRegistry 过滤加固

**User Story:** 作为插件安全管理员，当 AstrBot WebUI 中注册了动态 SubAgent（通过 `subagent_orchestrator.handoffs`）时，我不希望动态 Agent 的名称与静态 Agent（CronAgent、ComputerAgent）冲突导致覆盖，也不希望未经验证的动态 Agent 被注入到 Planner 的工具集中。

#### Acceptance Criteria

1. THE `HandoffRegistry.discover()`（L15-33）SHALL 在 L26 通过 `agent_name in static_names` 过滤同名动态 Agent，确认当前实现已正确。
2. THE `HandoffRegistry` SHALL 在注入动态 Agent 前验证 `getattr(handoff, "active", True)` 为 `True`（当前未检查，应新增）。
3. THE `HandoffRegistry` SHALL 在日志中记录每个被注入的动态 Agent 的 `provider_id` 和 `name`（L30-32 已实现 ✅）。
4. THE `Sys3Router.get_all_agents()`（L25-28）SHALL 将静态 agents 排在动态 agents 之前，确保静态 agents 优先匹配（当前 L28 `[*self._static_agents, *dynamic_agents]` 已实现 ✅）。

#### Notes / Constraints

- 涉及文件：`astrmai/workmode/tools/handoff_registry.py` 全文 48 行
- 当前状态：L26 已有名称过滤 ✅；L28 静态优先 ✅
- 需新增：L24 循环中增加 `if not getattr(handoff, "active", True): continue`
- 验证：构造 WebUI 中注册 `transfer_to_computer` 同名 Agent → 确认被过滤 → 构造 `active=False` 的 Agent → 确认不被注入。

---

### Requirement 6: CronHeartbeatGuard job 恢复的 session 隔离

**User Story:** 作为多群部署的 Bot 管理员，当 CronHeartbeatGuard 在启动时恢复 DB 中残留的定时任务时，我不希望其他 session（如另一个群或另一个 unified_msg_origin）的 cron job 被错误地恢复到当前 session，所以 job 恢复是按 session 隔离的。

#### Acceptance Criteria

1. THE `CronHeartbeatGuard.reload_all_lost_jobs()`（L20-44）SHALL 在恢复 job 前验证 `snap.target_origin` 匹配当前活跃 session 的 `unified_msg_origin`（或提供全局恢复开关）。
2. THE `_revive_job()`（L97-111）SHALL 在构造 `CronJob` 时保留 `target_origin` 字段，使恢复后的 job 不会跨 session 触发。
3. WHEN `snap.target_origin` 为空（旧数据），THE 恢复逻辑 SHALL 将其视为兼容模式并正常恢复（附加 warning 日志）。
4. THE `CronSnapshot` ORM 模型 SHALL 在 `reload_all_lost_jobs()` 中读取 `snap.target_origin` 字段（确认 `get_all_active_cron_snapshots()` 已包含此字段）。

#### Notes / Constraints

- 涉及文件：`astrmai/workmode/cron_guard/heartbeat.py` L20-111
- 当前状态：`reload_all_lost_jobs()` 恢复所有 active snapshot，不检查 session 归属。如果一个群 A 创建了 cron job → DB snapshot 保存 → Bot 重启 → 群 B 的 heartbeat 触发 → 群 A 的 job 被恢复到群 B 的 context 中（如果 `cron_manager` 是全局共享的，则可能正确；如果是 per-session 的，则错误）。
- 需确认：AstrBot 的 `cron_manager` 是全局单例还是 per-session？如果是全局单例，当前行为正确；如果是 per-session，需增加 session 过滤。
- 验证：两个群分别创建 cron job → 重启 → 确认各自群只恢复自己的 job。

---

## Out of Scope

- M1 ComputerAgent 沙箱（Round 1 已修复）
- 硬伤修复、状态机竞态（已完成）
- SubAgent 功能新增
- Cron job 执行逻辑修改

## High-Risk Confirmation List

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| HK1 | M5 动态 Agent `active` 检查新增 → 如果现有 WebUI 动态 Agent 未设置 `active=True`，升级后可能被意外过滤 | 🟡 | 默认值 `True`（`getattr(handoff, "active", True)`），未设置时不过滤 |
| HK2 | M6 session 过滤新增 → 如果 `cron_manager` 是全局单例，增加 session 过滤反而会阻止合法的跨 session 恢复 | 🟡 | 先确认 AstrBot cron_manager 的架构（全局 vs per-session），再决定是否加过滤 |

## Dependency Map

```
M2 (审计) → M3 (审计) → M5 (加固) → M6 (session 隔离)
                  ↘ M4 (审计)
```

M2/M3/M4 为审计确认（只读），可并行。M5/M6 为加固改动，建议 M5 先于 M6。

## Verification Strategy

| 验证层 | 方式 | 覆盖 |
|--------|-----|:--:|
| 审计 | 代码阅读确认 M2 L33-35 | M2 |
| 审计 | 代码阅读确认 M3 L390-406 | M3 |
| 审计 | 代码阅读确认 M4 L61-64 | M4 |
| 单元 | `HandoffRegistry` active 过滤 | M5 |
| 单元 | `CronHeartbeatGuard` session 过滤 | M6 |
| 集成 | 多群 cron job 恢复隔离 | M6 |
| LSP | 全部变更文件 | M5, M6 |
