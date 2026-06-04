# Agent 10

Agent ID:
`019e71c0-b032-7df3-95a5-e37ff999f8b6`

状态：
已完成

结论：
本轮复检下，显式的“下层反向 import 上层”违规没有再复现，但架构边界并没有真正收口：`presentation` 和 `webui` 仍在直接碰 runtime / persistence 内部细节，临时 compat 桥也已经被固化成长期公共面。

当前问题：
- `高` `presentation` 仍直接操纵 concrete runtime，而不是经 contract/facade 访问。`handle_global_message()` 在 [message_entry.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/events/message_entry.py:41) 直接读写 `group_reply_wait_manager`、`chat_loop_kernel`、`lifecycle.manager`、`reflect_tracker`、`evolution`、`attention_gate`、`host_bridge`。
- `高` WebUI 对 runtime 内部的直连仍是现实存在，不是个别残留。`PluginApiAdapter.get_runtime()` 在 [plugin_api.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/adapters/plugin_api.py:238) 直接暴露原始 runtime；随后 `admin_ui_service`、`memory_ui_service`、`persona_ui_service`、`review_ui_service` 都在拆 runtime 内部属性。对应回归测试不是禁止，而是白名单放行这些逃生口。
- `高` WebUI 仍直接碰 persistence 细节。典型点包括 [dashboard_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/dashboard_service.py:18) 的原始 SQL 和 [dashboard_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/webui/backend/services/dashboard_service.py:40) 的 `default_db_path()`，以及 `admin_ui_service`、`user_ui_service`、`memory_ui_service` 的直接库表访问。
- `中` `runtime/compat` 临时桥已经被“制度化”。[legacy_compat.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/infrastructure/compat/legacy_compat.py:15) 仍在生产代码里读写大量 legacy event extras；`PluginRuntimeContext` 还通过 `LEGACY_RUNTIME_ATTRS` + `export_legacy_attrs()` 向宿主导出大面积旧属性；`PluginApiAdapter` 公开 `get_runtime()`；`plugin_facade.py` 与 `plugin_pages.py` 仍通过全局 `ACTIVE_FACADE` 绑 runtime。回归测试也把这些桥的导入点和写入点写死了，更像“冻结现状”，不是“迫使桥层退场”。
- `中` God Object 还在继续膨胀。`AdminUiService` 已到 1145 行并承载约 80 个方法，`MemoryUiService` 843 行，`PluginRuntimeContext` 514 行；最近 10 个提交里 `admin_ui_service.py` 还新增了 450 行、`plugin_pages.py` 新增了 61 行。

测试缺口：
- `低` 架构回归套件的入口不稳定：`python -m pytest tests/regression/architecture -q` 实测 `36 passed`，但直接跑 `pytest tests/regression/architecture -q` 会在 [test_shared_test_support_refactor.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/tests/regression/architecture/test_shared_test_support_refactor.py:4) 收集期报 `ModuleNotFoundError: tests.fixtures`。这会让 CI 或同事在错误入口下拿到假阴性。

本轮未复现：
- 显式 `infrastructure -> presentation/webui`、`conversation -> presentation` 这类反向 import 违规，本轮按 `python -m pytest tests/regression/architecture -q` 和 `rg` 未复现。
- `presentation` 直连 persistence import，本轮未复现；但它直连 concrete runtime 的问题仍然存在。
