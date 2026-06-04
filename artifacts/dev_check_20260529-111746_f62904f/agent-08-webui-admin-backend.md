# Agent 08

Agent ID:
`019e71c0-872f-7220-be68-dccc9c1afe0c`

状态：
已完成

发现：
1. `[高]` 管理后端仍然可以直接热改核心 runtime，而不是只经过稳定 facade 或重载边界。[settings_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/settings_ui_service.py:101) 在保存 section/整份配置时会立刻调用 `apply_config()`，[plugin_api.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:194) 又会直接改写 `runtime.raw_config`、`runtime.config` 并调用 `rebuild_infrastructure_settings()`；这正是“backend service 直接穿透 core runtime”。对应行为还被测试固化了，[test_webui_backend_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_webui_backend_refactor.py:976) 明确断言配置应用后会直接改动已绑定 runtime。
2. `[中]` 调试/观测服务仍明显依赖 runtime 私有实现细节，后续 runtime 内部重构很容易把 WebUI 打断，即使公开行为没变。[admin_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:647) 直接读 `manager._states`，[admin_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/admin_ui_service.py:473) 直接消费 `"_templates"`，[memory_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/memory_ui_service.py:246) 调 `store._candidate_to_dict()`，[review_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/review_ui_service.py:257) 走 `service.store.update_memory()`，[persona_ui_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/persona_ui_service.py:73) 还会回退到 `plugin_api._read_json()` 和 `persona_summarizer.pending_tasks`。
3. `[中]` `routes/service/schema` 在 learning/proactive/chat-runtime 这条线上仍然是混层的，前端契约依旧主要靠无类型字典硬连。`schemas.py` 只给 health/observability/context-economy 这些面建了响应模型，[schemas.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/schemas.py:26)；但 [proactive_routes.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/proactive_routes.py:21)、[learning_routes.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/learning_routes.py:15)、[chats_routes.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/routes/chats_routes.py:15) 仍直接透传 service dict。与此同时前端在 [learning.js](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/frontend/js/pages/learning.js:60) 依赖 `dream_agent_bound`、`reflect_tracker`、`auto_check_task`、`latest_activity_ts`、`wait_target_name` 这些具体字段；但现有前端壳测试只校验 URL 命中，不校验 payload 形状，[test_webui_frontend_shell_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/test_webui_frontend_shell_refactor.py:56) 这里仍有明确测试缺口。
4. `[中]` 参考复检套件默认并不能直接跑通，当前验证仍依赖外部 `PYTHONPATH` 约束。直接在仓库根跑 `pytest tests\\unit\\webui tests\\test_plugin_pages_admin_refactor.py tests\\test_webui_backend_refactor.py tests\\test_webui_frontend_shell_refactor.py -q` 会先报 39 个 `ModuleNotFoundError: astrmai.webui`；显式设置 `PYTHONPATH=C:\\Users\\zlj\\Desktop\\mai\\astrmai_plugin_refactored_final` 后，同一批用例才变成 `52 passed`。这里没有看到把测试导入路径固定下来的 pytest 配置，这仍是一个实际的复检/CI 缺口。

验证：
按参考范围做了只读复检并运行了相关测试。结论是：在补上 `PYTHONPATH` 之后，参考套件本身是通过的，所以这轮没有发现一个已经被现有用例直接打爆的 dashboard/memory/persona trace 字段回归；剩下主要是边界穿透、实现耦合和验证缺口。

补充：
正式检查里已把 `astrmai/webui/venv` 和 `mock_frontend_server.*` 排除在源码结论之外；其中 `venv/.venv/site-packages` 也和架构回归测试的 skip 规则一致。
