# 审查报告：astrmai/webui/
> task_id: r12-webui | 审查时间: 2025-07-15T10:00:00+08:00

## 概述
- 审查文件数: 23
- 发现总数: 17
- 严重: 5 | 中等: 7 | 建议: 5

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | **backend/services/admin_ui_service.py:26-37** | **God Object 反模式 —— `AdminUiService` (1148 行) 的 7 域服务全部循环委托回自身。** `ObservabilityService`、`HeartflowService`、`CognitionService`、`ChatRuntimeService`、`SchedulerService`、`LearningService`、`ToolsService` 的构造函数仅保存 `plugin_api`，其所有 `async` 方法均通过 `from .admin_ui_service import AdminUiService; return await AdminUiService(self._api).xxx()` 委托回 `AdminUiService` 的对应方法。这导致 `AdminUiService` 承载了全部 7 个领域的约 40+ 个 public 方法，形成**单向依赖环**：路由 → 领域服务 → AdminUiService → 领域服务（import 循环被运行时 import 绕过，但逻辑耦合极深）。修改任一领域逻辑都需修改同一个 1148 行文件。 |
| 2 | **backend/services/admin_ui_service.py:323-328 (clear_heartflow_cooldowns)** 以及 **backend/services/admin_ui_service.py:1136-1148 (clear_chat_runtime)** | **直接修改 manager 的私有属性（`_pulses_by_chat`、`_impulse_decisions_by_chat`、`_action_decisions_by_chat`）。** 使用 `getattr(manager, "_pulses_by_chat", {}).pop(chat_id, None)` 直接操作下划线前缀的内部字典。这些属性无公开 API 契约，运行时对象的内部命名一旦重构，WebUI 将静默失效（`.pop()` 在属性不存在时返回 `{}` 并丢弃 `chat_id` 参数，不会报错）。属于**封装破坏**。 |
| 3 | **backend/services/memory_ui_service.py:206-207** | **SQL 注入风险 —— `_columns()` 使用 f-string 拼接表名。** `f"PRAGMA table_info({table})"` 中 `table` 参数直接来自调用者（`_insert`、`_update`）。虽然当前调用链中表名是硬编码的常量（`"MemoryEvent"`、`"canonical_memories"`、`"DailyReflection"`、`"MemoryNode"`、`"Jargon"`），但 `_insert` 和 `_update` 本身作为公开方法接受任意 `table` 参数，未来若新增路由或重构时传入用户可控的表名，即可触发注入。同时 `_insert` 和 `_update` 也使用 `f"INSERT INTO {table}"` / `f"UPDATE {table} SET ..."`（第 217、228 行）。 |
| 4 | **backend/adapters/plugin_api.py:23** | **全局可变状态 `ACTIVE_FACADE` 暴露底层 Runtime 对象。** `ACTIVE_FACADE` 是一个模块级全局变量，类型为 `Any`，存储完整的 `RuntimeFacadeProtocol` 实例。`PluginApiAdapter.facade` 属性（第 42 行 `self.facade`）被全代码库直接读取：`self.plugin_api.facade is not None` 出现在至少 30+ 处。若恶意代码（如插件或中间件）获取到 `ACTIVE_FACADE`，即可调用该 Protocol 上的所有方法（`get_planner`、`get_gateway`、`get_runtime_diagnostics` 等）。`set_active_facade()` 虽有日志警告但无访问控制。 |
| 5 | **backend/server.py:10-14** | **CORS 配置允许任意来源（`allow_origins=["*"]`）。** 尽管 `allow_credentials=False` 略微缓解了凭证泄露风险，但 `allow_origins=["*"]` + `allow_methods=["*"]` + `allow_headers=["*"]` 意味着任何网站均可发起跨域请求。若用户在内网环境中使用 WebUI 且同时浏览恶意站点，该站点可读取 `/api` 端点响应（非凭证请求）。对于管理面板应限制为特定来源或同源。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 6 | **backend/services/memory_ui_service.py:234-240** | **SQL fallback 路径绕过 v2_store 的访问控制。** `list_canonical()` 在 `store.list_canonical()` 不可用（`runtime_bound=False`）时，直接回退到 SQL 查询 `SELECT * FROM canonical_memories ...`。该 SQL 查询直接使用用户参数拼接 WHERE 子句（尽管参数化），但**绕过**了 v2_store 可能实施的租户隔离、软删除过滤、权限校验等业务逻辑。这意味着当内存引擎离线时，通过 WebUI 仍可直接读取原始数据库内容。 |
| 7 | **backend/services/memory_ui_service.py:239** | **SQL fallback 中 `LIMIT ? OFFSET ?` 参数使用 `max(1, min(...))` 可能意外返回 1 行。** 当传参 `limit=0` 或负数时，`max(1, min(int(limit or 100), 500))` 会强制设为 1，而非返回 0 行。调用方可能期望 `limit=0` 表示"不限制"，但实际变成了 1。`offset` 同样会对负数取 `max(0, ...)`，逻辑尚可接受。 |
| 8 | **backend/services/memory_ui_service.py:494-497** | **SQL fallback 中 `list_jargon()` 异常时静默吞异常并降级到 Legacy 表。** 当 `canonical_memories` 表查询抛异常时，`except Exception: pass` 完全忽略错误，然后直接查询 `Jargon` 旧表。这使得数据库损坏或 schema 变更时的故障难以排查，运营人员看不到任何错误日志。 |
| 9 | **backend/adapters/plugin_api.py:11-15** | **全局可变状态 `APPLY_STATUS` 是模块级可变 dict。** 多协程并发调用 `apply_config()` 时会同时写入同一个 `APPLY_STATUS` 全局变量（第 166-186 行），存在竞态条件：`apply_config` 内对 `APPLY_STATUS` 的赋值不是原子操作，A 协程写入的 `"applied_at"` 可能被 B 协程覆盖。虽然后续 `get_apply_status()` 返回 `dict(APPLY_STATUS)` 拷贝，但写入本身无锁保护。 |
| 10 | **backend/services/admin_ui_service.py:1122-1133** | **异常被广泛静默吞掉。** 在 `active_chats`、`chat_activity`、`chat_runtime`、`runtime_health` 等方法中，当 `coordinator.list_active_chats()` 或 `coordinator.get_activity_snapshot()` 抛出异常时，均被 `except Exception: active_chats = 0` 或 `except Exception: pass` 吞掉，没有任何 `_logger.warning` 或 `logging` 输出。这使得运行时错误完全不可见。 |
| 11 | **backend/adapters/plugin_api.py:160-172** | `_apply_hot_config` 在 `facade` 没有 `apply_hot_config` 时直接 `raise RuntimeError`，但调用者 `apply_config`（第 166 行）未捕获该异常。若用户提交配置变更后 facade 不支持热更新，整个请求会返回 500 Internal Server Error，`APPLY_STATUS` 状态不会更新为 "error"，导致前端显示"应用成功"与实际状态不一致。 |
| 12 | **backend/services/admin_ui_service.py:791-798** | **`tools_status()` 直接硬编码导入 `PlannerSideInputMixin`。** 从 `....conversation.planning.planner_side_inputs` 导入具体实现类，并通过 `sorted(PlannerSideInputMixin.CHAT_TOOL_NAMES)` 读取类属性。这创建了从 WebUI 服务到 `conversation.planning` 领域的硬依赖，绕过了 `PluginApiAdapter` 的抽象层。若 `PlannerSideInputMixin` 的类属性改名或移到其他地方，此方法会直接崩溃。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 13 | **backend/auth.py:33-43** | **密钥回退策略可能泄露随机令牌。** 当 `ASTRMAI_WEBUI_SECRET` 未设置时，使用 `secrets.token_hex(32)` 生成会话级密钥并打印警告到 stderr。该随机密钥使**所有之前签发的令牌立即失效**，但每次进程重启都会生成新密钥。建议增加环境变量检查的文档说明，或提供 `generate_secret_key` 辅助脚本。 |
| 14 | **backend/auth.py:72-88** | **所有用户共享单一角色 "admin"。** JWT 的 `sub` 始终为 `"admin"`，没有角色/权限分层。`get_current_user` 仅验证令牌有效性，任何持有有效令牌的用户都有**完全的管理员权限**（读/写/删除所有数据、修改配置、执行数据库迁移、清空运行时状态）。建议至少区分 `admin`（读写）和 `viewer`（只读）角色。 |
| 15 | **backend/routes/memory_routes.py:15** | **`_service()` 每次调用创建新实例。** `MemoryUiService(get_db, PluginApiAdapter())` 在每个路由 handler 中重新构造 `PluginApiAdapter()`（其 `__post_init__` 调用 `get_active_facade()` 查询全局变量）。FastAPI 的 `Depends` 缓存机制对非参数依赖不适用，导致每次请求都重复查询 `ACTIVE_FACADE`。建议使用 `@lru_cache` 或单例。 |
| 16 | **backend/services/memory_ui_service.py:238-240** | **SQL fallback 路径的 `sort_dir` 和 `sort_by` 参数未做白名单校验。** 虽然当前代码中该 SQL 用于 `list_canonical()` 且 ORDER BY 是硬编码的 `update_time DESC`，但 `context_economy_templates` 中的排序字段虽已校验，建议所有排序参数统一使用白名单模式而非黑名单。 |
| 17 | **backend/services/admin_ui_service.py:26-37** | **领域服务构造函数仅保存 `plugin_api`，所有方法都通过运行时 import 委托给 `AdminUiService`。** 这意味着 `ObservabilityService` 等 7 个服务类实际上只是**方法名的重定向层**，没有独立的行为。建议要么(a) 将 `AdminUiService` 拆分为 7 个独立的领域服务（每个约 150-200 行），要么(b) 移除这些委托类，让路由直接调用 `AdminUiService`（并重命名为 `DashboardApi` 等），消除虚假抽象层。当前设计增加了认知负载却未带来解耦收益。 |

