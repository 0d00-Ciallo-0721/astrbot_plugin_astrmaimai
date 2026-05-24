# 窗口 8：WebUI / 管理页 / 调试后端

模块：
WebUI / 管理页 / 调试后端

职责：
为 AstrMai 提供 standalone FastAPI 管理后端、Plugin Pages 管理 API，以及 `dashboard`、`memories` 等调试/观测页面的数据入口。

关键文件：
- `astrmai/webui/backend/adapters/plugin_api.py`
- `astrmai/webui/backend/services/admin_ui_service.py`
- `astrmai/webui/backend/services/memory_ui_service.py`
- `astrmai/webui/backend/routes/cognition_routes.py`
- `astrmai/webui/backend/routes/memory_routes.py`
- `astrmai/webui/frontend/js/pages/dashboard.js`
- `astrmai/webui/frontend/js/pages/memory.js`
- `astrmai/webui/plugin_pages.py`

现有测试：
- `tests/unit/webui/test_user_ui_service_migrated.py`
- 备注：该测试只覆盖 `UserUiService` 用户档案/切片迁移逻辑，与 runtime/trace/dashboard/plugin_pages 热点不对应。

主要发现：
1. backend service 对核心 runtime 的边界非常薄，已经直接穿透到内部对象甚至私有状态。
   - 依据：`astrmai/webui/backend/routes/cognition_routes.py:11-12`、`memory_routes.py:11-12` 会直达 `admin_ui_service.py:45-46,637-720,1049-1213` 与 `memory_ui_service.py:16-18,201-213,267-350,561-565,801-867`。
   - 进一步依据：`plugin_api.py:37-39,191-192` 暴露 runtime；`AdminUiService` 直接读写 `system2_planner`、`proactive_task`、`observability_hub`、`heartflow_manager` 的私有状态，`MemoryUiService` 直接操作 `memory_engine.v2_store`、`maintenance_service`、`index_projector`、`write_service`。
2. admin UI 对内部实现细节耦合很深，Plugin Pages 与 standalone `/api` 已经出现入口漂移。
   - 依据：`astrmai/webui/frontend/js/pages/dashboard.js:102-105,379-395` 调用 `/cognition/context-economy` 和 `/cognition/context-economy/templates`。
   - 进一步依据：对应 FastAPI 路由存在于 `astrmai/webui/backend/routes/cognition_routes.py:112-132`，但 `astrmai/webui/plugin_pages.py:572-668` 的注册表里没有这两个入口。
3. 路由、service、schema 混层明显，`schemas.py` 没有形成真实契约边界。
   - 依据：`astrmai/webui/backend/schemas.py:10-51` 只定义了少量模型。
   - 进一步依据：`memory_routes.py:56-133,143-218`、`users_routes.py:21-58`、`config_routes.py:30-55`、`review_routes.py:45-51` 大量接口仍直接收 `Dict[str, Any]` 或返回裸 `dict`。
4. 正式检查范围应排除 `astrmai/webui/venv/*` 和 `astrmai/webui/mock_frontend_server*`。
   - 依据：生产调用链只看到 `astrmai/webui/backend/server.py:20-27` 挂载 `frontend` 与 `astrmai/webui/plugin_pages.py:565-690` 注册 API，仓内搜索也未发现生产代码引用 mock server 或 venv。

未实现/不完整项：
1. `astrmai/webui/plugin_pages.py` 尚未补齐 `context-economy` / `context-economy/templates` handler 与注册，Plugin Pages API 与 FastAPI 路由不一致。
2. `astrmai/webui/backend/routes.py` 与 `astrmai/webui/backend/routes/__init__.py` 维护两份同构 router 装配，`server.py` 当前只使用前者，双入口未收敛。

高风险点：
1. FastAPI `/api` 对 runtime 的绑定依赖全局 `ACTIVE_FACADE`。
   - 依据：`astrmai/webui/backend/server.py:20` 启动 API 时没有显式注入 facade，只有 `astrmai/app/plugin_facade.py:20-24` 或 `astrmai/webui/plugin_pages.py:568` 会设置它。
2. 验证入口与实现热点严重错位。
   - 依据：最近高频改动集中在 `admin_ui_service.py`、`memory_ui_service.py`、`dashboard.js`、`memory.js`、`plugin_pages.py`，但 `tests/unit/webui/*` 只有 `test_user_ui_service_migrated.py`。

建议下一步：
1. 把 runtime diagnostics / admin actions 收口到稳定 facade 或 DTO 层，去掉 `AdminUiService` 对 private 属性的直接读写，并统一 FastAPI 与 Plugin Pages 的 route manifest。
2. 在 `tests/unit/webui` 补上 `AdminUiService`、`MemoryUiService`、`plugin_pages.py` 路由对齐、dashboard/memory observability 字段契约测试；同时把 `astrmai/webui/venv/` 和 `astrmai/webui/mock_frontend_server*` 加入正式审查排除规则。
