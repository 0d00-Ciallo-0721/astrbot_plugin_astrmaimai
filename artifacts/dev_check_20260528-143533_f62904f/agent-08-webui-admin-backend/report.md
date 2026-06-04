# Agent 08

Agent ID:
`019e6d5c-3559-71d2-9ead-78d3c4594896`

状态：
已完成

模块：
WebUI / 管理页 / 调试后端

职责：
独立 FastAPI WebUI、AstrBot Plugin Pages 管理 API、调试/观测数据聚合，以及前端管理页与这些 API 的对接。

关键文件：
[server.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/server.py:1>)
[routes/__init__.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/__init__.py:1>)
[plugin_api.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:1>)
[admin_ui_service.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:1>)
[plugin_pages.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/plugin_pages.py:1>)
[dashboard.js](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/dashboard.js:1>)
[persona.js](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/persona.js:1>)
[config.js](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/config.js:1>)

现有测试：
1. [tests/test_plugin_pages_admin_refactor.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_plugin_pages_admin_refactor.py:18>)：覆盖 `plugin_pages.py` 路由注册、AstrBot 桥接边界、管理页不暴露 `/config` 与 `/persona` 写接口。
2. [tests/test_webui_backend_refactor.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_webui_backend_refactor.py:70>)：覆盖聚合 router、`PluginApiAdapter`、context economy、scheduler、chat trace 等调试接口的 happy path。
3. [tests/unit/webui/test_user_ui_service_migrated.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/unit/webui/test_user_ui_service_migrated.py:25>)：覆盖用户画像写回与手工锁字段。
4. 已实际执行 `python -m pytest tests/test_plugin_pages_admin_refactor.py tests/test_webui_backend_refactor.py tests/unit/webui -q`，结果 `39 passed`。

主要发现：
1. 独立 `persona` 页面已经和当前后端契约脱节。[persona.js](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/persona.js:18>) 仍把 `/persona` 当可读写接口，但 [persona_ui_service.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/persona_ui_service.py:59>) 已明确把 `get_persona`/`update_persona` 改成只返回错误，且 [persona_routes.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/persona_routes.py:9>) 仍把前端请求导向这里。当前页面会进入、显示空编辑框、保存必然失败。
2. `AdminUiService` 直接穿透 runtime 私有缓存，而且两个“清理 chat 状态”接口语义不一致。[clear_heartflow_cooldowns](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:705>) 会清 `_action_decisions_by_chat` 并重置 session 连续计数；[clear_chat_runtime](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:1199>) 只清部分缓存和 `cooldown_tags`。前端 [learning.js](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/learning.js:72>) 走的是 `/chats/{chat_id}/runtime/clear`，因此很可能留下旧 Heartflow 状态。
3. routes/service/schema 边界存在漂移。运行时真正导入的是包入口 [routes/__init__.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/__init__.py:1>)，同级 [routes.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes.py:1>) 是影子聚合器；同时 [config_routes.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/config_routes.py:30>) 在 route 层直接写配置，绕过 `SettingsUiService`，而 [memory_routes.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/memory_routes.py:149>) 等大量 mutation 仍直接收 `Dict[str, Any]`，没有统一 schema 契约。

未实现/不完整项：
1. 正式检查范围应排除 `astrmai/webui/venv/**` 与 `astrmai/webui/mock_frontend_server*`；本轮已人工排除，但当前参考测试没有把这条边界固化成自动化契约。
2. `runtime`、`cognition`、`heartflow`、`context-economy` 返回体没有对应 Pydantic schema；前端 [dashboard.js](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/dashboard.js:71>) 和 [dashboard/index.html](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/pages/dashboard/index.html:243>) 直接绑定 ad-hoc 字段，缺少 payload contract 测试。

高风险点：
1. `AdminUiService` 对 `system2_planner`、`gateway`、`proactive_task`、`observability_hub`、`runtime_coordinator`、`memory_engine` 以及多处私有字段的反射访问很重，[admin_ui_service.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:62>) 到 [admin_ui_service.py](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:1213>) 都能看到这类模式，后续 runtime 改名或挪层时 WebUI 很容易静默降级。
2. [PluginApiAdapter.apply_config](</C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:168>) 直接改 `runtime.raw_config` / `runtime.config` 并调用 `rebuild_infrastructure_settings()`，这是直接穿透核心 runtime 的热更新路径；现有参考测试只覆盖“会调用 rebuild”的 happy path，没有覆盖跨组件重绑或失败回滚。

建议下一步：
1. 先处理 `persona` 漂移：要么把独立 `persona` 页改成只读 `persona_slices` 诊断页，要么直接从 standalone frontend 移除，并补一条前后端联调测试覆盖当前只读契约。
2. 收敛 WebUI 契约边界：删除影子 `backend/routes.py`，把 `config`/`memory`/`review` 等 mutation 请求统一到 service + schema 流程，并补两类测试：`clear_chat_runtime` 与 `clear_heartflow_cooldowns` 的语义一致性测试、以及 dashboard 调试 payload 的 contract 测试。