## 亮点

1. **`PluginApiAdapter` 的窄门面设计意图良好。** 通过 `get_planner()`、`get_gateway()`、`get_memory_engine()` 等具名方法提供类型安全的路由访问，文档化（docstring 清晰），并提供了 `_memory_sub()` 辅助方法处理 memory 子组件的穿透访问。

2. **SQL 参数化基本到位。** 在 `SettingsUiService`、`DashboardRepository` 等组件的 SQL 查询中，WHERE 子句值均使用 `?` 参数化，且 `MemoryUiService._insert`/`_update` 虽拼接表名但列值均为参数化，避免了最严重的注入风险。

3. **日志回滚和原子写入。** `_write_json_atomic` + `_backup_json_file` 提供了配置文件的备份和原子替换机制（先写 `.tmp`，再 `os.replace`），在配置写入期间崩溃不会损坏原始文件。

4. **认证覆盖完整。** 所有 15 个路由模块的每个 handler 都使用了 `Depends(get_current_user)`，不存在未经认证暴露的端点。

## 总结

`astrmai/webui/backend` 模块整体质量中等偏上，**认证覆盖完整**是最大的安全优点。核心问题集中在架构层面：**`AdminUiService` 是一个 1148 行的 God Object**，且 7 个领域服务（`ObservabilityService`、`HeartflowService`、`CognitionService`、`ChatRuntimeService`、`SchedulerService`、`LearningService`、`ToolsService`）全部通过运行时 import 循环委托回 `AdminUiService`，形成**虚假抽象层**——这些服务类没有独立行为，仅增加调用栈深度。**`MemoryUiService` 的 SQL fallback 路径**是安全薄弱环节（f-string 表名拼接 + 异常静默吞掉 + 绕过 v2_store 业务逻辑）。**`PluginApiAdapter.facade` 的直接暴露**使全局运行时对象可通过 `self.plugin_api.facade` 随处访问。建议优先重构 God Object 拆分，然后用白名单加固 SQL 表名拼接，最后为 `ACTIVE_FACADE` 添加访问代理层。
